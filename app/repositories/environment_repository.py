import uuid

from sqlalchemy import ColumnElement, Select, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import EnvironmentNotFoundError
from app.models import Environment, ServiceEnvironment


class EnvironmentRepository:
    """환경 표 조회·저장. 소프트 삭제된 행은 보지 않는다. 트랜잭션은 서비스가 연다."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, environment: Environment) -> None:
        self._session.add(environment)

    async def find_live_by_id(
        self, environment_id: uuid.UUID, *, for_update: bool = False, for_share: bool = False
    ) -> Environment | None:
        """for_update=True 면 SELECT ... FOR UPDATE, for_share=True 면 FOR SHARE 로 행을 잠근다.

        FOR SHARE 는 환경 삭제(FOR UPDATE)와는 부딪히지만, 다른 트랜잭션이 이 환경을 FK 로
        가리키는 행을 넣거나 고칠 때 잡는 FOR KEY SHARE 와는 부딪히지 않는다.

        세션에 이미 있는 객체도 DB 의 지금 값으로 덮어쓴다(populate_existing). 커밋 뒤에도 객체를
        만료하지 않는 세션이라, 그렇지 않으면 다른 요청이 바꾼 값을 보지 못한다.
        """
        statement = (
            select(Environment)
            .where(Environment.id == environment_id, _is_live())
            .execution_options(populate_existing=True)
        )
        if for_update:
            statement = statement.with_for_update()
        elif for_share:
            statement = statement.with_for_update(read=True)
        return await self._session.scalar(statement)

    async def get_live_by_id(
        self, environment_id: uuid.UUID, *, for_update: bool = False, for_share: bool = False
    ) -> Environment:
        environment = await self.find_live_by_id(
            environment_id, for_update=for_update, for_share=for_share
        )
        if environment is None:
            raise EnvironmentNotFoundError(
                "environment not found", environment_id=str(environment_id)
            )
        return environment

    async def find_live_by_name(self, name: str) -> Environment | None:
        return await self._session.scalar(
            select(Environment).where(Environment.name == name, _is_live())
        )

    async def search_live(self, kind: str | None, offset: int, limit: int) -> list[Environment]:
        statement = (
            _filter_by_kind(select(Environment).where(_is_live()), kind)
            .order_by(Environment.created_at, Environment.id)
            .offset(offset)
            .limit(limit)
        )
        return list((await self._session.scalars(statement)).all())

    async def count_live(self, kind: str | None) -> int:
        statement = _filter_by_kind(
            select(func.count()).select_from(Environment).where(_is_live()), kind
        )
        return await self._session.scalar(statement) or 0

    async def has_live_service_environments(self, environment_id: uuid.UUID) -> bool:
        statement = select(
            exists().where(
                ServiceEnvironment.environment_id == environment_id,
                ServiceEnvironment.is_deleted.is_(False),
            )
        )
        return bool(await self._session.scalar(statement))


def _is_live() -> ColumnElement[bool]:
    return Environment.is_deleted.is_(False)


def _filter_by_kind[*Ts](statement: Select[*Ts], kind: str | None) -> Select[*Ts]:
    return statement if kind is None else statement.where(Environment.kind == kind)
