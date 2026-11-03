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
    # 이력에서 고른 이전 성공 요청의 이미지·설정으로 되돌리는 요청(새 빌드 없음).
    ROLLBACK = "ROLLBACK"


class DeployRequestCancelReason(StrEnum):
    SUPERSEDED = "SUPERSEDED"
    SERVICE_DELETED = "SERVICE_DELETED"
    # 되돌리기를 시작할 때 연결의 현재 배포가 접수 때와 달랐다(그사이 다른 배포가 성공).
    BASE_CHANGED = "BASE_CHANGED"


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


class LogLevel(StrEnum):
    """로그 한 줄의 수준(배포 작업 로그·빌드·실행 로그 공통)."""

    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


class LogSource(StrEnum):
    """로그 조회 대상. DEPLOY_JOB 은 DB 에 저장한 배포 작업 로그, 나머지는 실행기 즉석 조회."""

    DEPLOY_JOB = "DEPLOY_JOB"
    BUILD = "BUILD"
    RUNTIME = "RUNTIME"


class DeployLogStage(StrEnum):
    """배포 작업 로그 한 줄이 속한 단계. 요청 상태 값과 되돌리기(REVERT)."""

    QUEUED = "QUEUED"
    BUILDING = "BUILDING"
    DEPLOYING = "DEPLOYING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    REVERT = "REVERT"


class DeployLogCode(StrEnum):
    """배포 작업 로그 한 줄의 코드(deploy_request_logs.code). DB CHECK 없이 앱이 이 값만 쓴다."""

    REQUEST_ACCEPTED = "REQUEST_ACCEPTED"
    STARTED = "STARTED"
    BUILD_ATTACHED = "BUILD_ATTACHED"
    BUILD_REUSED = "BUILD_REUSED"
    DEPLOYMENT_CREATED = "DEPLOYMENT_CREATED"
    DEPLOY_REQUESTED = "DEPLOY_REQUESTED"
    DEPLOY_APPLIED = "DEPLOY_APPLIED"
    HEALTH_PROBE_OK = "HEALTH_PROBE_OK"
    HEALTH_PROBE_HTTP_ERROR = "HEALTH_PROBE_HTTP_ERROR"
    HEALTH_PROBE_UNREACHABLE = "HEALTH_PROBE_UNREACHABLE"
    SUCCEEDED = "SUCCEEDED"
    STANDBY_SYNC_REQUESTED = "STANDBY_SYNC_REQUESTED"
    FAILED = "FAILED"
    REVERT_SUCCEEDED = "REVERT_SUCCEEDED"
    REVERT_FAILED = "REVERT_FAILED"
    CANCELLED_SUPERSEDED = "CANCELLED_SUPERSEDED"
    CANCELLED_SERVICE_DELETED = "CANCELLED_SERVICE_DELETED"
    CANCELLED_BASE_CHANGED = "CANCELLED_BASE_CHANGED"
    LOG_TRUNCATED = "LOG_TRUNCATED"


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
    # 대상(subject_id)은 이전 계획 id 다.
    MIGRATION_PLAN = "MIGRATION_PLAN"


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


class OrganizationRole(StrEnum):
    """조직 멤버 역할. VIEWER < DEVELOPER < ADMIN. 에이전트 토큰은 역할 없이 읽기만 한다."""

    ADMIN = "ADMIN"
    DEVELOPER = "DEVELOPER"
    VIEWER = "VIEWER"

    @property
    def rank(self) -> int:
        return _ROLE_RANKS[self]

    def includes(self, required: "OrganizationRole") -> bool:
        """이 역할이 required 역할 이상인가."""
        return self.rank >= required.rank


_ROLE_RANKS = {
    OrganizationRole.VIEWER: 1,
    OrganizationRole.DEVELOPER: 2,
    OrganizationRole.ADMIN: 3,
}


class InvitationStatus(StrEnum):
    """초대 상태. PENDING 에서만 ACCEPTED·EXPIRED·REVOKED 로 바뀐다."""

    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class ActorType(StrEnum):
    """요청한 쪽. USER 는 세션 쿠키로 들어온 사람, AGENT 는 에이전트 토큰."""

    USER = "USER"
    AGENT = "AGENT"


class AuditResult(StrEnum):
    """거부 기록 결과. 이번에는 거부(403)만 남긴다."""

    DENIED = "DENIED"


# --- 이전 계획 (API 서버 8) ---


class MigrationPlanStatus(StrEnum):
    """이전 계획 상태. 승인이 곧 실행 시작이다. 끝 상태는 되감지 않는다."""

    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    RUNNING = "RUNNING"
    ROLLING_BACK = "ROLLING_BACK"
    SUCCEEDED = "SUCCEEDED"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    STALE = "STALE"

    @property
    def is_terminal(self) -> bool:
        return self not in OPEN_MIGRATION_PLAN_STATUSES


# 서비스당 하나만 있을 수 있는 끝나지 않은 상태(부분 unique).
OPEN_MIGRATION_PLAN_STATUSES = frozenset(
    {
        MigrationPlanStatus.PENDING_APPROVAL,
        MigrationPlanStatus.APPROVED,
        MigrationPlanStatus.RUNNING,
        MigrationPlanStatus.ROLLING_BACK,
    }
)
# 실행 중(승인 뒤): 이 동안 그 서비스의 배포 요청·되돌리기·서비스 삭제 접수를 막는다.
IN_PROGRESS_MIGRATION_PLAN_STATUSES = frozenset(
    {
        MigrationPlanStatus.APPROVED,
        MigrationPlanStatus.RUNNING,
        MigrationPlanStatus.ROLLING_BACK,
    }
)


