import uuid
from collections.abc import Iterable

from sqlalchemy import ColumnElement, exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Environment, Service, ServiceEnvironment


class ServiceEnvironmentRepository:
    """서비스-환경 연결 조회·저장. 잠금은 `FOR NO KEY UPDATE` 이고 id 오름차순으로 잡는다."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, service_environment: ServiceEnvironment) -> None:
        self._session.add(service_environment)

    async def find_by_id(self, service_environment_id: uuid.UUID) -> ServiceEnvironment | None:
        statement = (
            select(ServiceEnvironment)
            .where(ServiceEnvironment.id == service_environment_id)
            .options(selectinload(ServiceEnvironment.environment))
            .execution_options(populate_existing=True)
        )
        return await self._session.scalar(statement)

    async def lock_live_by_service(self, service_id: uuid.UUID) -> list[ServiceEnvironment]:
        """서비스의 살아 있는 연결을 id 오름차순으로 모두 잠근다(잠금 순서 규칙)."""
        statement = (
            select(ServiceEnvironment)
            .where(ServiceEnvironment.service_id == service_id, _is_live())
            .order_by(ServiceEnvironment.id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())

    async def lock_by_id(self, service_environment_id: uuid.UUID) -> ServiceEnvironment | None:
        statement = (
            select(ServiceEnvironment)
            .where(ServiceEnvironment.id == service_environment_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
        return await self._session.scalar(statement)

    async def search_live_by_service(self, service_id: uuid.UUID) -> list[ServiceEnvironment]:
        """서비스 상세용. 환경 행을 함께 읽는다(삭제된 환경도). 만든 순서."""
        statement = (
            select(ServiceEnvironment)
            .where(ServiceEnvironment.service_id == service_id, _is_live())
            .options(selectinload(ServiceEnvironment.environment))
            .order_by(ServiceEnvironment.created_at, ServiceEnvironment.id)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())

    async def search_environment_ids(
        self, service_environment_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, uuid.UUID]:
        """연결 id → 환경 id(지운 연결 포함)."""
        ids = set(service_environment_ids)
        if not ids:
            return {}
        rows = await self._session.execute(
            select(ServiceEnvironment.id, ServiceEnvironment.environment_id).where(
                ServiceEnvironment.id.in_(ids)
            )
        )
        return {connection_id: environment_id for connection_id, environment_id in rows.all()}

    async def search_live_current_by_environment_kind(self, kind: str) -> list[ServiceEnvironment]:
        """그 종류 환경의 살아 있는 연결 중 현재 배포가 있는 것(살아 있는 서비스만).

        서비스·환경·현재 배포를 함께 읽는다. 온프레 이미지 받기 자격 갱신이 쓴다.
        """
        statement = (
            select(ServiceEnvironment)
            .join(Environment, Environment.id == ServiceEnvironment.environment_id)
            .join(Service, Service.id == ServiceEnvironment.service_id)
            .where(
                _is_live(),
                ServiceEnvironment.current_deployment_id.is_not(None),
                Environment.kind == kind,
                Service.is_deleted.is_(False),
            )
            .options(
                selectinload(ServiceEnvironment.environment),
                selectinload(ServiceEnvironment.service),
                selectinload(ServiceEnvironment.current_deployment),
            )
            .order_by(ServiceEnvironment.id)
        )
        return list((await self._session.scalars(statement)).all())

    async def is_public_host_in_use(self, public_host: str) -> bool:
        statement = select(
            exists().where(ServiceEnvironment.public_host == public_host, _is_live())
        )
        return bool(await self._session.scalar(statement))


def _is_live() -> ColumnElement[bool]:
    return ServiceEnvironment.is_deleted.is_(False)
