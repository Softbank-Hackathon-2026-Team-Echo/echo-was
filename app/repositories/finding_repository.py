import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.enums import SecurityControlId, SecurityFindingMode, SecurityFindingSource
from app.models import Finding

# 커서에서 한 번에 가져올 ORM 행 수. 판정 결과 전체를 자르는 상한이 아니다.
FINDING_FETCH_BATCH_SIZE = 500


class FindingRepository:
    """보안 증거 저장·조회. 조직 조건은 늘 건다(다른 조직 증거는 보이지 않는다)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, finding: Finding) -> None:
        self._session.add(finding)

    async def search_unexpired_by_organization(
        self,
        organization_id: uuid.UUID,
        now: datetime,
        *,
        control_id: SecurityControlId | None = None,
        excluded_sources: Sequence[SecurityFindingSource] = (),
    ) -> list[Finding]:
        """판정용 만료 전 실제 증거 전체. 원문 상세 없이 판정 필드만 최신순으로 읽는다.

        전역 LIMIT·(통제, 출처)별 배분은 다른 범위의 증거나 반복 PASS 가 유효한 FAIL 을
        밀어낼 수 있다. 같은 커서의 결과를 끝까지 읽고 범위·유효성·worst-of 는 규칙표에 맡긴다.
        SIMULATED 는 SQL 에서 제외한다. 배치 크기는 전송 단위이며 전체 메모리 상한은 아니다.
        """
        stmt = (
            select(Finding)
            .options(
                load_only(
                    Finding.id,
                    Finding.organization_id,
                    Finding.service_id,
                    Finding.environment_id,
                    Finding.image_digest,
                    Finding.config_commit,
                    Finding.was_version,
                    Finding.control_id,
                    Finding.result,
                    Finding.mode,
                    Finding.source,
                    Finding.policy_version,
                    Finding.reviewer_user_id,
                    Finding.basis_url,
                    Finding.confirmation,
                    Finding.collected_at,
                    Finding.expires_at,
                    raiseload=True,
                )
            )
            .where(
                Finding.organization_id == organization_id,
                Finding.expires_at > now,
                Finding.mode != SecurityFindingMode.SIMULATED,
            )
            .order_by(Finding.collected_at.desc(), Finding.id)
            .execution_options(yield_per=FINDING_FETCH_BATCH_SIZE)
        )
        if control_id is not None:
            stmt = stmt.where(Finding.control_id == control_id)
        if excluded_sources:
            stmt = stmt.where(Finding.source.not_in(excluded_sources))
        rows = await self._session.stream_scalars(stmt)
        try:
            return [finding async for finding in rows]
        finally:
            await rows.close()

    async def search_for_deploy(
        self, organization_id: uuid.UUID, image_digest: str, now: datetime
    ) -> list[Finding]:
        """실제로 적용할 digest 의 이미지 통제만 끝까지 읽는다. 유효 FAIL 을 자르지 않는다."""
        stmt = (
            select(Finding)
            .options(
                load_only(
                    Finding.id,
                    Finding.organization_id,
                    Finding.service_id,
                    Finding.environment_id,
                    Finding.image_digest,
                    Finding.config_commit,
                    Finding.was_version,
                    Finding.control_id,
                    Finding.result,
                    Finding.mode,
                    Finding.source,
                    Finding.policy_version,
                    Finding.reviewer_user_id,
                    Finding.basis_url,
                    Finding.confirmation,
                    Finding.collected_at,
                    Finding.expires_at,
                    Finding.source_ref,
                    raiseload=True,
                )
            )
            .where(
                Finding.organization_id == organization_id,
                Finding.image_digest == image_digest,
                Finding.control_id.in_((SecurityControlId.IMG_1, SecurityControlId.IMG_2)),
                Finding.expires_at > now,
                Finding.mode != SecurityFindingMode.SIMULATED,
            )
            .execution_options(yield_per=FINDING_FETCH_BATCH_SIZE)
        )
        rows = await self._session.stream_scalars(stmt)
        try:
            return [finding async for finding in rows]
        finally:
            await rows.close()

    async def has_build_scan(self, organization_id: uuid.UUID, build_prefix: str) -> bool:
        # 수집은 세 보고서를 한 트랜잭션으로 저장한다. 만료된 원문도 다시 신선하게 만들지 않는다.
        stmt = (
            select(Finding.id)
            .where(
                Finding.organization_id == organization_id,
                Finding.source_ref.startswith(build_prefix),
            )
            .limit(1)
        )
        return await self._session.scalar(stmt) is not None

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
