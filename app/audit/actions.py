"""감사 로그 이름표: 행동 이름, 대상 종류, SYSTEM 줄을 쓴 쪽, 쓰기 경로 → 행동 이름 표.

쓰기 경로를 새로 만들면 WRITE_ROUTE_ACTIONS 에 이름을 더한다. 이름이 빠지면 시험이 실패한다
(앱의 모든 POST·PUT·PATCH·DELETE 중 로그아웃만 뺀다).
"""

from enum import StrEnum


class AuditAction(StrEnum):
    # --- 사람(USER) ---
    ORGANIZATION_CREATED = "ORGANIZATION_CREATED"
    ORGANIZATION_SETTINGS_CHANGED = "ORGANIZATION_SETTINGS_CHANGED"
    MEMBER_ROLE_CHANGED = "MEMBER_ROLE_CHANGED"
    MEMBER_REMOVED = "MEMBER_REMOVED"
    MEMBER_JOINED = "MEMBER_JOINED"
    INVITATION_CREATED = "INVITATION_CREATED"
    INVITATION_REVOKED = "INVITATION_REVOKED"
    AGENT_TOKEN_ISSUED = "AGENT_TOKEN_ISSUED"
    AGENT_TOKEN_REVOKED = "AGENT_TOKEN_REVOKED"
    ENVIRONMENT_CREATED = "ENVIRONMENT_CREATED"
    ENVIRONMENT_UPDATED = "ENVIRONMENT_UPDATED"
    ENVIRONMENT_DELETED = "ENVIRONMENT_DELETED"
    ENVIRONMENT_CONNECTION_CHECKED = "ENVIRONMENT_CONNECTION_CHECKED"
    SERVICE_CREATED = "SERVICE_CREATED"
    SERVICE_UPDATED = "SERVICE_UPDATED"
    SERVICE_DELETE_REQUESTED = "SERVICE_DELETE_REQUESTED"
    SERVICE_VARIABLE_SET = "SERVICE_VARIABLE_SET"
    SERVICE_VARIABLE_DELETED = "SERVICE_VARIABLE_DELETED"
    DEPLOY_REQUESTED = "DEPLOY_REQUESTED"
    ROLLBACK_REQUESTED = "ROLLBACK_REQUESTED"
    MIGRATION_PLAN_CREATED = "MIGRATION_PLAN_CREATED"
    MIGRATION_PLAN_APPROVED = "MIGRATION_PLAN_APPROVED"
    MIGRATION_PLAN_REJECTED = "MIGRATION_PLAN_REJECTED"
    MIGRATION_PLAN_CANCELLED = "MIGRATION_PLAN_CANCELLED"
    MIGRATION_ABORT_REQUESTED = "MIGRATION_ABORT_REQUESTED"
    SECURITY_EXCEPTION_REQUESTED = "SECURITY_EXCEPTION_REQUESTED"
    SECURITY_EXCEPTION_APPROVED = "SECURITY_EXCEPTION_APPROVED"
    SECURITY_EXCEPTION_REVOKED = "SECURITY_EXCEPTION_REVOKED"
    # --- 서버(SYSTEM): 끝 상태 ---
    DEPLOY_FINISHED = "DEPLOY_FINISHED"
    ROLLBACK_FINISHED = "ROLLBACK_FINISHED"
    MIGRATION_FINISHED = "MIGRATION_FINISHED"
    SERVICE_DELETED = "SERVICE_DELETED"
    # --- AI 에이전트(AGENT): 성공한 읽기 ---
    RESOURCE_READ = "RESOURCE_READ"


class AuditTargetType(StrEnum):
    """audit_logs.target_type. 멤버 역할 변경·내보내기의 대상도 user 다."""

    ORGANIZATION = "organization"
    USER = "user"
    INVITATION = "invitation"
    AGENT_TOKEN = "agent_token"
    ENVIRONMENT = "environment"
    SERVICE = "service"
    DEPLOY_REQUEST = "deploy_request"
    MIGRATION_PLAN = "migration_plan"
    SECURITY_EXCEPTION = "security_exception"


