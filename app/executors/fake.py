"""가짜 실행기(`executor_mode = fake`). 네트워크 호출 없이 step_seconds 가 지나면 성공한다.

- external_ref 에 시작 시각과 결과를 정하는 데 필요한 값을 담아, 일꾼 프로세스가 바뀌어도 같은
  답을 준다.
- 같은 멱등 키로 다시 부르면 같은 external_ref(같은 작업)를 돌려준다. calls 에 호출마다 키를 남긴다.
- 시험은 인스턴스를 만들 때 실패·멈춤 대상을 넣어 특정 커밋·연결에서 실패하게 만들 수 있다
  (운영 설정에는 이 값이 없다).
- 가짜 digest 는 sha256(서비스 id + 커밋), 이미지 주소는 registry.echo.test/<서비스 이름> 이다.
- 로그 읽기(read_logs·read_runtime_logs)는 external_ref 만으로 정해지는 줄을 준다. 줄 시각은
  external_ref 에 든 시작 시각부터 LOG_LINE_INTERVAL 간격이다(시계를 쓰지 않아 같은 cursor 면 늘
  같은 줄). cursor 는 줄 번호 문자열이다.
- 시험은 log_injections 로 "토큰처럼 보이는 줄"을 넣을 수 있다. 이 줄은 가리지 않고 그대로 돌려줘
  서버 쪽 마스킹을 시험한다(운영 설정에는 이 값이 없다).
- 이전 계획용 트래픽·지표·데이터 확인 가짜는 결정적인 값을 준다: 트래픽은 바로 적용, 지표는 오류율
  0.1%·p95 120ms·요청 수 max(1, round(rps × 비율/100 × 창 초)), 복제 지연 0.3초. 누적 카운터
  (read_counts)는 주입 시계로 "rps × 그 연결 비율/100 × 지난 초"를 쌓고 5xx 는 0 이다. rps 는 설정
  MIGRATION_EXPECTED_RPS 한 값을 넣는다.
"""

import hashlib
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.clock import Clock
from app.enums import LogLevel
from app.executors.base import (
    BuildFailed,
    BuildPollResult,
    BuildRunning,
    BuildSpec,
    BuildSucceeded,
    CountsResult,
    CountsUnavailable,
    DataSyncLag,
    DataSyncResult,
    DataSyncUnavailable,
    DeployApplied,
    DeployFailed,
    DeploymentSpec,
    DeployPending,
    DeployPollResult,
    DeployTarget,
    HealthOk,
    HealthProbeResult,
    InvalidLogCursor,
    LogChunk,
    LogLine,
    MainRoute,
    Metrics,
    MetricsResult,
    MetricsUnavailable,
    RequestCounts,
    Reverted,
    RevertFailed,
    RevertResult,
    RouteApplied,
    RouteResult,
    SourceAccess,
    TeardownFailed,
    TeardownPending,
    TeardownPollResult,
    TeardownRemoved,
    TrafficApplied,
    TrafficFailed,
    TrafficResult,
    WeightedTarget,
    check_weights_total,
)

FAKE_REGISTRY = "registry.echo.test"
_SEPARATOR = "|"
LOG_LINE_INTERVAL = timedelta(seconds=2)


def _log_lines(started_at: datetime, messages: list[tuple[LogLevel, str]]) -> list[LogLine]:
    return [
        LogLine(at=started_at + LOG_LINE_INTERVAL * index, level=level, message=message)
        for index, (level, message) in enumerate(messages)
    ]


def _page(lines: list[LogLine], cursor: str | None, limit: int) -> LogChunk:
    """cursor = 다음에 읽을 줄 번호. 끝을 넘은 번호는 빈 청크다."""
    if cursor is None:
        start = 0
    elif cursor.isascii() and cursor.isdigit():
        start = int(cursor)
    else:
        raise InvalidLogCursor
    page = lines[start : start + limit]
    if not page:
        return LogChunk(lines=(), next_cursor=cursor, has_more=False)
    end = start + len(page)
    return LogChunk(lines=tuple(page), next_cursor=str(end), has_more=end < len(lines))


def _inject(
    messages: list[tuple[LogLevel, str]], injections: tuple[str, ...]
) -> list[tuple[LogLevel, str]]:
    """시험이 넣은 줄을 첫 줄 뒤에 끼운다."""
    return [*messages[:1], *((LogLevel.INFO, line) for line in injections), *messages[1:]]


