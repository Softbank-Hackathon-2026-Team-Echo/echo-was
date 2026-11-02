"""고정 진입점(Envoy 가중치 API) 실행기 (`entrypoint_executor_mode = http`).

echo-infra 의 고정 진입점 VM 이 여는 가중치 API 를 부른다(계약: echo-infra
`contracts/executor-credentials.md` §3).

- TrafficExecutor: `PUT /weights` 로 대표 주소 트래픽 비율을 적용한다.
- MainRouteExecutor: 한 연결에 100, 나머지 0 으로 같은 API 를 부른다.
- MetricsProvider: `GET /summary` 로 연결 하나의 구간 지표(요청 수·오류율·p95)를 읽는다.

진입점의 환경 이름(`aws`·`gcp`)은 환경 종류와 같다. 다르거나 한 종류에 환경이 여럿이면
`entrypoint_environment_map`(`<환경 id>=<진입점 이름>`)으로 정한다. 진입점이 모르는 이름이나
두 연결이 같은 이름으로 모이면 실패 코드 ENTRYPOINT_ENV_UNMAPPED 다.

토큰은 비밀 참조(`entrypoint_token_ref`)로만 받아 호출마다 비밀 조회기에서 읽는다. 저장·로그·
실패 코드에 넣지 않는다. 권한 오류(401·503)는 시간 초과와 다른 코드로 돌려준다.
"""

import logging
import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import httpx

from app.clients.secret_client import SecretClient, SecretLookupError
from app.core.clock import Clock
from app.core.config import Settings
from app.core.secrets import SecretRef, parse_secret_ref
from app.executors.base import (
    DeployTarget,
    MainRoute,
    Metrics,
    MetricsResult,
    MetricsUnavailable,
    RouteApplied,
    RouteFailed,
    RouteResult,
    TrafficApplied,
    TrafficFailed,
    TrafficResult,
    WeightedTarget,
    check_weights_total,
)

logger = logging.getLogger(__name__)

ENTRYPOINT_UNAUTHORIZED = "ENTRYPOINT_UNAUTHORIZED"
ENTRYPOINT_TOKEN_MISSING = "ENTRYPOINT_TOKEN_MISSING"
ENTRYPOINT_REJECTED = "ENTRYPOINT_REJECTED"
ENTRYPOINT_UNREACHABLE = "ENTRYPOINT_UNREACHABLE"
ENTRYPOINT_ERROR = "ENTRYPOINT_ERROR"
ENTRYPOINT_ENV_UNMAPPED = "ENTRYPOINT_ENV_UNMAPPED"

# 진입점 /summary 의 window 는 1~9999 초다. 지켜보기 구간이 그보다 길어도 끝 쪽 9999초만 본다.
SUMMARY_WINDOW_MAX_SECONDS = 9999
PROMETHEUS_OK = "ok"


def build_entrypoint_client(settings: Settings, secret_client: SecretClient) -> "EntrypointClient":
    """설정으로 클라이언트를 만든다. 주소·토큰 참조가 없으면 설정 검증이 먼저 막는다."""
    if not settings.entrypoint_base_url or not settings.entrypoint_token_ref:
        raise ValueError("entrypoint_base_url and entrypoint_token_ref are required")
    environment_map = dict(
        (key.strip(), name.strip())
        for key, _, name in (item.partition("=") for item in settings.entrypoint_environment_map)
    )
    return EntrypointClient(
        settings.entrypoint_base_url,
        parse_secret_ref(settings.entrypoint_token_ref),
        secret_client,
        environment_map=environment_map,
        timeout_seconds=settings.entrypoint_request_timeout_seconds,
    )


