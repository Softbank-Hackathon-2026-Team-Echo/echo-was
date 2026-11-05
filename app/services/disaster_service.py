"""재난 미리보기·만들기·조회·종료와 긴급 이전 트리거(API 서버 9).

만들기(`declare_disaster`) 한 요청의 흐름
1. (트랜잭션) 조직 환경 → 반경·피해/안전/모름 → disasters 행 + 감사 DISASTER_DECLARED. 커밋한다.
   뒤 단계가 실패해도 재난 행은 남는다.
2. 기준 미달이면 계획 없이 끝난다.
3. 피해 환경에서 서비스 중인 서비스마다(id 오름차순) 서로 독립으로
   도착 고르기 → MigrationPlanService.create_plan(EMERGENCY) → 조직이 허용하면 approve_plan.
   두 함수는 각자 트랜잭션을 열므로 여기서 트랜잭션을 열어 둔 채 부르지 않는다.
4. 건너뛴 서비스가 있으면 감사 줄을 하나 더 쓴다(건너뜀은 다른 곳에 저장되지 않는다).

실패한 트랜잭션의 롤백은 세션 객체를 전부 만료시킨다. 그래서 서비스 처리 사이에는 ORM 객체를 들고
있지 않고 값만 넘긴다(비동기 세션에서 만료된 객체를 읽으면 실패한다).
"""

import logging
import uuid
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import InvalidEnvironmentConfigError, UnknownEnvironmentKindError
from app.audit.actions import AuditAction, AuditTargetType
from app.audit.recorder import AuditActor, AuditRecorder, AuditTarget
from app.core.clock import Clock
from app.core.exceptions import (
    AppError,
    DisasterNotActiveError,
    DisasterNotFoundError,
    MigrationPlanNotFoundError,
    SelfApprovalForbiddenError,
)
from app.core.places import Coordinates
from app.enums import (
    DisasterPlanOutcome,
    DisasterPlanReason,
    DisasterSource,
    DisasterStatus,
    DisasterType,
    EnvironmentConnectionStatus,
    MigrationPlanStatus,
)
from app.models import Disaster, Environment, MigrationPlan, ServiceEnvironment, User
from app.repositories.deployment_repository import DeploymentRepository
from app.repositories.disaster_repository import DisasterRepository
from app.repositories.environment_repository import EnvironmentRepository
from app.repositories.migration_plan_repository import MigrationPlanRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.repositories.user_repository import UserRepository
from app.services.disaster_rules import (
    DISTANCE_LOG_DIGITS,
    EnvironmentClassification,
    EnvironmentLocation,
    TargetCandidate,
    choose_target,
    classify_environments,
    distance_km,
    meets_threshold,
    radius_km,
    summarize_plan,
)
from app.services.migration_plan_service import (
    CreateMigrationPlanCommand,
    DisasterTrigger,
    MigrationPlanService,
)
from app.services.migration_plan_support import (
    DEFAULT_MAX_ERROR_RATE_PERCENT,
    DEFAULT_MAX_P95_MS,
    DEFAULT_MAX_REPLICATION_LAG_SECONDS,
    single_source,
)

logger = logging.getLogger(__name__)

MIGRATION_PLAN_OPEN_REASON = "MIGRATION_PLAN_OPEN"
INTERNAL_ERROR_REASON = "INTERNAL_ERROR"
# 진입점 가중치 API 가 아는 환경 이름. 환경 종류와 같고, 그 밖은 진입점 매핑 설정에 있어야 한다.
ENTRYPOINT_ENVIRONMENT_KINDS = frozenset({"aws", "gcp"})


@dataclass(frozen=True)
class DisasterInput:
    type: DisasterType
    magnitude: Decimal
    epicenter: Coordinates


@dataclass(frozen=True)
class DisasterPreview:
    radius_km: int
    meets_threshold: bool
    classification: EnvironmentClassification


@dataclass(frozen=True)
class DisasterPlanResult:
    """plans[] 한 줄. plan_status 는 계획이 있을 때 그 순간의 상태다."""

    service_id: uuid.UUID
    plan_id: uuid.UUID | None
    outcome: DisasterPlanOutcome
    reason: str | None
    target_environment_id: uuid.UUID | None
    plan_status: MigrationPlanStatus | None


