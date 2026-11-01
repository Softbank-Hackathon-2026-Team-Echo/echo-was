import uuid

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ServiceNotFoundError
from app.models import Service


class ServiceRepository:
    """서비스 표 조회·저장. 트랜잭션은 서비스 계층이 연다.

    lock=True 는 `FOR NO KEY UPDATE`(상태만 바꾸는 잠금)다. 다른 트랜잭션이 이 행을 FK 로 가리키는
    행을 넣을 때 잡는 `FOR KEY SHARE` 와 부딪히지 않는다. 읽은 값은 세션에 있던 객체도 DB 값으로
    덮어쓴다(populate_existing).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, service: Service) -> None:
        self._session.add(service)

    async def find_by_id(self, service_id: uuid.UUID, *, lock: bool = False) -> Service | None:
        """소프트 삭제된 행도 돌려준다(일꾼이 삭제 여부를 보고 요청을 닫는다)."""
        statement = (
            select(Service)
            .where(Service.id == service_id)
            .execution_options(populate_existing=True)
        )
        if lock:
            statement = statement.with_for_update(key_share=True)
        return await self._session.scalar(statement)

    async def find_live_by_id(self, service_id: uuid.UUID, *, lock: bool = False) -> Service | None:
        statement = (
            select(Service)
            .where(Service.id == service_id, _is_live())
            .execution_options(populate_existing=True)
        )
        if lock:
            statement = statement.with_for_update(key_share=True)
        return await self._session.scalar(statement)

    async def get_live_by_id(self, service_id: uuid.UUID, *, lock: bool = False) -> Service:
        service = await self.find_live_by_id(service_id, lock=lock)
        if service is None:
            raise ServiceNotFoundError("service not found", service_id=str(service_id))
        return service

    async def find_live_by_name(self, name: str) -> Service | None:
        return await self._session.scalar(select(Service).where(Service.name == name, _is_live()))

    async def search_live(self, offset: int, limit: int) -> list[Service]:
        statement = (
            select(Service)
            .where(_is_live())
            .order_by(Service.created_at, Service.id)
            .offset(offset)
            .limit(limit)
        )
        return list((await self._session.scalars(statement)).all())

    async def count_live(self) -> int:
        statement = select(func.count()).select_from(Service).where(_is_live())
        return await self._session.scalar(statement) or 0


def _is_live() -> ColumnElement[bool]:
    return Service.is_deleted.is_(False)
