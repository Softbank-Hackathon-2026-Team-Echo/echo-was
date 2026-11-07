"""감사 로그 이름표: 행동 이름, 대상 종류, SYSTEM 줄을 쓴 쪽, 쓰기 경로 → 행동 이름 표.

쓰기 경로를 새로 만들면 WRITE_ROUTE_ACTIONS(러너 경로는 RUNNER_ROUTE_ACTIONS)에 이름을 더한다.
이름이 빠지면 시험이 실패한다(앱의 모든 POST·PUT·PATCH·DELETE 중 로그아웃만 뺀다).
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
    SERVICE_VARIABLE_COPIED = "SERVICE_VARIABLE_COPIED"
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
    CVE_WATCH_CHECKED = "CVE_WATCH_CHECKED"
    CVE_WATCH_INJECTED = "CVE_WATCH_INJECTED"
    CVE_DIAGNOSIS_REQUESTED = "CVE_DIAGNOSIS_REQUESTED"
    PRECHECK_REQUESTED = "PRECHECK_REQUESTED"
    PRECHECK_CANCELLED = "PRECHECK_CANCELLED"
    AGENT_PATCH_APPROVED = "AGENT_PATCH_APPROVED"
    AGENT_PATCH_REJECTED = "AGENT_PATCH_REJECTED"
    DIAGNOSIS_REQUESTED = "DIAGNOSIS_REQUESTED"
    DISASTER_DECLARED = "DISASTER_DECLARED"
    DISASTER_ENDED = "DISASTER_ENDED"
    DISASTER_EVACUATION_REQUESTED = "DISASTER_EVACUATION_REQUESTED"
    DISASTER_FEED_INJECTED = "DISASTER_FEED_INJECTED"
    # 시연 조직 스냅샷(details.snapshot 이 그 기록 자체다)·복귀.
    DEMO_SNAPSHOT = "DEMO_SNAPSHOT"
    DEMO_RESTORE = "DEMO_RESTORE"
    # --- 서버(SYSTEM): 끝 상태 ---
    DEPLOY_FINISHED = "DEPLOY_FINISHED"
    DEPLOY_SECURITY_CHECKED = "DEPLOY_SECURITY_CHECKED"
    ROLLBACK_FINISHED = "ROLLBACK_FINISHED"
    MIGRATION_FINISHED = "MIGRATION_FINISHED"
    # 이전 계획의 DB 쓰기 전환 단계(API 서버 17): 컷오버 Job 을 만듦 · 그 단계를 판정함.
    MIGRATION_DB_CUTOVER_STARTED = "MIGRATION_DB_CUTOVER_STARTED"
    MIGRATION_DB_CUTOVER_FINISHED = "MIGRATION_DB_CUTOVER_FINISHED"
    SERVICE_DELETED = "SERVICE_DELETED"
    # --- 서버(SYSTEM, component = cve-watch): 주기 CVE 감시가 새 증거를 남김 ---
    CVE_WATCH_FINDINGS_RECORDED = "CVE_WATCH_FINDINGS_RECORDED"
    # --- 서버(SYSTEM, component = ai-runner): AI 실행 ---
    AGENT_RUN_CLAIMED = "AGENT_RUN_CLAIMED"
    AGENT_RUN_FINISHED = "AGENT_RUN_FINISHED"
    AGENT_RUN_TIMED_OUT = "AGENT_RUN_TIMED_OUT"
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
    AGENT_RUN = "agent_run"
    DISASTER = "disaster"
    FINDING = "finding"


class AuditComponent(StrEnum):
    """SYSTEM 줄을 쓴 쪽. API 는 사람 요청을 처리하다 서버 규칙으로 자동 종료한 경우다."""

    API = "api"
    DEPLOY_WORKER = "deploy-worker"
    MIGRATION_WORKER = "migration-worker"
    TEARDOWN_WORKER = "teardown-worker"
    AI_RUNNER = "ai-runner"
    CVE_WATCH = "cve-watch"
    LIVE_DISASTER_FEED = "live-disaster-feed"
    # 플랫폼 자기 시험(보안 5-3): 일회용 에이전트 토큰 발급·폐기.
    SECURITY_SELF_TEST = "security-self-test"


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
    ("POST", f"{_ORG}/services/{{service_id}}/variables:copy"): (
        AuditAction.SERVICE_VARIABLE_COPIED
    ),
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
    ("POST", f"{_ORG}/security/cve-watch:check"): AuditAction.CVE_WATCH_CHECKED,
    ("POST", f"{_ORG}/security/cve-watch:inject"): AuditAction.CVE_WATCH_INJECTED,
    # 이슈 #98(CHANGE_PROPOSAL) 병합 전에는 501 이라 성공 줄이 없다(SPEC sec-6-cve-watch 2-d).
    ("POST", f"{_ORG}/security/findings/{{finding_id}}/diagnosis"): (
        AuditAction.CVE_DIAGNOSIS_REQUESTED
    ),
    ("POST", f"{_ORG}/prechecks"): AuditAction.PRECHECK_REQUESTED,
    ("POST", f"{_ORG}/prechecks/{{precheck_id}}/cancel"): AuditAction.PRECHECK_CANCELLED,
    ("POST", f"{_ORG}/prechecks/{{precheck_id}}/patches/{{patch_id}}/approve"): (
        AuditAction.AGENT_PATCH_APPROVED
    ),
    ("POST", f"{_ORG}/prechecks/{{precheck_id}}/patches/{{patch_id}}/reject"): (
        AuditAction.AGENT_PATCH_REJECTED
    ),
    ("POST", f"{_ORG}/deploy-requests/{{deploy_request_id}}/diagnosis"): (
        AuditAction.DIAGNOSIS_REQUESTED
    ),
    ("POST", f"{_ORG}/disasters"): AuditAction.DISASTER_DECLARED,
    ("POST", f"{_ORG}/disasters/{{disaster_id}}/end"): AuditAction.DISASTER_ENDED,
    ("POST", f"{_ORG}/disasters/{{disaster_id}}/evacuate"): (
        AuditAction.DISASTER_EVACUATION_REQUESTED
    ),
    ("POST", f"{_ORG}/disasters/feed:inject"): AuditAction.DISASTER_FEED_INJECTED,
    ("POST", f"{_ORG}/demo/snapshot"): AuditAction.DEMO_SNAPSHOT,
    ("POST", f"{_ORG}/demo/restore"): AuditAction.DEMO_RESTORE,
}

_RUNNER = "/api/v1/runner/runs"

# 러너 쓰기 경로 → SYSTEM(component = ai-runner) 줄의 행동 이름. None 은 줄을 쓰지 않는 경로다.
# 시험은 "쓰기 경로 = WRITE_ROUTE_ACTIONS ∪ RUNNER_ROUTE_ACTIONS ∪ 로그아웃"을 확인한다.
RUNNER_ROUTE_ACTIONS: dict[tuple[str, str], AuditAction | None] = {
    ("POST", f"{_RUNNER}:claim"): AuditAction.AGENT_RUN_CLAIMED,
    ("POST", f"{_RUNNER}/{{run_id}}/heartbeat"): None,
    ("POST", f"{_RUNNER}/{{run_id}}/events"): None,
    ("POST", f"{_RUNNER}/{{run_id}}/result"): AuditAction.AGENT_RUN_FINISHED,
}
