"""이전 계획 만들기·조회·승인·거절·취소·중단 유스케이스. 실행은 일꾼(MigrationPlanProcessor)이 한다.

계획·서비스·환경은 모두 경로의 조직 안에서만 찾는다(계획은 서비스의 조직으로 거른다).

잠금 순서(배포 접수와 같은 하나): 환경(만들기만, FOR SHARE) → 서비스 → 그 서비스의 연결(id 오름차순)
→ 이전 계획 → 할 일. 상태는 조건부 UPDATE 로만 바꾸고, 실행 로그 줄은 반영됐을 때만 같은
트랜잭션에서 쓴다.

만료(EXPIRED)·낡음(STALE) 전환은 트랜잭션 안에서 적고 커밋한 뒤 트랜잭션 밖에서 409 를 올린다
(안에서 올리면 전환과 로그가 되돌려진다). 자기 승인 403 은 상태를 바꾸지 않으므로 안에서 올린다.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import ExecutorMode
from app.core.exceptions import (
    DeployInProgressError,
    EnvironmentUnhealthyError,
    ErrorField,
    MigrationPlanNotFoundError,
    MigrationPlanOpenError,
    OrganizationNotFoundError,
    PlanExpiredError,
    PlanNotPendingError,
    PlanNotRunningError,
    PlanStaleError,
    RequestFieldInvalidError,
    SelfApprovalForbiddenError,
    ServiceDeletingError,
    SimulateNotAllowedError,
    SourceNotSingleError,
    TargetNotReadyError,
    TargetSyncingError,
)
from app.enums import (
    DeployRequestStatus,
    DeployRequestTrigger,
    EnvironmentConnectionStatus,
    JobKind,
    JobStatus,
    MigrationLogCode,
    MigrationPlanMode,
    MigrationPlanStatus,
    MigrationSimulateReason,
    ServiceStatus,
)
from app.models import (
    Environment,
    Job,
    MigrationPlan,
    MigrationPlanLog,
    Service,
    ServiceEnvironment,
    User,
)
from app.models.migration_plan import OPEN_PLAN_UNIQUE_INDEX
from app.repositories.deploy_request_repository import DeployRequestRepository
from app.repositories.deployment_repository import DeploymentRepository
from app.repositories.environment_repository import EnvironmentRepository
from app.repositories.job_repository import JobRepository
from app.repositories.migration_plan_log_repository import MigrationPlanLogRepository
from app.repositories.migration_plan_repository import MigrationPlanRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.repositories.service_repository import ServiceRepository
from app.repositories.user_repository import UserRepository
from app.services.deploy_request_service import INVALID_CURSOR_REASON
from app.services.integrity import violated_constraint_name
from app.services.migration_plan_log import MigrationPlanLogWriter
from app.services.migration_plan_support import (
    CONDITION_MAX_ERROR_RATE,
    CONDITION_MAX_P95_MS,
    CONDITION_MAX_REPLICATION_LAG,
    SHIFT_STEPS,
    SIMULATE_FAIL_AT_STEP,
    SIMULATE_REASON,
    STEP_REQUIRED_REQUESTS,
    build_steps,
    expected_seconds,
    fingerprint_changes,
    initial_step_results,
    read_fingerprint,
    skip_pending_steps,
)

logger = logging.getLogger(__name__)

SAME_ENVIRONMENT_REASON = "SAME_ENVIRONMENT"
NO_DEPLOYMENT_REASON = "NO_DEPLOYMENT"
VERSION_MISMATCH_REASON = "VERSION_MISMATCH"
LAST_SYNC_FAILED_REASON = "LAST_SYNC_FAILED"
CHANGED_REASON = "CHANGED"


class _Outcome(StrEnum):
    """트랜잭션을 커밋한 뒤 밖에서 올릴 거절."""

    EXPIRED = "EXPIRED"
    STALE = "STALE"


@dataclass(frozen=True)
class CreateMigrationPlanCommand:
    target_environment_id: uuid.UUID
    # 비율 단계(10·50·100%) 순 필요 건수 3개와 단계 최대 대기(초). None 이면 설정 기본값.
    required_requests: Sequence[int] | None
    max_wait_seconds: int | None
    max_error_rate_percent: float
    max_p95_ms: int
    max_replication_lag_seconds: float
    simulate_fail_at_step: int | None = None
    simulate_reason: MigrationSimulateReason | None = None


@dataclass(frozen=True)
class MigrationPlanView:
    plan: MigrationPlan
    users_by_id: dict[uuid.UUID, User]
    environment_id_by_connection: dict[uuid.UUID, uuid.UUID]

    def user(self, user_id: uuid.UUID | None) -> User | None:
        return self.users_by_id.get(user_id) if user_id is not None else None

    @property
    def source_environment_id(self) -> uuid.UUID:
        return self.environment_id_by_connection[self.plan.source_service_environment_id]

    @property
    def target_environment_id(self) -> uuid.UUID:
        return self.environment_id_by_connection[self.plan.target_service_environment_id]


@dataclass(frozen=True)
class MigrationPlanSearchResult:
    items: list[MigrationPlanView]
    total: int


@dataclass(frozen=True)
class MigrationLogPageView:
    simulated: bool
    lines: list[MigrationPlanLog] = field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


@dataclass(frozen=True)
class _Locked:
    service: Service
    connections: list[ServiceEnvironment]
    plan: MigrationPlan


class MigrationPlanService:
    def __init__(
        self,
        session: AsyncSession,
        clock: Clock,
        executor_mode: ExecutorMode,
        required_requests_default: Sequence[int],
        max_wait_seconds_default: int,
        expected_rps: int,
        step_overhead_seconds: int,
        approval_ttl_seconds: int,
    ) -> None:
        self._session = session
        self._clock = clock
        self._executor_mode = executor_mode
        self._required_requests_default = tuple(required_requests_default)
        self._max_wait_seconds_default = max_wait_seconds_default
        self._expected_rps = expected_rps
        self._step_overhead_seconds = step_overhead_seconds
        self._approval_ttl = timedelta(seconds=approval_ttl_seconds)
        self._services = ServiceRepository(session)
        self._environments = EnvironmentRepository(session)
        self._connections = ServiceEnvironmentRepository(session)
        self._deploy_requests = DeployRequestRepository(session)
        self._deployments = DeploymentRepository(session)
        self._plans = MigrationPlanRepository(session)
        self._logs = MigrationPlanLogRepository(session)
        self._jobs = JobRepository(session)
        self._organizations = OrganizationRepository(session)
        self._users = UserRepository(session)
        self._log = MigrationPlanLogWriter(session)

    # --- 만들기 ---

    async def create_plan(
        self,
        organization_id: uuid.UUID,
        service_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        command: CreateMigrationPlanCommand,
    ) -> MigrationPlanView:
        """출발(지금 100% 연결) → 도착(같은 이미지의 예비 연결) 계획을 PENDING_APPROVAL 로 만든다.

        거절 순서: 서비스·환경(404·409) → 열린 계획 → 출발 → 같은 환경 → 열린 배포 요청 → 도착.
        """
        if (
            command.simulate_fail_at_step is not None
            and self._executor_mode is not ExecutorMode.FAKE
        ):
            raise SimulateNotAllowedError("simulate is only accepted with fake executors")

        async with self._session.begin():
            service = await self._services.get_live_by_id(organization_id, service_id)
            _check_service_accepts(service)
            environment = await self._environments.get_live_by_id(
                organization_id, command.target_environment_id
            )
            _check_environment_accepts(environment)

        now = self._clock.now()
        async with self._session.begin():
            environment = await self._environments.get_live_by_id(
                organization_id, command.target_environment_id, for_share=True
            )
            _check_environment_accepts(environment)
            service = await self._services.get_live_by_id(organization_id, service_id, lock=True)
            _check_service_accepts(service)
            connections = await self._connections.lock_live_by_service(service.id)
            await self._expire_pending_for_service(service.id, now)
            open_plan = await self._plans.find_open_for_service(service.id)
            if open_plan is not None:
                raise MigrationPlanOpenError(str(open_plan.id))
            source = _single_source(connections)
            target = next(
                (item for item in connections if item.environment_id == environment.id), None
            )
            if target is not None and target.id == source.id:
                raise RequestFieldInvalidError("targetEnvironmentId", SAME_ENVIRONMENT_REASON)
            await self._check_no_open_deploy_requests(service.id, target)
            target = await self._check_target_ready(source, target)
            plan = self._new_plan(service, source, target, actor_user_id, command, now)
            # 서비스 행을 잠근 뒤라 판정 사이에 다른 계획이 끝나거나 생기지 않는다.
            plan.mode = decide_plan_mode(
                await self._plans.find_last_succeeded_for_service(service.id), source, target
            )
            plan.fingerprint = await read_fingerprint(
                self._session, service, connections, source.id, target.id
            )
            await self._insert(plan)
            await self._log.append(
                plan,
                MigrationLogCode.PLAN_CREATED,
                now,
                data={
                    "expectedSeconds": plan.expected_seconds,
                    "requiredRequests": [
                        plan.steps[index][STEP_REQUIRED_REQUESTS] for index in SHIFT_STEPS
                    ],
                    "maxWaitSeconds": plan.observe_seconds,
                    "mode": plan.mode,
                },
            )
            view = (await self._views([plan]))[0]
        _log_plan("migration plan created", "create_migration_plan", plan)
        return view

    def _new_plan(
        self,
        service: Service,
        source: ServiceEnvironment,
        target: ServiceEnvironment,
        actor_user_id: uuid.UUID,
        command: CreateMigrationPlanCommand,
        now: datetime,
    ) -> MigrationPlan:
        required_requests = tuple(command.required_requests or self._required_requests_default)
        max_wait_seconds = command.max_wait_seconds or self._max_wait_seconds_default
        steps = build_steps(
            required_requests, max_wait_seconds, self._expected_rps, self._step_overhead_seconds
        )
        simulate = (
            {
                SIMULATE_FAIL_AT_STEP: command.simulate_fail_at_step,
                SIMULATE_REASON: str(command.simulate_reason),
            }
            if command.simulate_fail_at_step is not None
            else None
        )
        return MigrationPlan(
            id=uuid.uuid4(),
            service_id=service.id,
            source_service_environment_id=source.id,
            target_service_environment_id=target.id,
            status=MigrationPlanStatus.PENDING_APPROVAL,
            steps=steps,
            step_results=initial_step_results(steps),
            rollback_conditions={
                CONDITION_MAX_ERROR_RATE: command.max_error_rate_percent,
                CONDITION_MAX_P95_MS: command.max_p95_ms,
                CONDITION_MAX_REPLICATION_LAG: command.max_replication_lag_seconds,
            },
            # 8-1 부터 이 칸에는 단계 최대 대기(maxWaitSeconds)를 적는다.
            observe_seconds=max_wait_seconds,
            expected_seconds=expected_seconds(steps),
            fingerprint={},
            simulate=simulate,
            simulated=self._executor_mode is ExecutorMode.FAKE,
            current_step=0,
            rollback_attempts=0,
            created_by_user_id=actor_user_id,
            self_approved=False,
            expires_at=now + self._approval_ttl,
            created_at=now,
            updated_at=now,
        )

    async def _insert(self, plan: MigrationPlan) -> None:
        """부분 unique 위반(동시 생성)이면 먼저 들어간 계획을 담아 409 MIGRATION_PLAN_OPEN."""
        try:
            async with self._session.begin_nested():
                self._plans.add(plan)
                await self._session.flush()
        except IntegrityError as exc:
            if violated_constraint_name(exc) != OPEN_PLAN_UNIQUE_INDEX:
                raise
            existing = await self._plans.find_open_for_service(plan.service_id)
            raise MigrationPlanOpenError(str(existing.id) if existing else None) from None

    async def _check_no_open_deploy_requests(
        self, service_id: uuid.UUID, target: ServiceEnvironment | None
    ) -> None:
        """서비스의 어느 연결이든 열린 배포 요청이 있으면 거절한다.

        그래서 계획을 만든 순간 열린 요청 목록은 늘 비어 있다.
        """
        open_requests = await self._deploy_requests.search_open_by_service(service_id)
        syncing = next(
            (
                request
                for request in open_requests
                if target is not None
                and request.service_environment_id == target.id
                and request.trigger == DeployRequestTrigger.STANDBY_SYNC
            ),
            None,
        )
        if syncing is not None:
            raise TargetSyncingError(
                "target standby is still following the serving version",
                details=[
                    ErrorField(
                        field="deployRequestId", reason="TARGET_SYNCING", value=str(syncing.id)
                    )
                ],
            )
        if open_requests:
            raise DeployInProgressError(
                "service has an open deploy request",
                details=[
                    ErrorField(
                        field="deployRequestId",
                        reason="DEPLOY_IN_PROGRESS",
                        value=str(open_requests[0].id),
                    )
                ],
            )

    async def _check_target_ready(
        self, source: ServiceEnvironment, target: ServiceEnvironment | None
    ) -> ServiceEnvironment:
        """도착 연결에 출발과 같은 이미지가 올라가 있어야 한다."""
        if target is None or target.current_deployment_id is None:
            raise TargetNotReadyError(
                "target environment has no deployment",
                details=[ErrorField(field="targetEnvironmentId", reason=NO_DEPLOYMENT_REASON)],
            )
        assert source.current_deployment_id is not None
        source_deployment = await self._deployments.find_by_id(source.current_deployment_id)
        target_deployment = await self._deployments.find_by_id(target.current_deployment_id)
        assert source_deployment is not None and target_deployment is not None
        if source_deployment.image_digest == target_deployment.image_digest:
            return target
        details = [ErrorField(field="targetEnvironmentId", reason=VERSION_MISMATCH_REASON)]
        latest = await self._deploy_requests.search_latest_standby_sync_by_connections([target.id])
        if latest and latest[0].status == DeployRequestStatus.FAILED and latest[0].error_code:
            details.append(
                ErrorField(
                    field="lastSyncError",
                    reason=LAST_SYNC_FAILED_REASON,
                    value=str(latest[0].error_code),
                )
            )
        raise TargetNotReadyError("target environment runs a different image", details=details)

    # --- 조회 ---

    async def search_plans(
        self,
        organization_id: uuid.UUID,
        service_id: uuid.UUID,
        statuses: Sequence[MigrationPlanStatus],
        page: int,
        size: int,
    ) -> MigrationPlanSearchResult:
        async with self._session.begin():
            await self._services.get_live_by_id(organization_id, service_id)
            plans = await self._plans.search_by_service(
                organization_id, service_id, statuses, offset=page * size, limit=size
            )
            total = await self._plans.count_by_service(organization_id, service_id, statuses)
            views = await self._views(plans)
        return MigrationPlanSearchResult(items=views, total=total)

    async def get_plan(self, organization_id: uuid.UUID, plan_id: uuid.UUID) -> MigrationPlanView:
        async with self._session.begin():
            plan = await self._get_in_organization(organization_id, plan_id)
            return (await self._views([plan]))[0]

    async def get_plan_logs(
        self, organization_id: uuid.UUID, plan_id: uuid.UUID, cursor: str | None, limit: int
    ) -> MigrationLogPageView:
        """cursor = 마지막으로 받은 seq. 계획 행의 simulated 를 싣는다."""
        after_seq = _parse_seq_cursor(cursor)
        async with self._session.begin():
            plan = await self._get_in_organization(organization_id, plan_id)
            rows = await self._logs.search_after(plan.id, after_seq, limit + 1)
        page = rows[:limit]
        return MigrationLogPageView(
            simulated=plan.simulated,
            lines=page,
            next_cursor=str(page[-1].seq) if page else cursor,
            has_more=len(rows) > limit,
        )

    # --- 승인·거절·취소 ---

    async def approve_plan(
        self, organization_id: uuid.UUID, plan_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> MigrationPlanView:
        """승인 = 실행 시작. 할 일을 넣고 APPROVED 로 바꾼다. 지문이 다르면 STALE 로 끝낸다."""
        async with self._session.begin():
            await self._get_in_organization(organization_id, plan_id)
        now = self._clock.now()
        rejection: tuple[_Outcome, list[str]] | None = None
        async with self._session.begin():
            organization = await self._organizations.find_by_id(organization_id)
            if organization is None:
                raise OrganizationNotFoundError
            locked = await self._lock(plan_id)
            plan = locked.plan
            self._check_pending(plan)
            is_self = plan.created_by_user_id == actor_user_id
            if is_self and not organization.allow_self_approval:
                raise SelfApprovalForbiddenError
            if await self._expire_if_due(plan, now):
                rejection = (_Outcome.EXPIRED, [])
            else:
                current = await read_fingerprint(
                    self._session,
                    locked.service,
                    locked.connections,
                    plan.source_service_environment_id,
                    plan.target_service_environment_id,
                )
                changed = fingerprint_changes(plan.fingerprint, current)
                if changed:
                    await self._mark_stale(plan, changed, now)
                    rejection = (_Outcome.STALE, changed)
                else:
                    await self._approve(plan, actor_user_id, is_self, now)
            view = await self._reload_view(plan_id)
        _raise_rejection(rejection)
        _log_plan("migration plan approved", "approve_migration_plan", view.plan)
        return view

    async def _approve(
        self, plan: MigrationPlan, actor_user_id: uuid.UUID, is_self: bool, now: datetime
    ) -> None:
        if not await self._plans.transition(
            plan.id,
            MigrationPlanStatus.PENDING_APPROVAL,
            {
                "status": MigrationPlanStatus.APPROVED,
                "approved_by_user_id": actor_user_id,
                "approved_at": now,
                "self_approved": is_self,
                "updated_at": now,
            },
        ):
            raise PlanNotPendingError("plan is not pending approval", plan_id=str(plan.id))
        self._jobs.add(
            Job(
                kind=JobKind.MIGRATION_PLAN,
                subject_id=plan.id,
                status=JobStatus.QUEUED,
                run_after=now,
                created_at=now,
                updated_at=now,
            )
        )
        await self._session.flush()
        await self._log.append(
            plan,
            MigrationLogCode.SELF_APPROVED if is_self else MigrationLogCode.APPROVED,
            now,
        )

    async def reject_plan(
        self, organization_id: uuid.UUID, plan_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> MigrationPlanView:
        """만든 사람의 거절도 자기 승인 스위치를 따른다.

        막혀 있으면 403 이고, 만든 사람은 취소를 쓴다.
        """
        async with self._session.begin():
            await self._get_in_organization(organization_id, plan_id)
        now = self._clock.now()
        rejection: tuple[_Outcome, list[str]] | None = None
        async with self._session.begin():
            organization = await self._organizations.find_by_id(organization_id)
            if organization is None:
                raise OrganizationNotFoundError
            locked = await self._lock(plan_id)
            plan = locked.plan
            self._check_pending(plan)
            if plan.created_by_user_id == actor_user_id and not organization.allow_self_approval:
                raise SelfApprovalForbiddenError
            if await self._expire_if_due(plan, now):
                rejection = (_Outcome.EXPIRED, [])
            else:
                await self._close_pending(
                    plan,
                    MigrationPlanStatus.REJECTED,
                    {"rejected_by_user_id": actor_user_id},
                    MigrationLogCode.REJECTED,
                    now,
                )
            view = await self._reload_view(plan_id)
        _raise_rejection(rejection)
        _log_plan("migration plan rejected", "reject_migration_plan", view.plan)
        return view

    async def cancel_plan(
        self, organization_id: uuid.UUID, plan_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> MigrationPlanView:
        async with self._session.begin():
            await self._get_in_organization(organization_id, plan_id)
        now = self._clock.now()
        rejection: tuple[_Outcome, list[str]] | None = None
        async with self._session.begin():
            locked = await self._lock(plan_id)
            plan = locked.plan
            self._check_pending(plan)
            if await self._expire_if_due(plan, now):
                rejection = (_Outcome.EXPIRED, [])
            else:
                await self._close_pending(
                    plan,
                    MigrationPlanStatus.CANCELLED,
                    {"cancelled_by_user_id": actor_user_id},
                    MigrationLogCode.CANCELLED,
                    now,
                )
            view = await self._reload_view(plan_id)
        _raise_rejection(rejection)
        _log_plan("migration plan cancelled", "cancel_migration_plan", view.plan)
        return view

    # --- 중단 ---

    async def abort_plan(
        self, organization_id: uuid.UUID, plan_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> MigrationPlanView:
        """중단을 적고 할 일을 지금으로 당긴다. 상태 전환(롤백·취소)은 일꾼이 한다.

        이미 중단을 받은 계획이면 아무것도 바꾸지 않는다(멱등). 열린 할 일이 없으면(비정상)
        APPROVED 는 그 자리에서 CANCELLED, RUNNING 은 할 일을 새로 넣는다.
        """
        async with self._session.begin():
            await self._get_in_organization(organization_id, plan_id)
        now = self._clock.now()
        async with self._session.begin():
            locked = await self._lock(plan_id)
            plan = locked.plan
            if plan.abort_requested_at is not None:
                return await self._reload_view(plan_id)
            status = MigrationPlanStatus(plan.status)
            if status not in (MigrationPlanStatus.APPROVED, MigrationPlanStatus.RUNNING):
                raise PlanNotRunningError("plan is not running", plan_id=str(plan.id))
            if await self._plans.transition(
                plan.id,
                status,
                {
                    "abort_requested_at": now,
                    "aborted_by_user_id": actor_user_id,
                    "updated_at": now,
                },
                [MigrationPlan.abort_requested_at.is_(None)],
            ):
                await self._log.append(plan, MigrationLogCode.ABORT_REQUESTED, now)
                if await self._jobs.pull_open_for_subject(plan.id, now) == 0:
                    await self._recover_missing_job(plan, status, actor_user_id, now)
            view = await self._reload_view(plan_id)
        _log_plan("migration abort requested", "abort_migration_plan", view.plan)
        return view

    async def _recover_missing_job(
        self,
        plan: MigrationPlan,
        status: MigrationPlanStatus,
        actor_user_id: uuid.UUID,
        now: datetime,
    ) -> None:
        if status is MigrationPlanStatus.APPROVED:
            if await self._plans.transition(
                plan.id,
                MigrationPlanStatus.APPROVED,
                {
                    "status": MigrationPlanStatus.CANCELLED,
                    "cancelled_by_user_id": actor_user_id,
                    "step_results": skip_pending_steps(plan.step_results),
                    "finished_at": now,
                    "updated_at": now,
                },
            ):
                await self._log.append(plan, MigrationLogCode.CANCELLED, now)
            return
        self._jobs.add(
            Job(
                kind=JobKind.MIGRATION_PLAN,
                subject_id=plan.id,
                status=JobStatus.QUEUED,
                run_after=now,
                created_at=now,
                updated_at=now,
            )
        )
        await self._session.flush()

    # --- 공통 ---

    async def _lock(self, plan_id: uuid.UUID) -> _Locked:
        """서비스 → 연결 → 계획 순서로 잠근다."""
        snapshot = await self._plans.find_by_id(plan_id)
        assert snapshot is not None
        service = await self._services.find_by_id(snapshot.service_id, lock=True)
        assert service is not None
        connections = await self._connections.lock_live_by_service(service.id)
        plan = await self._plans.find_by_id(plan_id, lock=True)
        assert plan is not None
        return _Locked(service=service, connections=connections, plan=plan)

    def _check_pending(self, plan: MigrationPlan) -> None:
        if plan.status != MigrationPlanStatus.PENDING_APPROVAL:
            raise PlanNotPendingError("plan is not pending approval", plan_id=str(plan.id))

    async def _expire_pending_for_service(self, service_id: uuid.UUID, now: datetime) -> None:
        plan = await self._plans.find_open_for_service(service_id)
        if plan is not None and plan.status == MigrationPlanStatus.PENDING_APPROVAL:
            await self._expire_if_due(plan, now)

    async def _expire_if_due(self, plan: MigrationPlan, now: datetime) -> bool:
        """승인 대기 시간이 지났으면 EXPIRED 로 끝낸다. 지났으면 True."""
        if plan.expires_at > now:
            return False
        await self._close_pending(
            plan, MigrationPlanStatus.EXPIRED, {}, MigrationLogCode.EXPIRED, now
        )
        return True

    async def _mark_stale(self, plan: MigrationPlan, changed: list[str], now: datetime) -> None:
        await self._close_pending(
            plan,
            MigrationPlanStatus.STALE,
            {},
            MigrationLogCode.STALE,
            now,
            data={"changed": changed},
        )

    async def _close_pending(
        self,
        plan: MigrationPlan,
        status: MigrationPlanStatus,
        values: dict[str, Any],
        code: MigrationLogCode,
        now: datetime,
        data: dict[str, Any] | None = None,
    ) -> None:
        if await self._plans.transition(
            plan.id,
            MigrationPlanStatus.PENDING_APPROVAL,
            {
                **values,
                "status": status,
                "step_results": skip_pending_steps(plan.step_results),
                "finished_at": now,
                "updated_at": now,
            },
        ):
            await self._log.append(plan, code, now, data=data)

    async def _get_in_organization(
        self, organization_id: uuid.UUID, plan_id: uuid.UUID
    ) -> MigrationPlan:
        plan = await self._plans.find_in_organization(organization_id, plan_id)
        if plan is None:
            raise MigrationPlanNotFoundError("migration plan not found", plan_id=str(plan_id))
        return plan

    async def _reload_view(self, plan_id: uuid.UUID) -> MigrationPlanView:
        plan = await self._plans.find_by_id(plan_id)
        assert plan is not None
        return (await self._views([plan]))[0]

    async def _views(self, plans: list[MigrationPlan]) -> list[MigrationPlanView]:
        """계획 수와 상관없이 쿼리 2번(사용자·연결)으로 사람 칸과 환경 id 를 채운다."""
        user_ids = {
            user_id
            for plan in plans
            for user_id in (
                plan.created_by_user_id,
                plan.approved_by_user_id,
                plan.rejected_by_user_id,
                plan.cancelled_by_user_id,
                plan.aborted_by_user_id,
            )
            if user_id is not None
        }
        users_by_id = {user.id: user for user in await self._users.search_by_ids(user_ids)}
        connection_ids = {
            connection_id
            for plan in plans
            for connection_id in (
                plan.source_service_environment_id,
                plan.target_service_environment_id,
            )
        }
        environment_id_by_connection = await self._connections.search_environment_ids(
            connection_ids
        )
        return [
            MigrationPlanView(
                plan=plan,
                users_by_id=users_by_id,
                environment_id_by_connection=environment_id_by_connection,
            )
            for plan in plans
        ]


def decide_plan_mode(
    last_succeeded: MigrationPlan | None,
    source: ServiceEnvironment,
    target: ServiceEnvironment,
) -> MigrationPlanMode:
    """새 계획이 가장 늦게 끝난 성공 계획을 거꾸로 하는 복귀(FAILBACK)인지 판정한다.

    성공한 계획만 본다(롤백·실패·취소된 계획은 트래픽을 옮기지 않았다). 그 계획이 이미 복귀였으면
    다시 거꾸로 하는 계획은 복귀가 아니라 새 이전(PLANNED)이다.
    """
    if (
        last_succeeded is not None
        and last_succeeded.source_service_environment_id == target.id
        and last_succeeded.target_service_environment_id == source.id
        and last_succeeded.mode != MigrationPlanMode.FAILBACK
    ):
        return MigrationPlanMode.FAILBACK
    return MigrationPlanMode.PLANNED


def _single_source(connections: list[ServiceEnvironment]) -> ServiceEnvironment:
    serving = [item for item in connections if item.traffic_weight > 0]
    if len(serving) != 1 or serving[0].traffic_weight != 100:
        raise SourceNotSingleError("service must be served by exactly one environment at 100%")
    source = serving[0]
    if source.current_deployment_id is None:
        raise SourceNotSingleError("serving environment has no deployment")
    return source


def _check_service_accepts(service: Service) -> None:
    if service.status == ServiceStatus.DELETING:
        raise ServiceDeletingError("service is being deleted", service_id=str(service.id))


def _check_environment_accepts(environment: Environment) -> None:
    if environment.connection_status == EnvironmentConnectionStatus.FAILED:
        raise EnvironmentUnhealthyError(
            "environment connection check failed", environment_id=str(environment.id)
        )


def _raise_rejection(rejection: tuple[_Outcome, list[str]] | None) -> None:
    if rejection is None:
        return
    outcome, changed = rejection
    if outcome is _Outcome.EXPIRED:
        raise PlanExpiredError("migration plan expired before approval")
    raise PlanStaleError(
        "service or environments changed after the plan was made",
        details=[ErrorField(field=name, reason=CHANGED_REASON) for name in changed],
    )


def _parse_seq_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not (cursor.isascii() and cursor.isdigit()):
        raise RequestFieldInvalidError("cursor", INVALID_CURSOR_REASON)
    return int(cursor)


def _log_plan(message: str, action: str, plan: MigrationPlan) -> None:
    logger.info(
        message,
        extra={
            "action": action,
            "migration_plan_id": str(plan.id),
            "service_id": str(plan.service_id),
            "status": plan.status,
        },
    )