@dataclass(frozen=True)
class DisasterView:
    disaster: Disaster
    users_by_id: dict[uuid.UUID, User]
    plans: list[DisasterPlanResult]

    def user(self, user_id: uuid.UUID | None) -> User | None:
        return self.users_by_id.get(user_id) if user_id is not None else None


@dataclass(frozen=True)
class DisasterSearchResult:
    items: list[DisasterView]
    total: int


@dataclass(frozen=True)
class _EnvironmentFacts:
    kind: str
    coordinates: Coordinates | None
    is_connection_failed: bool


@dataclass(frozen=True)
class _TriggerContext:
    """서비스 처리 내내 바뀌지 않는 값(ORM 객체 없음)."""

    organization_id: uuid.UUID
    actor_user_id: uuid.UUID
    disaster_id: uuid.UUID
    magnitude: float
    epicenter: Coordinates
    environments: dict[uuid.UUID, _EnvironmentFacts]
    affected_environment_ids: frozenset[uuid.UUID]
    # 이 재난의 안전 환경에서 복귀를 막는 다른 재난(ACTIVE·기준 넘음)의 피해 환경을 뺀 것.
    safe_environment_ids: frozenset[uuid.UUID]
    allow_self_approval: bool


@dataclass(frozen=True)
class _TargetChoice:
    environment_id: uuid.UUID
    source_distance_km: float