@dataclass(frozen=True)
class FakeCall:
    method: str
    idempotency_key: str


class _FakeOperations:
    """멱등 키 → external_ref. external_ref = prefix|키|시작 시각(ms)|값..."""

    def __init__(self, prefix: str, clock: Clock, step_seconds: float) -> None:
        self._prefix = prefix
        self._clock = clock
        self._step = timedelta(seconds=step_seconds)
        self._external_refs: dict[str, str] = {}
        self.calls: list[FakeCall] = []

    def start(self, method: str, idempotency_key: str, *values: str) -> str:
        self.calls.append(FakeCall(method, idempotency_key))
        existing = self._external_refs.get(idempotency_key)
        if existing is not None:
            return existing
        started_ms = int(self._clock.now().timestamp() * 1000)
        external_ref = _SEPARATOR.join([self._prefix, idempotency_key, str(started_ms), *values])
        self._external_refs[idempotency_key] = external_ref
        return external_ref

    def parse(self, external_ref: str) -> tuple[bool, list[str]]:
        """(step_seconds 가 지났는지, 담아 둔 값들)."""
        started_at, values = self.parse_started(external_ref)
        return self._clock.now() >= started_at + self._step, values

    def parse_started(self, external_ref: str) -> tuple[datetime, list[str]]:
        """(시작 시각, 담아 둔 값들). 시계를 보지 않는다."""
        prefix, _key, started_ms, *values = external_ref.split(_SEPARATOR)
        if prefix != self._prefix:
            raise ValueError("unknown external_ref")
        return datetime.fromtimestamp(int(started_ms) / 1000, UTC), values

    @property
    def operation_keys(self) -> list[str]:
        return list(self._external_refs)


class FakeBuildExecutor:
    def __init__(
        self,
        clock: Clock,
        step_seconds: float,
        *,
        fail_commits: Iterable[str] = (),
        stall_commits: Iterable[str] = (),
        log_injections: Iterable[str] = (),
    ) -> None:
        self._operations = _FakeOperations("fake-build", clock, step_seconds)
        self._fail_commits = frozenset(fail_commits)
        self._stall_commits = frozenset(stall_commits)
        self._log_injections = tuple(log_injections)
        self.received_tokens = 0

    @property
    def simulated(self) -> bool:
        return True

    @property
    def calls(self) -> list[FakeCall]:
        return self._operations.calls

    @property
    def operation_keys(self) -> list[str]:
        return self._operations.operation_keys

    async def start(self, build: BuildSpec, source: SourceAccess, idempotency_key: str) -> str:
        if source.read_token is not None:
            self.received_tokens += 1
        return self._operations.start(
            "start", idempotency_key, str(build.service_id), build.commit_sha, build.service_name
        )

    async def poll(self, external_ref: str) -> BuildPollResult:
        is_done, (service_id, commit_sha, service_name) = self._operations.parse(external_ref)
        if commit_sha in self._stall_commits or not is_done:
            return BuildRunning()
        if commit_sha in self._fail_commits:
            return BuildFailed()
        return BuildSucceeded(
            image_repository=f"{FAKE_REGISTRY}/{service_name}",
            image_digest=_fake_digest(service_id, commit_sha),
        )

    async def read_logs(self, external_ref: str, cursor: str | None, limit: int) -> LogChunk:
        started_at, (service_id, commit_sha, service_name) = self._operations.parse_started(
            external_ref
        )
        image = f"{FAKE_REGISTRY}/{service_name}"
        messages = [
            (LogLevel.INFO, f"Fetching source at commit {commit_sha[:12]}"),
            (LogLevel.INFO, "Detecting build configuration"),
            (LogLevel.INFO, "Installing dependencies"),
            (LogLevel.INFO, f"Building image {image}"),
        ]
        if commit_sha in self._fail_commits:
            messages.append((LogLevel.ERROR, "Build step failed with exit code 1"))
        else:
            messages += [
                (LogLevel.INFO, f"Pushing image {image}"),
                (LogLevel.INFO, f"Image digest {_fake_digest(service_id, commit_sha)}"),
                (LogLevel.INFO, "Build finished"),
            ]
        return _page(_log_lines(started_at, _inject(messages, self._log_injections)), cursor, limit)


