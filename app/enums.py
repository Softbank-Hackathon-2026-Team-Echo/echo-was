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
    # 실제 실행기(API 서버 3-2). 권한·설정 오류를 시간 초과로 뭉개지 않으려고 나눈다.
    BUILD_DISPATCH_FAILED = "BUILD_DISPATCH_FAILED"
    CLUSTER_UNAUTHORIZED = "CLUSTER_UNAUTHORIZED"
    IMAGE_PULL_FAILED = "IMAGE_PULL_FAILED"
    CONTAINER_START_FAILED = "CONTAINER_START_FAILED"
    # 앱 환경 변수(API 서버 10): 배포 기록에 고정한 버전을 보관함에서 영구히 꺼내지 못함.
    ENV_SECRET_UNAVAILABLE = "ENV_SECRET_UNAVAILABLE"


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
    ENV_VARS_APPLIED = "ENV_VARS_APPLIED"
    DEPLOY_APPLIED = "DEPLOY_APPLIED"
    HEALTH_PROBE_OK = "HEALTH_PROBE_OK"
    HEALTH_PROBE_HTTP_ERROR = "HEALTH_PROBE_HTTP_ERROR"
    HEALTH_PROBE_UNREACHABLE = "HEALTH_PROBE_UNREACHABLE"
    SUCCEEDED = "SUCCEEDED"
    STANDBY_SYNC_REQUESTED = "STANDBY_SYNC_REQUESTED"
    # 배포 실패 진단(API 서버 4-1): 컨테이너 상태 1줄 + 로그 마지막 줄(최대 50, 줄마다 1행).
    CONTAINER_DIAGNOSTICS = "CONTAINER_DIAGNOSTICS"
    CONTAINER_LOG = "CONTAINER_LOG"
    FAILED = "FAILED"
    REVERT_SUCCEEDED = "REVERT_SUCCEEDED"
    # 되돌릴 이전 배포가 없어(첫 배포 실패) 실패한 버전만 거뒀다. 실행 중인 버전 없음.
    REVERT_SUCCEEDED_NO_PREVIOUS = "REVERT_SUCCEEDED_NO_PREVIOUS"
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
    """누가 했나. USER 는 세션 쿠키로 들어온 사람, AGENT 는 에이전트 토큰, SYSTEM 은 서버 자신."""

    USER = "USER"
    AGENT = "AGENT"
    SYSTEM = "SYSTEM"


