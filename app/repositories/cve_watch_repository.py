import uuid
from collections.abc import Collection
from datetime import datetime

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import SecurityControlId, SecurityFindingMode, SecurityFindingSource
from app.models import Finding, ServiceDependencySnapshot


class CveWatchRepository:
    """CVE 감시가 쓰는 조회·저장(의존성 목록, 중복 제거, 이미 알려진 취약점)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add_snapshot(self, snapshot: ServiceDependencySnapshot) -> None:
        self._session.add(snapshot)

    async def find_latest_snapshot(
        self, service_id: uuid.UUID, image_digest: str
    ) -> ServiceDependencySnapshot | None:
        stmt = (
            select(ServiceDependencySnapshot)
            .where(
                ServiceDependencySnapshot.service_id == service_id,
                ServiceDependencySnapshot.image_digest == image_digest,
            )
            .order_by(ServiceDependencySnapshot.collected_at.desc(), ServiceDependencySnapshot.id)
            .limit(1)
        )
        return (await self._session.scalars(stmt)).first()

    async def lock_organization(self, organization_id: uuid.UUID) -> None:
        """같은 조직의 CVE 감시 기록을 한 줄로 세운다(일꾼·즉시 조회가 겹쳐도 중복이 없다)."""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"cve-watch:{organization_id}"},
        )

    async def search_live_dedupe_keys(
        self, organization_id: uuid.UUID, keys: Collection[str], now: datetime
    ) -> dict[str, uuid.UUID]:
        """만료 전 증거가 이미 있는 열쇠 → 그 증거 id(가장 최근 것)."""
        if not keys:
            return {}
        stmt = (
            select(Finding.dedupe_key, Finding.id)
            .where(
                Finding.organization_id == organization_id,
                Finding.dedupe_key.in_(list(keys)),
                Finding.expires_at > now,
            )
            .order_by(Finding.collected_at)
        )
        rows = (await self._session.execute(stmt)).all()
        return {key: finding_id for key, finding_id in rows if key is not None}

    async def search_known_vulnerability_ids(
        self, organization_id: uuid.UUID, image_digests: Collection[str], now: datetime
    ) -> dict[str, set[str]]:
        """digest → 빌드 검사(Trivy)가 이미 보고한 취약점 id. 감시는 새 취약점으로 치지 않는다."""
        if not image_digests:
            return {}
        stmt = select(Finding.image_digest, Finding.items).where(
            Finding.organization_id == organization_id,
            Finding.image_digest.in_(list(image_digests)),
            Finding.control_id == SecurityControlId.IMG_1,
            Finding.source == SecurityFindingSource.TRIVY_IMAGE,
            Finding.mode != SecurityFindingMode.SIMULATED,
            Finding.expires_at > now,
        )
        known: dict[str, set[str]] = {}
        for digest, items in (await self._session.execute(stmt)).all():
            if not digest or not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("item_id"), str):
                    known.setdefault(digest, set()).add(item["item_id"])
        return known

    async def retire_simulated_overflow(
        self,
        organization_id: uuid.UUID,
        *,
        keep_ids: Collection[uuid.UUID],
        limit: int,
        now: datetime,
    ) -> int:
        """살아 있는 가짜(SIMULATED) CVE 증거를 limit 건까지만 남기고 오래된 것부터 지운다.

        keep_ids(이번 주입이 돌려준 증거)는 지우지 않는다. 호출하는 쪽이 keep_ids 를 limit 건
        이하로 줄여 넘기므로(이번 주입분 포함 총수 ≤ limit) 남는 수는 항상 limit 이하다.
        만료 시각을 당기지 않고 지우는 까닭:
        `expires_after_collected` CHECK 때문에 같은 시각에 모은 행은 지금으로 만료시킬 수 없고, 가짜
        증거는 판정에 쓰이지 않아 남길 값이 없다. 지운 수를 돌려준다(감사 줄에 남긴다).
        """
        keep = list(keep_ids)
        stmt = (
            select(Finding.id)
            .where(
                Finding.organization_id == organization_id,
                Finding.source == SecurityFindingSource.CVE_WATCH,
                Finding.mode == SecurityFindingMode.SIMULATED,
                Finding.expires_at > now,
            )
            .order_by(Finding.collected_at.desc(), Finding.created_at.desc(), Finding.id.desc())
            .offset(max(limit - len(keep), 0))
        )
        if keep:
            stmt = stmt.where(Finding.id.not_in(keep))
        overflow = list((await self._session.scalars(stmt)).all())
        if overflow:
            await self._session.execute(
                delete(Finding)
                .where(Finding.id.in_(overflow))
                .execution_options(synchronize_session=False)
            )
        return len(overflow)

    async def find_finding(
        self, organization_id: uuid.UUID, finding_id: uuid.UUID
    ) -> Finding | None:
        stmt = select(Finding).where(
            Finding.organization_id == organization_id, Finding.id == finding_id
        )
        return (await self._session.scalars(stmt)).first()
