"""배포 흐름의 외부 실행기 인터페이스. 계약은 docs/executors.md 에 있다.

- BuildExecutor: 소스 → 이미지(image_repository + sha256 digest).
- DeployExecutor: digest 로 환경 하나에 배포·확인·되돌리기.
- MainRouteExecutor: 서비스 대표 주소를 한 연결로 보낸다(고정 진입점 프록시).
- HealthChecker: 환경별 퍼블릭 주소 + 헬스 경로를 한 번 확인한다.
- TeardownExecutor: 연결 하나를 클러스터에서 내린다.

모든 시작 호출은 우리가 먼저 정한 멱등 키를 받는다(빌드 = builds.id, 배포·되돌리기 =
deployments.id, 대표 주소 = deployments.id, 내리기 = 연결 id + teardown_attempt). 같은 키로 다시
불러도 같은 작업을 돌려줘야 한다. external_ref·실패 코드에는 비밀값을 넣지 않는다.

이 패키지는 app.models 를 import 하지 않는다. 호출하는 쪽이 아래 값 객체로 바꿔 넘긴다.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from app.core.secrets import SecretValue
from app.enums import DeployRequestErrorCode, TeardownErrorCode

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


# --- 인터페이스 ---


class BuildExecutor(Protocol):
    @property
    def simulated(self) -> bool: ...

    async def start(self, build: BuildSpec, source: SourceAccess, idempotency_key: str) -> str:
        """빌드를 시작하고 external_ref 를 돌려준다. 같은 키면 같은 작업."""
        ...

    async def poll(self, external_ref: str) -> BuildPollResult: ...


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
