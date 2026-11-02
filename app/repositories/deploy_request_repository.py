import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import ColumnElement, Select, exists, func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import (
    ACTIVE_DEPLOY_REQUEST_STATUSES,
    DeployRequestStatus,
    DeployRequestTrigger,
)
from app.models import DeployRequest, Service
from app.repositories.conditional_update import update_where

_OPEN_STATUSES = (DeployRequestStatus.QUEUED, *ACTIVE_DEPLOY_REQUEST_STATUSES)


class DeployRequestRepository:
    """배포 요청 조회·저장. 상태는 transition(조건부 UPDATE)으로만 바꾼다."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, deploy_request: DeployRequest) -> None:
        self._session.add(deploy_request)

    async def find_by_id(
        self, deploy_request_id: uuid.UUID, *, lock: bool = False
    ) -> DeployRequest | None:
        statement = (
            select(DeployRequest)
            .where(DeployRequest.id == deploy_request_id)
            .execution_options(populate_existing=True)
        )
        if lock:
            statement = statement.with_for_update(key_share=True)
        return await self._session.scalar(statement)

    async def find_in_organization(
        self, organization_id: uuid.UUID, deploy_request_id: uuid.UUID
    ) -> DeployRequest | None:
        """배포 요청에는 조직 칸이 없어 서비스의 조직으로 거른다(소프트 삭제된 서비스 포함)."""
        statement = (
            select(DeployRequest)
            .join(Service, Service.id == DeployRequest.service_id)
            .where(
                DeployRequest.id == deploy_request_id, Service.organization_id == organization_id
            )
            .execution_options(populate_existing=True)
        )
        return await self._session.scalar(statement)

    async def transition(
        self,
        deploy_request_id: uuid.UUID,
        expected: DeployRequestStatus | Iterable[DeployRequestStatus],
        values: dict[str, Any],
    ) -> bool:
        """지금 상태가 expected 일 때만 values 를 적는다. 적었으면 True."""
        statuses = [expected] if isinstance(expected, DeployRequestStatus) else list(expected)
        return await update_where(
            self._session,
            DeployRequest,
            deploy_request_id,
            [DeployRequest.status.in_(statuses)],
            values,
        )

    async def update_if(
        self,
        deploy_request_id: uuid.UUID,
        conditions: list[ColumnElement[bool]],
        values: dict[str, Any],
    ) -> bool:
        return await update_where(
            self._session, DeployRequest, deploy_request_id, conditions, values
        )

    async def find_queued_for_connection(
        self, service_environment_id: uuid.UUID
    ) -> DeployRequest | None:
        return await self._session.scalar(
            select(DeployRequest)
            .where(
                DeployRequest.service_environment_id == service_environment_id,
                DeployRequest.status == DeployRequestStatus.QUEUED,
            )
            .execution_options(populate_existing=True)
        )

    async def find_active_for_connection(
        self, service_environment_id: uuid.UUID
    ) -> DeployRequest | None:
        return await self._session.scalar(
            select(DeployRequest)
            .where(
                DeployRequest.service_environment_id == service_environment_id,
                DeployRequest.status.in_(ACTIVE_DEPLOY_REQUEST_STATUSES),
            )
            .execution_options(populate_existing=True)
        )

    async def search_queued_by_service(self, service_id: uuid.UUID) -> list[DeployRequest]:
        statement = (
            select(DeployRequest)
            .where(
                DeployRequest.service_id == service_id,
                DeployRequest.status == DeployRequestStatus.QUEUED,
            )
            .order_by(DeployRequest.id)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())

    async def has_active_for_service(self, service_id: uuid.UUID) -> bool:
        statement = select(
            exists().where(
                DeployRequest.service_id == service_id,
                DeployRequest.status.in_(ACTIVE_DEPLOY_REQUEST_STATUSES),
            )
        )
        return bool(await self._session.scalar(statement))

    async def has_open_on_build(self, build_id: uuid.UUID, *, excluding: uuid.UUID) -> bool:
        """이 빌드를 기다리는 끝나지 않은 다른 요청이 있는가."""
        statement = select(
            exists().where(
                DeployRequest.build_id == build_id,
                DeployRequest.id != excluding,
                DeployRequest.status.in_(_OPEN_STATUSES),
            )
        )
        return bool(await self._session.scalar(statement))

    async def search_by_service(
        self,
        organization_id: uuid.UUID,
        service_id: uuid.UUID,
        environment_id: uuid.UUID | None,
        offset: int,
        limit: int,
    ) -> list[DeployRequest]:
        """최신순(created_at 내림차순, 같으면 id 내림차순)."""
        statement = (
            _filter_by_service(select(DeployRequest), organization_id, service_id, environment_id)
            .order_by(DeployRequest.created_at.desc(), DeployRequest.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list((await self._session.scalars(statement)).all())

    async def count_by_service(
        self, organization_id: uuid.UUID, service_id: uuid.UUID, environment_id: uuid.UUID | None
    ) -> int:
        statement = _filter_by_service(
            select(func.count()).select_from(DeployRequest),
            organization_id,
            service_id,
            environment_id,
        )
        return await self._session.scalar(statement) or 0

    async def search_open_by_connections(
        self, service_environment_ids: Sequence[uuid.UUID]
    ) -> list[DeployRequest]:
        """연결들의 대기·실행 중 요청(연결마다 최대 2개)."""
        if not service_environment_ids:
            return []
        statement = select(DeployRequest).where(
            DeployRequest.service_environment_id.in_(service_environment_ids),
            DeployRequest.status.in_(_OPEN_STATUSES),
        )
        return list((await self._session.scalars(statement)).all())

    async def search_latest_standby_sync_by_connections(
        self, service_environment_ids: Sequence[uuid.UUID]
    ) -> list[DeployRequest]:
        """연결마다 가장 최근 STANDBY_SYNC 요청 하나."""
        if not service_environment_ids:
            return []
        statement = (
            select(DeployRequest)
            .where(
                DeployRequest.service_environment_id.in_(service_environment_ids),
                DeployRequest.trigger == DeployRequestTrigger.STANDBY_SYNC,
            )
            .order_by(
                DeployRequest.service_environment_id,
                DeployRequest.created_at.desc(),
                DeployRequest.id.desc(),
            )
            .ext(distinct_on(DeployRequest.service_environment_id))
        )
        return list((await self._session.scalars(statement)).all())

    async def search_by_deployment_ids(
        self, deployment_ids: Sequence[uuid.UUID]
    ) -> list[DeployRequest]:
        if not deployment_ids:
            return []
        statement = select(DeployRequest).where(DeployRequest.deployment_id.in_(deployment_ids))
        return list((await self._session.scalars(statement)).all())


def _filter_by_service[*Ts](
    statement: Select[*Ts],
    organization_id: uuid.UUID,
    service_id: uuid.UUID,
    environment_id: uuid.UUID | None,
) -> Select[*Ts]:
    statement = statement.join(Service, Service.id == DeployRequest.service_id).where(
        DeployRequest.service_id == service_id, Service.organization_id == organization_id
    )
    if environment_id is not None:
        statement = statement.where(DeployRequest.environment_id == environment_id)
    return statement
