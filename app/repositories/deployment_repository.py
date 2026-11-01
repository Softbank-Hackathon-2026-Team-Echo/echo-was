import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import DeploymentStatus
from app.models import Deployment
from app.repositories.conditional_update import update_where


class DeploymentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, deployment: Deployment) -> None:
        self._session.add(deployment)

    async def find_by_id(self, deployment_id: uuid.UUID) -> Deployment | None:
        statement = (
            select(Deployment)
            .where(Deployment.id == deployment_id)
            .execution_options(populate_existing=True)
        )
        return await self._session.scalar(statement)

    async def search_by_ids(self, deployment_ids: Sequence[uuid.UUID]) -> list[Deployment]:
        if not deployment_ids:
            return []
        statement = select(Deployment).where(Deployment.id.in_(deployment_ids))
        return list((await self._session.scalars(statement)).all())

    async def transition(
        self, deployment_id: uuid.UUID, expected: DeploymentStatus, values: dict[str, Any]
    ) -> bool:
        return await update_where(
            self._session, Deployment, deployment_id, [Deployment.status == expected], values
        )
