"""환경 자동 관측의 짧은 저장 트랜잭션을 위한 조회. commit은 서비스가 맡는다."""

import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import BuildStatus
from app.models import Build, DeployRequest, Environment, Finding, Service, ServiceEnvironment


class EnvironmentEvidenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_environment(
        self, organization_id: uuid.UUID, environment_id: uuid.UUID, *, lock: bool = False
    ) -> Environment | None:
        query = select(Environment).where(
            Environment.id == environment_id,
            Environment.organization_id == organization_id,
            Environment.is_deleted.is_(False),
        )
        return await self._session.scalar(query.with_for_update() if lock else query)

    async def retire_previous(
        self,
        organization_id: uuid.UUID,
        environment_id: uuid.UUID,
        collection_key: str,
        policy_version: str,
        started_at: datetime,
    ) -> bool:
        # 호출자는 같은 환경 행의 FOR UPDATE 잠금을 이미 보유한다.
        conditions = (
            Finding.organization_id == organization_id,
            Finding.environment_id == environment_id,
            Finding.collection_key == collection_key,
            Finding.policy_version == policy_version,
        )
        newest = await self._session.scalar(
            select(func.max(Finding.collection_started_at)).where(*conditions)
        )
        if newest is not None and newest >= started_at:
            return False
        await self._session.execute(
            update(Finding)
            .where(*conditions, Finding.superseded_at.is_(None))
            .values(superseded_at=started_at, updated_at=started_at)
        )
        return True

    async def current_builds(self, organization_id: uuid.UUID) -> list[uuid.UUID]:
        query = (
            select(Build.id)
            .join(DeployRequest, DeployRequest.build_id == Build.id)
            .join(
                ServiceEnvironment,
                ServiceEnvironment.current_deployment_id == DeployRequest.deployment_id,
            )
            .join(Service, Service.id == ServiceEnvironment.service_id)
            .where(
                Service.organization_id == organization_id,
                Service.is_deleted.is_(False),
                ServiceEnvironment.is_deleted.is_(False),
                Build.status == BuildStatus.SUCCEEDED,
                Build.simulated.is_(False),
            )
            .distinct()
        )
        return list(await self._session.scalars(query))
