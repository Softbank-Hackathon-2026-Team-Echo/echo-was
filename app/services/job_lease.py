"""할 일 임대와 실행 결과. 일꾼의 단계 처리기(배포 요청·서비스 내리기)가 함께 쓴다."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.enums import JobStatus
from app.repositories.job_repository import ClaimedJob, JobRepository

# 임대를 이만큼 잃은 할 일은 WORKER_LOST 로 끝낸다.
MAX_LEASE_LOSSES = 3


class LeaseLostError(Exception):
    """다른 일꾼이 임대를 가져갔다. 이 실행의 결과는 버린다(트랜잭션이 되돌려진다)."""


@dataclass(frozen=True)
class JobOutcome:
    """한 번 실행의 결과. QUEUED 면 run_after 뒤에 다시 집힌다."""

    status: JobStatus
    run_after: datetime | None = None
    error_code: str | None = None

    @classmethod
    def done(cls) -> "JobOutcome":
        return cls(status=JobStatus.DONE)

    @classmethod
    def again(cls, run_after: datetime) -> "JobOutcome":
        return cls(status=JobStatus.QUEUED, run_after=run_after)

    @classmethod
    def failed(cls, error_code: str) -> "JobOutcome":
        return cls(status=JobStatus.FAILED, error_code=error_code)


class JobLease:
    """집은 할 일의 임대. 쓰기 트랜잭션마다 맨 마지막에 hold() 로 할 일 행을 잠가 확인·연장한다."""

    def __init__(
        self, session: AsyncSession, job: ClaimedJob, clock: Clock, lease: timedelta
    ) -> None:
        self._session = session
        self._job = job
        self._clock = clock
        self._lease = lease
        self._jobs = JobRepository(session)

    @property
    def job(self) -> ClaimedJob:
        return self._job

    @property
    def is_exhausted(self) -> bool:
        return self._job.lease_losses >= MAX_LEASE_LOSSES

    async def hold(self) -> None:
        """열린 트랜잭션 안에서 부른다. 임대가 내 것이 아니면 LeaseLostError."""
        if not await self._jobs.hold_lease(
            self._job.id, self._job.lease_token, self._clock.now(), self._lease
        ):
            raise LeaseLostError

    async def extend(self) -> None:
        """외부 호출 직전에 임대를 늘린다(따로 커밋)."""
        async with self._session.begin():
            await self.hold()