class AuditResult(StrEnum):
    """감사 줄의 결과. DENIED 는 권한이 없어 막힌 요청(403)이다."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
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


class MigrationPlanMode(StrEnum):
    """이전 계획 종류. 서버가 만들 때 한 번 판정해 저장한다(클라이언트가 정하지 않는다).

    FAILBACK 은 같은 서비스의 가장 늦게 끝난 성공 계획을 출발·도착만 바꿔 거꾸로 하는 계획이다(그
    계획이 FAILBACK 이면 아니다). EMERGENCY 는 재난 트리거(API 서버 9)만 만든다.
    """

    PLANNED = "PLANNED"
    EMERGENCY = "EMERGENCY"
    FAILBACK = "FAILBACK"


# --- 재난 (API 서버 9) ---


class DisasterType(StrEnum):
    """재난 종류. 지진은 규모 기준이 있고, 태풍은 규모와 상관없이 대상이다."""

    EARTHQUAKE = "EARTHQUAKE"
    TYPHOON = "TYPHOON"


class DisasterSource(StrEnum):
    """재난 정보 출처. 지금은 사람이 버튼으로 낸 시뮬레이션뿐이다."""

    SIMULATED = "SIMULATED"


class DisasterStatus(StrEnum):
    """ACTIVE 이고 기준을 넘은 재난의 피해 환경은 이전 도착으로 고를 수 없다(복귀 차단)."""

    ACTIVE = "ACTIVE"
    ENDED = "ENDED"


class DisasterPlanOutcome(StrEnum):
    """재난 응답 plans[] 한 줄의 요약. 실제 상태는 planStatus 와 계획 API 로 본다."""

    APPROVED = "APPROVED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    SKIPPED = "SKIPPED"


class DisasterPlanReason(StrEnum):
    """plans[].reason 중 재난 트리거만 쓰는 값. 그 밖은 계획 API 오류 코드를 그대로 쓴다."""

    NO_SAFE_TARGET = "NO_SAFE_TARGET"
    SELF_APPROVAL_DISABLED = "SELF_APPROVAL_DISABLED"
    SOURCE_NOT_AFFECTED = "SOURCE_NOT_AFFECTED"


class CutoverKind(StrEnum):
    """인프라 스크립트가 고객 DB 의 `echo_ops.cutover_runs` 에 남긴 결과의 종류(원본 표기 그대로).

    `cutover.sh` 는 CUTOVER, `rollback.sh` 는 ROLLBACK 을 쓴다. 복귀(FAILBACK) 계획은 ROLLBACK,
    그 밖의 계획은 CUTOVER 를 찾는다.
    """

    CUTOVER = "CUTOVER"
    ROLLBACK = "ROLLBACK"


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
    # 실행 시작 때 보안 게이트가 막았다(필수 통제 FAIL·UNKNOWN, 유효한 예외 없음). 트래픽은 그대로.
    SECURITY_GATE_BLOCKED = "SECURITY_GATE_BLOCKED"


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
    # 보안 게이트 판정(승인 때·실행 시작 때). data 에 정책 버전·막은 통제·증거·예외 ID.
    SECURITY_GATE_PASSED = "SECURITY_GATE_PASSED"
    SECURITY_GATE_BLOCKED = "SECURITY_GATE_BLOCKED"
    # 재난 트리거가 만든 계획의 첫 줄(PLAN_CREATED 앞). data 에 재난 id·규모·출발 거리.
    DISASTER_TRIGGERED = "DISASTER_TRIGGERED"


# --- 보안 판정 (보안·거버넌스 3) ---


class SecurityControlId(StrEnum):
    """보안 통제 ID(보안·거버넌스 1 SPEC §5.1). 문서·화면과 같은 표기로 저장한다."""

    IMG_1 = "C-IMG-1"
    IMG_2 = "C-IMG-2"
    IMG_3 = "C-IMG-3"
    CFG_1 = "C-CFG-1"
    TLS_1 = "C-TLS-1"
    TLS_2 = "C-TLS-2"
    NET_1 = "C-NET-1"
    NET_2 = "C-NET-2"
    IAM_1 = "C-IAM-1"
    IAM_2 = "C-IAM-2"
    SEC_1 = "C-SEC-1"
    AUD_1 = "C-AUD-1"
    AUD_2 = "C-AUD-2"
    CI_1 = "C-CI-1"
    AI_1 = "C-AI-1"
    DB_1 = "C-DB-1"
    DST_1 = "C-DST-1"
    POST_1 = "C-POST-1"


class SecurityResult(StrEnum):
    """증거·통제·매트릭스 칸의 결과. 확인하지 못한 것(UNKNOWN)과 목업은 PASS 가 아니다."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    # 그 환경에 해당하지 않는 통제(예: 온프레의 클라우드 감사 로그).
    NA = "NA"


class SecurityFindingMode(StrEnum):
    """증거를 어떻게 얻었나. SIMULATED(가짜 실행기·목업) 증거는 판정에 쓰지 않는다."""

    REAL = "REAL"
    CACHE = "CACHE"
    SIMULATED = "SIMULATED"


