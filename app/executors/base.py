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
- CutoverRunReader: 인프라 스크립트가 고객 DB 에 남긴 컷오버·롤백 결과를 읽는다(API 서버 16).

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
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from app.adapters.base import EnvironmentConfig
from app.core.secrets import SecretValue
from app.enums import CutoverKind, DeployRequestErrorCode, LogLevel, TeardownErrorCode

# --- 넘기는 값 ---


@dataclass(frozen=True)
class SourceAccess:
    """빌드할 소스. read_token 은 비공개 저장소일 때만 있고 메모리로만 넘긴다(1시간 설치 토큰)."""

    repo_url: str
    commit_sha: str
    read_token: SecretValue | None = field(default=None, repr=False)


@dataclass(frozen=True)
class BuildSpec:
    """빌드 하나.

    resumed_after 는 일꾼이 다시 시작해 같은 빌드를 다시 부를 때(요청함 시각은 있고 external_ref 는
    없음) 그 요청함 시각이다. 실제 빌드 실행기는 이 값이 있으면 새로 시작하기 전에 같은 키의 작업을
    더 오래 찾는다(직전 시작이 성공했는데 번호를 저장하지 못한 경우).
    """

    build_id: uuid.UUID
    service_id: uuid.UUID
    service_name: str
    repo_url: str
    commit_sha: str
    repo_private: bool = False
    resumed_after: datetime | None = None


@dataclass(frozen=True)
class DeployTarget:
    """배포·되돌리기·내리기 대상 연결 하나.

    public_host 는 환경별 주소, main_public_host 는 서비스 대표 주소다. 실제 실행기는 환경 접속
    정보(environment_config·environment_secret_ref)로 클러스터에 붙고, 앱 네임스페이스는 대표 주소의
    첫 레이블(`{서비스}-{suffix}`)이다. 비밀값은 담지 않는다(위치만).
    """

    service_id: uuid.UUID
    service_name: str
    service_environment_id: uuid.UUID
    environment_id: uuid.UUID
    environment_kind: str
    public_host: str
    container_port: int
    environment_name: str = ""
    environment_config: EnvironmentConfig | None = None
    environment_secret_ref: str | None = None
    main_public_host: str = ""


@dataclass(frozen=True)
class ClusterEnvironment:
    """실제 실행기가 클러스터에 붙을 때 쓰는 환경 값. config 를 읽지 못하면 None(설정 오류)."""

    environment_id: uuid.UUID
    kind: str
    name: str
    config: EnvironmentConfig | None
    secret_ref: str | None


def cluster_environment(target: DeployTarget) -> ClusterEnvironment:
    return ClusterEnvironment(
        environment_id=target.environment_id,
        kind=target.environment_kind,
        name=target.environment_name,
        config=target.environment_config,
        secret_ref=target.environment_secret_ref,
    )


class EnvironmentLookup(Protocol):
    """poll 처럼 external_ref 만 받는 호출에서 환경 접속 정보를 다시 읽는다(지운 환경 포함)."""

    async def find_environment(self, environment_id: uuid.UUID) -> ClusterEnvironment | None: ...


@dataclass(frozen=True)
class DeploymentSpec:
    """배포 기록 한 건. digest 로만 배포한다.

    env_values 는 처리기가 호출 직전에 보관함에서 꺼낸 앱 환경 변수다(API 서버 10). None 이면
    실행기는 앱 Secret 을 읽지도 쓰지도 않고, {} 면 echo-was 소유 Secret 만 비우고, 1개 이상이면
    통째로 바꾼다. 메모리로만 넘기고 저장·로그하지 않는다.
    """

    deployment_id: uuid.UUID
    image_repository: str
    image_digest: str
    config_snapshot: dict[str, Any]
    env_values: Mapping[str, SecretValue] | None = field(default=None, repr=False)


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
class BuildSecurityScan:
    """빌드 실행이 남긴 보안 검사 원문(보안·거버넌스 2·11). 판정하지 않고 그대로 넘긴다.

    보고서가 None 이면 그 검사가 실패했거나 파일이 없었다. 받는 쪽이 UNKNOWN 증거로 남긴다.
    """

    build_id: str
    image_digest: str
    source_ref: str
    scanned_at: datetime | None
    trivy_version: str | None
    gitleaks_version: str | None
    trivy_report: dict[str, Any] | None
    gitleaks_report: object | None


@dataclass(frozen=True)
class DeployPending:
    pass


@dataclass(frozen=True)
class DeployApplied:
    pass


# 실패 진단에 담는 컨테이너 로그 마지막 줄 수·한 줄 길이 상한(API 서버 4-1).
CONTAINER_LOG_TAIL_LINES = 50
CONTAINER_LOG_LINE_MAX_LENGTH = 400


@dataclass(frozen=True)
class ContainerDiagnostics:
    """배포 실패를 판정한 순간(되돌리기·정리 전) 읽은 컨테이너의 마지막 기록(API 서버 4-1).

    - reason: 지금 대기 이유(`CrashLoopBackOff`·`ImagePullBackOff`·`ErrImagePull` 등)
    - termination_reason: 마지막 종료 이유(`OOMKilled`·`Error`·`Completed` 등)
    - exit_code: 마지막 종료 코드(지금 종료 상태가 있으면 그것, 없으면 직전 종료)
    - log_tail: 컨테이너 로그 마지막 CONTAINER_LOG_TAIL_LINES 줄(재시작했으면 직전 컨테이너 로그).
      실행기는 비밀값 모양(mask_secrets)만 가리고 줄 길이는 자르지 않는다. 처리기가 그 배포의
      환경 변수 값을 가린 **뒤에** 한 줄을 CONTAINER_LOG_LINE_MAX_LENGTH 자로 자른다
      (container_diagnostics.mask_container_log — 자른 뒤 가리면 잘린 값이 남는다).
    - log_truncated: 그보다 앞 줄이 더 있었다.
    - deadline: 진단 전체(로그 읽기 + 처리기의 환경 변수 재조회)의 마감 시각(이벤트 루프 시각
      `loop.time()`). 실행기가 진단을 읽기 시작할 때 정한다. None 이면 처리기가 설정값으로 정한다.
    읽지 못한 값은 None·빈 튜플이다. 읽기 실패는 배포 실패 처리를 막지 않는다.
    """

    container: str | None = None
    reason: str | None = None
    termination_reason: str | None = None
    exit_code: int | None = None
    restart_count: int | None = None
    log_tail: tuple[str, ...] = ()
    log_truncated: bool = False
    deadline: float | None = field(default=None, compare=False)


