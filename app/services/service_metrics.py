"""서비스 대시보드 지표(운영 2): 범위 검증 · PromQL 조립 · 응답 변환 · 프로세스 캐시.

출처는 진입점 Prometheus 의 앱 지표(인프라 9 OTel 수집기 remote write)다. 서비스 하나는
`namespace` 라벨(대표 주소 첫 레이블 `{서비스}-{suffix}`)로, 환경은 `cloud_provider` 라벨로 가른다.
환경 ↔ `cloud_provider` 는 진입점 실행기와 같은 매핑(환경 id → 종류 → 종류 그대로)이다.

Prometheus 를 읽지 못해도 예외를 올리지 않는다(응답 200, `metricsStatus = UNAVAILABLE`, 시리즈 빈
배열, 요약 null). 값을 지어내지 않는다: NaN·빈 결과는 null, 가짜 복제 지연(simulated)은 null.
"""

import asyncio
import logging
import math
import re
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import State

from app.clients.prometheus_client import PrometheusClient, PrometheusQueryError, Series
from app.core.clock import Clock
from app.core.exceptions import RequestFieldInvalidError
from app.enums import ServiceMetricsStatus
from app.executors.base import DataSyncChecker, DataSyncLag, DeployTarget
from app.executors.entrypoint import entrypoint_environment_name
from app.models import Service, ServiceEnvironment
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.repositories.service_repository import ServiceRepository
from app.services.deploy_support import deploy_target
from app.services.service_view import reference_connection

logger = logging.getLogger(__name__)

DEFAULT_RANGE = "15m"
DEFAULT_STEP = "10s"
MIN_RANGE_SECONDS = 5 * 60
MAX_RANGE_SECONDS = 24 * 60 * 60
MIN_STEP_SECONDS = 5
MAX_STEP_SECONDS = 5 * 60
MAX_POINTS = 1500
# 수집기가 15초마다 긁으므로 rate 창이 그보다 짧으면 비어 버린다. Grafana 패널과 같은 1분 이상.
MIN_RATE_WINDOW_SECONDS = 60
SLO_TARGET = 0.999
HEALTH_PATHS = ("/healthz", "/readyz", "/metrics")
CLOUD_PROVIDER_LABEL = "cloud_provider"

INVALID_DURATION_REASON = "INVALID_DURATION"
RANGE_OUT_OF_BOUNDS_REASON = "RANGE_OUT_OF_BOUNDS"
STEP_OUT_OF_BOUNDS_REASON = "STEP_OUT_OF_BOUNDS"
TOO_MANY_POINTS_REASON = "TOO_MANY_POINTS"

SERVICE_METRICS_CACHE_MAX_ENTRIES = 512

_DURATION = re.compile(r"([1-9][0-9]{0,4})([smh])")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}
_RE2_SPECIAL = re.compile(r"([\\.+*?()|\[\]{}^$])")


# --- 범위 검증 ---


@dataclass(frozen=True)
class MetricsWindow:
    """받은 글자(range·step)와 초. 응답에는 받은 글자를 그대로 돌려준다."""

    range_text: str
    step_text: str
    range_seconds: int
    step_seconds: int


def parse_duration(text: str) -> int | None:
    """`<1~5자리 정수><s|m|h>` → 초. 형식이 틀리면 None."""
    match = _DURATION.fullmatch(text)
    if match is None:
        return None
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


def parse_metrics_window(range_text: str, step_text: str) -> MetricsWindow:
    """range 5m~24h, step 5s~5m, step ≤ range, 점 수 ≤ 1500. 어기면 422 VALIDATION_ERROR."""
    range_seconds = parse_duration(range_text)
    if range_seconds is None:
        raise RequestFieldInvalidError("range", INVALID_DURATION_REASON)
    step_seconds = parse_duration(step_text)
    if step_seconds is None:
        raise RequestFieldInvalidError("step", INVALID_DURATION_REASON)
    if not MIN_RANGE_SECONDS <= range_seconds <= MAX_RANGE_SECONDS:
        raise RequestFieldInvalidError("range", RANGE_OUT_OF_BOUNDS_REASON)
    if not MIN_STEP_SECONDS <= step_seconds <= MAX_STEP_SECONDS or step_seconds > range_seconds:
        raise RequestFieldInvalidError("step", STEP_OUT_OF_BOUNDS_REASON)
    if range_seconds // step_seconds + 1 > MAX_POINTS:
        raise RequestFieldInvalidError("step", TOO_MANY_POINTS_REASON)
    return MetricsWindow(range_text, step_text, range_seconds, step_seconds)


