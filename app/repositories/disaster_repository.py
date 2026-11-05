import uuid
from collections.abc import Collection
from typing import Any

from sqlalchemy import Select, cast, func, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Text

from app.enums import DisasterStatus
from app.models import Disaster
from app.repositories.conditional_update import update_where


class DisasterRepository:
    """재난 조회·저장. 상태는 end(조건부 UPDATE)로만 바꾼다. API 조회는 조직 조건이 필수다."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, disaster: Disaster) -> None:
        self._session.add(disaster)

    async def find_in_organization(
        self, organization_id: uuid.UUID, disaster_id: uuid.UUID
    ) -> Disaster | None:
        return await self._session.scalar(
            select(Disaster)
            .where(Disaster.id == disaster_id, Disaster.organization_id == organization_id)
            .execution_options(populate_existing=True)
        )

    async def search_by_organization(
        self,
        organization_id: uuid.UUID,
        status: DisasterStatus | None,
        offset: int,
        limit: int,
    ) -> list[Disaster]:
        """최신순(created_at 내림차순, 같으면 id 내림차순)."""
        statement = (
            _filter_by_organization(select(Disaster), organization_id, status)
            .order_by(Disaster.created_at.desc(), Disaster.id.desc())
            .offset(offset)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())

    async def count_by_organization(
        self, organization_id: uuid.UUID, status: DisasterStatus | None
    ) -> int:
        statement = _filter_by_organization(
            select(func.count()).select_from(Disaster), organization_id, status
        )
        return await self._session.scalar(statement) or 0

    async def search_blocking_affecting(
        self, environment_ids: Collection[uuid.UUID]
    ) -> list[Disaster]:
        """environment_ids 중 하나라도 피해 환경으로 가진, 복귀를 막는 재난. 최신순.

        복귀를 막는 재난 = ACTIVE 이고 이전 대상 기준을 넘은 재난. 기준 미달 재난은 이전을 일으키지
        않으므로 그 지역으로 돌아가는 것도 막지 않는다. 환경 id 는 조직 하나에만 속하므로 다른 조직
        재난이 섞이지 않는다.
        """
        if not environment_ids:
            return []
        return list(
            (
                await self._session.scalars(
                    select(Disaster)
                    .where(
                        Disaster.status == DisasterStatus.ACTIVE,
                        Disaster.meets_threshold.is_(True),
                        Disaster.affected_environment_ids.op("?|")(
                            cast([str(item) for item in environment_ids], ARRAY(Text))
                        ),
                    )
                    .order_by(Disaster.created_at.desc(), Disaster.id.desc())
                    .execution_options(populate_existing=True)
                )
            ).all()
        )

    async def search_blocking_by_organization(self, organization_id: uuid.UUID) -> list[Disaster]:
        """그 조직의 복귀를 막는 재난 전부(트리거가 도착 후보를 거를 때 쓴다)."""
        statement = (
            _filter_by_organization(select(Disaster), organization_id, DisasterStatus.ACTIVE)
            .where(Disaster.meets_threshold.is_(True))
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())

    async def end(self, disaster_id: uuid.UUID, values: dict[str, Any]) -> bool:
        """ACTIVE 일 때만 끝낸다. 적었으면 True."""
        return await update_where(
            self._session,
            Disaster,
            disaster_id,
            [Disaster.status == DisasterStatus.ACTIVE],
            {**values, "status": DisasterStatus.ENDED},
        )


def _filter_by_organization[*Ts](
    statement: Select[*Ts], organization_id: uuid.UUID, status: DisasterStatus | None
) -> Select[*Ts]:
    statement = statement.where(Disaster.organization_id == organization_id)
    if status is not None:
        statement = statement.where(Disaster.status == status)
    return statement