@dataclass(frozen=True)
class DeployFailed:
    code: DeployRequestErrorCode = DeployRequestErrorCode.DEPLOY_FAILED
    # 컨테이너 실패를 판정했을 때만 채운다(API 서버 4-1). 비교에서 빼 기존 판정 시험과 호환한다.
    diagnostics: ContainerDiagnostics | None = field(default=None, compare=False)


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
    """연결 하나의 구간 지표. 오류율은 퍼센트(0~100), p95 는 밀리초(못 읽었으면 None)."""

    error_rate_percent: float
    p95_ms: float | None
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
    """복제 지연(초). reason 은 지연을 재지 않고 통과한 이유(도착이 이미 쓰기 담당 등)."""

    seconds: float
    reason: str | None = None


@dataclass(frozen=True)
class DataSyncUnavailable:
    pass


type DataSyncResult = DataSyncLag | DataSyncUnavailable


@dataclass(frozen=True)
class CutoverRunTarget:
    """컷오버 결과를 읽을 환경(이전 계획의 도착 환경)."""

    environment_id: uuid.UUID
    environment_kind: str


@dataclass(frozen=True)
class CutoverRun:
    """`echo_ops.cutover_runs` 한 행. 표에서 NULL 을 허용하는 칸은 None 일 수 있다."""

    kind: CutoverKind
    paused_at: datetime
    resumed_at: datetime
    write_pause_ms: int
    drain_ms: int | None
    catchup_ms: int | None
    switch_ms: int | None
    rows_total: int | None
    missing_rows: int | None
    duplicate_rows: int | None


@dataclass(frozen=True)
class CutoverRunNotFound:
    pass


@dataclass(frozen=True)
class CutoverRunUnavailable:
    """읽지 못했다. code 는 고정 코드이고 주소·비밀값·외부 원문을 담지 않는다."""

    code: str


type CutoverRunResult = CutoverRun | CutoverRunNotFound | CutoverRunUnavailable


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
    # 배포 작업 로그 줄의 코드(DeployLogCode). 실행기가 즉석으로 읽는 빌드·실행 로그는 None.
    code: str | None = None


@dataclass(frozen=True)
class LogChunk:
    """시간 순 줄들. next_cursor 로 다음 줄부터 이어 읽는다. 줄이 없으면 받은 cursor 를 돌려준다."""

    lines: tuple[LogLine, ...]
    next_cursor: str | None
    has_more: bool


class InvalidLogCursor(Exception):
    """실행기가 알아보지 못하는 cursor."""


# --- 실행기가 알리는 실패 ---


class BuildStartError(Exception):
    """빌드를 시작하지 못했고 다시 불러도 소용없다(권한·워크플로 없음 등).

    처리기가 빌드를 그 코드로 닫는다.
    """

    def __init__(self, code: DeployRequestErrorCode) -> None:
        super().__init__(code)
        self.code = code


class DeployApplyError(Exception):
    """배포를 반영하지 못했고 다시 불러도 소용없다(권한·환경 설정 누락 등).

    처리기가 요청을 그 코드로 실패시킨다(배포 기록이 있으므로 되돌리기를 부른다).
    """

    def __init__(self, code: DeployRequestErrorCode) -> None:
        super().__init__(code)
        self.code = code


class ExecutorUnavailableError(Exception):
    """외부 시스템에 닿지 못했다(연결 실패·5xx·429). 실행기 안 재시도 뒤에만 올린다.

    처리기는 잡지 않는다. 일꾼이 임대 만료 뒤 같은 키로 다시 부르고, 3번이면 WORKER_LOST 로 끝난다.
    code 는 고정 코드(예: BUILD_UNREACHABLE)이고 응답 원문·비밀값을 담지 않는다.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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


@runtime_checkable
class BuildSecurityScanReader(Protocol):
    """빌드 실행의 보안 검사 원문을 읽는다(실제 빌드 실행기만, 가짜는 없음).

    없거나 읽지 못하면 None 이고 예외를 올리지 않는다(증거 수집이 배포를 막지 않는다).
    """

    async def read_security_scan(self, external_ref: str) -> BuildSecurityScan | None: ...


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


class CutoverRunReader(Protocol):
    async def find_first(
        self,
        target: CutoverRunTarget,
        kind: CutoverKind,
        since: datetime,
        until: datetime | None,
    ) -> CutoverRunResult:
        """[since, until) 안의 kind 행 중 paused_at 이 가장 이른 것. until 이 None 이면 끝이 없다.

        없으면 CutoverRunNotFound, 읽지 못하면 CutoverRunUnavailable 이고 예외를 올리지 않는다.
        """
        ...


class NoCutoverRuns:
    """기본 구현. 늘 행이 없다(가짜 숫자를 만들지 않는다)."""

    async def find_first(
        self,
        target: CutoverRunTarget,
        kind: CutoverKind,
        since: datetime,
        until: datetime | None,
    ) -> CutoverRunResult:
        return CutoverRunNotFound()
