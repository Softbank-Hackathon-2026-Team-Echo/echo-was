"""Model·Schema 가 함께 쓰는 Enum 을 한곳에 둔다. 값은 DB 에 저장되는 코드(UPPER_SNAKE)다.

환경 종류(aws·gcp·onprem)는 Enum 으로 두지 않는다. 새 종류를 파일 하나로 더할 수 있도록
설정 양식 레지스트리(app.adapters)의 키로 관리한다.
"""

from enum import StrEnum


class DeploymentStatus(StrEnum):
    """배포 상태. 허용 전환은 용어 사전의 '배포 상태'를 따른다. 끝 상태는 되감지 않는다."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class EnvironmentConnectionStatus(StrEnum):
    """환경 접속 상태. 등록·다시 확인·config/secretRef 수정 때 바뀐다. 단건 조회는 바꾸지 않는다."""

    UNCHECKED = "UNCHECKED"
    CONNECTED = "CONNECTED"
    FAILED = "FAILED"


class ConnectionErrorCode(StrEnum):
    """접속 확인·클러스터 정보 읽기 실패 이유. DB CHECK 를 걸지 않고 앱이 이 값만 쓴다."""

    TIMEOUT = "TIMEOUT"
    SECRET_REF_MISSING = "SECRET_REF_MISSING"
    SECRET_REF_INVALID = "SECRET_REF_INVALID"
    SECRET_NOT_FOUND = "SECRET_NOT_FOUND"
    SECRET_STORE_UNSUPPORTED = "SECRET_STORE_UNSUPPORTED"
    CONFIG_INVALID = "CONFIG_INVALID"
    AUTH_FAILED = "AUTH_FAILED"
    UNREACHABLE = "UNREACHABLE"
    UNKNOWN = "UNKNOWN"


class ServiceStatus(StrEnum):
    """서비스 상태. 삭제를 받으면 DELETING, 모든 연결을 내리면 소프트 삭제된다."""

    ACTIVE = "ACTIVE"
    DELETING = "DELETING"


class TeardownErrorCode(StrEnum):
    """내리기 실패 이유. 서비스 delete_error·연결 teardown_error 에 쓴다."""

    TEARDOWN_FAILED = "TEARDOWN_FAILED"
    TEARDOWN_TIMEOUT = "TEARDOWN_TIMEOUT"
    WORKER_LOST = "WORKER_LOST"


class DeployRequestStatus(StrEnum):
    """배포 요청 상태. 취소는 QUEUED 에서만 한다. 끝 상태는 되감지 않는다."""

    QUEUED = "QUEUED"
    BUILDING = "BUILDING"
    DEPLOYING = "DEPLOYING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_DEPLOY_REQUEST_STATUSES

    @property
    def is_active(self) -> bool:
        """같은 연결에 하나만 있을 수 있는 실행 중 상태."""
        return self in ACTIVE_DEPLOY_REQUEST_STATUSES


ACTIVE_DEPLOY_REQUEST_STATUSES = frozenset(
    {DeployRequestStatus.BUILDING, DeployRequestStatus.DEPLOYING, DeployRequestStatus.VERIFYING}
)
_TERMINAL_DEPLOY_REQUEST_STATUSES = frozenset(
    {DeployRequestStatus.SUCCEEDED, DeployRequestStatus.FAILED, DeployRequestStatus.CANCELLED}
)


class DeployRequestTrigger(StrEnum):
    MANUAL = "MANUAL"
    # 서비스 중 연결의 성공을 예비 연결이 따라가는 요청.
    STANDBY_SYNC = "STANDBY_SYNC"


class DeployRequestCancelReason(StrEnum):
    SUPERSEDED = "SUPERSEDED"
    SERVICE_DELETED = "SERVICE_DELETED"


class DeployRequestErrorCode(StrEnum):
    """배포 요청 실패 이유. DB CHECK 를 걸지 않고 앱이 이 값만 쓴다."""

    REPO_NOT_ACCESSIBLE = "REPO_NOT_ACCESSIBLE"
    REF_NOT_FOUND = "REF_NOT_FOUND"
    SOURCE_PROVIDER_UNAVAILABLE = "SOURCE_PROVIDER_UNAVAILABLE"
    BUILD_FAILED = "BUILD_FAILED"
    BUILD_TIMEOUT = "BUILD_TIMEOUT"
    DEPLOY_FAILED = "DEPLOY_FAILED"
    DEPLOY_TIMEOUT = "DEPLOY_TIMEOUT"
    HEALTH_CHECK_FAILED = "HEALTH_CHECK_FAILED"
    HEALTH_CHECK_TIMEOUT = "HEALTH_CHECK_TIMEOUT"
    PUBLIC_URL_UNREACHABLE = "PUBLIC_URL_UNREACHABLE"
    WORKER_LOST = "WORKER_LOST"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class RevertStatus(StrEnum):
    """배포 기록이 생긴 뒤 실패했을 때 부른 되돌리기의 결과."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class RefType(StrEnum):
    BRANCH = "BRANCH"
    COMMIT = "COMMIT"


class HealthCheckResult(StrEnum):
    """헬스체크 한 번의 결과 종류(deploy_requests.last_health_result)."""

    OK = "OK"
    HTTP_ERROR = "HTTP_ERROR"
    UNREACHABLE = "UNREACHABLE"


class SimulatedFailureStage(StrEnum):
    """가짜 모드에서 요청 하나를 일부러 실패시킬 단계(simulate.failAt)."""

    BUILDING = "BUILDING"
    DEPLOYING = "DEPLOYING"
    VERIFYING = "VERIFYING"


class BuildStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in (BuildStatus.SUCCEEDED, BuildStatus.FAILED)


class JobKind(StrEnum):
    DEPLOY_REQUEST = "DEPLOY_REQUEST"
    SERVICE_TEARDOWN = "SERVICE_TEARDOWN"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ServiceEnvironmentRole(StrEnum):
    """연결 역할(응답에서 계산, 저장 안 함). SERVING: 비율 > 0, STANDBY: 비율 0 + 현재 배포 있음."""

    SERVING = "SERVING"
    STANDBY = "STANDBY"
    NONE = "NONE"


class VersionStatus(StrEnum):
    """예비 연결이 서비스 중 연결과 같은 이미지인지."""

    IN_SYNC = "IN_SYNC"
    OUT_OF_SYNC = "OUT_OF_SYNC"