def _fake_digest(service_id: str, commit_sha: str) -> str:
    return "sha256:" + hashlib.sha256(f"{service_id}{commit_sha}".encode()).hexdigest()


class FakeDeployExecutor:
    def __init__(
        self,
        clock: Clock,
        step_seconds: float,
        *,
        fail_service_environment_ids: Iterable[uuid.UUID] = (),
        stall_service_environment_ids: Iterable[uuid.UUID] = (),
        fail_revert: bool = False,
        log_injections: Iterable[str] = (),
    ) -> None:
        self._operations = _FakeOperations("fake-deploy", clock, step_seconds)
        self._fail = {str(value) for value in fail_service_environment_ids}
        self._stall = {str(value) for value in stall_service_environment_ids}
        self._fail_revert = fail_revert
        self._log_injections = tuple(log_injections)
        self.reverts: list[FakeCall] = []

    @property
    def simulated(self) -> bool:
        return True

    @property
    def calls(self) -> list[FakeCall]:
        return self._operations.calls

    @property
    def operation_keys(self) -> list[str]:
        return self._operations.operation_keys

    def fail_service_environment(self, service_environment_id: uuid.UUID) -> None:
        self._fail.add(str(service_environment_id))

    async def apply(
        self, deployment: DeploymentSpec, target: DeployTarget, idempotency_key: str
    ) -> str:
        return self._operations.start("apply", idempotency_key, str(target.service_environment_id))

    async def poll(self, external_ref: str) -> DeployPollResult:
        is_done, (service_environment_id,) = self._operations.parse(external_ref)
        if service_environment_id in self._stall or not is_done:
            return DeployPending()
        if service_environment_id in self._fail:
            return DeployFailed()
        return DeployApplied()

    async def revert(
        self, target: DeployTarget, previous: DeploymentSpec | None, idempotency_key: str
    ) -> RevertResult:
        self.reverts.append(FakeCall("revert", idempotency_key))
        if self._fail_revert:
            return RevertFailed()
        return Reverted(external_ref=f"fake-revert{_SEPARATOR}{idempotency_key}")

    async def read_runtime_logs(
        self,
        target: DeployTarget,
        deployment: DeploymentSpec,
        external_ref: str,
        cursor: str | None,
        limit: int,
    ) -> LogChunk:
        started_at, (service_environment_id,) = self._operations.parse_started(external_ref)
        health_path = str(deployment.config_snapshot.get("healthCheckPath", "/"))
        messages = [
            (
                LogLevel.INFO,
                f"Pulling image {deployment.image_repository}@{deployment.image_digest}",
            ),
            (LogLevel.INFO, f"Starting {target.service_name} ({target.environment_kind})"),
            (LogLevel.INFO, f"Listening on 0.0.0.0:{target.container_port}"),
        ]
        if service_environment_id in self._fail:
            messages.append((LogLevel.ERROR, "Container exited with code 1"))
        else:
            messages += [
                (LogLevel.INFO, f"GET {health_path} 200"),
                (LogLevel.INFO, "Ready to serve traffic"),
            ]
        return _page(_log_lines(started_at, _inject(messages, self._log_injections)), cursor, limit)


class FakeMainRouteExecutor:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.calls: list[FakeCall] = []

    @property
    def simulated(self) -> bool:
        return True

    async def route_main(
        self, route: MainRoute, target: DeployTarget, idempotency_key: str
    ) -> RouteResult:
        self.calls.append(FakeCall("route_main", idempotency_key))
        return RouteApplied(applied_at=self._clock.now())


class FakeHealthChecker:
    """늘 통과한다. 실패 규칙 시험은 시험 코드의 대본 헬스체커를 쓴다."""

    @property
    def simulated(self) -> bool:
        return True

    async def probe(self, url: str, path: str, timeout_seconds: float) -> HealthProbeResult:
        return HealthOk(status_code=200)