class SecurityFindingSource(StrEnum):
    """증거를 만든 도구·경로. 통제마다 받을 수 있는 출처가 규칙표에 정해져 있다."""

    TRIVY_IMAGE = "TRIVY_IMAGE"
    TRIVY_CONFIG = "TRIVY_CONFIG"
    TRIVY_SECRET = "TRIVY_SECRET"
    GITLEAKS = "GITLEAKS"
    TLS_PROBE = "TLS_PROBE"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    WAS_TEST = "WAS_TEST"
    WAS_STATE = "WAS_STATE"
    AUDIT_LOG = "AUDIT_LOG"
    PRECHECK = "PRECHECK"
    ENTRYPOINT = "ENTRYPOINT"
    DATA_ENGINE = "DATA_ENGINE"
    CLOUD_API = "CLOUD_API"
    KUBERNETES_API = "KUBERNETES_API"
    GITHUB_API = "GITHUB_API"
    # AI 에이전트가 낸 결과. PASS 를 만들 수 없고, 사람이 확인하기 전에는 WARN 으로만 쓴다.
    AI_REVIEW = "AI_REVIEW"


class AiFindingConfirmation(StrEnum):
    """AI 결과를 사람이 확인한 상태. ESCALATED 여야 차단 사유가 된다."""

    UNCONFIRMED = "UNCONFIRMED"
    ESCALATED = "ESCALATED"
    DISMISSED = "DISMISSED"


class SecurityExceptionStatus(StrEnum):
    """기한부 예외의 상태(응답용, 저장하지 않고 승인·철회·만료 칸과 지금 시각으로 정한다).

    REVOKED > EXPIRED > ACTIVE(승인됨) > PENDING(승인 대기) 순서로 본다. 판정에는 ACTIVE 만 쓴다.
    """

    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class SecurityCheckPhase(StrEnum):
    """판정 시점. 준비(배포·설정 변경·주기 수집) · 실행(이전 승인·일꾼 시작) · 전환 직후."""

    PREPARATION = "PREPARATION"
    EXECUTION = "EXECUTION"
    POST_SWITCH = "POST_SWITCH"


class SecurityMatrixCell(StrEnum):
    """보안 매트릭스 4칸(API 키 iam·encryption·auditLog·policy)."""

    IAM = "IAM"
    ENCRYPTION = "ENCRYPTION"
    AUDIT_LOG = "AUDIT_LOG"
    POLICY = "POLICY"


class SecurityGateStatus(StrEnum):
    """매트릭스의 게이트 관점(저장 안 함): 게이트가 이 통제·칸을 막는가."""

    PASS = "PASS"
    BLOCKED = "BLOCKED"
    # 증거는 FAIL·UNKNOWN 이지만 유효한 기한부 예외가 걸려 막지 않는다.
    WAIVED = "WAIVED"


class SecurityCompareChange(StrEnum):
    """이전 전후 비교에서 도착 칸이 출발 칸보다 어떤가(저장 안 함, 보안 5-2).

    비교 열쇠는 결과 심각도(NA < PASS < WARN < UNKNOWN < FAIL)이고, 같으면 게이트 관점
    (PASS < WAIVED < BLOCKED)으로 가른다.
    """

    WORSE = "WORSE"
    SAME = "SAME"
    BETTER = "BETTER"