class EntrypointCallError(Exception):
    """진입점 호출 실패. code 는 실행기 실패 코드이고 비밀값·원문은 담지 않는다."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class EntrypointClient:
    """가중치 API 호출. 요청마다 토큰을 비밀 조회기에서 읽고 AsyncClient 를 새로 연다."""

    def __init__(
        self,
        base_url: str,
        token_ref: SecretRef,
        secret_client: SecretClient,
        *,
        environment_map: Mapping[str, str] | None = None,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token_ref = token_ref
        self._secret_client = secret_client
        self._environment_map = dict(environment_map or {})
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def environment_name(self, target: DeployTarget) -> str:
        """연결의 진입점 환경 이름. 환경 id 매핑 → 환경 종류 매핑 → 환경 종류 그대로."""
        by_id = self._environment_map.get(str(target.environment_id))
        if by_id is not None:
            return by_id
        return self._environment_map.get(target.environment_kind, target.environment_kind)

    async def get_weights(self) -> dict[str, int]:
        return _as_weights(await self._request("GET", "/weights"))

    async def put_weights(self, weights: Mapping[str, int]) -> dict[str, int]:
        return _as_weights(await self._request("PUT", "/weights", json=dict(weights)))

    async def get_summary(self, window_seconds: int) -> dict[str, Any]:
        data = await self._request("GET", "/summary", params={"window": f"{window_seconds}s"})
        if not isinstance(data, dict):
            raise EntrypointCallError(ENTRYPOINT_ERROR)
        return data

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, int] | None = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        try:
            token = await self._secret_client.get_secret(self._token_ref)
        except SecretLookupError:
            raise EntrypointCallError(ENTRYPOINT_TOKEN_MISSING) from None
        headers = {"Authorization": f"Bearer {token.get_secret_value()}"}
        async with httpx.AsyncClient(
            base_url=self._base_url,
            transport=self._transport,
            timeout=self._timeout_seconds,
            follow_redirects=False,
        ) as http:
            try:
                response = await http.request(
                    method, path, json=json, params=params, headers=headers
                )
            except httpx.TransportError:
                raise EntrypointCallError(ENTRYPOINT_UNREACHABLE) from None
        if response.status_code == httpx.codes.UNAUTHORIZED:
            raise EntrypointCallError(ENTRYPOINT_UNAUTHORIZED)
        if response.status_code == httpx.codes.SERVICE_UNAVAILABLE:
            raise EntrypointCallError(ENTRYPOINT_TOKEN_MISSING)
        if response.status_code == httpx.codes.BAD_REQUEST:
            raise EntrypointCallError(ENTRYPOINT_REJECTED)
        if not response.is_success:
            raise EntrypointCallError(ENTRYPOINT_ERROR)
        try:
            return response.json()
        except ValueError:
            raise EntrypointCallError(ENTRYPOINT_ERROR) from None


def _as_weights(data: Any) -> dict[str, int]:
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool)
        for key, value in data.items()
    ):
        raise EntrypointCallError(ENTRYPOINT_ERROR)
    return {str(key): int(value) for key, value in data.items()}


async def _apply_weights(client: EntrypointClient, requested: Mapping[str, int]) -> str | None:
    """진입점에 비율을 적용한다. 계획에 없는 환경은 0. 실패하면 실패 코드, 성공이면 None."""
    try:
        current = await client.get_weights()
        if any(name not in current for name in requested):
            return ENTRYPOINT_ENV_UNMAPPED
        body = {name: 0 for name in current}
        body.update(requested)
        await client.put_weights(body)
    except EntrypointCallError as error:
        return error.code
    return None


def _requested_weights(
    client: EntrypointClient, weights: Mapping[DeployTarget, int]
) -> dict[str, int] | None:
    """연결별 비율을 진입점 이름별 비율로. 두 연결이 같은 이름이면 None(매핑 오류)."""
    requested: dict[str, int] = {}
    for target, weight in weights.items():
        name = client.environment_name(target)
        if name in requested:
            return None
        requested[name] = weight
    return requested


class EntrypointTrafficExecutor:
    """`PUT /weights`. 같은 멱등 키는 이 프로세스에서 처음 결과를 다시 준다(진입점 호출은 멱등이라
    다른 프로세스가 같은 키로 다시 불러도 같은 상태가 된다)."""

    def __init__(self, client: EntrypointClient, clock: Clock) -> None:
        self._client = client
        self._clock = clock
        self._results: dict[str, TrafficResult] = {}

    @property
    def simulated(self) -> bool:
        return False

    async def set_weights(
        self, route: MainRoute, weights: tuple[WeightedTarget, ...], idempotency_key: str
    ) -> TrafficResult:
        check_weights_total(weights)
        existing = self._results.get(idempotency_key)
        if existing is not None:
            return existing
        requested = _requested_weights(self._client, {item.target: item.weight for item in weights})
        code = (
            ENTRYPOINT_ENV_UNMAPPED
            if requested is None
            else await _apply_weights(self._client, requested)
        )
        result: TrafficResult
        if code is None:
            result = TrafficApplied(applied_at=self._clock.now())
        else:
            result = TrafficFailed(code=code)
            logger.warning(
                "entrypoint weights not applied",
                extra={
                    "action": "set_weights",
                    "service_id": str(route.service_id),
                    "idempotency_key": idempotency_key,
                    "error_code": code,
                },
            )
        self._results[idempotency_key] = result
        return result


class EntrypointMainRouteExecutor:
    """대표 주소를 한 연결로: 그 연결 100, 나머지 0 으로 `PUT /weights`."""

    def __init__(self, client: EntrypointClient, clock: Clock) -> None:
        self._client = client
        self._clock = clock
        self._results: dict[str, RouteResult] = {}

    @property
    def simulated(self) -> bool:
        return False

    async def route_main(
        self, route: MainRoute, target: DeployTarget, idempotency_key: str
    ) -> RouteResult:
        existing = self._results.get(idempotency_key)
        if existing is not None:
            return existing
        code = await _apply_weights(self._client, {self._client.environment_name(target): 100})
        result: RouteResult
        if code is None:
            result = RouteApplied(applied_at=self._clock.now())
        else:
            result = RouteFailed(code=code)
            logger.warning(
                "entrypoint main route not applied",
                extra={
                    "action": "route_main",
                    "service_id": str(route.service_id),
                    "service_environment_id": str(target.service_environment_id),
                    "error_code": code,
                },
            )
        self._results[idempotency_key] = result
        return result


class EntrypointMetricsProvider:
    """`GET /summary?window=<구간 초>s` 의 환경 항목을 Metrics 로 바꾼다.

    Prometheus 가 응답하지 않거나 그 환경에 아직 값이 없으면(`null`) MetricsUnavailable 이다.
    """

    def __init__(self, client: EntrypointClient) -> None:
        self._client = client

    @property
    def simulated(self) -> bool:
        return False

    async def read(self, target: DeployTarget, since: datetime, until: datetime) -> MetricsResult:
        window_seconds = max(
            1, min(SUMMARY_WINDOW_MAX_SECONDS, round((until - since).total_seconds()))
        )
        try:
            summary = await self._client.get_summary(window_seconds)
        except EntrypointCallError as error:
            logger.warning(
                "entrypoint summary unavailable",
                extra={
                    "action": "read_metrics",
                    "service_environment_id": str(target.service_environment_id),
                    "error_code": error.code,
                },
            )
            return MetricsUnavailable()
        envs = summary.get("envs")
        entry = envs.get(self._client.environment_name(target)) if isinstance(envs, dict) else None
        if summary.get("prometheus") != PROMETHEUS_OK or not isinstance(entry, dict):
            return MetricsUnavailable()
        rps = entry.get("rps")
        if not isinstance(rps, int | float) or isinstance(rps, bool):
            return MetricsUnavailable()
        error_ratio = entry.get("error_ratio")
        p95_ms = entry.get("p95_ms")
        return Metrics(
            error_rate_percent=round(_as_float(error_ratio) * 100, 4),
            p95_ms=_as_float(p95_ms),
            request_count=max(0, math.floor(float(rps) * window_seconds + 0.5)),
        )


def _as_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)
