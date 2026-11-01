import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentToken


class AgentTokenRepository:
    """에이전트 토큰 조회·저장. 원문은 받지 않고 SHA-256 으로만 찾는다."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, token: AgentToken) -> None:
        self._session.add(token)

    async def find_active_by_hash(self, token_hash: str, now: datetime) -> AgentToken | None:
        return await self._session.scalar(
            select(AgentToken).where(
                AgentToken.token_hash == token_hash,
                AgentToken.revoked_at.is_(None),
                AgentToken.expires_at > now,
            )
        )

    async def touch_last_used(
        self, token_id: uuid.UUID, now: datetime, min_interval: timedelta
    ) -> None:
        """마지막 사용 시각을 min_interval 에 한 번만 적는다(요청마다 쓰기를 내지 않는다)."""
        await self._session.execute(
            update(AgentToken)
            .where(
                AgentToken.id == token_id,
                or_(
                    AgentToken.last_used_at.is_(None),
                    AgentToken.last_used_at <= now - min_interval,
                ),
            )
            .values(last_used_at=now)
            .execution_options(synchronize_session=False)
        )

    async def find_in_organization(
        self, organization_id: uuid.UUID, token_id: uuid.UUID
    ) -> AgentToken | None:
        return await self._session.scalar(
            select(AgentToken)
            .where(AgentToken.id == token_id, AgentToken.organization_id == organization_id)
            .execution_options(populate_existing=True)
        )

    async def search_by_organization(
        self, organization_id: uuid.UUID, offset: int, limit: int
    ) -> list[AgentToken]:
        """최신순."""
        statement = (
            select(AgentToken)
            .where(AgentToken.organization_id == organization_id)
            .order_by(AgentToken.created_at.desc(), AgentToken.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list((await self._session.scalars(statement)).all())

    async def count_by_organization(self, organization_id: uuid.UUID) -> int:
        statement = (
            select(func.count())
            .select_from(AgentToken)
            .where(AgentToken.organization_id == organization_id)
        )
        return await self._session.scalar(statement) or 0
