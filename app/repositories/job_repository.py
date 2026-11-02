import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import JobKind, JobStatus
from app.models import Job
from app.repositories.conditional_update import update_where


@dataclass(frozen=True)
class ClaimedJob:
    """일꾼이 집은 할 일. 결과는 같은 lease_token 일 때만 반영한다."""

    id: uuid.UUID
    kind: JobKind
    subject_id: uuid.UUID
    lease_token: uuid.UUID
    lease_losses: int


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, job: Job) -> None:
        self._session.add(job)

    async def claim_next(self, now: datetime, lease: timedelta, owner: str) -> ClaimedJob | None:
        """실행할 할 일 하나를 집는다. 다른 일꾼이 잠근 행은 건너뛴다(SKIP LOCKED).

        임대가 끝난 RUNNING 할 일을 집으면 lease_losses 를 1 늘린다.
        """
        statement = (
            select(Job)
            .where(
                or_(
                    and_(Job.status == JobStatus.QUEUED, Job.run_after <= now),
                    and_(Job.status == JobStatus.RUNNING, Job.lease_expires_at < now),
                )
            )
            .order_by(Job.run_after, Job.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        job = await self._session.scalar(statement)
        if job is None:
            return None
        lease_losses = job.lease_losses + (1 if job.status == JobStatus.RUNNING else 0)
        lease_token = uuid.uuid4()
        await update_where(
            self._session,
            Job,
            job.id,
            [],
            {
                "status": JobStatus.RUNNING,
                "lease_token": lease_token,
                "lease_owner": owner,
                "lease_expires_at": now + lease,
                "lease_losses": lease_losses,
                "updated_at": now,
            },
        )
        return ClaimedJob(
            id=job.id,
            kind=JobKind(job.kind),
            subject_id=job.subject_id,
            lease_token=lease_token,
            lease_losses=lease_losses,
        )

    async def hold_lease(
        self, job_id: uuid.UUID, lease_token: uuid.UUID, now: datetime, lease: timedelta
    ) -> bool:
        """임대가 아직 내 것이면 행을 잠그고 임대를 늘린다. 잠금 순서상 할 일 행은 맨 마지막이다."""
        return await update_where(
            self._session,
            Job,
            job_id,
            [Job.status == JobStatus.RUNNING, Job.lease_token == lease_token],
            {"lease_expires_at": now + lease, "updated_at": now},
        )

    async def release(
        self,
        job_id: uuid.UUID,
        lease_token: uuid.UUID,
        now: datetime,
        status: JobStatus,
        *,
        run_after: datetime | None = None,
        last_error_code: str | None = None,
    ) -> bool:
        """임대를 내려놓는다. status=QUEUED 면 run_after 뒤에 다시 집힌다."""
        values: dict[str, Any] = {
            "status": status,
            "lease_token": None,
            "lease_owner": None,
            "lease_expires_at": None,
            "updated_at": now,
        }
        if run_after is not None:
            values["run_after"] = run_after
        if last_error_code is not None:
            values["last_error_code"] = last_error_code
        return await update_where(
            self._session,
            Job,
            job_id,
            [Job.status == JobStatus.RUNNING, Job.lease_token == lease_token],
            values,
        )

    async def cancel_queued_for_subject(self, subject_id: uuid.UUID, now: datetime) -> int:
        """대상의 QUEUED 할 일을 CANCELLED 로 닫는다.

        RUNNING 은 그대로 둔다(그 일꾼이 다음 실행에서 끝 상태를 보고 닫는다).
        """
        statement = (
            update(Job)
            .where(Job.subject_id == subject_id, Job.status == JobStatus.QUEUED)
            .values(status=JobStatus.CANCELLED, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        result = cast(CursorResult[Any], await self._session.execute(statement))
        return result.rowcount

    async def pull_open_for_subject(self, subject_id: uuid.UUID, now: datetime) -> int:
        """대상의 열린(QUEUED·RUNNING) 할 일의 run_after 를 지금으로 당긴다. 바꾼 행 수.

        RUNNING 할 일은 그 일꾼이 결과를 적을 때 run_after 를 다시 정한다(중단은 다음 실행 맨 앞에서
        본다).
        """
        statement = (
            update(Job)
            .where(
                Job.subject_id == subject_id,
                Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
            )
            .values(run_after=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        result = cast(CursorResult[Any], await self._session.execute(statement))
        return result.rowcount

    async def find_open_for_subject(self, subject_id: uuid.UUID) -> Job | None:
        statement = (
            select(Job)
            .where(
                Job.subject_id == subject_id,
                Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
            )
            .execution_options(populate_existing=True)
        )
        return await self._session.scalar(statement)