class MigrationStepKind(StrEnum):
    """계획 단계 종류. 단계는 고정 5개(사전 확인 → 10% → 50% → 데이터 확인 → 100%)다."""

    PREFLIGHT = "PREFLIGHT"
    SHIFT = "SHIFT"
    DATA_SYNC_CHECK = "DATA_SYNC_CHECK"


class MigrationStepPhase(StrEnum):
    """지금 단계 안의 위치. 비율 단계는 APPLY → OBSERVE → JUDGE, 데이터 확인은 JUDGE 만."""

    APPLY = "APPLY"
    OBSERVE = "OBSERVE"
    JUDGE = "JUDGE"


class MigrationStepStatus(StrEnum):
    """단계 결과(step_results[i].status)."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class MigrationErrorCode(StrEnum):
    """이전 계획 실패·롤백 원인. 롤백 결과는 MigrationRollbackStatus 로 따로 둔다."""

    PREFLIGHT_FAILED = "PREFLIGHT_FAILED"
    TRAFFIC_APPLY_FAILED = "TRAFFIC_APPLY_FAILED"
    HEALTH_CHECK_FAILED = "HEALTH_CHECK_FAILED"
    METRICS_UNAVAILABLE = "METRICS_UNAVAILABLE"
    THRESHOLD_EXCEEDED = "THRESHOLD_EXCEEDED"
    DATA_LAG_EXCEEDED = "DATA_LAG_EXCEEDED"
    ABORTED = "ABORTED"
    WORKER_LOST = "WORKER_LOST"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    # 도착에 갔어야 할 만큼 출발에 요청이 왔는데 도착이 거의 받지 못했다(진입점이 비율을 안 따름).
    TARGET_NOT_RECEIVING = "TARGET_NOT_RECEIVING"


class MigrationRollbackStatus(StrEnum):
    """출발 100% 로 되돌린 결과. NOT_NEEDED 는 트래픽을 바꾼 적이 없어 되돌릴 필요가 없었다."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    NOT_NEEDED = "NOT_NEEDED"


class MigrationSimulateReason(StrEnum):
    """가짜 모드에서 단계 하나를 일부러 실패시킬(또는 트래픽 적음으로 넘길) 원인(simulate.reason).

    LATENCY 는 저장된 옛 계획을 읽으려고 값만 남긴다. 요청으로는 받지 않고(422) 처리기는 아무
    실패도 만들지 않는다(p95 는 판정에 쓰지 않는다).
    """

    ERROR_RATE = "ERROR_RATE"
    LATENCY = "LATENCY"
    HEALTH = "HEALTH"
    DATA_LAG = "DATA_LAG"
    TRAFFIC = "TRAFFIC"
    TARGET_NOT_RECEIVING = "TARGET_NOT_RECEIVING"
    LOW_TRAFFIC = "LOW_TRAFFIC"


class MigrationJudgeReason(StrEnum):
    """비율 단계가 건수 세기를 끝내고 판정(JUDGE)으로 넘어간 이유(step_results[i].judgeReason)."""

    # 도착이 필요 건수를 오류 허용 안에서 처리했다.
    COUNT_REACHED = "COUNT_REACHED"
    # 최대 대기가 끝날 때까지 건수를 못 채웠지만 서비스 전체가 조용했다(헬스체크로 통과).
    LOW_TRAFFIC = "LOW_TRAFFIC"


class MigrationLogCode(StrEnum):
    """이전 실행 로그 한 줄의 코드(migration_plan_logs.code). DB CHECK 없이 앱이 이 값만 쓴다."""

    PLAN_CREATED = "PLAN_CREATED"
    APPROVED = "APPROVED"
    SELF_APPROVED = "SELF_APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    STALE = "STALE"
    ABORT_REQUESTED = "ABORT_REQUESTED"
    STARTED = "STARTED"
    PREFLIGHT_OK = "PREFLIGHT_OK"
    PREFLIGHT_FAILED = "PREFLIGHT_FAILED"
    WEIGHT_APPLIED = "WEIGHT_APPLIED"
    WEIGHT_APPLY_FAILED = "WEIGHT_APPLY_FAILED"
    JUDGE_PASSED = "JUDGE_PASSED"
    JUDGE_FAILED = "JUDGE_FAILED"
    DATA_SYNC_OK = "DATA_SYNC_OK"
    DATA_SYNC_FAILED = "DATA_SYNC_FAILED"
    SUCCEEDED = "SUCCEEDED"
    ROLLBACK_STARTED = "ROLLBACK_STARTED"
    ROLLBACK_APPLIED = "ROLLBACK_APPLIED"
    ROLLBACK_APPLY_FAILED = "ROLLBACK_APPLY_FAILED"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    FAILED = "FAILED"
    COUNTERS_RESET = "COUNTERS_RESET"
    LOG_TRUNCATED = "LOG_TRUNCATED"