class DisasterService:
    def __init__(
        self,
        session: AsyncSession,
        clock: Clock,
        migration_plans: MigrationPlanService,
        entrypoint_environment_keys: Collection[str],
    ) -> None:
        self._session = session
        self._clock = clock
        self._migration_plans = migration_plans
        self._entrypoint_environment_keys = frozenset(entrypoint_environment_keys)
        self._disasters = DisasterRepository(session)
        self._environments = EnvironmentRepository(session)
        self._connections = ServiceEnvironmentRepository(session)
        self._deployments = DeploymentRepository(session)
        self._plans = MigrationPlanRepository(session)
        self._organizations = OrganizationRepository(session)
        self._users = UserRepository(session)
        self._audit = AuditRecorder(session, clock)

    # --- 미리보기 ---

    async def preview_disaster(
        self, organization_id: uuid.UUID, disaster: DisasterInput
    ) -> DisasterPreview:
        """저장하지 않는다."""
        async with self._session.begin():
            environments = await self._environments.search_all_live(organization_id)
            radius = radius_km(disaster.magnitude)
            return DisasterPreview(
                radius_km=radius,
                meets_threshold=meets_threshold(disaster.type, disaster.magnitude),
                classification=classify_environments(
                    _locations(environments), disaster.epicenter, radius
                ),
            )

    # --- 만들기·트리거 ---

    async def declare_disaster(
        self, organization_id: uuid.UUID, actor_user_id: uuid.UUID, command: DisasterInput
    ) -> DisasterView:
        """재난을 저장하고, 기준을 넘으면 피해 서비스마다 긴급 이전 계획을 만든다."""
        now = self._clock.now()
        radius = radius_km(command.magnitude)
        is_over_threshold = meets_threshold(command.type, command.magnitude)
        async with self._session.begin():
            environments = await self._environments.search_all_live(organization_id)
            facts = {environment.id: _facts(environment) for environment in environments}
            classification = classify_environments(
                _locations(environments), command.epicenter, radius
            )
            disaster = Disaster(
                organization_id=organization_id,
                type=command.type,
                source=DisasterSource.SIMULATED,
                magnitude=command.magnitude,
                epicenter_lat=command.epicenter.lat,
                epicenter_lng=command.epicenter.lng,
                radius_km=radius,
                meets_threshold=is_over_threshold,
                status=DisasterStatus.ACTIVE,
                affected_environment_ids=_id_strings(classification.affected),
                safe_environment_ids=_id_strings(classification.safe),
                unknown_environment_ids=_id_strings(classification.unknown),
                created_by_user_id=actor_user_id,
                started_at=now,
                created_at=now,
                updated_at=now,
            )
            self._disasters.add(disaster)
            await self._session.flush()
            disaster_id = disaster.id
            self._record_audit(
                AuditAction.DISASTER_DECLARED,
                disaster,
                actor_user_id,
                {
                    "type": str(command.type),
                    "magnitude": float(command.magnitude),
                    "radiusKm": radius,
                    "meetsThreshold": is_over_threshold,
                    "affectedEnvironmentIds": disaster.affected_environment_ids,
                },
            )
        _log_disaster("disaster declared", "declare_disaster", disaster_id, organization_id)

        results: list[DisasterPlanResult] = []
        if is_over_threshold:
            results = await self._trigger_plans(
                organization_id, actor_user_id, disaster_id, command, facts, classification
            )
            await self._record_skipped(organization_id, actor_user_id, disaster_id, results)
        async with self._session.begin():
            disaster = await self._get_in_organization(organization_id, disaster_id)
            users_by_id = await self._users_by_id([disaster])
            return DisasterView(disaster, users_by_id, results)

    async def _trigger_plans(
        self,
        organization_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        disaster_id: uuid.UUID,
        command: DisasterInput,
        facts: dict[uuid.UUID, _EnvironmentFacts],
        classification: EnvironmentClassification,
    ) -> list[DisasterPlanResult]:
        if not classification.affected:
            return []
        async with self._session.begin():
            service_ids = await self._connections.search_serving_service_ids(
                organization_id, classification.affected
            )
            blocking_disasters = await self._disasters.search_blocking_by_organization(
                organization_id
            )
            blocked_environment_ids = {
                uuid.UUID(environment_id)
                for blocking in blocking_disasters
                for environment_id in blocking.affected_environment_ids
            }
            organization = await self._organizations.find_by_id(organization_id)
            allow_self_approval = organization is not None and organization.allow_self_approval
        context = _TriggerContext(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            disaster_id=disaster_id,
            magnitude=float(command.magnitude),
            epicenter=command.epicenter,
            environments=facts,
            affected_environment_ids=frozenset(classification.affected),
            safe_environment_ids=frozenset(classification.safe) - blocked_environment_ids,
            allow_self_approval=allow_self_approval,
        )
        return [await self._process_service(context, service_id) for service_id in service_ids]

    async def _process_service(
        self, context: _TriggerContext, service_id: uuid.UUID
    ) -> DisasterPlanResult:
        """한 서비스의 실패는 그 서비스의 결과만 바꾼다."""
        try:
            return await self._create_and_approve(context, service_id)
        except AppError as exc:
            return _skipped(service_id, exc.code)
        except Exception:
            logger.exception(
                "disaster plan creation failed",
                extra={
                    "action": "trigger_disaster_plan",
                    "disaster_id": str(context.disaster_id),
                    "service_id": str(service_id),
                },
            )
            return _skipped(service_id, INTERNAL_ERROR_REASON)

    async def _create_and_approve(
        self, context: _TriggerContext, service_id: uuid.UUID
    ) -> DisasterPlanResult:
        choice = await self._choose_target(context, service_id)
        if isinstance(choice, DisasterPlanResult):
            return choice
        view = await self._migration_plans.create_plan(
            context.organization_id,
            service_id,
            context.actor_user_id,
            _emergency_command(choice.environment_id),
            DisasterTrigger(
                disaster_id=context.disaster_id,
                magnitude=context.magnitude,
                distance_km=choice.source_distance_km,
            ),
        )
        plan_id = view.plan.id
        if not context.allow_self_approval:
            return DisasterPlanResult(
                service_id=service_id,
                plan_id=plan_id,
                outcome=DisasterPlanOutcome.PENDING_APPROVAL,
                reason=DisasterPlanReason.SELF_APPROVAL_DISABLED,
                target_environment_id=choice.environment_id,
                plan_status=MigrationPlanStatus.PENDING_APPROVAL,
            )
        return await self._approve(context, service_id, plan_id, choice.environment_id)

    async def _approve(
        self,
        context: _TriggerContext,
        service_id: uuid.UUID,
        plan_id: uuid.UUID,
        target_environment_id: uuid.UUID,
    ) -> DisasterPlanResult:
        """승인에 실패해도 계획은 남는다. 실패 뒤에는 계획을 다시 읽어 지금 상태를 돌려준다."""
        reason: str | None = None
        try:
            await self._migration_plans.approve_plan(
                context.organization_id, plan_id, context.actor_user_id
            )
        except SelfApprovalForbiddenError:
            # 트리거가 스위치를 읽은 뒤 어드민이 끈 경우.
            reason = DisasterPlanReason.SELF_APPROVAL_DISABLED
        except AppError as exc:
            reason = exc.code
        except Exception:
            logger.exception(
                "disaster plan approval failed",
                extra={
                    "action": "trigger_disaster_plan",
                    "disaster_id": str(context.disaster_id),
                    "migration_plan_id": str(plan_id),
                },
            )
            reason = INTERNAL_ERROR_REASON
        async with self._session.begin():
            plan = await self._plans.find_by_id(plan_id)
            if plan is None:
                raise MigrationPlanNotFoundError("migration plan not found", plan_id=str(plan_id))
            status = MigrationPlanStatus(plan.status)
            is_approved = plan.approved_at is not None
        return DisasterPlanResult(
            service_id=service_id,
            plan_id=plan_id,
            outcome=summarize_plan(status, is_approved),
            reason=reason,
            target_environment_id=target_environment_id,
            plan_status=status,
        )

    async def _choose_target(
        self, context: _TriggerContext, service_id: uuid.UUID
    ) -> _TargetChoice | DisasterPlanResult:
        """잠그지 않고 읽어 도착을 고른다. 값은 create_plan 이 잠금 안에서 다시 검사한다.

        시간이 지난 승인 대기 계획은 열린 계획으로 보지 않는다(create_plan 이 만료시킨다).
        """
        now = self._clock.now()
        async with self._session.begin():
            open_plan = await self._plans.find_open_for_service(service_id)
            if open_plan is not None and not _is_overdue_pending(open_plan, now):
                return DisasterPlanResult(
                    service_id=service_id,
                    plan_id=open_plan.id,
                    outcome=DisasterPlanOutcome.SKIPPED,
                    reason=MIGRATION_PLAN_OPEN_REASON,
                    target_environment_id=None,
                    plan_status=MigrationPlanStatus(open_plan.status),
                )
            connections = await self._connections.search_live_by_service(service_id)
            source = single_source(connections)
            # 서비스 목록을 읽은 뒤 트래픽이 다른 환경으로 옮겨 갔으면 이 재난의 대상이 아니다.
            if source.environment_id not in context.affected_environment_ids:
                return _skipped(service_id, DisasterPlanReason.SOURCE_NOT_AFFECTED)
            source_coordinates = context.environments[source.environment_id].coordinates
            assert source_coordinates is not None
            source_digest = await self._image_digest(source)
            candidates = await self._target_candidates(context, connections)
        target = choose_target(candidates, source_digest, context.epicenter)
        if target is None:
            return _skipped(service_id, DisasterPlanReason.NO_SAFE_TARGET)
        return _TargetChoice(
            environment_id=target.environment_id,
            source_distance_km=round(
                distance_km(source_coordinates, context.epicenter), DISTANCE_LOG_DIGITS
            ),
        )

    async def _target_candidates(
        self, context: _TriggerContext, connections: Iterable[ServiceEnvironment]
    ) -> list[TargetCandidate]:
        """안전 환경에 있는 예비 연결(비율 0, 현재 배포 있음)."""
        candidates: list[TargetCandidate] = []
        for connection in connections:
            environment_id = connection.environment_id
            if (
                connection.traffic_weight > 0
                or connection.current_deployment_id is None
                or environment_id not in context.safe_environment_ids
            ):
                continue
            facts = context.environments[environment_id]
            assert facts.coordinates is not None
            candidates.append(
                TargetCandidate(
                    environment_id=environment_id,
                    coordinates=facts.coordinates,
                    image_digest=await self._image_digest(connection),
                    is_connection_failed=facts.is_connection_failed,
                    has_entrypoint_name=self._has_entrypoint_name(environment_id, facts.kind),
                )
            )
        return candidates

    def _has_entrypoint_name(self, environment_id: uuid.UUID, kind: str) -> bool:
        return (
            kind in ENTRYPOINT_ENVIRONMENT_KINDS
            or kind in self._entrypoint_environment_keys
            or str(environment_id) in self._entrypoint_environment_keys
        )

    async def _image_digest(self, connection: ServiceEnvironment) -> str:
        assert connection.current_deployment_id is not None
        deployment = await self._deployments.find_by_id(connection.current_deployment_id)
        assert deployment is not None
        return deployment.image_digest

    async def _record_skipped(
        self,
        organization_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        disaster_id: uuid.UUID,
        results: Sequence[DisasterPlanResult],
    ) -> None:
        """건너뜀은 저장하지 않으므로 나중에 다시 볼 수 있게 감사 줄 details 에 남긴다."""
        skipped = [
            {
                "serviceId": str(result.service_id),
                "planId": str(result.plan_id) if result.plan_id is not None else None,
                "reason": result.reason,
            }
            for result in results
            if result.outcome is DisasterPlanOutcome.SKIPPED
        ]
        if not skipped:
            return
        async with self._session.begin():
            disaster = await self._get_in_organization(organization_id, disaster_id)
            self._record_audit(
                AuditAction.DISASTER_DECLARED, disaster, actor_user_id, {"skippedPlans": skipped}
            )

    # --- 조회 ---

    async def search_disasters(
        self,
        organization_id: uuid.UUID,
        status: DisasterStatus | None,
        page: int,
        size: int,
    ) -> DisasterSearchResult:
        async with self._session.begin():
            disasters = await self._disasters.search_by_organization(
                organization_id, status, offset=page * size, limit=size
            )
            total = await self._disasters.count_by_organization(organization_id, status)
            return DisasterSearchResult(items=await self._views(disasters), total=total)

    async def get_disaster(
        self, organization_id: uuid.UUID, disaster_id: uuid.UUID
    ) -> DisasterView:
        async with self._session.begin():
            disaster = await self._get_in_organization(organization_id, disaster_id)
            return (await self._views([disaster]))[0]

    # --- 종료 ---

    async def end_disaster(
        self, organization_id: uuid.UUID, disaster_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> DisasterView:
        """재난을 끝낸다(복귀 잠금이 풀린다). 진행 중인 이전 계획은 건드리지 않는다."""
        now = self._clock.now()
        async with self._session.begin():
            disaster = await self._get_in_organization(organization_id, disaster_id)
            if not await self._disasters.end(
                disaster_id, {"ended_at": now, "ended_by_user_id": actor_user_id, "updated_at": now}
            ):
                raise DisasterNotActiveError(str(disaster_id))
            self._record_audit(AuditAction.DISASTER_ENDED, disaster, actor_user_id, None)
            disaster = await self._get_in_organization(organization_id, disaster_id)
            view = (await self._views([disaster]))[0]
        _log_disaster("disaster ended", "end_disaster", disaster_id, organization_id)
        return view

    # --- 공통 ---

    def _record_audit(
        self,
        action: AuditAction,
        disaster: Disaster,
        actor_user_id: uuid.UUID,
        details: dict[str, Any] | None,
    ) -> None:
        self._audit.record(
            organization_id=disaster.organization_id,
            actor=AuditActor.user(actor_user_id),
            action=action,
            target=AuditTarget(
                AuditTargetType.DISASTER, disaster.id, f"{disaster.type} M{disaster.magnitude:.1f}"
            ),
            details=details,
        )

    async def _get_in_organization(
        self, organization_id: uuid.UUID, disaster_id: uuid.UUID
    ) -> Disaster:
        disaster = await self._disasters.find_in_organization(organization_id, disaster_id)
        if disaster is None:
            raise DisasterNotFoundError(str(disaster_id))
        return disaster

    async def _users_by_id(self, disasters: Iterable[Disaster]) -> dict[uuid.UUID, User]:
        user_ids = {
            user_id
            for disaster in disasters
            for user_id in (disaster.created_by_user_id, disaster.ended_by_user_id)
            if user_id is not None
        }
        return {user.id: user for user in await self._users.search_by_ids(user_ids)}

    async def _views(self, disasters: list[Disaster]) -> list[DisasterView]:
        """조회의 plans[] 는 disaster_id 로 찾은 계획이다(건너뛴 서비스는 저장돼 있지 않다)."""
        users_by_id = await self._users_by_id(disasters)
        plans = await self._plans.search_by_disaster_ids([item.id for item in disasters])
        connection_ids = {plan.target_service_environment_id for plan in plans}
        environment_id_by_connection = await self._connections.search_environment_ids(
            connection_ids
        )
        results_by_disaster: dict[uuid.UUID, list[DisasterPlanResult]] = {}
        for plan in plans:
            assert plan.disaster_id is not None
            results_by_disaster.setdefault(plan.disaster_id, []).append(
                _result_from_plan(plan, environment_id_by_connection)
            )
        return [
            DisasterView(disaster, users_by_id, results_by_disaster.get(disaster.id, []))
            for disaster in disasters
        ]


def _facts(environment: Environment) -> _EnvironmentFacts:
    return _EnvironmentFacts(
        kind=environment.kind,
        coordinates=_coordinates(environment),
        is_connection_failed=(environment.connection_status == EnvironmentConnectionStatus.FAILED),
    )


def _coordinates(environment: Environment) -> Coordinates | None:
    """저장된 config 를 지금 양식으로 읽지 못하는 환경은 위치를 모르는 환경과 같다."""
    try:
        return environment.parsed_config().coordinates()
    except (InvalidEnvironmentConfigError, UnknownEnvironmentKindError):
        return None


def _locations(environments: Iterable[Environment]) -> list[EnvironmentLocation]:
    return [
        EnvironmentLocation(environment.id, _coordinates(environment))
        for environment in environments
    ]


def _id_strings(ids: Iterable[uuid.UUID]) -> list[str]:
    return [str(item) for item in ids]


def _is_overdue_pending(plan: MigrationPlan, now: datetime) -> bool:
    return plan.status == MigrationPlanStatus.PENDING_APPROVAL and plan.expires_at <= now


def _emergency_command(target_environment_id: uuid.UUID) -> CreateMigrationPlanCommand:
    """계획 기본값 그대로다. 재난이라고 단계를 빨리 넘기지 않는다."""
    return CreateMigrationPlanCommand(
        target_environment_id=target_environment_id,
        required_requests=None,
        max_wait_seconds=None,
        max_error_rate_percent=DEFAULT_MAX_ERROR_RATE_PERCENT,
        max_p95_ms=DEFAULT_MAX_P95_MS,
        max_replication_lag_seconds=DEFAULT_MAX_REPLICATION_LAG_SECONDS,
    )


def _skipped(service_id: uuid.UUID, reason: str) -> DisasterPlanResult:
    return DisasterPlanResult(
        service_id=service_id,
        plan_id=None,
        outcome=DisasterPlanOutcome.SKIPPED,
        reason=reason,
        target_environment_id=None,
        plan_status=None,
    )


def _result_from_plan(
    plan: MigrationPlan, environment_id_by_connection: dict[uuid.UUID, uuid.UUID]
) -> DisasterPlanResult:
    status = MigrationPlanStatus(plan.status)
    outcome = summarize_plan(status, plan.approved_at is not None)
    return DisasterPlanResult(
        service_id=plan.service_id,
        plan_id=plan.id,
        outcome=outcome,
        # 승인 없이 닫힌 계획은 그 상태가 이유다. 조회에는 만들 때의 이유가 저장돼 있지 않다.
        reason=str(status) if outcome is DisasterPlanOutcome.SKIPPED else None,
        target_environment_id=environment_id_by_connection.get(plan.target_service_environment_id),
        plan_status=status,
    )


def _log_disaster(
    message: str, action: str, disaster_id: uuid.UUID, organization_id: uuid.UUID
) -> None:
    logger.info(
        message,
        extra={
            "action": action,
            "disaster_id": str(disaster_id),
            "organization_id": str(organization_id),
        },
    )