def window_bounds(now: datetime, window: MetricsWindow) -> tuple[datetime, datetime]:
    """끝 = 지금을 step 배수로 내림, 시작 = 끝 − range. 같은 step 안 요청은 같은 점을 받는다."""
    end_epoch = math.floor(now.timestamp() / window.step_seconds) * window.step_seconds
    end = datetime.fromtimestamp(end_epoch, tz=UTC)
    return end - timedelta(seconds=window.range_seconds), end


# --- PromQL 조립 ---


@dataclass(frozen=True)
class MetricsFilter:
    """서비스 하나의 앱 지표 거르기: 앱 네임스페이스 + 뺄 헬스 경로."""

    namespace: str
    excluded_paths: tuple[str, ...]


def service_metrics_filter(service: Service) -> MetricsFilter:
    """네임스페이스는 대표 주소 첫 레이블. 헬스 경로(고정 3개 + 서비스 헬스체크 경로)는 뺀다."""
    excluded = list(HEALTH_PATHS)
    if service.health_check_path not in excluded:
        excluded.append(service.health_check_path)
    return MetricsFilter(
        namespace=service.public_host.split(".", 1)[0], excluded_paths=tuple(excluded)
    )


def promql_string(value: str) -> str:
    """PromQL 큰따옴표 문자열 글자."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def re2_escape(value: str) -> str:
    """RE2 정규식 특수 문자를 글자 그대로 맞게 이스케이프한다."""
    return _RE2_SPECIAL.sub(r"\\\1", value)


def label_matchers(metrics_filter: MetricsFilter, *, errors_only: bool = False) -> str:
    excluded = "|".join(re2_escape(path) for path in metrics_filter.excluded_paths)
    matchers = [
        f"namespace={promql_string(metrics_filter.namespace)}",
        f"path!~{promql_string(excluded)}",
    ]
    if errors_only:
        matchers.append('status=~"5.."')
    return "{" + ",".join(matchers) + "}"


def rate_window_seconds(step_seconds: int) -> int:
    return max(step_seconds, MIN_RATE_WINDOW_SECONDS)


def series_queries(metrics_filter: MetricsFilter, step_seconds: int) -> dict[str, str]:
    """그래프 점: 환경(cloud_provider)별 rps·5xx rps·p95(ms)."""
    window = f"[{rate_window_seconds(step_seconds)}s]"
    every = label_matchers(metrics_filter)
    errors = label_matchers(metrics_filter, errors_only=True)
    by = CLOUD_PROVIDER_LABEL
    return {
        "rps": f"sum by ({by}) (rate(http_requests_total{every}{window}))",
        "errors": f"sum by ({by}) (rate(http_requests_total{errors}{window}))",
        "p95": (
            f"histogram_quantile(0.95, sum by ({by}, le) "
            f"(rate(http_request_duration_seconds_bucket{every}{window}))) * 1000"
        ),
    }


def summary_queries(metrics_filter: MetricsFilter, range_seconds: int) -> dict[str, str]:
    """요약: 구간 전체·환경 합의 요청 수·5xx 수·p95(ms). 끝 시각 instant 질의."""
    window = f"[{range_seconds}s]"
    every = label_matchers(metrics_filter)
    errors = label_matchers(metrics_filter, errors_only=True)
    return {
        "total": f"sum(increase(http_requests_total{every}{window}))",
        "errors": f"sum(increase(http_requests_total{errors}{window}))",
        "p95": (
            "histogram_quantile(0.95, sum by (le) "
            f"(rate(http_request_duration_seconds_bucket{every}{window}))) * 1000"
        ),
    }


# --- 응답 변환 ---


@dataclass(frozen=True)
class MetricsConnection:
    """시리즈 하나가 될 연결과 그 환경의 cloud_provider 이름."""

    environment_id: uuid.UUID
    environment_name: str
    cloud_provider: str


@dataclass(frozen=True)
class MetricsPointView:
    at: datetime
    rps: float
    success_rate: float | None
    p95_ms: float | None


@dataclass(frozen=True)
class EnvironmentSeriesView:
    environment_id: uuid.UUID
    environment_name: str
    cloud_provider: str
    points: tuple[MetricsPointView, ...]


@dataclass(frozen=True)
class MetricsSummaryView:
    success_rate: float | None
    p95_ms: float | None
    replication_lag_ms: float | None
    error_budget_left: float | None
    slo_target: float = SLO_TARGET


@dataclass(frozen=True)
class ServiceMetricsView:
    window: MetricsWindow
    as_of: datetime
    status: ServiceMetricsStatus
    error_code: str | None
    series: tuple[EnvironmentSeriesView, ...]
    summary: MetricsSummaryView


def _by_provider(series: Sequence[Series]) -> dict[str, dict[datetime, float | None]]:
    values: dict[str, dict[datetime, float | None]] = {}
    for item in series:
        provider = item.labels.get(CLOUD_PROVIDER_LABEL)
        if provider is None:
            continue
        values.setdefault(provider, {}).update({sample.at: sample.value for sample in item.samples})
    return values


def success_rate_of(total: float | None, errors: float | None) -> float | None:
    """1 − 5xx/전체. 전체가 없거나 0 이면 null, 5xx 가 없으면 0 건."""
    if total is None or total <= 0:
        return None
    ratio = (errors or 0.0) / total
    return round(min(1.0, max(0.0, 1.0 - ratio)), 6)


def error_budget_left(success_rate: float | None, slo_target: float = SLO_TARGET) -> float | None:
    """SLO 99.9% 기준 남은 에러 버짓 = clamp(1 − (1 − 성공률) / (1 − SLO), 0, 1)."""
    if success_rate is None:
        return None
    left = 1.0 - (1.0 - success_rate) / (1.0 - slo_target)
    return round(min(1.0, max(0.0, left)), 4)


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def build_series(
    connections: Sequence[MetricsConnection], results: Mapping[str, Sequence[Series]]
) -> tuple[EnvironmentSeriesView, ...]:
    """연결마다 시리즈 하나. rps 가 있는 시각만 점이 된다.

    같은 cloud_provider 이름의 연결이 둘이면 가를 수 없어 둘 다 빈 점이다
    (같은 값을 두 번 붙이지 않는다).
    """
    rps = _by_provider(results.get("rps", ()))
    errors = _by_provider(results.get("errors", ()))
    p95 = _by_provider(results.get("p95", ()))
    counts: dict[str, int] = {}
    for connection in connections:
        counts[connection.cloud_provider] = counts.get(connection.cloud_provider, 0) + 1
    views = []
    for connection in connections:
        provider = connection.cloud_provider
        points: tuple[MetricsPointView, ...] = ()
        if counts[provider] == 1:
            provider_errors = errors.get(provider, {})
            provider_p95 = p95.get(provider, {})
            points = tuple(
                MetricsPointView(
                    at=at,
                    rps=round(value, 4),
                    success_rate=success_rate_of(value, provider_errors.get(at)),
                    p95_ms=_round(provider_p95.get(at), 1),
                )
                for at, value in sorted(rps.get(provider, {}).items())
                if value is not None
            )
        views.append(
            EnvironmentSeriesView(
                environment_id=connection.environment_id,
                environment_name=connection.environment_name,
                cloud_provider=provider,
                points=points,
            )
        )
    return tuple(views)


def _single_value(series: Sequence[Series]) -> float | None:
    for item in series:
        for sample in item.samples:
            return sample.value
    return None


def build_summary(
    results: Mapping[str, Sequence[Series]], replication_lag_ms: float | None
) -> MetricsSummaryView:
    success_rate = success_rate_of(
        _single_value(results.get("total", ())), _single_value(results.get("errors", ()))
    )
    return MetricsSummaryView(
        success_rate=success_rate,
        p95_ms=_round(_single_value(results.get("p95", ())), 1),
        replication_lag_ms=replication_lag_ms,
        error_budget_left=error_budget_left(success_rate),
    )


def empty_summary(replication_lag_ms: float | None) -> MetricsSummaryView:
    return MetricsSummaryView(
        success_rate=None,
        p95_ms=None,
        replication_lag_ms=replication_lag_ms,
        error_budget_left=None,
    )


# --- 복제 지연 ---


@dataclass(frozen=True)
class LagTargets:
    """서비스 중 연결(출발)과 예비 연결들(도착). DB 트랜잭션 안에서 값으로 만든다."""

    source: DeployTarget
    standbys: tuple[DeployTarget, ...]


def lag_targets(service: Service, connections: Sequence[ServiceEnvironment]) -> LagTargets | None:
    source = reference_connection(list(connections))
    if source is None:
        return None
    standbys = tuple(
        deploy_target(service, item, item.environment)
        for item in connections
        if item.traffic_weight == 0 and item.current_deployment_id is not None
    )
    if not standbys:
        return None
    return LagTargets(deploy_target(service, source, source.environment), standbys)


async def read_replication_lag_ms(
    data_sync: DataSyncChecker | None, targets: LagTargets | None
) -> float | None:
    """예비마다 출발 → 예비 복제 지연을 읽어 가장 큰 값(ms). 가짜·못 읽음·재지 않은 통과는 null."""
    if data_sync is None or data_sync.simulated or targets is None:
        return None
    results = await asyncio.gather(
        *(data_sync.replication_lag(targets.source, standby) for standby in targets.standbys),
        return_exceptions=True,
    )
    seconds = [
        result.seconds
        for result in results
        if isinstance(result, DataSyncLag) and result.reason is None
    ]
    for result in results:
        if isinstance(result, BaseException):
            logger.warning(
                "service metrics replication lag failed",
                extra={"action": "read_service_metrics", "error_type": type(result).__name__},
            )
    return round(max(seconds) * 1000, 1) if seconds else None


# --- 캐시 ---


type MetricsCacheKey = tuple[uuid.UUID, int, int]


@dataclass(frozen=True)
class _CachedMetrics:
    view: ServiceMetricsView
    expires_at: datetime


@dataclass
class _KeyLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    holders: int = 0


class ServiceMetricsCache:
    """(서비스 id, range 초, step 초) → 응답 뷰. 가득 차면 먼저 넣은 것부터 버린다."""

    def __init__(self, max_entries: int = SERVICE_METRICS_CACHE_MAX_ENTRIES) -> None:
        self._max_entries = max_entries
        self._entries: OrderedDict[MetricsCacheKey, _CachedMetrics] = OrderedDict()
        self._locks: dict[MetricsCacheKey, _KeyLock] = {}

    def find(self, key: MetricsCacheKey, now: datetime) -> ServiceMetricsView | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= now:
            del self._entries[key]
            return None
        return entry.view

    def store(self, key: MetricsCacheKey, view: ServiceMetricsView, expires_at: datetime) -> None:
        self._entries.pop(key, None)
        self._entries[key] = _CachedMetrics(view, expires_at)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    @asynccontextmanager
    async def reading(self, key: MetricsCacheKey) -> AsyncIterator[None]:
        """같은 키를 동시에 읽지 않게 줄 세운다. 기다리는 쪽이 없으면 잠금을 치운다."""
        key_lock = self._locks.setdefault(key, _KeyLock())
        key_lock.holders += 1
        try:
            async with key_lock.lock:
                yield
        finally:
            key_lock.holders -= 1
            if key_lock.holders == 0:
                del self._locks[key]


def service_metrics_cache(state: State) -> ServiceMetricsCache:
    """앱 state 의 캐시(없으면 만든다)."""
    cache = getattr(state, "service_metrics_cache", None)
    if not isinstance(cache, ServiceMetricsCache):
        cache = ServiceMetricsCache()
        state.service_metrics_cache = cache
    return cache


# --- 서비스 ---


@dataclass(frozen=True)
class _ServiceSnapshot:
    service_id: uuid.UUID
    metrics_filter: MetricsFilter
    connections: tuple[MetricsConnection, ...]
    lag_targets: LagTargets | None


class ServiceMetricsService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        prometheus: PrometheusClient | None,
        data_sync: DataSyncChecker | None,
        cache: ServiceMetricsCache,
        clock: Clock,
        environment_map: Mapping[str, str],
        cache_seconds: int,
    ) -> None:
        self._session = session
        self._prometheus = prometheus
        self._data_sync = data_sync
        self._cache = cache
        self._clock = clock
        self._environment_map = dict(environment_map)
        self._cache_ttl = timedelta(seconds=cache_seconds)

    async def get_metrics(
        self, organization_id: uuid.UUID, service_id: uuid.UUID, window: MetricsWindow
    ) -> ServiceMetricsView:
        """서비스 확인(DB)은 매번, 외부 읽기는 캐시. 외부 실패는 예외 없이 뷰의 상태로."""
        snapshot = await self._snapshot(organization_id, service_id)
        key: MetricsCacheKey = (snapshot.service_id, window.range_seconds, window.step_seconds)
        cached = self._cache.find(key, self._clock.now())
        if cached is not None:
            return cached
        async with self._cache.reading(key):
            cached = self._cache.find(key, self._clock.now())
            if cached is not None:
                return cached
            view = await self._read(snapshot, window)
            self._cache.store(key, view, self._clock.now() + self._cache_ttl)
            return view

    async def _snapshot(
        self, organization_id: uuid.UUID, service_id: uuid.UUID
    ) -> _ServiceSnapshot:
        async with self._session.begin():
            service = await ServiceRepository(self._session).get_live_by_id(
                organization_id, service_id
            )
            connections = await ServiceEnvironmentRepository(self._session).search_live_by_service(
                service.id
            )
            return _ServiceSnapshot(
                service_id=service.id,
                metrics_filter=service_metrics_filter(service),
                connections=tuple(
                    MetricsConnection(
                        environment_id=item.environment.id,
                        environment_name=item.environment.name,
                        cloud_provider=entrypoint_environment_name(
                            item.environment.id, item.environment.kind, self._environment_map
                        ),
                    )
                    for item in connections
                ),
                lag_targets=lag_targets(service, connections),
            )

    async def _read(self, snapshot: _ServiceSnapshot, window: MetricsWindow) -> ServiceMetricsView:
        start, end = window_bounds(self._clock.now(), window)
        prometheus, lag_ms = await asyncio.gather(
            self._read_prometheus(snapshot, window, start, end),
            read_replication_lag_ms(self._data_sync, snapshot.lag_targets),
        )
        status, error_code, range_results, instant_results = prometheus
        if status is not ServiceMetricsStatus.OK:
            return ServiceMetricsView(
                window=window,
                as_of=end,
                status=status,
                error_code=error_code,
                series=(),
                summary=empty_summary(lag_ms),
            )
        return ServiceMetricsView(
            window=window,
            as_of=end,
            status=status,
            error_code=None,
            series=build_series(snapshot.connections, range_results),
            summary=build_summary(instant_results, lag_ms),
        )

    async def _read_prometheus(
        self,
        snapshot: _ServiceSnapshot,
        window: MetricsWindow,
        start: datetime,
        end: datetime,
    ) -> tuple[ServiceMetricsStatus, str | None, dict[str, list[Series]], dict[str, list[Series]]]:
        if self._prometheus is None:
            return ServiceMetricsStatus.NOT_CONFIGURED, None, {}, {}
        range_result, instant_result = await asyncio.gather(
            self._prometheus.query_range(
                series_queries(snapshot.metrics_filter, window.step_seconds),
                start,
                end,
                window.step_seconds,
            ),
            self._prometheus.query(
                summary_queries(snapshot.metrics_filter, window.range_seconds), end
            ),
            return_exceptions=True,
        )
        for outcome in (range_result, instant_result):
            if isinstance(outcome, PrometheusQueryError):
                logger.warning(
                    "service metrics unavailable",
                    extra={
                        "action": "read_service_metrics",
                        "service_id": str(snapshot.service_id),
                        "error_code": outcome.code,
                    },
                )
                return ServiceMetricsStatus.UNAVAILABLE, outcome.code, {}, {}
            if isinstance(outcome, BaseException):
                raise outcome
        assert not isinstance(range_result, BaseException)
        assert not isinstance(instant_result, BaseException)
        return ServiceMetricsStatus.OK, None, range_result, instant_result
