import uuid
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentRunner
from app.repositories.conditional_update import update_where


class AgentRunnerRepository:
    """AI 러너 신원 조회·저장. 토큰 원문은 받지 않고 SHA-256 으로만 찾는다."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, runner: AgentRunner) -> None:
        self._session.add(runner)

    async def find_active_by_hash(self, token_hash: str) -> AgentRunner | None:
        return await self._session.scalar(
            select(AgentRunner).where(
                AgentRunner.token_hash == token_hash,
                AgentRunner.revoked_at.is_(None),
            )
        )

    async def find_by_name(self, name: str) -> AgentRunner | None:
        return await self._session.scalar(select(AgentRunner).where(AgentRunner.name == name))

    async def touch_last_seen(
        self, runner_id: uuid.UUID, now: datetime, min_interval: timedelta
    ) -> None:
        """마지막으로 본 시각을 min_interval 에 한 번만 적는다(요청마다 쓰기를 내지 않는다)."""
        await self._session.execute(
            update(AgentRunner)
            .where(
                AgentRunner.id == runner_id,
                or_(
                    AgentRunner.last_seen_at.is_(None),
                    AgentRunner.last_seen_at <= now - min_interval,
                ),
            )
            .values(last_seen_at=now)
            .execution_options(synchronize_session=False)
        )

    async def revoke(self, runner_id: uuid.UUID, now: datetime) -> bool:
        return await update_where(
            self._session,
            AgentRunner,
            runner_id,
            [AgentRunner.revoked_at.is_(None)],
            {"revoked_at": now},
        )
