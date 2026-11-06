import uuid
from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.enums import AgentRunKind, AgentRunStatus
from app.models import AgentRun
from app.models.agent_run import CLAIMED_STATUSES, FINISHED_STATUSES, OPEN_STATUSES
from app.repositories.conditional_update import update_where


def _is_lease_alive(now: datetime) -> ColumnElement[bool]:
    """임대와 마감이 모두 남아 있다. _is_overdue 와 정확히 반대다(경계 포함)."""
    return and_(AgentRun.lease_until >= now, AgentRun.deadline_at > now)


def _is_overdue(now: datetime) -> ColumnElement[bool]:
    """열린 실행 중 마감이 지났거나, 러너가 가져간 뒤 임대가 끊긴 것."""
    return and_(
        AgentRun.status.in_(OPEN_STATUSES),
        or_(
            AgentRun.deadline_at <= now,
            and_(AgentRun.status != AgentRunStatus.QUEUED, AgentRun.lease_until < now),
        ),
    )


class AgentRunRepository:
    """AI 실행 조회와 상태 전이. 상태는 조건부 UPDATE 로만 바꾼다(0행이면 호출자가 다시 읽는다)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, run: AgentRun) -> None:
        self._session.add(run)

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

    async def find_by_id(self, run_id: uuid.UUID) -> AgentRun | None:
        return await self._session.scalar(
            select(AgentRun).where(AgentRun.id == run_id).execution_options(populate_existing=True)
        )

    async def find_precheck(
        self, organization_id: uuid.UUID, run_id: uuid.UUID, *, lock: bool = False
    ) -> AgentRun | None:
        """그 조직의 사전 점검 실행. 다른 조직 것과 다른 종류는 없는 것과 같다.

        lock=True 는 `FOR UPDATE` 다. 그 실행의 패치 결정을 한 번에 하나씩 처리한다.
        """
        statement = (
            select(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.organization_id == organization_id,
                AgentRun.kind == AgentRunKind.PRECHECK,
            )
            .execution_options(populate_existing=True)
        )
        if lock:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def find_in_organization(
        self, organization_id: uuid.UUID, run_id: uuid.UUID, kinds: Collection[AgentRunKind]
    ) -> AgentRun | None:
        """그 조직의 실행 중 kinds 에 든 종류(읽기 전용). 다른 조직·다른 종류는 None."""
        statement = (
            select(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.organization_id == organization_id,
                AgentRun.kind.in_(list(kinds)),
            )
            .execution_options(populate_existing=True)
        )
        return await self._session.scalar(statement)

    async def search_by_ids(self, run_ids: Sequence[uuid.UUID]) -> list[AgentRun]:
        if not run_ids:
            return []
        statement = (
            select(AgentRun)
            .where(AgentRun.id.in_(run_ids))
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())

    async def search_finished_for_residency(
        self, organization_id: uuid.UUID, from_at: datetime, to_at: datetime, limit: int
    ) -> list[AgentRun]:
        """그 조직에서 [from_at, to_at) 안에 끝난 실행(최신순). 요약에 쓰는 칸만 읽는다."""
        statement = (
            select(AgentRun)
            .options(
                load_only(
                    AgentRun.service_id,
                    AgentRun.kind,
                    AgentRun.status,
                    AgentRun.finished_at,
                    AgentRun.simulated,
                    AgentRun.model_id,
                    AgentRun.processing,
                )
            )
            .where(
                AgentRun.organization_id == organization_id,
                AgentRun.status.in_(FINISHED_STATUSES),
                AgentRun.finished_at >= from_at,
                AgentRun.finished_at < to_at,
            )
            .order_by(AgentRun.finished_at.desc(), AgentRun.id.desc())
            .limit(limit)
        )
        return list((await self._session.scalars(statement)).all())

    async def find_id_by_token_hash(self, token_hash: str) -> uuid.UUID | None:
        """토큰 해시의 실행 id. 끝난 실행도 찾는다(상태로 409 를 가른다)."""
        return await self._session.scalar(
            select(AgentRun.id).where(AgentRun.run_token_hash == token_hash)
        )

    async def find_open_for_service(self, service_id: uuid.UUID) -> AgentRun | None:
        return await self._session.scalar(
            select(AgentRun)
            .where(AgentRun.service_id == service_id, AgentRun.status.in_(OPEN_STATUSES))
            .execution_options(populate_existing=True)
        )

    async def lock_overdue(
        self,
        now: datetime,
        limit: int,
        *,
        service_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
    ) -> list[AgentRun]:
        """만료할 실행을 잠근다. 다른 호출이 잡은 행은 건너뛰고 다음 호출에 맡긴다."""
        statement = select(AgentRun).where(_is_overdue(now))
        if service_id is not None:
            statement = statement.where(AgentRun.service_id == service_id)
        if run_id is not None:
            statement = statement.where(AgentRun.id == run_id)
        statement = (
            statement.order_by(AgentRun.deadline_at, AgentRun.id)
            .limit(limit)
            .with_for_update(skip_locked=True, key_share=True)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())

    async def lock_next_queued(
        self, now: datetime, kinds: Sequence[AgentRunKind]
    ) -> AgentRun | None:
        """가져갈 실행 하나(주어진 종류 중 오래된 순). 다른 러너가 잠근 행은 건너뛴다."""
        statement = (
            select(AgentRun)
            .where(
                AgentRun.status == AgentRunStatus.QUEUED,
                AgentRun.kind.in_(kinds),
                AgentRun.deadline_at > now,
            )
            .order_by(AgentRun.created_at, AgentRun.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        return await self._session.scalar(statement)

    async def claim(
        self,
        run_id: uuid.UUID,
        runner_id: uuid.UUID,
        run_token_hash: str,
        now: datetime,
        lease_until: datetime,
    ) -> bool:
        return await update_where(
            self._session,
            AgentRun,
            run_id,
            [AgentRun.status == AgentRunStatus.QUEUED, AgentRun.deadline_at > now],
            {
                "status": AgentRunStatus.CLAIMED,
                "claimed_by_runner_id": runner_id,
                "run_token_hash": run_token_hash,
                "claimed_at": now,
                "lease_until": lease_until,
                "updated_at": now,
            },
        )

    async def extend_lease(self, run_id: uuid.UUID, now: datetime, lease_until: datetime) -> bool:
        """임대를 늘린다. 첫 호출이면 CLAIMED → RUNNING(started_at)이다."""
        return await update_where(
            self._session,
            AgentRun,
            run_id,
            [AgentRun.status.in_(CLAIMED_STATUSES), _is_lease_alive(now)],
            {
                "status": AgentRunStatus.RUNNING,
                "started_at": func.coalesce(AgentRun.started_at, now),
                "lease_until": lease_until,
                "updated_at": now,
            },
        )

    async def finish(self, run_id: uuid.UUID, now: datetime, values: dict[str, Any]) -> bool:
        """러너의 결과를 적는다. 임대·마감이 남아 있을 때만."""
        return await update_where(
            self._session,
            AgentRun,
            run_id,
            [AgentRun.status.in_(CLAIMED_STATUSES), _is_lease_alive(now)],
            {**values, "finished_at": now, "updated_at": now},
        )

    async def expire(self, run_id: uuid.UUID, now: datetime, failure_code: str) -> bool:
        return await update_where(
            self._session,
            AgentRun,
            run_id,
            [_is_overdue(now)],
            {
                "status": AgentRunStatus.TIMED_OUT,
                "failure_code": failure_code,
                "finished_at": now,
                "updated_at": now,
            },
        )

    async def cancel(self, run_id: uuid.UUID, now: datetime) -> bool:
        return await update_where(
            self._session,
            AgentRun,
            run_id,
            [AgentRun.status.in_(OPEN_STATUSES)],
            {"status": AgentRunStatus.CANCELLED, "finished_at": now, "updated_at": now},
        )