class SecurityReasonCode(StrEnum):
    """통제 판정 사유(기계 판독용). 게이트가 막으면 이 값을 통제 ID 와 함께 돌려준다."""

    CONTROL_FAILED = "CONTROL_FAILED"
    CONTROL_WARNING = "CONTROL_WARNING"
    # 수집기가 확인하지 못했다고 적은 증거(시간 초과·권한 부족 등).
    EVIDENCE_UNKNOWN = "EVIDENCE_UNKNOWN"
    EVIDENCE_MISSING = "EVIDENCE_MISSING"
    EVIDENCE_EXPIRED = "EVIDENCE_EXPIRED"
    EVIDENCE_SIMULATED = "EVIDENCE_SIMULATED"
    # 사람 점검 기록에 점검자·근거 링크가 없다.
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"
    SOURCE_NOT_ALLOWED = "SOURCE_NOT_ALLOWED"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    # 증거를 결과로 바꾼 규칙표 버전이 지금과 다르다(다시 정규화해야 한다).
    POLICY_VERSION_MISMATCH = "POLICY_VERSION_MISMATCH"
    PRECHECK_MISSING = "PRECHECK_MISSING"
    AI_UNCONFIRMED = "AI_UNCONFIRMED"
    AI_ESCALATED = "AI_ESCALATED"
    EXCEPTION_APPLIED = "EXCEPTION_APPLIED"
    EXCEPTION_EXPIRED = "EXCEPTION_EXPIRED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class SecurityFindingReason(StrEnum):
    """검사 원문을 증거로 바꾸며 정한 이유(보안·거버넌스 2). 증거 한 건에 하나 붙는다."""

    NO_ISSUES = "NO_ISSUES"
    # 이미지 취약점: HIGH 이상이고 수정 버전이 있다(FAIL).
    FIXABLE_HIGH_VULNERABILITY = "FIXABLE_HIGH_VULNERABILITY"
    # 이미지 취약점: 수정 버전이 없는 HIGH 이상 또는 MEDIUM 이하만 있다(WARN).
    VULNERABILITY_WARNING = "VULNERABILITY_WARNING"
    SECRET_FOUND = "SECRET_FOUND"
    MISCONFIGURATION_HIGH = "MISCONFIGURATION_HIGH"
    MISCONFIGURATION_WARNING = "MISCONFIGURATION_WARNING"
    # 원문이 기대한 대상(digest·커밋)의 것이 아니다(UNKNOWN).
    ARTIFACT_MISMATCH = "ARTIFACT_MISMATCH"
    # 원문 모양이 기대와 다르다(UNKNOWN).
    REPORT_INVALID = "REPORT_INVALID"
    TLS_OK = "TLS_OK"
    TLS_CERT_INVALID = "TLS_CERT_INVALID"
    TLS_HOSTNAME_MISMATCH = "TLS_HOSTNAME_MISMATCH"
    TLS_EXPIRES_SOON = "TLS_EXPIRES_SOON"
    TLS_VERSION_TOO_LOW = "TLS_VERSION_TOO_LOW"
    TLS_HANDSHAKE_FAILED = "TLS_HANDSHAKE_FAILED"
    # 시간 초과·연결 불가는 확인하지 못한 것이다(UNKNOWN).
    TLS_TIMEOUT = "TLS_TIMEOUT"
    TLS_UNREACHABLE = "TLS_UNREACHABLE"
    # 접속하지 않은 주소. DNS 결과가 모두 사설 대역이면 공개 경로가 아니다(NA, 대표 주소 시험이
    # 공개 경로를 본다). 루프백·링크 로컬·예약 대역이나 공인·사설이 섞이면 의심 설정(UNKNOWN).
    # Echo 가 만든 주소(공개 도메인 아래)가 아니면 접속하지 않는다(UNKNOWN).
    TLS_HOST_NOT_PUBLIC = "TLS_HOST_NOT_PUBLIC"
    TLS_HOST_SUSPICIOUS = "TLS_HOST_SUSPICIOUS"
    TLS_HOST_NOT_ALLOWED = "TLS_HOST_NOT_ALLOWED"
    # 출발·도착 이미지 일치(C-IMG-3, WAS 상태). 한쪽에 현재 배포가 없어도 불일치다.
    IMAGE_MATCH = "IMAGE_MATCH"
    IMAGE_MISMATCH = "IMAGE_MISMATCH"
    # 비밀 전달 경로(C-SEC-1, WAS 상태). 기준 연결의 앱 환경 변수 키가 대상 연결에 모두 있다
    # (기준에 키가 없어도 PASS) · 빠진 키가 있다(FAIL, 근거 줄은 키 이름만).
    SECRETS_READY = "SECRETS_READY"
    SECRETS_MISSING = "SECRETS_MISSING"
    # 사람 점검 기록. 결과는 점검자가 정한 값(result)이다.
    MANUAL_REVIEW_RECORDED = "MANUAL_REVIEW_RECORDED"


