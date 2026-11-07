"""시연 조직 스냅샷·복귀가 읽는 조회(조직 조건 필수).

쓰기는 없다(스냅샷 줄은 AuditRecorder 가 쓴다).

복귀는 같은 세션으로 다른 서비스 함수를 부른 뒤 다시 읽으므로, 모든 조회는 populate_existing 으로
세션에 남은 옛 객체 값을 덮어쓴다(조건부 UPDATE 는 메모리의 객체를 바꾸지 않는다).
"""

import uuid
from collections.abc import Collection
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.audit.actions import AuditAction
from app.enums import (
    OPEN_MIGRATION_PLAN_STATUSES,
    AgentRunKind,
    AgentRunStatus,
    AuditResult,
    DeployRequestStatus,
    DisasterStatus,
)
from app.models import (
    AgentPatchApproval,
    AgentRun,
    AuditLog,
    Deployment,
    DeployRequest,
    Disaster,
    MigrationPlan,
    SecurityException,
    Service,
    ServiceEnvironment,
)
from app.models.agent_run import OPEN_STATUSES as OPEN_RUN_STATUSES

_OPEN_DEPLOY_REQUEST_STATUSES = [status for status in DeployRequestStatus if not status.is_terminal]


class DemoSnapshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_latest_snapshot(
        self, organization_id: uuid.UUID, details_key: str
    ) -> AuditLog | None:
        """성공(SUCCEEDED)이고 details 에 스냅샷이 든 가장 늦은 DEMO_SNAPSHOT 줄.

        같은 action 이름으로 거부(DENIED) 줄도 쌓인다(권한 없는 POST /demo/snapshot, details 빈 값).
        그 줄을 고르면 정상 스냅샷이 가려지므로 결과·스냅샷 존재로 거른다.
        """
        return await self._session.scalar(
            select(AuditLog)
            .where(
                AuditLog.organization_id == organization_id,
                AuditLog.action == AuditAction.DEMO_SNAPSHOT.value,
                AuditLog.result == AuditResult.SUCCEEDED.value,
                AuditLog.details.has_key(details_key),
            )
            .order_by(AuditLog.seq.desc())
            .limit(1)
        )

    async def find_plan(self, plan_id: uuid.UUID) -> MigrationPlan | None:
        statement = (
            select(MigrationPlan)
            .where(MigrationPlan.id == plan_id)
            .execution_options(populate_existing=True)
        )
        return await self._session.scalar(statement)

    async def search_live_services(self, organization_id: uuid.UUID) -> list[Service]:
        statement = (
            select(Service)
            .where(Service.organization_id == organization_id, Service.is_deleted.is_(False))
            .order_by(Service.created_at, Service.id)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())

    async def search_live_services_by_ids(
        self, service_ids: Collection[uuid.UUID]
    ) -> list[Service]:
        if not service_ids:
            return []
        statement = (
            select(Service)
            .where(Service.id.in_(list(service_ids)), Service.is_deleted.is_(False))
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())

    async def search_live_connections(
        self, service_ids: Collection[uuid.UUID]
    ) -> list[ServiceEnvironment]:
        """환경 행을 함께 읽는다. 만든 순서."""
        if not service_ids:
            return []
        statement = (
            select(ServiceEnvironment)
            .where(
                ServiceEnvironment.service_id.in_(list(service_ids)),
                ServiceEnvironment.is_deleted.is_(False),
            )
            .options(selectinload(ServiceEnvironment.environment))
            .order_by(ServiceEnvironment.created_at, ServiceEnvironment.id)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())

    async def search_deployments(self, deployment_ids: Collection[uuid.UUID]) -> list[Deployment]:
        if not deployment_ids:
            return []
        statement = (
            select(Deployment)
            .where(Deployment.id.in_(list(deployment_ids)))
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())

    async def search_succeeded_requests_by_deployment(
        self, deployment_ids: Collection[uuid.UUID]
    ) -> list[DeployRequest]:
        """그 배포 기록을 만든 성공 요청(되돌리기 대상)."""
        if not deployment_ids:
            return []
        statement = (
            select(DeployRequest)
            .where(
                DeployRequest.deployment_id.in_(list(deployment_ids)),
                DeployRequest.status == DeployRequestStatus.SUCCEEDED,
            )
            .order_by(DeployRequest.created_at, DeployRequest.id)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())

    async def search_open_plans(self, organization_id: uuid.UUID) -> list[MigrationPlan]:
        statement = (
            select(MigrationPlan)
            .join(Service, Service.id == MigrationPlan.service_id)
            .where(
                Service.organization_id == organization_id,
                MigrationPlan.status.in_(list(OPEN_MIGRATION_PLAN_STATUSES)),
            )
            .order_by(MigrationPlan.created_at, MigrationPlan.id)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())

    async def search_plans_created_since(
        self, organization_id: uuid.UUID, since: datetime
    ) -> list[MigrationPlan]:
        statement = (
            select(MigrationPlan)
            .join(Service, Service.id == MigrationPlan.service_id)
            .where(Service.organization_id == organization_id, MigrationPlan.created_at >= since)
            .order_by(MigrationPlan.created_at, MigrationPlan.id)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())

    async def search_active_disasters(self, organization_id: uuid.UUID) -> list[Disaster]:
        statement = (
            select(Disaster)
            .where(
                Disaster.organization_id == organization_id,
                Disaster.status == DisasterStatus.ACTIVE,
            )
            .order_by(Disaster.created_at, Disaster.id)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())

    async def search_open_deploy_requests(self, organization_id: uuid.UUID) -> list[DeployRequest]:
        statement = (
            select(DeployRequest)
            .join(Service, Service.id == DeployRequest.service_id)
            .where(
                Service.organization_id == organization_id,
                DeployRequest.status.in_(_OPEN_DEPLOY_REQUEST_STATUSES),
            )
            .order_by(DeployRequest.created_at, DeployRequest.id)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())

    async def search_open_runs(self, organization_id: uuid.UUID) -> list[AgentRun]:
        statement = (
            select(AgentRun)
            .where(
                AgentRun.organization_id == organization_id,
                AgentRun.status.in_(OPEN_RUN_STATUSES),
            )
            .order_by(AgentRun.created_at, AgentRun.id)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())

    async def search_succeeded_patch_parents_since(
        self, organization_id: uuid.UUID, since: datetime
    ) -> list[AgentRun]:
        """스냅샷 뒤 끝난 사전 점검·배포 실패 진단(수정안이 있을 수 있는 실행)."""
        statement = (
            select(AgentRun)
            .where(
                AgentRun.organization_id == organization_id,
                AgentRun.kind.in_([AgentRunKind.PRECHECK, AgentRunKind.CHANGE_PROPOSAL]),
                AgentRun.status == AgentRunStatus.SUCCEEDED,
                AgentRun.finished_at >= since,
            )
            .order_by(AgentRun.finished_at, AgentRun.id)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())

    async def search_apply_runs_since(
        self, organization_id: uuid.UUID, since: datetime
    ) -> list[AgentRun]:
        statement = (
            select(AgentRun)
            .where(
                AgentRun.organization_id == organization_id,
                AgentRun.kind == AgentRunKind.APPLY_PATCH,
                AgentRun.created_at >= since,
            )
            .order_by(AgentRun.created_at, AgentRun.id)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())

    async def search_decided_patch_keys(
        self, run_ids: Collection[uuid.UUID]
    ) -> set[tuple[uuid.UUID, str]]:
        if not run_ids:
            return set()
        rows = await self._session.execute(
            select(AgentPatchApproval.run_id, AgentPatchApproval.patch_id).where(
                AgentPatchApproval.run_id.in_(list(run_ids))
            )
        )
        return {(run_id, patch_id) for run_id, patch_id in rows.all()}

    async def search_security_exceptions(
        self, organization_id: uuid.UUID
    ) -> list[SecurityException]:
        statement = (
            select(SecurityException)
            .where(SecurityException.organization_id == organization_id)
            .order_by(SecurityException.created_at, SecurityException.id)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())
