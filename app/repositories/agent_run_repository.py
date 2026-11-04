import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import AgentRunKind, AgentRunStatus
from app.models import AgentRun


class AgentRunRepository:
    """AI 실행 조회."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_latest_succeeded_precheck(
        self,
        service_id: uuid.UUID,
        target_environment_id: uuid.UUID,
        finished_since: datetime,
    ) -> AgentRun | None:
        """그 서비스·도착 환경의 AI 사전 점검 중 finished_since 뒤에 성공으로 끝난 가장 늦은 것."""
        stmt = (
            select(AgentRun)
            .where(
                AgentRun.service_id == service_id,
                AgentRun.target_environment_id == target_environment_id,
                AgentRun.kind == AgentRunKind.PRECHECK,
                AgentRun.status == AgentRunStatus.SUCCEEDED,
                AgentRun.finished_at > finished_since,
            )
            .order_by(AgentRun.finished_at.desc(), AgentRun.id)
            .limit(1)
        )
        return (await self._session.scalars(stmt)).first()