# --- AI 에이전트 실행 (WAS ↔ 러너 계약 v1) ---
# 원본: echo-ai-agent specs/ai-1-appendix-a-was-runner-contract.md


class AgentRunKind(StrEnum):
    """AI 실행 종류. CHANGE_PROPOSAL 은 실패한 배포의 AI 진단 + 수정안(SPEC api-98)."""

    PRECHECK = "PRECHECK"
    APPLY_PATCH = "APPLY_PATCH"
    CHANGE_PROPOSAL = "CHANGE_PROPOSAL"


class AgentRunStatus(StrEnum):
    """QUEUED → CLAIMED → RUNNING → SUCCEEDED·FAILED·TIMED_OUT·CANCELLED. 자동 재시도 없음."""

    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


class AgentRunStep(StrEnum):
    """러너 진행 이벤트의 단계(러너 `RunStep` 과 같은 값)."""

    SANDBOX_CREATE = "SANDBOX_CREATE"
    INPUT_LOAD = "INPUT_LOAD"
    CONFIG_DIFF = "CONFIG_DIFF"
    ANALYZE = "ANALYZE"
    PATCH_PROPOSE = "PATCH_PROPOSE"
    PATCH_VERIFY = "PATCH_VERIFY"
    SANDBOX_DELETE = "SANDBOX_DELETE"


class AgentPatchDecision(StrEnum):
    """AI 패치에 대한 사람의 결정. BLOCKED 패치는 승인할 수 없다(API 가 막는다)."""

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class AgentRunOrigin(StrEnum):
    """러너 결과 한 줄의 출처. RULE 은 고정 규칙 비교, AI 는 모델 판단이다."""

    RULE = "RULE"
    AI = "AI"


class AgentRunSeverity(StrEnum):
    """설정 차이·기능 차이의 심각도."""

    INFO = "INFO"
    WARN = "WARN"
    FAIL = "FAIL"


class AgentPatchStatus(StrEnum):
    """AI 수정안 상태. 러너 값을 믿지 않고 WAS 가 계산한다.

    결과 JSON 에는 검사 결과로 다시 계산한 PROPOSED·BLOCKED 만 저장한다. 나머지는 결정 행과 반영
    실행을 더해 응답 때 계산한다(compute_patch_status).
    """

    PROPOSED = "PROPOSED"
    BLOCKED = "BLOCKED"
    REJECTED = "REJECTED"
    APPROVED = "APPROVED"
    APPLIED = "APPLIED"
    APPLY_FAILED = "APPLY_FAILED"


class AgentPatchBlockReason(StrEnum):
    """수정안이 BLOCKED 인 이유."""

    PROTECTED_FILES_TOUCHED = "PROTECTED_FILES_TOUCHED"
    CHECKS_MISSING = "CHECKS_MISSING"
    CHECK_NOT_PASSED = "CHECK_NOT_PASSED"


class DiagnosisResult(StrEnum):
    """배포 실패 진단(CHANGE_PROPOSAL)의 결론(SPEC api-98)."""

    DIAGNOSED = "DIAGNOSED"  # 원인을 찾았다.
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"  # 근거가 모자라 단정하지 못했다.
    NO_FAILURE_EVIDENCE = "NO_FAILURE_EVIDENCE"  # 실패 흔적(로그·종료 코드)이 없었다.


class DiagnosisCategory(StrEnum):
    """진단한 원인의 분류."""

    CODE = "CODE"
    CONFIG = "CONFIG"
    BUILD = "BUILD"
    RUNTIME = "RUNTIME"
    PLATFORM = "PLATFORM"


class DiagnosisEvidenceSource(StrEnum):
    """진단 근거 한 줄의 출처. LOG 는 실패 로그 줄, FILE 은 저장소 파일 줄, CONFIG 는 설정 값."""

    LOG = "LOG"
    FILE = "FILE"
    CONFIG = "CONFIG"


