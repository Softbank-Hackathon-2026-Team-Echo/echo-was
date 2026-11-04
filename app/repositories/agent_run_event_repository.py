import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import AgentRunStep, LogLevel
from app.models import AgentRunEvent


@dataclass(frozen=True)
class AgentRunStepLevelSummary:
    """한 단계·한 수준의 이벤트 묶음. 화면의 단계 표시(steps[])는 이것을 합쳐 만든다."""

    step: AgentRunStep
    level: LogLevel
    first_at: datetime
    last_at: datetime


class AgentRunEventRepository:
    """러너 진행 이벤트 조회·저장. 줄은 고치거나 지우지 않는다."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_ignoring_duplicates(self, rows: Sequence[dict[str, Any]]) -> None:
        """같은 (run_id, seq) 는 조용히 무시한다(러너의 재전송)."""
        statement = (
            insert(AgentRunEvent)
            .values(list(rows))
            .on_conflict_do_nothing(index_elements=[AgentRunEvent.run_id, AgentRunEvent.seq])
        )
        await self._session.execute(statement)

    async def find_last_seq(self, run_id: uuid.UUID) -> int:
        """실행의 가장 큰 seq. 줄이 없으면 0."""
        statement = select(func.coalesce(func.max(AgentRunEvent.seq), 0)).where(
            AgentRunEvent.run_id == run_id
        )
        return int(await self._session.scalar(statement) or 0)

    async def search_after(
        self, run_id: uuid.UUID, after_seq: int, limit: int
    ) -> list[AgentRunEvent]:
        """seq > after_seq 인 줄을 seq 오름차순으로 limit 개까지."""
        statement = (
            select(AgentRunEvent)
            .where(AgentRunEvent.run_id == run_id, AgentRunEvent.seq > after_seq)
            .order_by(AgentRunEvent.seq)
            .limit(limit)
        )
        return list((await self._session.scalars(statement)).all())

    async def search_step_summaries(self, run_id: uuid.UUID) -> list[AgentRunStepLevelSummary]:
        statement = (
            select(
                AgentRunEvent.step,
                AgentRunEvent.level,
                func.min(AgentRunEvent.at),
                func.max(AgentRunEvent.at),
            )
            .where(AgentRunEvent.run_id == run_id)
            .group_by(AgentRunEvent.step, AgentRunEvent.level)
        )
        rows = (await self._session.execute(statement)).all()
        return [
            AgentRunStepLevelSummary(AgentRunStep(step), LogLevel(level), first_at, last_at)
            for step, level, first_at, last_at in rows
        ]
