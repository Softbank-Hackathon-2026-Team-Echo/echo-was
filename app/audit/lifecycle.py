"""배포 요청·이전 계획의 대상 값과 끝 상태 SYSTEM 줄.

끝 상태를 저장하는 트랜잭션 안에서, 조건부 UPDATE 가 반영됐을 때만 부른다. 끝 상태는 되감지 않으므로
대상마다 끝 줄은 하나다. 행의 status 는 조건부 UPDATE 뒤에도 옛 값이라 끝 상태를 인수로 받는다.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.actions import AuditAction, AuditComponent, AuditTargetType
from app.audit.recorder import AuditActor, AuditRecorder, AuditTarget
from app.core.clock import Clock
from app.enums import (
    AuditResult,
    DeployRequestCancelReason,
    DeployRequestErrorCode,
    DeployRequestStatus,
    DeployRequestTrigger,
    MigrationErrorCode,
    MigrationPlanStatus,
)
from app.models import DeployRequest, Environment, MigrationPlan, Service, ServiceEnvironment

COMMIT_LABEL_LENGTH = 7


async def deploy_request_target(session: AsyncSession, request: DeployRequest) -> AuditTarget:
    """라벨: `서비스 · 환경 · 커밋 7자`."""
    service = await session.get_one(Service, request.service_id)
    environment = await session.get_one(Environment, request.environment_id)
    commit = request.commit_sha[:COMMIT_LABEL_LENGTH]
    return AuditTarget(
        AuditTargetType.DEPLOY_REQUEST,
        request.id,
        f"{service.name} · {environment.name} · {commit}",
    )


async def migration_plan_target(session: AsyncSession, plan: MigrationPlan) -> AuditTarget:
    """라벨: `서비스 · 출발 → 도착`."""
    service = await session.get_one(Service, plan.service_id)
    rows = await session.execute(
        select(ServiceEnvironment.id, Environment.name)
        .join(Environment, Environment.id == ServiceEnvironment.environment_id)
        .where(
            ServiceEnvironment.id.in_(
                (plan.source_service_environment_id, plan.target_service_environment_id)
            )
        )
    )
    names: dict[uuid.UUID, str] = {row[0]: row[1] for row in rows}
    source = names[plan.source_service_environment_id]
    target = names[plan.target_service_environment_id]
    return AuditTarget(
        AuditTargetType.MIGRATION_PLAN, plan.id, f"{service.name} · {source} → {target}"
    )


async def record_deploy_request_finished(
    session: AsyncSession,
    clock: Clock,
    request: DeployRequest,
    *,
    status: DeployRequestStatus,
    component: AuditComponent,
    cancel_reason: DeployRequestCancelReason | None = None,
    error_code: DeployRequestErrorCode | None = None,
) -> None:
    service = await session.get_one(Service, request.service_id)
    trigger = DeployRequestTrigger(request.trigger)
    details: dict[str, Any] = {"status": status.value, "trigger": trigger.value}
    if cancel_reason is not None:
        details["cancelReason"] = cancel_reason.value
    if error_code is not None:
        details["errorCode"] = error_code.value
    if request.simulated:
        details["simulated"] = True
    AuditRecorder(session, clock).record(
        organization_id=service.organization_id,
        actor=AuditActor.system(component),
        action=(
            AuditAction.ROLLBACK_FINISHED
            if trigger is DeployRequestTrigger.ROLLBACK
            else AuditAction.DEPLOY_FINISHED
        ),
        result=(
            AuditResult.SUCCEEDED if status is DeployRequestStatus.SUCCEEDED else AuditResult.FAILED
        ),
        target=await deploy_request_target(session, request),
        details=details,
    )


async def record_migration_plan_finished(
    session: AsyncSession,
    clock: Clock,
    plan: MigrationPlan,
    *,
    status: MigrationPlanStatus,
    component: AuditComponent,
    error_code: MigrationErrorCode | str | None = None,
) -> None:
    service = await session.get_one(Service, plan.service_id)
    details: dict[str, Any] = {"status": status.value}
    if error_code is not None:
        details["errorCode"] = str(error_code)
    if plan.simulated:
        details["simulated"] = True
    AuditRecorder(session, clock).record(
        organization_id=service.organization_id,
        actor=AuditActor.system(component),
        action=AuditAction.MIGRATION_FINISHED,
        result=(
            AuditResult.SUCCEEDED if status is MigrationPlanStatus.SUCCEEDED else AuditResult.FAILED
        ),
        target=await migration_plan_target(session, plan),
        details=details,
    )
