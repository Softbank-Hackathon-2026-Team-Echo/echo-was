import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Finding

# 판정 한 번에 읽는 증거 상한. 조직의 유효한 증거가 이보다 많으면 최신 것부터 읽는다.
FINDING_SEARCH_LIMIT = 500


class FindingRepository:
    """보안 증거 저장·조회. 조직 조건은 늘 건다(다른 조직 증거는 보이지 않는다)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, finding: Finding) -> None:
        self._session.add(finding)

    async def search_unexpired_by_organization(
        self, organization_id: uuid.UUID, now: datetime, limit: int = FINDING_SEARCH_LIMIT
    ) -> list[Finding]:
        stmt = (
            select(Finding)
            .where(Finding.organization_id == organization_id, Finding.expires_at > now)
            .order_by(Finding.collected_at.desc(), Finding.id)
            .limit(limit)
        )
        return list((await self._session.scalars(stmt)).all())
