from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, UserSession


class UserSessionRepository:
    """로그인 세션 조회·저장. 쿠키 값은 받지 않고 그 SHA-256 으로만 찾는다."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, user_session: UserSession) -> None:
        self._session.add(user_session)

    async def find_active_by_hash(
        self, token_hash: str, now: datetime
    ) -> tuple[UserSession, User] | None:
        """폐기되지 않았고 만료 전인 세션과 그 사용자."""
        statement = (
            select(UserSession, User)
            .join(User, User.id == UserSession.user_id)
            .where(
                UserSession.token_hash == token_hash,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now,
            )
        )
        row = (await self._session.execute(statement)).first()
        return None if row is None else (row[0], row[1])

    async def revoke_by_hash(self, token_hash: str, now: datetime) -> None:
        await self._session.execute(
            update(UserSession)
            .where(UserSession.token_hash == token_hash, UserSession.revoked_at.is_(None))
            .values(revoked_at=now)
            .execution_options(synchronize_session=False)
        )
