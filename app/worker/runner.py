"""일꾼: 할 일 표를 폴링해 하나씩 집고(SKIP LOCKED + 임대) 한 단계 진행한 뒤 결과를 돌려놓는다.

할 일 종류(kind)마다 처리기가 하나다(배포 요청·서비스 내리기·이전 계획). 모르는 종류는 예외다.

- claim_next_job(): 실행할 할 일 하나를 집는다(RUNNING + 새 lease_token).
- process(job): 단계 하나를 진행하고, 같은 lease_token 일 때만 결과(DONE·다시·FAILED)를 적는다.
- run_once(): 둘을 한 번. 시험은 이것과 주입한 시계로 결정적으로 돌린다.
- run(stop): stop 이 설정될 때까지 worker_concurrency 개의 루프를 돌린다. 지금 실행만 끝내고 멈춘다.
"""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.app_secret_store import AppSecretStore
from app.clients.source_repository import SourceRepositoryClient
from app.core.clock import Clock
from app.core.config import Settings
from app.core.logging import log_context
from app.enums import JobKind
from app.executors import ExecutorSet
from app.repositories.job_repository import ClaimedJob, JobRepository
from app.services.deploy_request_processor import DeployRequestProcessor
from app.services.job_lease import JobLease, JobOutcome, LeaseLostError
from app.services.migration_plan_processor import MigrationPlanProcessor
from app.services.service_teardown_processor import ServiceTeardownProcessor

logger = logging.getLogger(__name__)


class UnknownJobKindError(Exception):
    """처리기가 없는 할 일 종류. 조용히 DONE 으로 닫지 않고 올린다(임대가 끝나면 다시 집힌다)."""


class Worker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        clock: Clock,
        executors: ExecutorSet,
        source_client: SourceRepositoryClient,
        app_secret_store: AppSecretStore,
        owner: str,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._clock = clock
        self._executors = executors
        self._source_client = source_client
        self._app_secret_store = app_secret_store
        self._owner = owner
        self._lease = timedelta(seconds=settings.worker_lease_seconds)

    async def claim_next_job(self) -> ClaimedJob | None:
        async with self._session_factory() as session, session.begin():
            return await JobRepository(session).claim_next(
                self._clock.now(), self._lease, self._owner
            )

    async def process(self, job: ClaimedJob) -> bool:
        """단계 하나를 진행한다. 결과를 적었으면 True, 임대를 잃어 버렸으면 False."""
        with log_context(job_id=str(job.id), job_kind=job.kind, subject_id=str(job.subject_id)):
            async with self._session_factory() as session:
                lease = JobLease(session, job, self._clock, self._lease)
                try:
                    outcome = await self._advance(session, lease)
                except LeaseLostError:
                    logger.warning(
                        "job lease lost, result discarded", extra={"action": "process_job"}
                    )
                    return False
                async with session.begin():
                    released = await JobRepository(session).release(
                        job.id,
                        job.lease_token,
                        self._clock.now(),
                        outcome.status,
                        run_after=outcome.run_after,
                        last_error_code=outcome.error_code,
                    )
            if not released:
                logger.warning(
                    "job lease lost before release, result discarded",
                    extra={"action": "process_job"},
                )
            return released

    async def run_once(self) -> bool:
        """할 일 하나를 처리했으면 True, 집을 할 일이 없으면 False."""
        job = await self.claim_next_job()
        if job is None:
            return False
        await self.process(job)
        return True

    async def run(self, stop: asyncio.Event) -> None:
        async with asyncio.TaskGroup() as group:
            for index in range(self._settings.worker_concurrency):
                group.create_task(self._loop(stop, index))

    async def _loop(self, stop: asyncio.Event, index: int) -> None:
        interval = self._settings.worker_poll_interval_seconds
        while not stop.is_set():
            try:
                worked = await self.run_once()
            except Exception:
                # 한 할 일의 예상 못 한 오류로 일꾼이 멈추지 않게 한다. 그 할 일은 임대가 끝나면
                # 다른 일꾼(또는 이 일꾼)이 다시 집고, 3번 잃으면 WORKER_LOST 로 끝난다.
                logger.exception("job processing crashed", extra={"action": "run_worker"})
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                except TimeoutError:
                    pass
        logger.info("worker loop stopped", extra={"action": "run_worker", "loop": index})

    async def _advance(self, session: AsyncSession, lease: JobLease) -> JobOutcome:
        match lease.job.kind:
            case JobKind.DEPLOY_REQUEST:
                return await DeployRequestProcessor(
                    session,
                    lease,
                    self._settings,
                    self._clock,
                    self._executors,
                    self._source_client,
                    self._app_secret_store,
                ).advance()
            case JobKind.SERVICE_TEARDOWN:
                return await ServiceTeardownProcessor(
                    session, lease, self._settings, self._clock, self._executors
                ).advance()
            case JobKind.MIGRATION_PLAN:
                return await MigrationPlanProcessor(
                    session, lease, self._settings, self._clock, self._executors
                ).advance()
            case _:
                raise UnknownJobKindError(str(lease.job.kind))
