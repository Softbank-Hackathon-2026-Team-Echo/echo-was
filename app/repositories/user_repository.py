import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User


class UserRepository:
    """사용자 표 조회·저장. 트랜잭션은 서비스가 연다."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_if_absent(
        self,
        *,
        github_id: int,
        github_login: str,
        name: str | None,
        avatar_url: str | None,
        now: datetime,
    ) -> uuid.UUID | None:
        """github_id 가 처음이면 넣고 id 를, 이미 있으면 None 을 돌려준다.

        `ON CONFLICT DO NOTHING` 이라 같은 사람의 로그인이 동시에 와도 오류 없이 한쪽만 넣는다
        (다른 쪽은 앞 트랜잭션이 커밋할 때까지 기다린 뒤 None 을 받는다).
        """
        statement = (
            insert(User)
            .values(
                id=uuid.uuid4(),
                github_id=github_id,
                github_login=github_login,
                name=name,
                avatar_url=avatar_url,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=[User.github_id])
            .returning(User.id)
        )
        return await self._session.scalar(statement)

    async def update_profile(
        self,
        github_id: int,
        *,
        github_login: str,
        name: str | None,
        avatar_url: str | None,
        now: datetime,
    ) -> None:
        await self._session.execute(
            update(User)
            .where(User.github_id == github_id)
            .values(github_login=github_login, name=name, avatar_url=avatar_url, updated_at=now)
            .execution_options(synchronize_session=False)
        )

    async def find_by_github_id(self, github_id: int) -> User | None:
        return await self._session.scalar(
            select(User)
            .where(User.github_id == github_id)
            .execution_options(populate_existing=True)
        )

    async def find_by_id(self, user_id: uuid.UUID) -> User | None:
        return await self._session.scalar(
            select(User).where(User.id == user_id).execution_options(populate_existing=True)
        )

    async def find_by_github_login(self, github_login: str) -> User | None:
        """GitHub 아이디는 대소문자를 가리지 않는다."""
        return await self._session.scalar(
            select(User).where(func.lower(User.github_login) == github_login.lower())
        )