class FakeTeardownExecutor:
    def __init__(
        self,
        clock: Clock,
        step_seconds: float,
        *,
        fail_service_environment_ids: Iterable[uuid.UUID] = (),
        stall_service_environment_ids: Iterable[uuid.UUID] = (),
    ) -> None:
        self._operations = _FakeOperations("fake-teardown", clock, step_seconds)
        self._fail = {str(value) for value in fail_service_environment_ids}
        self._stall = {str(value) for value in stall_service_environment_ids}

    @property
    def simulated(self) -> bool:
        return True

    @property
    def calls(self) -> list[FakeCall]:
        return self._operations.calls

    def fail_service_environment(self, service_environment_id: uuid.UUID) -> None:
        self._fail.add(str(service_environment_id))

    def recover_service_environment(self, service_environment_id: uuid.UUID) -> None:
        self._fail.discard(str(service_environment_id))

    async def remove(self, target: DeployTarget, idempotency_key: str) -> str:
        return self._operations.start("remove", idempotency_key, str(target.service_environment_id))

    async def poll(self, external_ref: str) -> TeardownPollResult:
        is_done, (service_environment_id,) = self._operations.parse(external_ref)
        if service_environment_id in self._stall or not is_done:
            return TeardownPending()
        if service_environment_id in self._fail:
            return TeardownFailed()
        return TeardownRemoved()


FAKE_TRAFFIC_FAILED = "FAKE_TRAFFIC_FAILED"
FAKE_ERROR_RATE_PERCENT = 0.1
FAKE_P95_MS = 120.0
# 비율 100% 일 때 초당 요청 수(설정 MIGRATION_EXPECTED_RPS 의 기본값과 같다).
FAKE_REQUESTS_PER_SECOND = 50
FAKE_REPLICATION_LAG_SECONDS = 0.3


def _never(_: str) -> bool:
    return False


class FakeTrafficExecutor:
    """바로 TrafficApplied(지금 시각)를 돌려준다. 같은 키면 처음 결과를 그대로 준다.

    연결마다 마지막으로 적용한 비율을 기억해 가짜 지표(요청 수)가 쓴다. 시험은 fail_when(키 → 참이면
    실패)으로 특정 키를 TrafficFailed 로 만든다(운영 설정에는 이 값이 없다).
    """

    def __init__(self, clock: Clock, *, fail_when: Callable[[str], bool] = _never) -> None:
        self._clock = clock
        self._fail_when = fail_when
        self._results: dict[str, TrafficResult] = {}
        self._weights: dict[uuid.UUID, int] = {}
        self.calls: list[FakeCall] = []
        self.applied: list[tuple[str, tuple[int, ...]]] = []

    @property
    def simulated(self) -> bool:
        return True

    def fail_when(self, predicate: Callable[[str], bool]) -> None:
        self._fail_when = predicate

    def weight_of(self, service_environment_id: uuid.UUID) -> int | None:
        return self._weights.get(service_environment_id)

    async def set_weights(
        self, route: MainRoute, weights: tuple[WeightedTarget, ...], idempotency_key: str
    ) -> TrafficResult:
        check_weights_total(weights)
        self.calls.append(FakeCall("set_weights", idempotency_key))
        existing = self._results.get(idempotency_key)
        if existing is not None:
            return existing
        result: TrafficResult
        if self._fail_when(idempotency_key):
            result = TrafficFailed(code=FAKE_TRAFFIC_FAILED)
        else:
            result = TrafficApplied(applied_at=self._clock.now())
            for item in weights:
                self._weights[item.target.service_environment_id] = item.weight
            self.applied.append((idempotency_key, tuple(item.weight for item in weights)))
        self._results[idempotency_key] = result
        return result


@dataclass
class _FakeCounter:
    """연결 하나의 가짜 누적 카운터. requests 는 소수까지 쌓고 읽을 때 내림한다."""

    requests: float = 0.0
    errors_5xx: int = 0
    read_at: datetime | None = None
    is_frozen: bool = False
    is_failing: bool = False


