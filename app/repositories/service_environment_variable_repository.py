import uuid
from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import ServiceEnvironmentVariable


class ServiceEnvironmentVariableRepository:
    """고객 앱 환경 변수 행(이름·위치·버전, 값 없음). 연결 행을 잠근 트랜잭션 안에서 쓴다."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, variable: ServiceEnvironmentVariable) -> None:
        self._session.add(variable)

    async def search_live_by_connection(
        self, service_environment_id: uuid.UUID
    ) -> list[ServiceEnvironmentVariable]:
        """살아 있는 변수를 키 이름 순으로. 마지막으로 넣은 사람을 함께 읽는다."""
        statement = (
            select(ServiceEnvironmentVariable)
            .where(
                ServiceEnvironmentVariable.service_environment_id == service_environment_id,
                ServiceEnvironmentVariable.is_deleted.is_(False),
            )
            .options(selectinload(ServiceEnvironmentVariable.updated_by))
            .order_by(ServiceEnvironmentVariable.key)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())

    async def search_live_keys_by_connections(
        self, service_environment_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, frozenset[str]]:
        """연결마다 살아 있는 변수 키 이름(보안 C-SEC-1). 키만 읽고, 키가 없는 연결은 빈 집합."""
        ids = list(dict.fromkeys(service_environment_ids))
        if not ids:
            return {}
        keys_by_connection: dict[uuid.UUID, set[str]] = {id_: set() for id_ in ids}
        rows = await self._session.execute(
            select(
                ServiceEnvironmentVariable.service_environment_id, ServiceEnvironmentVariable.key
            ).where(
                ServiceEnvironmentVariable.service_environment_id.in_(ids),
                ServiceEnvironmentVariable.is_deleted.is_(False),
            )
        )
        for connection_id, key in rows:
            keys_by_connection[connection_id].add(key)
        return {
            connection_id: frozenset(keys) for connection_id, keys in keys_by_connection.items()
        }

    async def find_live(
        self, service_environment_id: uuid.UUID, key: str
    ) -> ServiceEnvironmentVariable | None:
        return await self._session.scalar(
            select(ServiceEnvironmentVariable)
            .where(
                ServiceEnvironmentVariable.service_environment_id == service_environment_id,
                ServiceEnvironmentVariable.key == key,
                ServiceEnvironmentVariable.is_deleted.is_(False),
            )
            .execution_options(populate_existing=True)
        )

    async def find_previous_secret_name(
        self, service_environment_id: uuid.UUID, key: str
    ) -> str | None:
        """이 연결·키의 가장 최근 행(지운 행 포함)이 쓰던 보관함 비밀 이름."""
        return await self._session.scalar(
            select(ServiceEnvironmentVariable.secret_name)
            .where(
                ServiceEnvironmentVariable.service_environment_id == service_environment_id,
                ServiceEnvironmentVariable.key == key,
            )
            .order_by(ServiceEnvironmentVariable.created_at.desc())
            .limit(1)
        )

    async def count_live(self, service_environment_id: uuid.UUID) -> int:
        count = await self._session.scalar(
            select(func.count())
            .select_from(ServiceEnvironmentVariable)
            .where(
                ServiceEnvironmentVariable.service_environment_id == service_environment_id,
                ServiceEnvironmentVariable.is_deleted.is_(False),
            )
        )
        return count or 0
