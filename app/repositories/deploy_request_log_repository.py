import uuid
from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DeployRequestLog


class DeployRequestLogRepository:
    """배포 작업 로그 줄 조회·저장. 줄은 고치거나 지우지 않는다."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, line: DeployRequestLog) -> None:
        self._session.add(line)

    async def find_last_seq(self, deploy_request_id: uuid.UUID) -> int:
        """요청의 마지막 seq. 줄이 없으면 0."""
        statement = select(func.coalesce(func.max(DeployRequestLog.seq), 0)).where(
            DeployRequestLog.deploy_request_id == deploy_request_id
        )
        return int(await self._session.scalar(statement) or 0)

    async def search_after(
        self, deploy_request_id: uuid.UUID, after_seq: int, limit: int
    ) -> list[DeployRequestLog]:
        """seq > after_seq 인 줄을 seq 오름차순으로 limit 개까지."""
        statement = (
            select(DeployRequestLog)
            .where(
                DeployRequestLog.deploy_request_id == deploy_request_id,
                DeployRequestLog.seq > after_seq,
            )
            .order_by(DeployRequestLog.seq)
            .limit(limit)
        )
        return list((await self._session.scalars(statement)).all())

    async def search_by_codes(
        self, deploy_request_id: uuid.UUID, codes: Iterable[str]
    ) -> list[DeployRequestLog]:
        """그 코드의 줄을 seq 오름차순으로(배포 상세 failure 칸, API 서버 4-1)."""
        statement = (
            select(DeployRequestLog)
            .where(
                DeployRequestLog.deploy_request_id == deploy_request_id,
                DeployRequestLog.code.in_([str(code) for code in codes]),
            )
            .order_by(DeployRequestLog.seq)
        )
        return list((await self._session.scalars(statement)).all())