class AuditComponent(StrEnum):
    """SYSTEM 줄을 쓴 쪽. API 는 사람 요청을 처리하다 서버 규칙으로 자동 종료한 경우다."""

    API = "api"
    DEPLOY_WORKER = "deploy-worker"
    MIGRATION_WORKER = "migration-worker"
    TEARDOWN_WORKER = "teardown-worker"


_ORG = "/api/v1/orgs/{org_id}"
_SERVICE_VARIABLE = (
    f"{_ORG}/services/{{service_id}}/environments/{{environment_id}}/variables/{{key}}"
)

# (메서드, 경로 틀) → 행동 이름. 로그아웃(POST /api/v1/auth/logout)만 조직 밖 행동이라 뺀다.
WRITE_ROUTE_ACTIONS: dict[tuple[str, str], AuditAction] = {
    ("POST", "/api/v1/orgs"): AuditAction.ORGANIZATION_CREATED,
    ("PATCH", f"{_ORG}/settings"): AuditAction.ORGANIZATION_SETTINGS_CHANGED,
    ("PATCH", f"{_ORG}/members/{{user_id}}"): AuditAction.MEMBER_ROLE_CHANGED,
    ("DELETE", f"{_ORG}/members/{{user_id}}"): AuditAction.MEMBER_REMOVED,
    ("POST", f"{_ORG}/invitations"): AuditAction.INVITATION_CREATED,
    ("DELETE", f"{_ORG}/invitations/{{invitation_id}}"): AuditAction.INVITATION_REVOKED,
    ("POST", f"{_ORG}/agent-tokens"): AuditAction.AGENT_TOKEN_ISSUED,
    ("DELETE", f"{_ORG}/agent-tokens/{{token_id}}"): AuditAction.AGENT_TOKEN_REVOKED,
    ("POST", f"{_ORG}/environments"): AuditAction.ENVIRONMENT_CREATED,
    ("PATCH", f"{_ORG}/environments/{{environment_id}}"): AuditAction.ENVIRONMENT_UPDATED,
    ("DELETE", f"{_ORG}/environments/{{environment_id}}"): AuditAction.ENVIRONMENT_DELETED,
    (
        "POST",
        f"{_ORG}/environments/{{environment_id}}/connection-check",
    ): AuditAction.ENVIRONMENT_CONNECTION_CHECKED,
    ("POST", f"{_ORG}/services"): AuditAction.SERVICE_CREATED,
    ("PATCH", f"{_ORG}/services/{{service_id}}"): AuditAction.SERVICE_UPDATED,
    ("DELETE", f"{_ORG}/services/{{service_id}}"): AuditAction.SERVICE_DELETE_REQUESTED,
    ("PUT", _SERVICE_VARIABLE): AuditAction.SERVICE_VARIABLE_SET,
    ("DELETE", _SERVICE_VARIABLE): AuditAction.SERVICE_VARIABLE_DELETED,
    ("POST", f"{_ORG}/services/{{service_id}}/deploy-requests"): AuditAction.DEPLOY_REQUESTED,
    ("POST", f"{_ORG}/deploy-requests/{{deploy_request_id}}/rollback"): (
        AuditAction.ROLLBACK_REQUESTED
    ),
    ("POST", f"{_ORG}/services/{{service_id}}/migration-plans"): AuditAction.MIGRATION_PLAN_CREATED,
    ("POST", f"{_ORG}/migration-plans/{{plan_id}}/approve"): AuditAction.MIGRATION_PLAN_APPROVED,
    ("POST", f"{_ORG}/migration-plans/{{plan_id}}/reject"): AuditAction.MIGRATION_PLAN_REJECTED,
    ("POST", f"{_ORG}/migration-plans/{{plan_id}}/cancel"): AuditAction.MIGRATION_PLAN_CANCELLED,
    ("POST", f"{_ORG}/migration-plans/{{plan_id}}/abort"): AuditAction.MIGRATION_ABORT_REQUESTED,
    ("POST", f"{_ORG}/security/exceptions"): AuditAction.SECURITY_EXCEPTION_REQUESTED,
    ("POST", f"{_ORG}/security/exceptions/{{exception_id}}/approve"): (
        AuditAction.SECURITY_EXCEPTION_APPROVED
    ),
    ("POST", f"{_ORG}/security/exceptions/{{exception_id}}/revoke"): (
        AuditAction.SECURITY_EXCEPTION_REVOKED
    ),
}
