"""일꾼이 삭제 중인 서비스의 연결을 모두 내리고 서비스를 소프트 삭제한다.

1. 서비스 → 연결들을 잠그고 남은 대기 요청을 취소한다. 실행 중 요청이 있으면 끝날 때까지 미룬다.
2. 연결마다 내리기를 진행한다(멱등 키 = 연결 id + teardown_attempt, "요청함 시각 저장 → 호출 →
   번호 저장"). 내린 연결은 바로 소프트 삭제한다(배포 기록은 남는다).
3. 연결이 모두 내려가면 서비스를 소프트 삭제한다. 한 연결이라도 실패·시간 초과면 서비스는
   DELETING 에 남고 delete_error 를 적은 뒤 할 일을 끝낸다. 다시 DELETE 를 보내면 남은 연결만
   내린다.
"""

import logging
import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.enums import (
    DeployRequestCancelReason,
    DeployRequestStatus,
    ServiceStatus,
    TeardownErrorCode,
)
from app.executors import ExecutorSet
from app.executors.base import TeardownFailed, TeardownPending, TeardownRemoved
from app.models import Service, ServiceEnvironment
from app.repositories.deploy_request_repository import DeployRequestRepository
from app.repositories.job_repository import JobRepository
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.repositories.service_repository import ServiceRepository
from app.services.deploy_support import deploy_target
from app.services.job_lease import JobLease, JobOutcome

logger = logging.getLogger(__name__)


