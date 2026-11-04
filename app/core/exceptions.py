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