class FakeMetricsProvider:
    """지표(p95 참고용)와 누적 요청·5xx 카운터(판정용)를 결정적으로 준다.

    - read: 오류율 0.1%·p95 120ms, 요청 수 max(1, round(rps × 비율/100 × 창 초)).
    - read_counts: 그 연결에 처음 읽은 순간 0 에서 시작해, 읽을 때마다 지난 시간 × rps × 지금 비율
      (weight_of, 모르면 100)/100 을 더한다. 비율이 바뀌어도 줄지 않는다. 5xx 는 0.

    시험용 손잡이(운영 설정에는 쓰지 않는다): unavailable(read 가 MetricsUnavailable), freeze(카운터
    고정), add_errors(5xx 더하기), set_counts(값 바꾸기, 줄이면 진입점 재시작 흉내),
    fail_counts(읽기 실패, 그동안에도 카운터는 쌓인다). 손잡이는 연결 id 로 가리킨다.
    """

    def __init__(
        self,
        clock: Clock,
        weight_of: Callable[[uuid.UUID], int | None] | None = None,
        *,
        requests_per_second: int = FAKE_REQUESTS_PER_SECOND,
        unavailable: bool = False,
    ) -> None:
        self._clock = clock
        self._weight_of = weight_of
        self._requests_per_second = requests_per_second
        self._counters: dict[uuid.UUID, _FakeCounter] = {}
        self._is_all_failing = False
        self.unavailable = unavailable
        self.calls: list[tuple[uuid.UUID, datetime, datetime]] = []
        self.count_calls: list[uuid.UUID] = []

    @property
    def simulated(self) -> bool:
        return True

    def _weight(self, service_environment_id: uuid.UUID) -> int:
        known = self._weight_of(service_environment_id) if self._weight_of else None
        return 100 if known is None else known

    async def read(self, target: DeployTarget, since: datetime, until: datetime) -> MetricsResult:
        self.calls.append((target.service_environment_id, since, until))
        if self.unavailable:
            return MetricsUnavailable()
        weight = self._weight(target.service_environment_id)
        window_seconds = max((until - since).total_seconds(), 0.0)
        request_count = max(1, round(self._requests_per_second * weight / 100 * window_seconds))
        return Metrics(
            error_rate_percent=FAKE_ERROR_RATE_PERCENT,
            p95_ms=FAKE_P95_MS,
            request_count=request_count,
        )

    async def read_counts(self, target: DeployTarget) -> CountsResult:
        service_environment_id = target.service_environment_id
        self.count_calls.append(service_environment_id)
        counter = self._accumulate(service_environment_id)
        if counter.is_failing or self._is_all_failing:
            return CountsUnavailable()
        return RequestCounts(requests=int(counter.requests), errors_5xx=counter.errors_5xx)

    def _accumulate(self, service_environment_id: uuid.UUID) -> _FakeCounter:
        counter = self._counters.setdefault(service_environment_id, _FakeCounter())
        now = self._clock.now()
        if counter.read_at is not None and not counter.is_frozen:
            elapsed = max((now - counter.read_at).total_seconds(), 0.0)
            counter.requests += (
                self._requests_per_second * self._weight(service_environment_id) / 100 * elapsed
            )
        counter.read_at = now
        return counter

    def freeze(self, service_environment_id: uuid.UUID, *, is_frozen: bool = True) -> None:
        """카운터를 지금 값에 고정한다(그 환경으로 요청이 가지 않는 상황)."""
        self._accumulate(service_environment_id).is_frozen = is_frozen

    def add_errors(self, service_environment_id: uuid.UUID, count: int) -> None:
        self._accumulate(service_environment_id).errors_5xx += count

    def set_counts(
        self, service_environment_id: uuid.UUID, requests: int, errors_5xx: int = 0
    ) -> None:
        """누적값을 바꾼다. 지금보다 작게 주면 진입점이 재시작해 0부터 다시 센 것처럼 보인다."""
        counter = self._accumulate(service_environment_id)
        counter.requests = float(requests)
        counter.errors_5xx = errors_5xx

    def fail_counts(
        self, service_environment_id: uuid.UUID | None = None, *, is_failing: bool = True
    ) -> None:
        """그 연결(None 이면 모든 연결)의 카운터 읽기를 CountsUnavailable 로 만든다."""
        if service_environment_id is None:
            self._is_all_failing = is_failing
            return
        self._accumulate(service_environment_id).is_failing = is_failing


class FakeDataSyncChecker:
    """복제 지연 0.3초. 시험은 unavailable=True 로 DataSyncUnavailable 을 만든다."""

    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    @property
    def simulated(self) -> bool:
        return True

    async def replication_lag(self, source: DeployTarget, target: DeployTarget) -> DataSyncResult:
        self.calls.append((source.service_environment_id, target.service_environment_id))
        if self.unavailable:
            return DataSyncUnavailable()
        return DataSyncLag(seconds=FAKE_REPLICATION_LAG_SECONDS)