class AgentRunFailureCode(StrEnum):
    """실행 실패 이유 중 약속한 값. 러너는 그 밖의 코드도 낼 수 있어 DB 칸은 자유 문자열이다."""

    DEADLINE = "DEADLINE"  # 러너가 마감 전에 끝내지 못해 스스로 실패로 냈다.
    LEASE_EXPIRED = "LEASE_EXPIRED"  # WAS: 러너의 임대가 끊겼다.
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"  # WAS: 마감이 지났다.


# --- AI 실행 데이터 레지던시 (AI 3) ---


class AgentRunModelProvider(StrEnum):
    """러너가 밝힌 모델 제공자. NONE 은 모델을 부르지 않았다는 뜻이다."""

    NONE = "NONE"
    LOCAL = "LOCAL"
    GOOGLE_VERTEX = "GOOGLE_VERTEX"
    AWS_BEDROCK = "AWS_BEDROCK"
    OPENAI = "OPENAI"
    ANTHROPIC = "ANTHROPIC"


class AgentRunDataClass(StrEnum):
    """모델에 넘긴 자료 종류. 변수 이름·digest·규칙 결과는 CONFIG 에 넣는다. 저장은 이 순서다."""

    CONFIG = "CONFIG"
    FINDINGS = "FINDINGS"
    SOURCE_CODE = "SOURCE_CODE"
    LOGS = "LOGS"
    SECRETS = "SECRETS"


class AiResidencyPolicySource(StrEnum):
    """요약 판정에 쓴 정책의 출처. 지금은 서버 코드 상수뿐이다."""

    DEFAULT = "DEFAULT"


class AiResidencyRegionlessCondition(StrEnum):
    """리전 없이(modelRegion null) 허용하는 제공자의 조건(AD-12).

    NO_FORBIDDEN_CLASSES: dataClasses 가 정책 금지 등급(forbidExternalForClasses)과
    겹치지 않을 때만.
    """

    NO_FORBIDDEN_CLASSES = "NO_FORBIDDEN_CLASSES"


class AiResidencyVerdict(StrEnum):
    """민감 등급 외부 전송 판정. 기록이 없는 실행이 있으면 0건이라고 단정하지 않는다."""

    ZERO = "ZERO"
    ZERO_WITH_UNRECORDED = "ZERO_WITH_UNRECORDED"
    DETECTED = "DETECTED"


class AiResidencyViolationReason(StrEnum):
    """기본 정책 위반 이유. 한 실행에 여러 개가 붙을 수 있다."""

    MODEL_REGION_NOT_ALLOWED = "MODEL_REGION_NOT_ALLOWED"
    FORBIDDEN_CLASS_SENT_EXTERNALLY = "FORBIDDEN_CLASS_SENT_EXTERNALLY"


class CostComponentKind(StrEnum):
    """비용 자원 항목의 묶음(운영 1)."""

    COMPUTE = "COMPUTE"
    DATABASE = "DATABASE"
    NETWORK = "NETWORK"
    STORAGE = "STORAGE"
    OTHER = "OTHER"


class CostSource(StrEnum):
    """비용 값의 출처. ESTIMATE = 사용량 × 단가 추정, BILLING = 청구 실측(운영 1)."""

    ESTIMATE = "ESTIMATE"
    BILLING = "BILLING"


class ServiceMetricsStatus(StrEnum):
    """서비스 대시보드 지표를 Prometheus 에서 읽었는가(운영 2).

    OK = 읽음(데이터가 없으면 points 가 빔), UNAVAILABLE = 읽지 못함(미응답·인증·형식),
    NOT_CONFIGURED = Prometheus 주소 설정이 없음(외부 호출 없음).
    """

    OK = "OK"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_CONFIGURED = "NOT_CONFIGURED"
