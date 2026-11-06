"""도메인 예외 계층: AppError → 카테고리 → 도메인 예외.

- code: 클라이언트가 분기하는 고정 코드(계약). 한 번 정하면 바꾸지 않는다.
- status_code: API 가 응답할 HTTP 상태.
- retryable: 같은 작업을 다시 시도해도 되는 실패인지. 배포 실행기가 재시도 여부를 가를 때 쓴다.
- fields: 로그에 남길 식별자. 메시지에 넣지 않고 여기에 담는다. 입력 값·비밀값은 넣지 않는다.
- details: 응답 `details` 로 내보낼 칸별 이유(선택). 칸 경로와 고정 이유 코드만 담는다.
  입력 값은 넣지 않는다.

raise 는 Service·Repository·Client·Model 이벤트에서 하고,
HTTP 변환은 exception_handlers 한곳에서 한다.
도메인 예외는 클라이언트 분기나 재시도 정책이 다를 때만 카테고리를 상속해 만든다.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class ErrorField:
    """응답 details 한 칸. field 는 camelCase 경로, reason 은 고정 코드다.

    value 는 클라이언트가 그대로 쓸 안내 값(예: GitHub App 설치 주소)이 있을 때만 담는다.
    입력 값·비밀값은 넣지 않는다.
    """

    field: str
    reason: str
    value: str | None = None


class AppError(Exception):
    code: ClassVar[str] = "INTERNAL_ERROR"
    status_code: ClassVar[int] = 500
    retryable: ClassVar[bool] = False
    # 응답에 실을 헤더(예: 401 의 쿠키 삭제 Set-Cookie). 하위 클래스가 필요할 때만 채운다.
    headers: Mapping[str, str] | None = None

    def __init__(
        self,
        message: str | None = None,
        *,
        details: Sequence[ErrorField] | None = None,
        **fields: object,
    ) -> None:
        self.message = message or self.code
        self.details = list(details) if details else None
        self.fields = fields
        super().__init__(self.message)


class NotFoundError(AppError):
    code = "NOT_FOUND"
    status_code = 404


class InvalidInputError(AppError):
    """도메인 규칙상 받을 수 없는 입력. 스키마 검증 실패(VALIDATION_ERROR)와 구분한다."""

    code = "INVALID_INPUT"
    status_code = 422


class ConflictError(AppError):
    code = "CONFLICT"
    status_code = 409


class UnauthorizedError(AppError):
    code = "UNAUTHORIZED"
    status_code = 401


class ForbiddenError(AppError):
    code = "FORBIDDEN"
    status_code = 403


class ExternalError(AppError):
    """외부 시스템(클라우드 API·클러스터·비밀 저장소) 호출 실패.

    Client 가 SDK 예외를 이것으로 바꾼다.
    """

    code = "EXTERNAL_ERROR"
    status_code = 502
    retryable = True


class EnvironmentNotFoundError(NotFoundError):
    code = "ENVIRONMENT_NOT_FOUND"


class EnvironmentNameConflictError(ConflictError):
    code = "ENVIRONMENT_NAME_CONFLICT"


class EnvironmentInUseError(ConflictError):
    """살아 있는 서비스-환경 연결이 있어 지울 수 없다."""

    code = "ENVIRONMENT_IN_USE"


class EnvironmentKindImmutableError(InvalidInputError):
    """환경 종류는 등록 뒤 바꿀 수 없다(배포 기록이 다른 종류로 바뀌는 것을 막는다)."""

    code = "ENVIRONMENT_KIND_IMMUTABLE"


class ServiceNotFoundError(NotFoundError):
    code = "SERVICE_NOT_FOUND"


class ServiceNameConflictError(ConflictError):
    code = "SERVICE_NAME_CONFLICT"


class ServiceDeletingError(ConflictError):
    """삭제 중인 서비스는 수정·배포 요청을 받지 않는다."""

    code = "SERVICE_DELETING"


class ServiceNameImmutableError(InvalidInputError):
    """서비스 이름은 바꿀 수 없다(퍼블릭 주소가 이름에서 나온다)."""

    code = "SERVICE_NAME_IMMUTABLE"


class PublicHostConflictError(ConflictError):
    """같은 퍼블릭 주소를 가진 살아 있는 서비스·연결이 이미 있다."""

    code = "PUBLIC_HOST_CONFLICT"


class EnvironmentUnhealthyError(ConflictError):
    """마지막 접속 확인이 실패한 환경에는 배포 요청을 받지 않는다."""

    code = "ENVIRONMENT_UNHEALTHY"


class DeployRequestNotFoundError(NotFoundError):
    code = "DEPLOY_REQUEST_NOT_FOUND"


class NotRollbackableError(ConflictError):
    """되돌리기 대상이 아니다: 성공하지 않았거나 배포 기록이 없거나 그 연결이 없다."""

    code = "NOT_ROLLBACKABLE"


class AlreadyCurrentError(ConflictError):
    """되돌리기 대상이 만든 배포 기록이 이미 그 연결의 현재 배포다."""

    code = "ALREADY_CURRENT"


class SimulateNotAllowedError(InvalidInputError):
    """simulate 는 가짜 실행기 모드에서만 받는다."""

    code = "SIMULATE_NOT_ALLOWED"


class RepoNotAccessibleError(NotFoundError):
    """저장소가 없거나, 비공개인데 GitHub App 이 설치되지 않았다(GitHub 가 둘을 구분해 주지 않는다).

    응답 details 에 App 설치 안내 주소(installUrl)를 담는다.
    """

    code = "REPO_NOT_ACCESSIBLE"

    def __init__(self, install_url: str | None) -> None:
        details = (
            [ErrorField(field="installUrl", reason="INSTALL_GITHUB_APP", value=install_url)]
            if install_url
            else None
        )
        super().__init__("repository not found or not accessible", details=details)


class RefNotFoundError(InvalidInputError):
    """브랜치·커밋을 저장소에서 찾지 못했다."""

    code = "REF_NOT_FOUND"

    def __init__(self) -> None:
        super().__init__("branch or commit not found")


class SourceProviderUnavailableError(ExternalError):
    """소스 저장소 제공자(GitHub) 한도 초과·5xx·시간 초과."""

    code = "SOURCE_PROVIDER_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("source provider unavailable")


# --- 인증·조직·역할 (API 서버 6) ---


class UnauthenticatedError(UnauthorizedError):
    """로그인하지 않았거나 세션·토큰이 만료·폐기됐다. 쿠키가 있었으면 지우는 헤더를 싣는다."""

    code = "UNAUTHENTICATED"

    def __init__(self, headers: Mapping[str, str] | None = None) -> None:
        super().__init__("authentication required")
        self.headers = headers


class AuditedForbiddenError(ForbiddenError):
    """거부 기록(audit_logs)을 남기는 403. 예외 처리기가 기록을 따로 커밋한 뒤 응답한다."""


class RoleForbiddenError(AuditedForbiddenError):
    code = "ROLE_FORBIDDEN"

    def __init__(self) -> None:
        super().__init__("role does not allow this action")


class AgentReadOnlyError(AuditedForbiddenError):
    """에이전트 토큰은 자기 조직을 읽기만 한다. 사람만 쓰는 경로도 이 오류다."""

    code = "AGENT_READ_ONLY"

    def __init__(self) -> None:
        super().__init__("agent tokens are read-only")


class OriginNotAllowedError(AuditedForbiddenError):
    """쿠키로 인증한 변경 요청의 Origin 이 허용 목록에 없다."""

    code = "ORIGIN_NOT_ALLOWED"

    def __init__(self) -> None:
        super().__init__("origin not allowed")


class CannotChangeOwnRoleError(AuditedForbiddenError):
    code = "CANNOT_CHANGE_OWN_ROLE"

    def __init__(self) -> None:
        super().__init__("cannot change own role")


class OrganizationNotFoundError(NotFoundError):
    """조직이 없거나 요청한 쪽이 멤버가 아니다(둘을 구분하지 않는다)."""

    code = "ORGANIZATION_NOT_FOUND"

    def __init__(self) -> None:
        super().__init__("organization not found")


class MemberNotFoundError(NotFoundError):
    code = "MEMBER_NOT_FOUND"


class InvitationNotFoundError(NotFoundError):
    code = "INVITATION_NOT_FOUND"


class AgentTokenNotFoundError(NotFoundError):
    code = "AGENT_TOKEN_NOT_FOUND"


class OrganizationSlugConflictError(ConflictError):
    code = "ORGANIZATION_SLUG_CONFLICT"

    def __init__(self) -> None:
        super().__init__("organization slug already in use")


class OrganizationSlugReservedError(ConflictError):
    code = "ORGANIZATION_SLUG_RESERVED"

    def __init__(self) -> None:
        super().__init__("organization slug is reserved")


class AlreadyMemberError(ConflictError):
    code = "ALREADY_MEMBER"


class LastAdminError(ConflictError):
    """변경 뒤 조직에 어드민이 0명이 된다."""

    code = "LAST_ADMIN"

    def __init__(self) -> None:
        super().__init__("organization must keep at least one admin")


class InvitationNotPendingError(ConflictError):
    code = "INVITATION_NOT_PENDING"


class GithubUserNotFoundError(InvalidInputError):
    code = "GITHUB_USER_NOT_FOUND"

    def __init__(self) -> None:
        super().__init__(
            "github user not found",
            details=[ErrorField(field="githubLogin", reason="GITHUB_USER_NOT_FOUND")],
        )


class SecretRefNotInOrganizationError(InvalidInputError):
    """env 비밀 참조가 이 조직의 열쇠 이름(ECHO_SECRET_<조직 키>__)으로 시작하지 않는다."""

    code = "SECRET_REF_NOT_IN_ORGANIZATION"

    def __init__(self) -> None:
        super().__init__(
            "secret reference does not belong to this organization",
            details=[ErrorField(field="secretRef", reason="SECRET_REF_NOT_IN_ORGANIZATION")],
        )


class RequestFieldInvalidError(InvalidInputError):
    """스키마 밖(설정 값 등)에서 정한 범위를 넘은 요청 칸. 스키마 검증과 같은 코드로 응답한다."""

    code = "VALIDATION_ERROR"

    def __init__(self, field: str, reason: str) -> None:
        super().__init__("request validation failed", details=[ErrorField(field, reason)])


class RequestFieldsInvalidError(InvalidInputError):
    """스키마 밖 규칙(예: 통제가 정한 범위 칸)에 맞지 않는 요청 칸 여러 개.

    스키마 검증과 같은 코드(VALIDATION_ERROR)로 응답한다.
    """

    code = "VALIDATION_ERROR"

    def __init__(self, details: Sequence[ErrorField]) -> None:
        super().__init__("request validation failed", details=details)


class GithubUnavailableError(ExternalError):
    """GitHub 사용자 API 장애·시간 초과·한도 초과(403·429)."""

    code = "GITHUB_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("github unavailable")


class GithubCodeRejectedError(ExternalError):
    """GitHub 가 로그인 코드를 받지 않았다(만료·재사용). 로그인 콜백은 OAUTH_DENIED 로 보낸다."""

    code = "GITHUB_CODE_REJECTED"
    retryable = False

    def __init__(self) -> None:
        super().__init__("github rejected the login code")


class LoginNotConfiguredError(AppError):
    """GitHub 로그인 설정(client id·secret 참조·콜백 주소·프론트 주소)이 비어 있다."""

    code = "LOGIN_NOT_CONFIGURED"
    status_code = 503

    def __init__(self) -> None:
        super().__init__("github login is not configured")


# --- 이전 계획 (API 서버 8) ---


class MigrationPlanNotFoundError(NotFoundError):
    code = "MIGRATION_PLAN_NOT_FOUND"


class SelfApprovalForbiddenError(AuditedForbiddenError):
    """계획을 만든 사람이 스스로 승인·거절하려 했고 조직이 자기 승인을 허용하지 않는다."""

    code = "SELF_APPROVAL_FORBIDDEN"

    def __init__(self) -> None:
        super().__init__("the plan creator cannot approve or reject it in this organization")


class MigrationPlanOpenError(ConflictError):
    """이 서비스에 끝나지 않은 이전 계획이 이미 있다. details.planId 가 그 계획이다."""

    code = "MIGRATION_PLAN_OPEN"

    def __init__(self, plan_id: str | None) -> None:
        details = (
            [ErrorField(field="planId", reason="MIGRATION_PLAN_OPEN", value=plan_id)]
            if plan_id
            else None
        )
        super().__init__("service already has an open migration plan", details=details)


class SourceNotSingleError(ConflictError):
    """서비스 중 연결이 정확히 하나(비율 100)가 아니다(없음·여럿·이전 중간 비율)."""

    code = "SOURCE_NOT_SINGLE"


class TargetNotReadyError(ConflictError):
    """도착 연결에 앱이 없거나 출발과 다른 이미지다. details 에 이유를 싣는다."""

    code = "TARGET_NOT_READY"


class TargetSyncingError(ConflictError):
    """도착 예비 연결이 아직 출발의 새 버전을 따라가는 중이다(열린 STANDBY_SYNC)."""

    code = "TARGET_SYNCING"


class DeployInProgressError(ConflictError):
    """이 서비스에 끝나지 않은 배포 요청이 있다. details.deployRequestId 가 그 요청이다."""

    code = "DEPLOY_IN_PROGRESS"


class PlanNotPendingError(ConflictError):
    code = "PLAN_NOT_PENDING"


class PlanNotRunningError(ConflictError):
    code = "PLAN_NOT_RUNNING"


class PlanStaleError(ConflictError):
    """계획을 만든 뒤 상태가 바뀌었다. 계획은 STALE 로 끝났고 details 에 달라진 항목을 싣는다."""

    code = "PLAN_STALE"


class PlanExpiredError(ConflictError):
    """승인 대기 시간이 지났다. 계획은 EXPIRED 로 끝났다."""

    code = "PLAN_EXPIRED"


class SecurityExceptionNotFoundError(NotFoundError):
    code = "SECURITY_EXCEPTION_NOT_FOUND"


class SecurityExceptionNotPendingError(ConflictError):
    """승인 대기(승인 전·철회 전·만료 전)가 아닌 예외를 승인하려 했다."""

    code = "SECURITY_EXCEPTION_NOT_PENDING"


class SecurityExceptionClosedError(ConflictError):
    """이미 철회했거나 만료된 예외를 철회하려 했다."""

    code = "SECURITY_EXCEPTION_CLOSED"


class SecurityGateBlockedError(ConflictError):
    """보안 게이트가 승인을 막았다. 계획은 승인 대기 그대로다.

    details 한 칸 = 막은 통제 하나의 사유 하나(field 는 통제 ID, reason 은 판정 사유 코드).
    """

    code = "SECURITY_GATE_BLOCKED"


class MigrationInProgressError(ConflictError):
    """이 서비스의 이전 계획이 실행 중이라 배포 요청·되돌리기·서비스 삭제를 받지 않는다."""

    code = "MIGRATION_IN_PROGRESS"

    def __init__(self, plan_id: str) -> None:
        super().__init__(
            "a migration plan is in progress for this service",
            details=[ErrorField(field="planId", reason="MIGRATION_IN_PROGRESS", value=plan_id)],
        )


# --- 고객 앱 환경 변수 (API 서버 10) ---


class ServiceEnvironmentNotFoundError(NotFoundError):
    """서비스와 같은 조직에 그 환경이 없다(살아 있는 연결도 없다).

    연결은 첫 배포 요청이나 변수 넣기 때 생긴다(API 서버 10-4).
    """

    code = "SERVICE_ENVIRONMENT_NOT_FOUND"

    def __init__(self) -> None:
        super().__init__("service is not connected to this environment")


class EnvVarNotFoundError(NotFoundError):
    code = "ENV_VAR_NOT_FOUND"

    def __init__(self) -> None:
        super().__init__("environment variable not found")


class EnvVarKeyInvalidError(InvalidInputError):
    code = "ENV_VAR_KEY_INVALID"

    def __init__(self) -> None:
        super().__init__(
            "environment variable name must match ^[A-Z][A-Z0-9_]{0,63}$",
            details=[ErrorField(field="key", reason="ENV_VAR_KEY_INVALID")],
        )


class EnvVarKeyReservedError(InvalidInputError):
    """플랫폼이 쓰는 이름(PORT, ECHO_ 로 시작하는 이름)이다."""

    code = "ENV_VAR_KEY_RESERVED"

    def __init__(self) -> None:
        super().__init__(
            "environment variable name is reserved by the platform",
            details=[ErrorField(field="key", reason="ENV_VAR_KEY_RESERVED")],
        )


class EnvVarValueInvalidError(InvalidInputError):
    """값이 비었거나 16,384바이트를 넘는다. 값·길이는 응답·로그에 넣지 않는다."""

    code = "ENV_VAR_VALUE_INVALID"

    def __init__(self) -> None:
        super().__init__(
            "environment variable value must be 1 to 16384 bytes of UTF-8",
            details=[ErrorField(field="value", reason="ENV_VAR_VALUE_INVALID")],
        )


class EnvVarLimitExceededError(InvalidInputError):
    code = "ENV_VAR_LIMIT_EXCEEDED"

    def __init__(self) -> None:
        super().__init__("a connection can have at most 40 environment variables")


class SecretStoreNotConfiguredError(ConflictError):
    """실제 실행기인데 보관함이 가짜다(APP_SECRET_STORE_MODE=fake). 값도 행도 받지 않는다."""

    code = "SECRET_STORE_NOT_CONFIGURED"

    def __init__(self) -> None:
        super().__init__("the app secret store is not configured")


class SecretStoreUnavailableError(ExternalError):
    """보관함 장애·시간 초과. 응답·로그에 값·비밀 이름을 넣지 않는다."""

    code = "SECRET_STORE_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("the app secret store is unavailable")


class SecretStoreForbiddenError(ExternalError):
    """보관함이 권한 없음으로 거절했다(IAM). 다시 불러도 같다."""

    code = "SECRET_STORE_FORBIDDEN"
    retryable = False

    def __init__(self) -> None:
        super().__init__("the app secret store denied access")


class RunnerTokenInvalidError(UnauthorizedError):
    """러너·실행 토큰이 없거나 형식이 틀리거나 무효다. 토큰 원문은 어디에도 넣지 않는다."""

    code = "RUNNER_TOKEN_INVALID"

    def __init__(self) -> None:
        super().__init__("runner or run token is missing or invalid")


class RunnerForbiddenError(ForbiddenError):
    """러너 경로에 사람(쿠키)·에이전트 토큰으로 왔다. 거부 줄은 남기지 않는다."""

    code = "RUNNER_FORBIDDEN"

    def __init__(self) -> None:
        super().__init__("runner routes accept runner and run tokens only")


class RunNotActiveError(ConflictError):
    """실행이 끝났거나 임대·마감이 지났다."""

    code = "RUN_NOT_ACTIVE"

    def __init__(self, run_id: str) -> None:
        super().__init__("run is not active", run_id=run_id)


class ResultAlreadySubmittedError(ConflictError):
    """이미 끝난 실행에 결과를 냈다."""

    code = "RESULT_ALREADY_SUBMITTED"

    def __init__(self, run_id: str) -> None:
        super().__init__("run has already finished", run_id=run_id)


class ResultInvalidError(InvalidInputError):
    """러너의 이벤트·결과가 형식·크기·AI PASS 규칙에 맞지 않는다. details 에 칸과 이유만 싣는다."""

    code = "RESULT_INVALID"

    def __init__(self, details: Sequence[ErrorField] | None = None) -> None:
        super().__init__("runner payload is invalid", details=details)


class PrecheckInProgressError(ConflictError):
    """그 서비스에 끝나지 않은 AI 실행이 있다. details 에 그 실행 id 를 싣는다."""

    code = "PRECHECK_IN_PROGRESS"

    def __init__(self, run_id: str | None = None) -> None:
        details = (
            [ErrorField(field="runId", reason="PRECHECK_IN_PROGRESS", value=run_id)]
            if run_id
            else None
        )
        super().__init__("service already has an open agent run", details=details)


class PrecheckNotFoundError(NotFoundError):
    """사전 점검이 없다. 다른 조직 것과 PRECHECK 가 아닌 실행도 같다."""

    code = "PRECHECK_NOT_FOUND"

    def __init__(self, precheck_id: str) -> None:
        super().__init__("precheck not found", precheck_id=precheck_id)


# --- 재난 (API 서버 9) ---


class DisasterNotFoundError(NotFoundError):
    """재난이 없다. 다른 조직 것도 같다."""

    code = "DISASTER_NOT_FOUND"

    def __init__(self, disaster_id: str) -> None:
        super().__init__("disaster not found", disaster_id=disaster_id)


class DisasterNotActiveError(ConflictError):
    """이미 끝난 재난을 끝내려 했다."""

    code = "DISASTER_NOT_ACTIVE"

    def __init__(self, disaster_id: str) -> None:
        super().__init__("disaster is not active", disaster_id=disaster_id)


class DisasterActiveError(ConflictError):
    """도착 환경이 복귀를 막는 재난의 피해 환경이다. details.disasterId 가 그 재난이다."""

    code = "DISASTER_ACTIVE"

    def __init__(self, disaster_id: str) -> None:
        super().__init__(
            "target environment is affected by an active disaster",
            details=[ErrorField(field="disasterId", reason="DISASTER_ACTIVE", value=disaster_id)],
        )


# --- AI 패치 결정 (API 서버 10) ---


class PatchNotFoundError(NotFoundError):
    """사전 점검 결과에 그 패치가 없다. 결과가 없거나 패치가 하나도 없는 실행도 같다."""

    code = "PATCH_NOT_FOUND"

    def __init__(self, patch_id: str) -> None:
        super().__init__("patch not found", patch_id=patch_id)


class RunNotSucceededError(ConflictError):
    """패치를 낸 사전 점검이 성공으로 끝나지 않아 결정할 수 없다."""

    code = "RUN_NOT_SUCCEEDED"

    def __init__(self, run_id: str) -> None:
        super().__init__("precheck run has not succeeded", run_id=run_id)


class PatchBlockedError(ConflictError):
    """검증을 통과하지 못한 패치는 승인할 수 없다. details 에 막힌 이유를 싣는다."""

    code = "PATCH_BLOCKED"

    def __init__(self, block_reason: str | None) -> None:
        super().__init__(
            "patch is blocked",
            details=[ErrorField(field="blockReason", reason=self.code, value=block_reason)],
        )


class PatchAlreadyDecidedError(ConflictError):
    """이미 다른 결정이 났다. details 에 먼저 난 결정을 싣는다."""

    code = "PATCH_ALREADY_DECIDED"

    def __init__(self, decision: str) -> None:
        super().__init__(
            "patch is already decided",
            details=[ErrorField(field="decision", reason=self.code, value=decision)],
        )


class PatchStaleError(ConflictError):
    """화면이 본 패치와 저장 값이 다르거나 사전 점검이 오래됐다. details 의 reason 으로 가른다."""

    code = "PATCH_STALE"
    HASH_MISMATCH_REASON = "PATCH_HASH_MISMATCH"
    EXPIRED_REASON = "PRECHECK_EXPIRED"

    def __init__(self, field: str, reason: str) -> None:
        super().__init__("patch is stale", details=[ErrorField(field=field, reason=reason)])
