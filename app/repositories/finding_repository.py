import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import SecurityFindingMode
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

    async def search_recent_for_precheck(
        self,
        organization_id: uuid.UUID,
        service_id: uuid.UUID,
        environment_ids: Sequence[uuid.UUID],
        image_digests: Sequence[str],
        now: datetime,
        limit: int,
    ) -> list[Finding]:
        """사전 점검 입력에 실을 증거: 그 서비스·환경·이미지의 만료 전 실제 증거. 최신순."""
        stmt = (
            select(Finding)
            .where(
                Finding.organization_id == organization_id,
                Finding.expires_at > now,
                Finding.mode != SecurityFindingMode.SIMULATED,
                or_(
                    Finding.service_id == service_id,
                    Finding.environment_id.in_(environment_ids),
                    Finding.image_digest.in_(image_digests),
                ),
            )
            .order_by(Finding.collected_at.desc(), Finding.id)
            .limit(limit)
        )
        return list((await self._session.scalars(stmt)).all())

    async def search_by_organization(
        self,
        organization_id: uuid.UUID,
        conditions: "FindingConditions",
        offset: int,
        limit: int,
    ) -> list[Finding]:
        """증거 목록(최신순). 조직 조건은 늘 걸고, 나머지 조건은 준 것만 건다."""
        stmt = (
            _apply(select(Finding), organization_id, conditions)
            .order_by(Finding.collected_at.desc(), Finding.id)
            .offset(offset)
            .limit(limit)
        )
        return list((await self._session.scalars(stmt)).all())

    async def count_by_organization(
        self, organization_id: uuid.UUID, conditions: "FindingConditions"
    ) -> int:
        stmt = _apply(select(func.count()).select_from(Finding), organization_id, conditions)
        return await self._session.scalar(stmt) or 0


@dataclass(frozen=True)
class FindingConditions:
    """증거 목록 조건. `unexpired_at` 을 주면 그 시각에 만료되지 않은 것만."""

    unexpired_at: datetime | None = None
    environment_id: uuid.UUID | None = None
    service_id: uuid.UUID | None = None
    control_id: str | None = None
    finding_ids: tuple[uuid.UUID, ...] = ()


def _apply[S: Select[Any]](stmt: S, organization_id: uuid.UUID, conditions: FindingConditions) -> S:
    stmt = stmt.where(Finding.organization_id == organization_id)
    if conditions.unexpired_at is not None:
        stmt = stmt.where(Finding.expires_at > conditions.unexpired_at)
    if conditions.environment_id is not None:
        stmt = stmt.where(Finding.environment_id == conditions.environment_id)
    if conditions.service_id is not None:
        stmt = stmt.where(Finding.service_id == conditions.service_id)
    if conditions.control_id is not None:
        stmt = stmt.where(Finding.control_id == conditions.control_id)
    if conditions.finding_ids:
        stmt = stmt.where(Finding.id.in_(conditions.finding_ids))
    return stmt
