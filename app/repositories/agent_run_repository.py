import uuid
from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, func, literal, or_, select
from sqlalchemy.dialects.postgresql import JSONB
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
        *,
        organization_id: uuid.UUID,
        source_environment_id: uuid.UUID,
    ) -> AgentRun | None:
        """같은 조직·서비스·출발·도착의 유효한 성공 사전 점검 중 최신 실제 실행을 우선한다.

        실제 실행이 없으면 최신 모의 실행을 반환해 EVIDENCE_SIMULATED 차단 사유를 유지한다.
        모의 실행을 통과 증거로 쓰지는 않는다.
        """
        stmt = (
            select(AgentRun)
            .where(
                AgentRun.organization_id == organization_id,
                AgentRun.service_id == service_id,
                AgentRun.source_environment_id == source_environment_id,
                AgentRun.target_environment_id == target_environment_id,
                AgentRun.kind == AgentRunKind.PRECHECK,
                AgentRun.status == AgentRunStatus.SUCCEEDED,
                AgentRun.finished_at > finished_since,
            )
            .order_by(AgentRun.simulated.asc(), AgentRun.finished_at.desc(), AgentRun.id)
            .limit(1)
        )
        return (await self._session.scalars(stmt)).first()

    async def find_by_id(self, run_id: uuid.UUID, *, lock: bool = False) -> AgentRun | None:
        """lock=True 는 `FOR UPDATE` 다(진단 반영 뒤 재배포를 한 번만 잇는다, SPEC api-98)."""
        statement = (
            select(AgentRun).where(AgentRun.id == run_id).execution_options(populate_existing=True)
        )
        if lock:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

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

    async def find_patch_parent(
        self, organization_id: uuid.UUID, run_id: uuid.UUID, *, lock: bool = False
    ) -> AgentRun | None:
        """그 조직의 패치를 내는 실행(사전 점검·배포 실패 진단). lock=True 는 `FOR UPDATE` 다."""
        statement = (
            select(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.organization_id == organization_id,
                AgentRun.kind.in_([AgentRunKind.PRECHECK, AgentRunKind.CHANGE_PROPOSAL]),
            )
            .execution_options(populate_existing=True)
        )
        if lock:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def find_diagnosis(self, deploy_request_id: uuid.UUID) -> AgentRun | None:
        """그 배포 요청의 진단(CHANGE_PROPOSAL). 요청마다 하나뿐이다(부분 unique)."""
        return await self._session.scalar(
            select(AgentRun)
            .where(
                AgentRun.deploy_request_id == deploy_request_id,
                AgentRun.kind == AgentRunKind.CHANGE_PROPOSAL,
            )
            .execution_options(populate_existing=True)
        )

    async def merge_result(self, run_id: uuid.UUID, now: datetime, result: dict[str, Any]) -> bool:
        """성공한 실행의 결과 JSON 을 바꾼다(WAS 가 덧붙이는 칸, 예: 재배포 연결). 성공일 때만."""
        return await update_where(
            self._session,
            AgentRun,
            run_id,
            [AgentRun.status == AgentRunStatus.SUCCEEDED],
            {"result": result, "updated_at": now},
        )

    async def patch_result(self, run_id: uuid.UUID, now: datetime, fields: dict[str, Any]) -> bool:
        """성공한 실행의 결과 JSON 에 `fields` 칸만 덧쓴다(JSONB `||`, 나머지 칸은 그대로).

        다른 요청이 그 사이 기록한 칸(예: redeployRequestId)을 지우지 않는다(SPEC api-19 D-11).
        성공일 때만.
        """
        merged = func.coalesce(AgentRun.result, literal({}, JSONB)).op("||", return_type=JSONB)(
            literal(fields, JSONB)
        )
        return await update_where(
            self._session,
            AgentRun,
            run_id,
            [AgentRun.status == AgentRunStatus.SUCCEEDED],
            {"result": merged, "updated_at": now},
        )

    async def claim_commit(
        self, run_id: uuid.UUID, now: datetime, owner: str, expected_owner: str | None
    ) -> bool:
        """행 잠금 아래 읽은 세대와 같을 때만 저장소 작업의 새 세대를 잡는다."""
        merged = func.coalesce(AgentRun.result, literal({}, JSONB)).op("||", return_type=JSONB)(
            literal(
                {
                    "commitState": "IN_PROGRESS",
                    "commitClaimedAt": now.isoformat(),
                    "commitOwner": owner,
                },
                JSONB,
            )
        )
        owner_field = AgentRun.result["commitOwner"].astext
        return await update_where(
            self._session,
            AgentRun,
            run_id,
            [
                AgentRun.status == AgentRunStatus.SUCCEEDED,
                owner_field == expected_owner
                if expected_owner is not None
                else owner_field.is_(None),
                ~func.coalesce(AgentRun.result, literal({}, JSONB)).has_key("redeployRequestId"),
            ],
            {"result": merged, "updated_at": now},
        )

    async def patch_owned_commit(
        self,
        run_id: uuid.UUID,
        now: datetime,
        owner: str,
        fields: dict[str, Any],
        *,
        fail_code: str | None = None,
    ) -> bool:
        """현재 세대만 release/fail/publish/link한다. 끝난 연결을 덮지 않는다."""
        merged = func.coalesce(AgentRun.result, literal({}, JSONB)).op("||", return_type=JSONB)(
            literal(fields, JSONB)
        )
        values: dict[str, Any] = {"result": merged, "updated_at": now}
        if fail_code is not None:
            values.update(status=AgentRunStatus.FAILED, failure_code=fail_code)
        return await update_where(
            self._session,
            AgentRun,
            run_id,
            [
                AgentRun.status == AgentRunStatus.SUCCEEDED,
                AgentRun.result["commitOwner"].astext == owner,
                AgentRun.result["commitState"].astext.in_(["IN_PROGRESS", "DONE"]),
                ~func.coalesce(AgentRun.result, literal({}, JSONB)).has_key("redeployRequestId"),
            ],
            values,
        )

    async def fail_after_success(
        self, run_id: uuid.UUID, now: datetime, failure_code: str, result: dict[str, Any]
    ) -> bool:
        """성공으로 끝난 반영 실행을 WAS 단계 실패로 바꾼다(SPEC api-19: 저장소 반영 실패).

        러너 검증은 성공했지만 WAS 가 서비스 저장소에 커밋하지 못했을 때만 쓴다. 성공일 때만.
        """
        return await update_where(
            self._session,
            AgentRun,
            run_id,
            [AgentRun.status == AgentRunStatus.SUCCEEDED],
            {
                "status": AgentRunStatus.FAILED,
                "failure_code": failure_code,
                "result": result,
                "updated_at": now,
            },
        )

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
        self,
        organization_id: uuid.UUID,
        from_at: datetime,
        to_at: datetime,
        limit: int,
        *,
        excluded_kinds: Collection[AgentRunKind] = (),
    ) -> list[AgentRun]:
        """그 조직에서 [from_at, to_at) 안에 끝난 실행(최신순). 요약에 쓰는 칸만 읽는다.

        excluded_kinds 는 LIMIT 전에 조회 조건으로 뺀다(빼는 종류가 상한을 먹지 않게, SPEC api-98).
        """
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
        if excluded_kinds:
            statement = statement.where(AgentRun.kind.not_in(list(excluded_kinds)))
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
