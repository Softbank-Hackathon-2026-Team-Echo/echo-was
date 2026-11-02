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
"""

import hashlib
import uuid
from collections.abc import Iterable
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
