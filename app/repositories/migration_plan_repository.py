import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import (
    IN_PROGRESS_MIGRATION_PLAN_STATUSES,
    OPEN_MIGRATION_PLAN_STATUSES,
    MigrationPlanStatus,
)
from app.models import MigrationPlan, Service
from app.repositories.conditional_update import update_where


class MigrationPlanRepository:
    """이전 계획 조회·저장. 상태는 transition(조건부 UPDATE)으로만 바꾼다.

    계획에는 조직 칸이 없어 API 조회는 서비스의 조직으로 거른다. 일꾼이 쓰는 find_by_id 는 조직
    조건이 없다. lock=True 는 `FOR NO KEY UPDATE`(잠금 순서상 서비스·연결 뒤)다.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, plan: MigrationPlan) -> None:
        self._session.add(plan)

    async def find_by_id(self, plan_id: uuid.UUID, *, lock: bool = False) -> MigrationPlan | None:
        statement = (
            select(MigrationPlan)
            .where(MigrationPlan.id == plan_id)
            .execution_options(populate_existing=True)
        )
        if lock:
            statement = statement.with_for_update(key_share=True)
        return await self._session.scalar(statement)

    async def find_in_organization(
        self, organization_id: uuid.UUID, plan_id: uuid.UUID
    ) -> MigrationPlan | None:
        """서비스의 조직으로 거른다(소프트 삭제된 서비스의 계획도 읽는다)."""
        statement = (
            select(MigrationPlan)
            .join(Service, Service.id == MigrationPlan.service_id)
            .where(MigrationPlan.id == plan_id, Service.organization_id == organization_id)
            .execution_options(populate_existing=True)
        )
        return await self._session.scalar(statement)

    async def transition(
        self,
        plan_id: uuid.UUID,
        expected: MigrationPlanStatus | Iterable[MigrationPlanStatus],
        values: dict[str, Any],
        conditions: Sequence[ColumnElement[bool]] = (),
    ) -> bool:
        """지금 상태가 expected(그리고 conditions)일 때만 values 를 적는다. 적었으면 True."""
        statuses = [expected] if isinstance(expected, MigrationPlanStatus) else list(expected)
        return await update_where(
            self._session,
            MigrationPlan,
            plan_id,
            [MigrationPlan.status.in_(statuses), *conditions],
            values,
        )

    async def find_open_for_service(self, service_id: uuid.UUID) -> MigrationPlan | None:
        """끝나지 않은(PENDING_APPROVAL·APPROVED·RUNNING·ROLLING_BACK) 계획. 서비스당 하나뿐이다."""
        return await self._find_for_service(service_id, OPEN_MIGRATION_PLAN_STATUSES)

    async def find_in_progress_for_service(self, service_id: uuid.UUID) -> MigrationPlan | None:
        """승인 뒤 실행 중(APPROVED·RUNNING·ROLLING_BACK)인 계획."""
        return await self._find_for_service(service_id, IN_PROGRESS_MIGRATION_PLAN_STATUSES)

    async def _find_for_service(
        self, service_id: uuid.UUID, statuses: Iterable[MigrationPlanStatus]
    ) -> MigrationPlan | None:
        return await self._session.scalar(
            select(MigrationPlan)
            .where(MigrationPlan.service_id == service_id, MigrationPlan.status.in_(statuses))
            .execution_options(populate_existing=True)
        )

    async def find_next_created_at(
        self, service_id: uuid.UUID, created_at: datetime
    ) -> datetime | None:
        """같은 서비스에서 created_at 뒤에 만든 계획 중 가장 이른 것의 created_at."""
        return await self._session.scalar(
            select(MigrationPlan.created_at)
            .where(MigrationPlan.service_id == service_id, MigrationPlan.created_at > created_at)
            .order_by(MigrationPlan.created_at)
            .limit(1)
        )

    async def find_last_succeeded_for_service(self, service_id: uuid.UUID) -> MigrationPlan | None:
        """가장 늦게 끝난 SUCCEEDED 계획. finished_at 이 같으면 created_at, 그다음 id 가 큰 것."""
        return await self._session.scalar(
            select(MigrationPlan)
            .where(
                MigrationPlan.service_id == service_id,
                MigrationPlan.status == MigrationPlanStatus.SUCCEEDED,
            )
            .order_by(
                MigrationPlan.finished_at.desc(),
                MigrationPlan.created_at.desc(),
                MigrationPlan.id.desc(),
            )
            .limit(1)
        )

    async def search_by_service(
        self,
        organization_id: uuid.UUID,
        service_id: uuid.UUID,
        statuses: Sequence[MigrationPlanStatus],
        offset: int,
        limit: int,
    ) -> list[MigrationPlan]:
        """최신순(created_at 내림차순, 같으면 id 내림차순). 빈 statuses 는 조건 없음."""
        statement = (
            _filter_by_service(select(MigrationPlan), organization_id, service_id, statuses)
            .order_by(MigrationPlan.created_at.desc(), MigrationPlan.id.desc())
            .offset(offset)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())

    async def count_by_service(
        self,
        organization_id: uuid.UUID,
        service_id: uuid.UUID,
        statuses: Sequence[MigrationPlanStatus],
    ) -> int:
        statement = _filter_by_service(
            select(func.count()).select_from(MigrationPlan), organization_id, service_id, statuses
        )
        return await self._session.scalar(statement) or 0


def _filter_by_service[*Ts](
    statement: Select[*Ts],
    organization_id: uuid.UUID,
    service_id: uuid.UUID,
    statuses: Sequence[MigrationPlanStatus],
) -> Select[*Ts]:
    statement = statement.join(Service, Service.id == MigrationPlan.service_id).where(
        MigrationPlan.service_id == service_id, Service.organization_id == organization_id
    )
    if statuses:
        statement = statement.where(MigrationPlan.status.in_(statuses))
    return statement