class ServiceTeardownProcessor:
    def __init__(
        self,
        session: AsyncSession,
        lease: JobLease,
        settings: Settings,
        clock: Clock,
        executors: ExecutorSet,
    ) -> None:
        self._session = session
        self._lease = lease
        self._settings = settings
        self._clock = clock
        self._executors = executors
        self._services = ServiceRepository(session)
        self._connections = ServiceEnvironmentRepository(session)
        self._requests = DeployRequestRepository(session)
        self._jobs = JobRepository(session)
        self._service_id = lease.job.subject_id

    async def advance(self) -> JobOutcome:
        if self._lease.is_exhausted:
            await self._record_delete_error(TeardownErrorCode.WORKER_LOST)
            return JobOutcome.failed(TeardownErrorCode.WORKER_LOST)

        now = self._clock.now()
        async with self._session.begin():
            service = await self._services.find_by_id(self._service_id, lock=True)
            if service is None or service.is_deleted or service.status != ServiceStatus.DELETING:
                await self._lease.hold()
                return JobOutcome.done()
            connections = await self._connections.lock_live_by_service(service.id)
            for queued in await self._requests.search_queued_by_service(service.id):
                if await self._requests.transition(
                    queued.id,
                    DeployRequestStatus.QUEUED,
                    {
                        "status": DeployRequestStatus.CANCELLED,
                        "cancel_reason": DeployRequestCancelReason.SERVICE_DELETED,
                        "finished_at": now,
                        "updated_at": now,
                    },
                ):
                    await self._jobs.cancel_queued_for_subject(queued.id, now)
            has_active = await self._requests.has_active_for_service(service.id)
            await self._lease.hold()
        if has_active:
            # 진행 중 요청은 끝까지 진행되고, 그 뒤에 내린다.
            return JobOutcome.again(
                now + timedelta(seconds=self._settings.worker_poll_interval_seconds)
            )

        for connection in connections:
            if connection.teardown_error is None:
                await self._advance_connection(service, connection)

        return await self._finish()

    async def _advance_connection(self, service: Service, snapshot: ServiceEnvironment) -> None:
        attempt = snapshot.teardown_attempt
        key = f"{snapshot.id}:{attempt}"
        now = self._clock.now()
        if snapshot.teardown_requested_at is None:
            async with self._session.begin():
                connection = await self._lock_connection(snapshot.id, attempt)
                if connection is None:
                    return
                if connection.teardown_requested_at is None:
                    connection.teardown_requested_at = now
            # 세션의 같은 객체라 snapshot 에도 방금 적은 시각이 들어 있다.

        if snapshot.teardown_external_ref is None:
            async with self._session.begin():
                current = await self._connections.find_by_id(snapshot.id)
            assert current is not None
            await self._lease.extend()
            external_ref = await self._executors.teardown.remove(
                deploy_target(service, current, current.environment), idempotency_key=key
            )
            async with self._session.begin():
                connection = await self._lock_connection(snapshot.id, attempt)
                if connection is not None and connection.teardown_external_ref is None:
                    connection.teardown_external_ref = external_ref
            return

        timeout = timedelta(seconds=self._settings.teardown_timeout_seconds)
        assert snapshot.teardown_requested_at is not None
        if now - snapshot.teardown_requested_at > timeout:
            await self._record_connection_error(
                snapshot.id, attempt, TeardownErrorCode.TEARDOWN_TIMEOUT
            )
            return

        await self._lease.extend()
        result = await self._executors.teardown.poll(snapshot.teardown_external_ref)
        match result:
            case TeardownPending():
                return
            case TeardownFailed(code=code):
                await self._record_connection_error(snapshot.id, attempt, code)
            case TeardownRemoved():
                async with self._session.begin():
                    connection = await self._lock_connection(snapshot.id, attempt)
                    if connection is not None:
                        connection.mark_as_deleted(self._clock.now())
                logger.info(
                    "service environment removed",
                    extra={
                        "action": "teardown_service_environment",
                        "service_id": str(service.id),
                        "service_environment_id": str(snapshot.id),
                    },
                )

    async def _finish(self) -> JobOutcome:
        now = self._clock.now()
        async with self._session.begin():
            service = await self._services.find_by_id(self._service_id, lock=True)
            assert service is not None
            connections = await self._connections.lock_live_by_service(service.id)
            await self._lease.hold()
            if not connections:
                service.delete_error = None
                service.mark_as_deleted(now)
                logger.info(
                    "service deleted",
                    extra={"action": "teardown_service", "service_id": str(service.id)},
                )
                return JobOutcome.done()
            if any(connection.teardown_error is None for connection in connections):
                return JobOutcome.again(
                    now + timedelta(seconds=self._settings.worker_poll_interval_seconds)
                )
            errors = {connection.teardown_error for connection in connections}
            service.delete_error = (
                TeardownErrorCode.TEARDOWN_FAILED
                if TeardownErrorCode.TEARDOWN_FAILED in errors
                else TeardownErrorCode.TEARDOWN_TIMEOUT
            )
        logger.warning(
            "service teardown failed",
            extra={
                "action": "teardown_service",
                "service_id": str(self._service_id),
                "error_code": service.delete_error,
            },
        )
        return JobOutcome.done()

    async def _record_connection_error(
        self, connection_id: uuid.UUID, attempt: int, code: TeardownErrorCode
    ) -> None:
        async with self._session.begin():
            connection = await self._lock_connection(connection_id, attempt)
            if connection is not None and connection.teardown_error is None:
                connection.teardown_error = code
        logger.warning(
            "service environment teardown failed",
            extra={
                "action": "teardown_service_environment",
                "service_environment_id": str(connection_id),
                "error_code": code,
            },
        )

    async def _record_delete_error(self, code: TeardownErrorCode) -> None:
        async with self._session.begin():
            service = await self._services.find_by_id(self._service_id, lock=True)
            await self._lease.hold()
            if (
                service is not None
                and not service.is_deleted
                and service.status == ServiceStatus.DELETING
            ):
                service.delete_error = code

    async def _lock_connection(
        self, connection_id: uuid.UUID, attempt: int
    ) -> ServiceEnvironment | None:
        """서비스 → 연결 순서로 잠근다. 그사이 내려갔거나 재시도로 attempt 가 바뀌었으면 None."""
        await self._services.find_by_id(self._service_id, lock=True)
        connection = await self._connections.lock_by_id(connection_id)
        await self._lease.hold()
        if connection is None or connection.is_deleted or connection.teardown_attempt != attempt:
            return None
        return connection
