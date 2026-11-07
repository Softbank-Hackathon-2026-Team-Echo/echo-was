"""시연 조직 스냅샷·복귀(시연 연습용 「언제든 시연 환경으로 돌아가기」).

- 스냅샷 = 감사 줄 하나(action DEMO_SNAPSHOT, 대상 = 조직)의 details.snapshot. 새 표가 없고,
  감사 줄은 고칠 수도 지울 수도 없어 스냅샷이 바뀌지 않는다. 가장 늦은 줄(seq 최대)이
  지금 스냅샷이다.
- 스냅샷은 깨끗한 상태에서만 딴다(열린 이전 계획·진행 중 재난·열린 배포 요청이 없고 서비스마다
  비율 100 연결이 하나).
- 복귀는 정해진 순서로 **기존 서비스 함수만** 부른다(취소·중단·재난 종료·AI 실행 취소·수정안 거절·
  이전 계획 만들기+승인·되돌리기). 권한·자기 승인·보안 게이트·지문 검사는 그 함수들이 그대로 한다.
  각 함수가 자기 트랜잭션을 열므로 여기서는 트랜잭션을 열어 둔 채 부르지 않고, 실패의 롤백이 세션
  객체를 만료시키므로 부르는 사이에 ORM 객체를 들고 있지 않는다(값만 넘긴다).
- 한 번에 끝나지 않는 일(중단 롤백 → 복귀 이전 → 되돌리기 배포)은 일꾼이 끝낸 뒤 다시 부르면 이어
  간다. 여러 번 불러도 안전하다.
- DB 쓰기 담당·데모 앱 데이터·AI 수정 PR 은 WAS 가 하지 않는다(수동 필요로만 알린다).
  보안 예외·환경·조직 설정은 되돌리지 않고 달라졌는지만 알린다. 감사 로그는 지우지 않는다.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from functools import partial
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.actions import AuditAction, AuditTargetType
from app.audit.recorder import AuditActor, AuditRecorder, AuditTarget
from app.core.clock import Clock
from app.core.exceptions import (
    AlreadyCurrentError,
    AppError,
    DemoSnapshotNotFoundError,
    DemoStateNotCleanError,
    DisasterNotActiveError,
    ErrorField,
    MigrationInProgressError,
    OrganizationNotFoundError,
    PatchStaleError,
    PlanExpiredError,
    RunNotActiveError,
    SelfApprovalForbiddenError,
)
from app.enums import (
    AgentPatchStatus,
    AgentRunKind,
    MigrationPlanStatus,
    MigrationStepKind,
    ServiceStatus,
)
from app.models import AuditLog, Organization
from app.repositories.demo_snapshot_repository import DemoSnapshotRepository
from app.repositories.environment_repository import EnvironmentRepository
from app.repositories.organization_repository import OrganizationRepository
from app.services.deploy_request_service import DeployRequestService
from app.services.disaster_service import DisasterService
from app.services.migration_plan_service import CreateMigrationPlanCommand, MigrationPlanService
from app.services.migration_plan_support import (
    DEFAULT_MAX_ERROR_RATE_PERCENT,
    DEFAULT_MAX_P95_MS,
    DEFAULT_MAX_REPLICATION_LAG_SECONDS,
    STEP_KIND,
)
from app.services.precheck_patch_service import PrecheckPatchService
from app.services.precheck_service import PrecheckService

logger = logging.getLogger(__name__)

SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_DETAILS_KEY = "snapshot"
PATCH_REJECT_REASON = "demo restore: 시연 환경 복귀로 결정 안 된 수정안을 정리함"
PULL_REQUEST_URL_KEY = "pullRequestUrl"


class DemoStep(StrEnum):
    """복귀 단계(이 순서로 돈다)."""

    MIGRATION_PLANS = "MIGRATION_PLANS"
    DISASTERS = "DISASTERS"
    AI_RUNS = "AI_RUNS"
    DEPLOY_REQUESTS = "DEPLOY_REQUESTS"
    TRAFFIC = "TRAFFIC"
    DEPLOYMENTS = "DEPLOYMENTS"
    DB_WRITER = "DB_WRITER"
    DEMO_APP_DATA = "DEMO_APP_DATA"
    AI_FIX_PULL_REQUESTS = "AI_FIX_PULL_REQUESTS"
    KEPT_SETTINGS = "KEPT_SETTINGS"
    AUDIT_LOGS = "AUDIT_LOGS"


# restored 판정에 쓰는 단계(되돌리는 단계). 나머지는 수동 필요·유지 안내다.
RESTORING_STEPS = (
    DemoStep.MIGRATION_PLANS,
    DemoStep.DISASTERS,
    DemoStep.AI_RUNS,
    DemoStep.DEPLOY_REQUESTS,
    DemoStep.TRAFFIC,
    DemoStep.DEPLOYMENTS,
)


class DemoStepStatus(StrEnum):
    OK = "OK"  # 이미 스냅샷과 같다.
    DONE = "DONE"  # 이번에 조치해 끝났다.
    STARTED = "STARTED"  # 조치를 시작했다(일꾼이 끝내야 한다). 다시 부른다.
    WAITING = "WAITING"  # 앞 단계·진행 중인 작업이 끝나야 한다. 다시 부른다.
    MANUAL_REQUIRED = "MANUAL_REQUIRED"  # WAS 가 하지 않는다. 사람이 한다.
    FAILED = "FAILED"  # 기존 함수가 거절했다(code).
    KEPT = "KEPT"  # 일부러 되돌리지 않는다(기록만).
    WOULD_DO = "WOULD_DO"  # dryRun: 이 조치를 할 것이다.


# 단계 상태 = 항목 상태 중 가장 앞의 것.
_STATUS_PRIORITY = (
    DemoStepStatus.FAILED,
    DemoStepStatus.MANUAL_REQUIRED,
    DemoStepStatus.WAITING,
    DemoStepStatus.STARTED,
    DemoStepStatus.WOULD_DO,
    DemoStepStatus.DONE,
    DemoStepStatus.KEPT,
    DemoStepStatus.OK,
)
_SETTLED = frozenset({DemoStepStatus.OK, DemoStepStatus.DONE})
_IN_FLIGHT = frozenset({DemoStepStatus.STARTED, DemoStepStatus.WAITING})
_CHANGED = frozenset({DemoStepStatus.DONE, DemoStepStatus.STARTED})


class DemoAction(StrEnum):
    """항목에 한(할) 일."""

    CANCEL_PLAN = "CANCEL_PLAN"
    ABORT_PLAN = "ABORT_PLAN"
    KEEP_FAILBACK_PLAN = "KEEP_FAILBACK_PLAN"
    WAIT_PLAN = "WAIT_PLAN"
    END_DISASTER = "END_DISASTER"
    CANCEL_AI_RUN = "CANCEL_AI_RUN"
    WAIT_AI_RUN = "WAIT_AI_RUN"
    REJECT_PATCH = "REJECT_PATCH"
    WAIT_DEPLOY_REQUEST = "WAIT_DEPLOY_REQUEST"
    CREATE_FAILBACK_PLAN = "CREATE_FAILBACK_PLAN"
    APPROVE_FAILBACK_PLAN = "APPROVE_FAILBACK_PLAN"
    ROLLBACK_DEPLOYMENT = "ROLLBACK_DEPLOYMENT"
    CHECK_DB_WRITER = "CHECK_DB_WRITER"
    RESET_APP_DATA = "RESET_APP_DATA"
    CLOSE_PULL_REQUEST = "CLOSE_PULL_REQUEST"
    COMPARE = "COMPARE"
    NONE = "NONE"


# 수동 필요·대기 항목의 고정 이유 코드.
WAITING_FOR_EARLIER_STEP = "WAITING_FOR_EARLIER_STEP"
ROLLBACK_IN_PROGRESS = "ROLLBACK_IN_PROGRESS"
ABORT_IN_PROGRESS = "ABORT_IN_PROGRESS"
FAILBACK_IN_PROGRESS = "FAILBACK_IN_PROGRESS"
APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
SERVING_ROLLBACK_FIRST = "SERVING_ROLLBACK_FIRST"
SERVICE_MISSING = "SERVICE_MISSING"
CONNECTION_MISSING = "CONNECTION_MISSING"
NOT_DEPLOYED = "NOT_DEPLOYED"
DB_CUTOVER_SINCE_SNAPSHOT = "DB_CUTOVER_SINCE_SNAPSHOT"
NO_DB_CUTOVER_SINCE_SNAPSHOT = "NO_DB_CUTOVER_SINCE_SNAPSHOT"
APP_RESET_ENDPOINT_MISSING = "APP_RESET_ENDPOINT_MISSING"
PULL_REQUEST_OPENED = "PULL_REQUEST_OPENED"
AUDIT_LOGS_NOT_DELETABLE = "AUDIT_LOGS_NOT_DELETABLE"
UNCHANGED = "UNCHANGED"

# 스냅샷을 막는 이유 코드(409 details.reason).
NOT_CLEAN_OPEN_PLAN = "OPEN_MIGRATION_PLAN"
NOT_CLEAN_ACTIVE_DISASTER = "ACTIVE_DISASTER"
NOT_CLEAN_OPEN_DEPLOY_REQUEST = "OPEN_DEPLOY_REQUEST"
NOT_CLEAN_NOT_SINGLE_SERVING = "SERVICE_NOT_SINGLE_SERVING"
NOT_CLEAN_DEPLOYMENT_SOURCE_MISSING = "DEPLOYMENT_SOURCE_MISSING"
NOT_CLEAN_SERVICE_DELETING = "SERVICE_DELETING"


# --- 스냅샷 값 ---


@dataclass(frozen=True)
class SnapshotConnection:
    service_environment_id: uuid.UUID
    environment_id: uuid.UUID
    environment_name: str
    environment_kind: str
    traffic_weight: int
    current_deployment_id: uuid.UUID | None
    deploy_request_id: uuid.UUID | None
    commit_sha: str | None
    image_digest: str | None


@dataclass(frozen=True)
class SnapshotService:
    service_id: uuid.UUID
    name: str
    serving_environment_id: uuid.UUID | None
    connections: list[SnapshotConnection]

    def connection_for(self, environment_id: uuid.UUID) -> SnapshotConnection | None:
        return next(
            (item for item in self.connections if item.environment_id == environment_id), None
        )


@dataclass(frozen=True)
class SnapshotEnvironment:
    environment_id: uuid.UUID
    name: str
    kind: str
    connection_status: str


@dataclass(frozen=True)
class SnapshotSecurityException:
    exception_id: uuid.UUID
    control_id: str
    service_id: uuid.UUID | None
    environment_id: uuid.UUID | None
    approved: bool
    revoked: bool
    expires_at: datetime


@dataclass(frozen=True)
class DemoSnapshotState:
    services: list[SnapshotService]
    environments: list[SnapshotEnvironment]
    security_exceptions: list[SnapshotSecurityException]
    allow_self_approval: bool
    schema_version: int = SNAPSHOT_SCHEMA_VERSION


@dataclass(frozen=True)
class DemoSnapshotView:
    snapshot_id: uuid.UUID
    taken_at: datetime
    taken_by_user_id: uuid.UUID | None
    state: DemoSnapshotState


# --- 복귀 결과 ---


@dataclass(frozen=True)
class DemoItemResult:
    action: DemoAction
    status: DemoStepStatus
    target_type: str | None = None
    target_id: uuid.UUID | None = None
    label: str | None = None
    code: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class DemoStepResult:
    step: DemoStep
    items: list[DemoItemResult]
    # 항목이 없을 때의 상태(보통 OK).
    empty_status: DemoStepStatus = DemoStepStatus.OK

    @property
    def status(self) -> DemoStepStatus:
        return summarize_status([item.status for item in self.items], self.empty_status)


@dataclass(frozen=True)
class DemoRestoreResult:
    snapshot_id: uuid.UUID
    snapshot_taken_at: datetime
    dry_run: bool
    steps: list[DemoStepResult]

    @property
    def restored(self) -> bool:
        return is_restored(self.steps)

    @property
    def call_again(self) -> bool:
        return needs_call_again(self.steps)

    @property
    def manual_required(self) -> list[DemoStep]:
        return manual_required_steps(self.steps)


def summarize_status(
    statuses: Sequence[DemoStepStatus], empty: DemoStepStatus = DemoStepStatus.OK
) -> DemoStepStatus:
    if not statuses:
        return empty
    return next(status for status in _STATUS_PRIORITY if status in statuses)


def is_restored(steps: Sequence[DemoStepResult]) -> bool:
    """되돌리는 단계(1~6)가 모두 OK·DONE 이다."""
    by_step = {step.step: step.status for step in steps}
    return all(by_step.get(step, DemoStepStatus.OK) in _SETTLED for step in RESTORING_STEPS)


def needs_call_again(steps: Sequence[DemoStepResult]) -> bool:
    return any(step.status in _IN_FLIGHT for step in steps)


def manual_required_steps(steps: Sequence[DemoStepResult]) -> list[DemoStep]:
    return [step.step for step in steps if step.status is DemoStepStatus.MANUAL_REQUIRED]


def changed_item_count(steps: Sequence[DemoStepResult]) -> int:
    """이번 호출이 조치한(DONE·STARTED) 항목 수."""
    return sum(1 for step in steps for item in step.items if item.status in _CHANGED)


def changed_anything(steps: Sequence[DemoStepResult]) -> bool:
    return changed_item_count(steps) > 0


def has_db_cutover_step(steps: Iterable[dict[str, Any]]) -> bool:
    return any(step.get(STEP_KIND) == MigrationStepKind.DB_CUTOVER.value for step in steps)


# --- JSON 변환(감사 줄 details.snapshot) ---


def state_to_json(state: DemoSnapshotState) -> dict[str, Any]:
    return {
        "schemaVersion": state.schema_version,
        "services": [
            {
                "serviceId": str(service.service_id),
                "name": service.name,
                "servingEnvironmentId": _str_or_none(service.serving_environment_id),
                "connections": [
                    {
                        "serviceEnvironmentId": str(item.service_environment_id),
                        "environmentId": str(item.environment_id),
                        "environmentName": item.environment_name,
                        "environmentKind": item.environment_kind,
                        "trafficWeight": item.traffic_weight,
                        "currentDeploymentId": _str_or_none(item.current_deployment_id),
                        "deployRequestId": _str_or_none(item.deploy_request_id),
                        "commitSha": item.commit_sha,
                        "imageDigest": item.image_digest,
                    }
                    for item in service.connections
                ],
            }
            for service in state.services
        ],
        "environments": [
            {
                "environmentId": str(item.environment_id),
                "name": item.name,
                "kind": item.kind,
                "connectionStatus": item.connection_status,
            }
            for item in state.environments
        ],
        "securityExceptions": [
            {
                "exceptionId": str(item.exception_id),
                "controlId": item.control_id,
                "serviceId": _str_or_none(item.service_id),
                "environmentId": _str_or_none(item.environment_id),
                "approved": item.approved,
                "revoked": item.revoked,
                "expiresAt": item.expires_at.isoformat(),
            }
            for item in state.security_exceptions
        ],
        "organization": {"allowSelfApproval": state.allow_self_approval},
    }


def state_from_json(data: dict[str, Any]) -> DemoSnapshotState:
    return DemoSnapshotState(
        schema_version=int(data.get("schemaVersion", SNAPSHOT_SCHEMA_VERSION)),
        services=[
            SnapshotService(
                service_id=uuid.UUID(service["serviceId"]),
                name=service["name"],
                serving_environment_id=_uuid_or_none(service.get("servingEnvironmentId")),
                connections=[
                    SnapshotConnection(
                        service_environment_id=uuid.UUID(item["serviceEnvironmentId"]),
                        environment_id=uuid.UUID(item["environmentId"]),
                        environment_name=item["environmentName"],
                        environment_kind=item["environmentKind"],
                        traffic_weight=int(item["trafficWeight"]),
                        current_deployment_id=_uuid_or_none(item.get("currentDeploymentId")),
                        deploy_request_id=_uuid_or_none(item.get("deployRequestId")),
                        commit_sha=item.get("commitSha"),
                        image_digest=item.get("imageDigest"),
                    )
                    for item in service.get("connections", [])
                ],
            )
            for service in data.get("services", [])
        ],
        environments=[
            SnapshotEnvironment(
                environment_id=uuid.UUID(item["environmentId"]),
                name=item["name"],
                kind=item["kind"],
                connection_status=item["connectionStatus"],
            )
            for item in data.get("environments", [])
        ],
        security_exceptions=[
            SnapshotSecurityException(
                exception_id=uuid.UUID(item["exceptionId"]),
                control_id=item["controlId"],
                service_id=_uuid_or_none(item.get("serviceId")),
                environment_id=_uuid_or_none(item.get("environmentId")),
                approved=bool(item["approved"]),
                revoked=bool(item["revoked"]),
                expires_at=datetime.fromisoformat(item["expiresAt"]),
            )
            for item in data.get("securityExceptions", [])
        ],
        allow_self_approval=bool(data.get("organization", {}).get("allowSelfApproval", False)),
    )


def _str_or_none(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


# --- 복귀 중 읽은 값(ORM 객체를 들고 다니지 않는다) ---


@dataclass(frozen=True)
class _OpenPlan:
    plan_id: uuid.UUID
    service_id: uuid.UUID
    status: MigrationPlanStatus
    abort_requested: bool
    target_environment_id: uuid.UUID | None


@dataclass(frozen=True)
class _LiveConnection:
    service_environment_id: uuid.UUID
    environment_id: uuid.UUID
    traffic_weight: int
    current_deployment_id: uuid.UUID | None
    image_digest: str | None


@dataclass
class _RestoreContext:
    organization_id: uuid.UUID
    actor_user_id: uuid.UUID
    dry_run: bool
    snapshot: DemoSnapshotView
    # 앞 단계가 아직 끝나지 않아 비율·배포 단계를 미룰 서비스.
    blocked_service_ids: set[uuid.UUID] = field(default_factory=set)


class DemoSnapshotService:
    def __init__(
        self,
        session: AsyncSession,
        clock: Clock,
        migration_plans: MigrationPlanService,
        disasters: DisasterService,
        deploy_requests: DeployRequestService,
        prechecks: PrecheckService,
        patches: PrecheckPatchService,
    ) -> None:
        self._session = session
        self._clock = clock
        self._migration_plans = migration_plans
        self._disasters = disasters
        self._deploy_requests = deploy_requests
        self._prechecks = prechecks
        self._patches = patches
        self._repository = DemoSnapshotRepository(session)
        self._environments = EnvironmentRepository(session)
        self._organizations = OrganizationRepository(session)
        self._audit = AuditRecorder(session, clock)

    # --- 스냅샷 ---

    async def take_snapshot(
        self, organization_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> DemoSnapshotView:
        """지금 상태를 기록한다. 깨끗하지 않으면 409 이고 아무것도 쓰지 않는다."""
        async with self._session.begin():
            organization = await self._get_organization(organization_id)
            state, problems = await self._read_state(organization)
            if problems:
                raise DemoStateNotCleanError(
                    "demo organization is not in a clean state", details=problems
                )
            audit_log = self._audit.record(
                organization_id=organization_id,
                actor=AuditActor.user(actor_user_id),
                action=AuditAction.DEMO_SNAPSHOT,
                target=AuditTarget(
                    AuditTargetType.ORGANIZATION, organization.id, organization.name
                ),
                details={SNAPSHOT_DETAILS_KEY: state_to_json(state)},
            )
            await self._session.flush()
            view = _snapshot_view(audit_log)
        logger.info(
            "demo snapshot taken",
            extra={
                "action": "take_demo_snapshot",
                "organization_id": str(organization_id),
                "snapshot_id": str(view.snapshot_id),
            },
        )
        return view

    async def get_snapshot(self, organization_id: uuid.UUID) -> DemoSnapshotView:
        async with self._session.begin():
            audit_log = await self._repository.find_latest_snapshot(organization_id)
            if audit_log is None:
                raise DemoSnapshotNotFoundError("demo snapshot not found")
            return _snapshot_view(audit_log)

    async def _read_state(
        self, organization: Organization
    ) -> tuple[DemoSnapshotState, list[ErrorField]]:
        problems: list[ErrorField] = []
        organization_id = organization.id
        for plan in await self._repository.search_open_plans(organization_id):
            problems.append(ErrorField("migrationPlans", NOT_CLEAN_OPEN_PLAN, str(plan.id)))
        for disaster in await self._repository.search_active_disasters(organization_id):
            problems.append(ErrorField("disasters", NOT_CLEAN_ACTIVE_DISASTER, str(disaster.id)))
        for open_request in await self._repository.search_open_deploy_requests(organization_id):
            problems.append(
                ErrorField("deployRequests", NOT_CLEAN_OPEN_DEPLOY_REQUEST, str(open_request.id))
            )
        services = await self._repository.search_live_services(organization_id)
        connections = await self._repository.search_live_connections([s.id for s in services])
        deployment_ids = [
            item.current_deployment_id
            for item in connections
            if item.current_deployment_id is not None
        ]
        digest_by_deployment = {
            item.id: item.image_digest
            for item in await self._repository.search_deployments(deployment_ids)
        }
        request_by_deployment = {
            request.deployment_id: request
            for request in await self._repository.search_succeeded_requests_by_deployment(
                deployment_ids
            )
        }
        snapshot_services: list[SnapshotService] = []
        for service in services:
            own = [item for item in connections if item.service_id == service.id]
            if service.status != ServiceStatus.ACTIVE:
                problems.append(ErrorField("services", NOT_CLEAN_SERVICE_DELETING, str(service.id)))
            deployed = [item for item in own if item.current_deployment_id is not None]
            serving = [item for item in own if item.traffic_weight > 0]
            serving_environment_id: uuid.UUID | None = None
            if deployed:
                if len(serving) != 1 or serving[0].traffic_weight != 100:
                    problems.append(
                        ErrorField("services", NOT_CLEAN_NOT_SINGLE_SERVING, str(service.id))
                    )
                else:
                    serving_environment_id = serving[0].environment_id
            snapshot_connections: list[SnapshotConnection] = []
            for item in own:
                request = (
                    request_by_deployment.get(item.current_deployment_id)
                    if item.current_deployment_id is not None
                    else None
                )
                if item.current_deployment_id is not None and request is None:
                    problems.append(
                        ErrorField("connections", NOT_CLEAN_DEPLOYMENT_SOURCE_MISSING, str(item.id))
                    )
                snapshot_connections.append(
                    SnapshotConnection(
                        service_environment_id=item.id,
                        environment_id=item.environment_id,
                        environment_name=item.environment.name,
                        environment_kind=item.environment.kind,
                        traffic_weight=item.traffic_weight,
                        current_deployment_id=item.current_deployment_id,
                        deploy_request_id=request.id if request is not None else None,
                        commit_sha=request.commit_sha if request is not None else None,
                        image_digest=(
                            digest_by_deployment.get(item.current_deployment_id)
                            if item.current_deployment_id is not None
                            else None
                        ),
                    )
                )
            snapshot_services.append(
                SnapshotService(
                    service_id=service.id,
                    name=service.name,
                    serving_environment_id=serving_environment_id,
                    connections=snapshot_connections,
                )
            )
        environments = [
            SnapshotEnvironment(
                environment_id=item.id,
                name=item.name,
                kind=item.kind,
                connection_status=str(item.connection_status),
            )
            for item in await self._environments.search_all_live(organization_id)
        ]
        exceptions = [
            SnapshotSecurityException(
                exception_id=item.id,
                control_id=str(item.control_id),
                service_id=item.service_id,
                environment_id=item.environment_id,
                approved=item.approved_at is not None,
                revoked=item.revoked_at is not None,
                expires_at=item.expires_at,
            )
            for item in await self._repository.search_security_exceptions(organization_id)
        ]
        state = DemoSnapshotState(
            services=snapshot_services,
            environments=environments,
            security_exceptions=exceptions,
            allow_self_approval=organization.allow_self_approval,
        )
        return state, problems

    # --- 복귀 ---

    async def restore(
        self, organization_id: uuid.UUID, actor_user_id: uuid.UUID, *, dry_run: bool
    ) -> DemoRestoreResult:
        snapshot = await self.get_snapshot(organization_id)
        context = _RestoreContext(organization_id, actor_user_id, dry_run, snapshot)
        steps = [
            await self._restore_plans(context),
            await self._end_disasters(context),
            await self._clean_ai_runs(context),
            await self._wait_deploy_requests(context),
        ]
        traffic = await self._restore_traffic(context)
        steps.append(traffic)
        steps.append(await self._restore_deployments(context))
        steps.append(await self._check_db_writer(context))
        steps.append(_demo_app_data_step(snapshot.state))
        steps.append(await self._list_fix_pull_requests(context))
        steps.append(await self._compare_kept_settings(context))
        steps.append(
            DemoStepResult(
                DemoStep.AUDIT_LOGS,
                [
                    DemoItemResult(
                        DemoAction.NONE, DemoStepStatus.KEPT, code=AUDIT_LOGS_NOT_DELETABLE
                    )
                ],
            )
        )
        result = DemoRestoreResult(snapshot.snapshot_id, snapshot.taken_at, dry_run, steps)
        if not dry_run and changed_anything(steps):
            async with self._session.begin():
                organization = await self._get_organization(organization_id)
                self._audit.record(
                    organization_id=organization_id,
                    actor=AuditActor.user(actor_user_id),
                    action=AuditAction.DEMO_RESTORE,
                    target=AuditTarget(
                        AuditTargetType.ORGANIZATION, organization.id, organization.name
                    ),
                    details={
                        "snapshotId": str(snapshot.snapshot_id),
                        "restored": result.restored,
                        "callAgain": result.call_again,
                        "steps": {str(step.step): str(step.status) for step in steps},
                        "changedItems": changed_item_count(steps),
                    },
                )
        logger.info(
            "demo restore finished",
            extra={
                "action": "restore_demo_snapshot",
                "organization_id": str(organization_id),
                "snapshot_id": str(snapshot.snapshot_id),
                "dry_run": dry_run,
                "restored": result.restored,
                "call_again": result.call_again,
            },
        )
        return result

    # 1. 열린 이전 계획

    async def _restore_plans(self, context: _RestoreContext) -> DemoStepResult:
        serving_by_service = _serving_by_service(context.snapshot.state)
        items: list[DemoItemResult] = []
        for plan in await self._open_plans(context.organization_id):
            is_failback = (
                plan.target_environment_id is not None
                and serving_by_service.get(plan.service_id) == plan.target_environment_id
            )
            target = ("migration_plan", plan.plan_id, None)
            if plan.status is MigrationPlanStatus.PENDING_APPROVAL:
                if is_failback:
                    # 스냅샷 서비스 중 환경으로 돌아가는 계획이다. 비율 단계가 승인한다.
                    items.append(_item(DemoAction.KEEP_FAILBACK_PLAN, DemoStepStatus.OK, target))
                    continue
                items.append(
                    await self._call(
                        context,
                        DemoAction.CANCEL_PLAN,
                        target,
                        partial(
                            self._migration_plans.cancel_plan,
                            context.organization_id,
                            plan.plan_id,
                            context.actor_user_id,
                        ),
                        done=DemoStepStatus.DONE,
                        settled_errors=(PlanExpiredError,),
                    )
                )
            elif plan.status is MigrationPlanStatus.ROLLING_BACK:
                items.append(
                    _item(
                        DemoAction.WAIT_PLAN, DemoStepStatus.WAITING, target, ROLLBACK_IN_PROGRESS
                    )
                )
            elif is_failback and not plan.abort_requested:
                items.append(
                    _item(
                        DemoAction.WAIT_PLAN, DemoStepStatus.WAITING, target, FAILBACK_IN_PROGRESS
                    )
                )
            elif plan.abort_requested:
                items.append(
                    _item(DemoAction.WAIT_PLAN, DemoStepStatus.WAITING, target, ABORT_IN_PROGRESS)
                )
            else:
                items.append(
                    await self._call(
                        context,
                        DemoAction.ABORT_PLAN,
                        target,
                        partial(
                            self._migration_plans.abort_plan,
                            context.organization_id,
                            plan.plan_id,
                            context.actor_user_id,
                        ),
                        done=DemoStepStatus.STARTED,
                    )
                )
            if items[-1].status not in _SETTLED:
                context.blocked_service_ids.add(plan.service_id)
        return DemoStepResult(DemoStep.MIGRATION_PLANS, items)

    # 2. 진행 중 재난(복귀 잠금)

    async def _end_disasters(self, context: _RestoreContext) -> DemoStepResult:
        async with self._session.begin():
            disasters = [
                (item.id, f"{item.type} M{item.magnitude:.1f}")
                for item in await self._repository.search_active_disasters(context.organization_id)
            ]
        items = [
            await self._call(
                context,
                DemoAction.END_DISASTER,
                ("disaster", disaster_id, label),
                partial(
                    self._disasters.end_disaster,
                    context.organization_id,
                    disaster_id,
                    context.actor_user_id,
                ),
                done=DemoStepStatus.DONE,
                settled_errors=(DisasterNotActiveError,),
            )
            for disaster_id, label in disasters
        ]
        return DemoStepResult(DemoStep.DISASTERS, items)

    # 3. 열린 AI 실행·결정 안 된 수정안

    async def _clean_ai_runs(self, context: _RestoreContext) -> DemoStepResult:
        since = context.snapshot.taken_at
        async with self._session.begin():
            open_runs = [
                (item.id, item.service_id, AgentRunKind(item.kind))
                for item in await self._repository.search_open_runs(context.organization_id)
            ]
            parents = await self._repository.search_succeeded_patch_parents_since(
                context.organization_id, since
            )
            decided = await self._repository.search_decided_patch_keys([run.id for run in parents])
            undecided = [
                (run.id, run.service_id, str(patch["id"]), str(patch["patchHash"]))
                for run in parents
                for patch in (run.result or {}).get("patches", [])
                if patch.get("status") == AgentPatchStatus.PROPOSED.value
                and (run.id, str(patch["id"])) not in decided
            ]
        items: list[DemoItemResult] = []
        for run_id, service_id, kind in open_runs:
            target = ("agent_run", run_id, str(kind))
            if kind is AgentRunKind.APPLY_PATCH:
                # 반영 실행을 취소하는 함수는 없다. 끝나면(재배포까지) 되돌리기 단계가 이어 간다.
                items.append(_item(DemoAction.WAIT_AI_RUN, DemoStepStatus.WAITING, target))
                context.blocked_service_ids.add(service_id)
                continue
            items.append(
                await self._call(
                    context,
                    DemoAction.CANCEL_AI_RUN,
                    target,
                    partial(
                        self._prechecks.cancel_precheck,
                        context.organization_id,
                        run_id,
                        context.actor_user_id,
                    ),
                    done=DemoStepStatus.DONE,
                    settled_errors=(RunNotActiveError,),
                )
            )
        for run_id, _, patch_id, patch_hash in undecided:
            items.append(
                await self._call(
                    context,
                    DemoAction.REJECT_PATCH,
                    ("agent_run", run_id, patch_id),
                    partial(
                        self._patches.reject_patch,
                        context.organization_id,
                        run_id,
                        patch_id,
                        context.actor_user_id,
                        patch_hash,
                        PATCH_REJECT_REASON,
                    ),
                    done=DemoStepStatus.DONE,
                    settled_errors=(PatchStaleError,),
                )
            )
        return DemoStepResult(DemoStep.AI_RUNS, items)

    # 4. 열린 배포 요청(취소 API 가 없다)

    async def _wait_deploy_requests(self, context: _RestoreContext) -> DemoStepResult:
        async with self._session.begin():
            requests = [
                (item.id, item.service_id, str(item.status))
                for item in await self._repository.search_open_deploy_requests(
                    context.organization_id
                )
            ]
        items = []
        for request_id, service_id, status in requests:
            items.append(
                _item(
                    DemoAction.WAIT_DEPLOY_REQUEST,
                    DemoStepStatus.WAITING,
                    ("deploy_request", request_id, status),
                )
            )
            context.blocked_service_ids.add(service_id)
        return DemoStepResult(DemoStep.DEPLOY_REQUESTS, items)

    # 5. 진입점 가중치(스냅샷 서비스 중 환경 100)

    async def _restore_traffic(self, context: _RestoreContext) -> DemoStepResult:
        items: list[DemoItemResult] = []
        open_plans = {
            plan.service_id: plan for plan in await self._open_plans(context.organization_id)
        }
        for service in context.snapshot.state.services:
            if service.serving_environment_id is None:
                continue
            target = ("service", service.service_id, service.name)
            live = await self._live_connections(service.service_id)
            if live is None:
                items.append(_item(DemoAction.NONE, DemoStepStatus.FAILED, target, SERVICE_MISSING))
                continue
            serving = next(
                (item for item in live if item.environment_id == service.serving_environment_id),
                None,
            )
            if serving is not None and serving.traffic_weight == 100:
                items.append(_item(DemoAction.NONE, DemoStepStatus.OK, target))
                continue
            plan = open_plans.get(service.service_id)
            if plan is not None and plan.target_environment_id == service.serving_environment_id:
                if plan.status is MigrationPlanStatus.PENDING_APPROVAL:
                    item = await self._approve_failback(context, plan.plan_id, target)
                else:
                    item = _item(
                        DemoAction.WAIT_PLAN,
                        DemoStepStatus.WAITING,
                        ("migration_plan", plan.plan_id, service.name),
                        FAILBACK_IN_PROGRESS,
                    )
                items.append(item)
                context.blocked_service_ids.add(service.service_id)
                continue
            if service.service_id in context.blocked_service_ids:
                items.append(
                    _item(
                        DemoAction.CREATE_FAILBACK_PLAN,
                        DemoStepStatus.WAITING,
                        target,
                        WAITING_FOR_EARLIER_STEP,
                    )
                )
                continue
            context.blocked_service_ids.add(service.service_id)
            if context.dry_run:
                items.append(
                    _item(DemoAction.CREATE_FAILBACK_PLAN, DemoStepStatus.WOULD_DO, target)
                )
                continue
            try:
                view = await self._migration_plans.create_plan(
                    context.organization_id,
                    service.service_id,
                    context.actor_user_id,
                    _failback_command(service.serving_environment_id),
                )
            except AppError as error:
                items.append(
                    _item(
                        DemoAction.CREATE_FAILBACK_PLAN, DemoStepStatus.FAILED, target, error.code
                    )
                )
                continue
            items.append(await self._approve_failback(context, view.plan.id, target))
        return DemoStepResult(DemoStep.TRAFFIC, items)

    async def _approve_failback(
        self,
        context: _RestoreContext,
        plan_id: uuid.UUID,
        target: tuple[str, uuid.UUID, str | None],
    ) -> DemoItemResult:
        detail = f"migrationPlanId={plan_id}"
        if context.dry_run:
            return _item(
                DemoAction.APPROVE_FAILBACK_PLAN, DemoStepStatus.WOULD_DO, target, detail=detail
            )
        try:
            await self._migration_plans.approve_plan(
                context.organization_id, plan_id, context.actor_user_id
            )
        except SelfApprovalForbiddenError:
            # 자기 승인이 꺼진 조직: 계획은 승인 대기로 남는다. 다른 개발자 이상이 승인한다.
            return _item(
                DemoAction.APPROVE_FAILBACK_PLAN,
                DemoStepStatus.MANUAL_REQUIRED,
                target,
                APPROVAL_REQUIRED,
                detail,
            )
        except AppError as error:
            return _item(
                DemoAction.APPROVE_FAILBACK_PLAN, DemoStepStatus.FAILED, target, error.code, detail
            )
        return _item(
            DemoAction.APPROVE_FAILBACK_PLAN, DemoStepStatus.STARTED, target, detail=detail
        )

    # 6. 배포(스냅샷 이미지로 되돌리기, 빌드 재사용)

    async def _restore_deployments(self, context: _RestoreContext) -> DemoStepResult:
        items: list[DemoItemResult] = []
        for service in context.snapshot.state.services:
            if service.serving_environment_id is None:
                items.append(
                    _item(
                        DemoAction.NONE,
                        DemoStepStatus.OK,
                        ("service", service.service_id, service.name),
                        NOT_DEPLOYED,
                    )
                )
                continue
            live = await self._live_connections(service.service_id)
            if live is None:
                items.append(
                    _item(
                        DemoAction.NONE,
                        DemoStepStatus.FAILED,
                        ("service", service.service_id, service.name),
                        SERVICE_MISSING,
                    )
                )
                continue
            live_by_connection = {item.service_environment_id: item for item in live}
            serving = service.connection_for(service.serving_environment_id)
            assert serving is not None
            others = [
                item
                for item in service.connections
                if item.environment_id != service.serving_environment_id
                and item.current_deployment_id is not None
            ]
            blocked = service.service_id in context.blocked_service_ids
            serving_item = await self._rollback_connection(
                context, service, serving, live_by_connection, blocked=blocked
            )
            items.append(serving_item)
            serving_settled = serving_item.status in _SETTLED and not blocked
            for item in others:
                if serving_settled:
                    items.append(
                        await self._rollback_connection(
                            context, service, item, live_by_connection, blocked=False
                        )
                    )
                elif not _same_image(item, live_by_connection):
                    items.append(
                        _item(
                            DemoAction.ROLLBACK_DEPLOYMENT,
                            DemoStepStatus.WAITING,
                            _connection_target(service, item),
                            SERVING_ROLLBACK_FIRST,
                        )
                    )
        return DemoStepResult(DemoStep.DEPLOYMENTS, items)

    async def _rollback_connection(
        self,
        context: _RestoreContext,
        service: SnapshotService,
        snapshot: SnapshotConnection,
        live_by_connection: dict[uuid.UUID, _LiveConnection],
        *,
        blocked: bool,
    ) -> DemoItemResult:
        target = _connection_target(service, snapshot)
        live = live_by_connection.get(snapshot.service_environment_id)
        if live is None:
            return _item(
                DemoAction.ROLLBACK_DEPLOYMENT, DemoStepStatus.FAILED, target, CONNECTION_MISSING
            )
        if _same_image(snapshot, live_by_connection):
            return _item(DemoAction.NONE, DemoStepStatus.OK, target)
        if blocked:
            return _item(
                DemoAction.ROLLBACK_DEPLOYMENT,
                DemoStepStatus.WAITING,
                target,
                WAITING_FOR_EARLIER_STEP,
            )
        request_id = snapshot.deploy_request_id
        assert request_id is not None
        return await self._call(
            context,
            DemoAction.ROLLBACK_DEPLOYMENT,
            target,
            partial(
                self._deploy_requests.create_rollback,
                context.organization_id,
                request_id,
                context.actor_user_id,
                None,
            ),
            done=DemoStepStatus.STARTED,
            settled_errors=(AlreadyCurrentError,),
            waiting_errors=(MigrationInProgressError,),
            detail=f"rollbackOfDeployRequestId={request_id}",
        )

    # 7~10. 수동 필요·유지 안내(읽기만)

    async def _check_db_writer(self, context: _RestoreContext) -> DemoStepResult:
        async with self._session.begin():
            plans = [
                (plan.id, str(plan.status))
                for plan in await self._repository.search_plans_created_since(
                    context.organization_id, context.snapshot.taken_at
                )
                if has_db_cutover_step(plan.steps)
            ]
        if not plans:
            return DemoStepResult(
                DemoStep.DB_WRITER,
                [
                    DemoItemResult(
                        DemoAction.CHECK_DB_WRITER,
                        DemoStepStatus.OK,
                        code=NO_DB_CUTOVER_SINCE_SNAPSHOT,
                    )
                ],
            )
        return DemoStepResult(
            DemoStep.DB_WRITER,
            [
                _item(
                    DemoAction.CHECK_DB_WRITER,
                    DemoStepStatus.MANUAL_REQUIRED,
                    ("migration_plan", plan_id, status),
                    DB_CUTOVER_SINCE_SNAPSHOT,
                )
                for plan_id, status in plans
            ],
        )

    async def _list_fix_pull_requests(self, context: _RestoreContext) -> DemoStepResult:
        async with self._session.begin():
            runs = await self._repository.search_apply_runs_since(
                context.organization_id, context.snapshot.taken_at
            )
            urls = [
                (run.id, str((run.result or {}).get(PULL_REQUEST_URL_KEY)))
                for run in runs
                if (run.result or {}).get(PULL_REQUEST_URL_KEY)
            ]
        return DemoStepResult(
            DemoStep.AI_FIX_PULL_REQUESTS,
            [
                _item(
                    DemoAction.CLOSE_PULL_REQUEST,
                    DemoStepStatus.MANUAL_REQUIRED,
                    ("agent_run", run_id, url),
                    PULL_REQUEST_OPENED,
                )
                for run_id, url in urls
            ],
        )

    async def _compare_kept_settings(self, context: _RestoreContext) -> DemoStepResult:
        async with self._session.begin():
            organization = await self._get_organization(context.organization_id)
            current, _ = await self._read_state(organization)
        return DemoStepResult(
            DemoStep.KEPT_SETTINGS,
            compare_kept_settings(context.snapshot.state, current),
            empty_status=DemoStepStatus.KEPT,
        )

    # --- 공통 ---

    async def _call(
        self,
        context: _RestoreContext,
        action: DemoAction,
        target: tuple[str, uuid.UUID, str | None],
        call: Callable[[], Awaitable[object]],
        *,
        done: DemoStepStatus,
        settled_errors: tuple[type[AppError], ...] = (),
        waiting_errors: tuple[type[AppError], ...] = (),
        detail: str | None = None,
    ) -> DemoItemResult:
        """기존 서비스 함수 하나를 부른다(dryRun 이면 부르지 않는다). 거절은 그 코드로 돌려준다."""
        if context.dry_run:
            return _item(action, DemoStepStatus.WOULD_DO, target, detail=detail)
        try:
            await call()
        except settled_errors as error:
            return _item(action, DemoStepStatus.OK, target, error.code, detail)
        except waiting_errors as error:
            return _item(action, DemoStepStatus.WAITING, target, error.code, detail)
        except AppError as error:
            return _item(action, DemoStepStatus.FAILED, target, error.code, detail)
        return _item(action, done, target, detail=detail)

    async def _open_plans(self, organization_id: uuid.UUID) -> list[_OpenPlan]:
        async with self._session.begin():
            plans = await self._repository.search_open_plans(organization_id)
            connections = await self._repository.search_live_connections(
                {plan.service_id for plan in plans}
            )
            environment_by_connection = {item.id: item.environment_id for item in connections}
            return [
                _OpenPlan(
                    plan_id=plan.id,
                    service_id=plan.service_id,
                    status=MigrationPlanStatus(plan.status),
                    abort_requested=plan.abort_requested_at is not None,
                    target_environment_id=environment_by_connection.get(
                        plan.target_service_environment_id
                    ),
                )
                for plan in plans
            ]

    async def _live_connections(self, service_id: uuid.UUID) -> list[_LiveConnection] | None:
        """서비스가 없으면(지워짐) None."""
        async with self._session.begin():
            connections = await self._repository.search_live_connections([service_id])
            if not connections:
                services = await self._repository.search_live_services_by_ids([service_id])
                if not services:
                    return None
            digests = {
                item.id: item.image_digest
                for item in await self._repository.search_deployments(
                    [c.current_deployment_id for c in connections if c.current_deployment_id]
                )
            }
            return [
                _LiveConnection(
                    service_environment_id=item.id,
                    environment_id=item.environment_id,
                    traffic_weight=item.traffic_weight,
                    current_deployment_id=item.current_deployment_id,
                    image_digest=(
                        digests.get(item.current_deployment_id)
                        if item.current_deployment_id is not None
                        else None
                    ),
                )
                for item in connections
            ]

    async def _get_organization(self, organization_id: uuid.UUID) -> Organization:
        organization = await self._organizations.find_by_id(organization_id)
        if organization is None:
            raise OrganizationNotFoundError
        return organization


def compare_kept_settings(
    snapshot: DemoSnapshotState, current: DemoSnapshotState
) -> list[DemoItemResult]:
    """되돌리지 않는 항목(환경·보안 예외·조직 설정) 중 스냅샷과 달라진 것.

    모두 같으면 한 줄(UNCHANGED)."""
    items: list[DemoItemResult] = []
    before_envs = {item.environment_id: item for item in snapshot.environments}
    after_envs = {item.environment_id: item for item in current.environments}
    for environment_id in sorted(before_envs.keys() | after_envs.keys(), key=str):
        before, after = before_envs.get(environment_id), after_envs.get(environment_id)
        shown_environment = after or before
        label = shown_environment.name if shown_environment is not None else None
        if before is None:
            code = "ENVIRONMENT_ADDED"
        elif after is None:
            code = "ENVIRONMENT_REMOVED"
        elif before.connection_status != after.connection_status:
            code = "ENVIRONMENT_CONNECTION_STATUS_CHANGED"
        else:
            continue
        items.append(
            _item(
                DemoAction.COMPARE,
                DemoStepStatus.KEPT,
                ("environment", environment_id, label),
                code,
            )
        )
    before_exceptions = {item.exception_id: item for item in snapshot.security_exceptions}
    after_exceptions = {item.exception_id: item for item in current.security_exceptions}
    for exception_id in sorted(before_exceptions.keys() | after_exceptions.keys(), key=str):
        old, new = before_exceptions.get(exception_id), after_exceptions.get(exception_id)
        shown_exception = new or old
        label = shown_exception.control_id if shown_exception is not None else None
        if old is None:
            code = "SECURITY_EXCEPTION_ADDED"
        elif new is None:
            code = "SECURITY_EXCEPTION_REMOVED"
        elif old != new:
            code = "SECURITY_EXCEPTION_CHANGED"
        else:
            continue
        items.append(
            _item(
                DemoAction.COMPARE,
                DemoStepStatus.KEPT,
                ("security_exception", exception_id, label),
                code,
            )
        )
    if snapshot.allow_self_approval != current.allow_self_approval:
        items.append(
            DemoItemResult(
                DemoAction.COMPARE,
                DemoStepStatus.KEPT,
                target_type="organization",
                code="ALLOW_SELF_APPROVAL_CHANGED",
                detail=f"allowSelfApproval={current.allow_self_approval}",
            )
        )
    if not items:
        items.append(DemoItemResult(DemoAction.COMPARE, DemoStepStatus.KEPT, code=UNCHANGED))
    return items


def _demo_app_data_step(state: DemoSnapshotState) -> DemoStepResult:
    """데모 앱 순번 쓰기 표: 앱에 reset 엔드포인트가 없고 WAS 는 앱 DB 에 붙지 않는다."""
    return DemoStepResult(
        DemoStep.DEMO_APP_DATA,
        [
            _item(
                DemoAction.RESET_APP_DATA,
                DemoStepStatus.MANUAL_REQUIRED,
                ("service", service.service_id, service.name),
                APP_RESET_ENDPOINT_MISSING,
            )
            for service in state.services
            if service.serving_environment_id is not None
        ],
    )


def _serving_by_service(state: DemoSnapshotState) -> dict[uuid.UUID, uuid.UUID]:
    return {
        service.service_id: service.serving_environment_id
        for service in state.services
        if service.serving_environment_id is not None
    }


def _same_image(
    snapshot: SnapshotConnection, live_by_connection: dict[uuid.UUID, _LiveConnection]
) -> bool:
    live = live_by_connection.get(snapshot.service_environment_id)
    if live is None or live.current_deployment_id is None:
        return False
    if live.current_deployment_id == snapshot.current_deployment_id:
        return True
    return snapshot.image_digest is not None and live.image_digest == snapshot.image_digest


def _failback_command(target_environment_id: uuid.UUID) -> CreateMigrationPlanCommand:
    """계획 기본값 그대로(재난 긴급 계획과 같은 값). DB 쓰기 전환은 넣지 않는다."""
    return CreateMigrationPlanCommand(
        target_environment_id=target_environment_id,
        required_requests=None,
        max_wait_seconds=None,
        max_error_rate_percent=DEFAULT_MAX_ERROR_RATE_PERCENT,
        max_p95_ms=DEFAULT_MAX_P95_MS,
        max_replication_lag_seconds=DEFAULT_MAX_REPLICATION_LAG_SECONDS,
    )


def _connection_target(
    service: SnapshotService, connection: SnapshotConnection
) -> tuple[str, uuid.UUID, str | None]:
    return (
        "service_environment",
        connection.service_environment_id,
        f"{service.name}@{connection.environment_name}",
    )


def _item(
    action: DemoAction,
    status: DemoStepStatus,
    target: tuple[str, uuid.UUID, str | None] | None = None,
    code: str | None = None,
    detail: str | None = None,
) -> DemoItemResult:
    return DemoItemResult(
        action=action,
        status=status,
        target_type=target[0] if target else None,
        target_id=target[1] if target else None,
        label=target[2] if target else None,
        code=code,
        detail=detail,
    )


def _snapshot_view(audit_log: AuditLog) -> DemoSnapshotView:
    return DemoSnapshotView(
        snapshot_id=audit_log.id,
        taken_at=audit_log.created_at,
        taken_by_user_id=audit_log.actor_user_id,
        state=state_from_json(audit_log.details[SNAPSHOT_DETAILS_KEY]),
    )
