"""배포 흐름의 외부 실행기 인터페이스. 계약은 docs/executors.md 에 있다.

- BuildExecutor: 소스 → 이미지(image_repository + sha256 digest).
- DeployExecutor: digest 로 환경 하나에 배포·확인·되돌리기.
- MainRouteExecutor: 서비스 대표 주소를 한 연결로 보낸다(고정 진입점 프록시).
- HealthChecker: 환경별 퍼블릭 주소 + 헬스 경로를 한 번 확인한다.
- TeardownExecutor: 연결 하나를 클러스터에서 내린다.
- TrafficExecutor: 대표 주소의 트래픽을 여러 연결에 비율로 나눠 보낸다(이전 계획, 실제는 Envoy).
- MetricsProvider: 연결 하나의 누적 요청·5xx 카운터(판정용)와 구간 지표(오류율·p95·요청 수,
  p95 참고용)를 읽는다(실제는 고정 진입점 Envoy 의 GET /counters·GET /summary).
- DataSyncChecker: 출발 → 도착 데이터 복제 지연을 읽는다(실제는 인프라 7).

모든 시작 호출은 우리가 먼저 정한 멱등 키를 받는다(빌드 = builds.id, 배포·되돌리기 =
deployments.id, 대표 주소 = deployments.id, 내리기 = 연결 id + teardown_attempt, 트래픽 비율 =
`{계획 id}:{단계}`·`{계획 id}:rollback:{시도 번호}`). 같은 키로 다시 불러도 같은 작업을 돌려줘야
한다. external_ref·실패 코드에는 비밀값을 넣지 않는다.

로그 읽기 계약(read_logs·read_runtime_logs): 상태를 바꾸지 않는 조회다. limit 을 넘지 않는 줄을
시간 순으로 돌려주고, 같은 cursor 면 같은 줄을 준다. 알아보지 못하는 cursor 는 InvalidLogCursor 다.
비밀값(설치 토큰·레지스트리 비밀번호·환경변수 값 등)은 가려서 돌려준다. 보존 기간이 지나 없으면
빈 청크다. 서버는 응답 직전에 알려진 토큰 모양을 한 번 더 가리지만 이 책임을 대신하지 않는다.

이 패키지는 app.models 를 import 하지 않는다. 호출하는 쪽이 아래 값 객체로 바꿔 넘긴다.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from app.core.secrets import SecretValue
from app.enums import DeployRequestErrorCode, LogLevel, TeardownErrorCode

# --- 넘기는 값 ---


@dataclass(frozen=True)
class SourceAccess:
    """빌드할 소스. read_token 은 비공개 저장소일 때만 있고 메모리로만 넘긴다(1시간 설치 토큰)."""

    repo_url: str
    commit_sha: str
    read_token: SecretValue | None = field(default=None, repr=False)


@dataclass(frozen=True)
class BuildSpec:
    build_id: uuid.UUID
    service_id: uuid.UUID
    service_name: str
    repo_url: str
    commit_sha: str


@dataclass(frozen=True)
class DeployTarget:
    """배포·되돌리기·내리기 대상 연결 하나."""

    service_id: uuid.UUID
    service_name: str
    service_environment_id: uuid.UUID
    environment_id: uuid.UUID
    environment_kind: str
    public_host: str
    container_port: int


@dataclass(frozen=True)
class DeploymentSpec:
    """배포 기록 한 건. digest 로만 배포한다."""

    deployment_id: uuid.UUID
    image_repository: str
    image_digest: str
    config_snapshot: dict[str, Any]


@dataclass(frozen=True)
class MainRoute:
    service_id: uuid.UUID
    service_name: str
    public_host: str


# --- 결과 ---


@dataclass(frozen=True)
class BuildRunning:
    pass


@dataclass(frozen=True)
class BuildSucceeded:
    image_repository: str
    image_digest: str


@dataclass(frozen=True)
class BuildFailed:
    code: DeployRequestErrorCode = DeployRequestErrorCode.BUILD_FAILED


type BuildPollResult = BuildRunning | BuildSucceeded | BuildFailed


@dataclass(frozen=True)
class DeployPending:
    pass


@dataclass(frozen=True)
class DeployApplied:
    pass


@dataclass(frozen=True)
class DeployFailed:
    code: DeployRequestErrorCode = DeployRequestErrorCode.DEPLOY_FAILED


type DeployPollResult = DeployPending | DeployApplied | DeployFailed


@dataclass(frozen=True)
class Reverted:
    external_ref: str


@dataclass(frozen=True)
class RevertFailed:
    code: DeployRequestErrorCode = DeployRequestErrorCode.DEPLOY_FAILED


type RevertResult = Reverted | RevertFailed


@dataclass(frozen=True)
class RouteApplied:
    applied_at: datetime


@dataclass(frozen=True)
class RouteFailed:
    code: str


type RouteResult = RouteApplied | RouteFailed


@dataclass(frozen=True)
class HealthOk:
    status_code: int


@dataclass(frozen=True)
class HealthHttpError:
    status_code: int


@dataclass(frozen=True)
class HealthUnreachable:
    pass


type HealthProbeResult = HealthOk | HealthHttpError | HealthUnreachable


# --- 이전 계획: 트래픽 비율·지표·데이터 복제 ---

TRAFFIC_WEIGHT_TOTAL = 100


@dataclass(frozen=True)
class WeightedTarget:
    """대표 주소 트래픽 중 이 연결로 보낼 비율(0~100)."""

    target: DeployTarget
    weight: int


@dataclass(frozen=True)
class TrafficApplied:
    applied_at: datetime


@dataclass(frozen=True)
class TrafficFailed:
    code: str


type TrafficResult = TrafficApplied | TrafficFailed


@dataclass(frozen=True)
class Metrics:
    """연결 하나의 구간 지표. 오류율은 퍼센트(0~100), p95 는 밀리초."""

    error_rate_percent: float
    p95_ms: float
    request_count: int


@dataclass(frozen=True)
class MetricsUnavailable:
    pass


type MetricsResult = Metrics | MetricsUnavailable


@dataclass(frozen=True, slots=True)
class RequestCounts:
    """진입점이 켜진 뒤 그 환경으로 보낸 누적 요청 수와 그중 5xx 응답 수.

    단조 증가가 원칙이다. 줄어들면(진입점 재시작) 부르는 쪽이 재시작으로 처리한다.
    errors_5xx <= requests 는 보장하지 않는다.
    """

    requests: int
    errors_5xx: int


@dataclass(frozen=True, slots=True)
class CountsUnavailable:
    """카운터를 읽지 못했다(일시 오류).

    code 는 실제 구현의 실패 코드(예: ENTRYPOINT_UNREACHABLE)다. 가짜는 None.
    """

    code: str | None = None


type CountsResult = RequestCounts | CountsUnavailable


@dataclass(frozen=True)
class DataSyncLag:
    seconds: float


@dataclass(frozen=True)
class DataSyncUnavailable:
    pass


type DataSyncResult = DataSyncLag | DataSyncUnavailable


def check_weights_total(weights: tuple[WeightedTarget, ...]) -> None:
    """비율 합계가 100 이 아니면 ValueError. 부르는 쪽의 버그라 실행기에 넘기지 않는다."""
    if sum(item.weight for item in weights) != TRAFFIC_WEIGHT_TOTAL or any(
        not 0 <= item.weight <= TRAFFIC_WEIGHT_TOTAL for item in weights
    ):
        raise ValueError("traffic weights must be between 0 and 100 and add up to 100")


@dataclass(frozen=True)
class TeardownPending:
    pass


@dataclass(frozen=True)
class TeardownRemoved:
    pass


@dataclass(frozen=True)
class TeardownFailed:
    code: TeardownErrorCode = TeardownErrorCode.TEARDOWN_FAILED


type TeardownPollResult = TeardownPending | TeardownRemoved | TeardownFailed


# --- 로그 읽기 ---


@dataclass(frozen=True)
class LogLine:
    at: datetime
    level: LogLevel
    message: str


@dataclass(frozen=True)
class LogChunk:
    """시간 순 줄들. next_cursor 로 다음 줄부터 이어 읽는다. 줄이 없으면 받은 cursor 를 돌려준다."""

    lines: tuple[LogLine, ...]
    next_cursor: str | None
    has_more: bool


class InvalidLogCursor(Exception):
    """실행기가 알아보지 못하는 cursor."""


# --- 인터페이스 ---


class BuildExecutor(Protocol):
    @property
    def simulated(self) -> bool: ...

    async def start(self, build: BuildSpec, source: SourceAccess, idempotency_key: str) -> str:
        """빌드를 시작하고 external_ref 를 돌려준다. 같은 키면 같은 작업."""
        ...

    async def poll(self, external_ref: str) -> BuildPollResult: ...

    async def read_logs(self, external_ref: str, cursor: str | None, limit: int) -> LogChunk:
        """빌드 로그를 읽는다(상태를 바꾸지 않는다). 계약은 아래 '로그 읽기 계약'과 같다."""
        ...


class DeployExecutor(Protocol):
    @property
    def simulated(self) -> bool: ...

    async def apply(
        self, deployment: DeploymentSpec, target: DeployTarget, idempotency_key: str
    ) -> str: ...

    async def poll(self, external_ref: str) -> DeployPollResult: ...

    async def revert(
        self, target: DeployTarget, previous: DeploymentSpec | None, idempotency_key: str
    ) -> RevertResult:
        """실패한 새 버전을 거두고 직전 버전(previous, 없으면 아무것도 없는 상태)을 유지한다."""
        ...

    async def read_runtime_logs(
        self,
        target: DeployTarget,
        deployment: DeploymentSpec,
        external_ref: str,
        cursor: str | None,
        limit: int,
    ) -> LogChunk:
        """배포 하나(external_ref = apply 가 돌려준 값)의 실행 로그를 읽는다."""
        ...


class MainRouteExecutor(Protocol):
    @property
    def simulated(self) -> bool: ...

    async def route_main(
        self, route: MainRoute, target: DeployTarget, idempotency_key: str
    ) -> RouteResult:
        """대표 주소를 target 연결로 보낸다. 적용했다고 답할 때만 트래픽 비율을 적는다."""
        ...


class HealthChecker(Protocol):
    @property
    def simulated(self) -> bool: ...

    async def probe(self, url: str, path: str, timeout_seconds: float) -> HealthProbeResult:
        """url + path 를 한 번 GET 한다. 2xx 면 HealthOk. 리다이렉트는 따르지 않는다."""
        ...


class TeardownExecutor(Protocol):
    @property
    def simulated(self) -> bool: ...

    async def remove(self, target: DeployTarget, idempotency_key: str) -> str: ...

    async def poll(self, external_ref: str) -> TeardownPollResult: ...


class TrafficExecutor(Protocol):
    @property
    def simulated(self) -> bool: ...

    async def set_weights(
        self, route: MainRoute, weights: tuple[WeightedTarget, ...], idempotency_key: str
    ) -> TrafficResult:
        """대표 주소 트래픽을 weights 대로 나눠 보낸다. 적용했다고 답할 때만 비율을 적는다.

        합계가 100 이 아니면 ValueError(check_weights_total). 같은 키면 같은 결과를 돌려준다.
        """
        ...


class MetricsProvider(Protocol):
    @property
    def simulated(self) -> bool: ...

    async def read(self, target: DeployTarget, since: datetime, until: datetime) -> MetricsResult:
        """[since, until) 구간의 지표. 읽을 수 없으면 MetricsUnavailable.

        이전 판정에는 쓰지 않고 단계 결과의 p95 참고 값으로만 쓴다.
        """
        ...

    async def read_counts(self, target: DeployTarget) -> CountsResult:
        """target 환경의 누적 요청·5xx 카운터. 상태를 바꾸지 않는 조회다.

        값은 음수가 아니고 단조 증가가 원칙이다(재시작하면 0부터 다시 쌓인다). 읽지 못하면
        CountsUnavailable 이고, 부르는 쪽은 일시 오류로 보고 다시 읽는다.
        """
        ...


class DataSyncChecker(Protocol):
    @property
    def simulated(self) -> bool: ...

    async def replication_lag(self, source: DeployTarget, target: DeployTarget) -> DataSyncResult:
        """출발 → 도착 데이터 복제 지연(초). 읽을 수 없으면 DataSyncUnavailable."""
        ...
