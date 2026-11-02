import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MigrationPlanLog


class MigrationPlanLogRepository:
    """이전 실행 로그 줄 조회·저장. 줄은 고치거나 지우지 않는다."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, line: MigrationPlanLog) -> None:
        self._session.add(line)

    async def find_last_seq(self, plan_id: uuid.UUID) -> int:
        """계획의 마지막 seq. 줄이 없으면 0."""
        statement = select(func.coalesce(func.max(MigrationPlanLog.seq), 0)).where(
            MigrationPlanLog.migration_plan_id == plan_id
        )
        return int(await self._session.scalar(statement) or 0)

    async def search_after(
        self, plan_id: uuid.UUID, after_seq: int, limit: int
    ) -> list[MigrationPlanLog]:
        """seq > after_seq 인 줄을 seq 오름차순으로 limit 개까지."""
        statement = (
            select(MigrationPlanLog)
            .where(MigrationPlanLog.migration_plan_id == plan_id, MigrationPlanLog.seq > after_seq)
            .order_by(MigrationPlanLog.seq)
            .limit(limit)
        )
        return list((await self._session.scalars(statement)).all())
