"""서비스 등록·조회·수정·삭제 유스케이스.

소스 저장소 확인(check_access)은 트랜잭션 밖에서 하고, 저장은 짧은 트랜잭션에서 한다.
삭제는 바로 지우지 않는다. 서비스를 DELETING 으로 바꾸고 대기 요청을 취소한 뒤 내리기 할 일을
만든다(202). 일꾼이 모든 연결을 내리면 서비스를 소프트 삭제한다.
"""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.actions import AuditAction, AuditComponent, AuditTargetType
from app.audit.lifecycle import record_deploy_request_finished
from app.audit.recorder import AuditActor, AuditRecorder, AuditTarget
from app.clients.source_repository import SourceRepositoryClient
from app.core.clock import Clock
from app.core.exceptions import (
    PublicHostConflictError,
    ServiceDeletingError,
    ServiceNameConflictError,
    ServiceNameImmutableError,
)
from app.core.repo_url import normalize_repo_url, parse_repo_url
from app.enums import (
    DeployLogCode,
    DeployRequestCancelReason,
    DeployRequestStatus,
    JobKind,
    JobStatus,
    ServiceStatus,
)
from app.models import Job, Service
from app.repositories.deploy_request_repository import DeployRequestRepository
from app.repositories.job_repository import JobRepository
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.repositories.service_repository import ServiceRepository
from app.services.deploy_job_log import DeployJobLog
from app.services.deploy_support import HostSuffixGenerator, service_public_host
from app.services.integrity import violated_constraint_name
from app.services.migration_guard import check_no_migration_in_progress
from app.services.service_view import ServiceDetailView, load_service_detail

logger = logging.getLogger(__name__)

NAME_UNIQUE_INDEX = "uq_services_org_name_live"
PUBLIC_HOST_UNIQUE_INDEX = "uq_services_public_host_live"
# 대표 주소가 이미 쓰이면 새 suffix 로 다시 시도하는 횟수(첫 시도 제외).
PUBLIC_HOST_RETRY_LIMIT = 3


class _PublicHostTakenError(Exception):
    """이 suffix 로 만든 대표 주소가 이미 쓰인다. 트랜잭션을 되돌리고 새 suffix 로 다시 시도한다."""


@dataclass(frozen=True)
class CreateServiceCommand:
    organization_id: uuid.UUID
    name: str
    description: str | None
    repo_url: str
    health_check_path: str
    health_check_timeout_seconds: int
    container_port: int


@dataclass(frozen=True)
class ServiceChanges:
    """수정 요청에서 들어온 칸. *_set 이 False 면 '들어오지 않음'이다."""

    name: str | None = None
    description: str | None = None
    description_set: bool = False
    repo_url: str | None = None
    health_check_path: str | None = None
    health_check_timeout_seconds: int | None = None
    container_port: int | None = None


@dataclass(frozen=True)
class ServiceSearchResult:
    items: list[Service]
    total: int


class ServiceService:
    def __init__(
        self,
        session: AsyncSession,
        source_client: SourceRepositoryClient,
        clock: Clock,
        public_base_domain: str,
        host_suffix_generator: HostSuffixGenerator,
    ) -> None:
        self._session = session
        self._source_client = source_client
        self._clock = clock
        self._public_base_domain = public_base_domain
        self._host_suffix_generator = host_suffix_generator
        self._services = ServiceRepository(session)
        self._service_environments = ServiceEnvironmentRepository(session)
        self._deploy_requests = DeployRequestRepository(session)
        self._jobs = JobRepository(session)
        self._job_log = DeployJobLog(session)
        self._audit = AuditRecorder(session, clock)

    def _record_audit(
        self,
        action: AuditAction,
        service: Service,
        actor_user_id: uuid.UUID,
        details: dict[str, object] | None = None,
    ) -> None:
        self._audit.record(
            organization_id=service.organization_id,
            actor=AuditActor.user(actor_user_id),
            action=action,
            target=AuditTarget(AuditTargetType.SERVICE, service.id, service.name),
            details=details,
        )

    async def create_service(
        self, command: CreateServiceCommand, actor_user_id: uuid.UUID
    ) -> ServiceDetailView:
        """저장소 접근을 확인하고 서비스를 ACTIVE 로 저장한다.

        대표 주소 `{이름}-{suffix}.{도메인}` 은 이때 한 번 정한다. 이름은 조직 안에서만 유일하다.
        주소가 이미 쓰이면 새 suffix 로 PUBLIC_HOST_RETRY_LIMIT 번까지 다시 시도하고, 그래도 겹치면
        409 PUBLIC_HOST_CONFLICT 다. 감사 줄은 시도마다 같은 블록에 쓰므로 커밋된 시도만 남는다.
        """
        repo = parse_repo_url(command.repo_url)
        access = await self._source_client.check_access(repo)
        for attempt in range(PUBLIC_HOST_RETRY_LIMIT + 1):
            host_suffix = self._host_suffix_generator()
            service = Service(
                name=command.name,
                description=command.description,
                repo_url=repo.url,
                repo_private=access.private,
                health_check_path=command.health_check_path,
                health_check_timeout_seconds=command.health_check_timeout_seconds,
                container_port=command.container_port,
                host_suffix=host_suffix,
                public_host=service_public_host(
                    command.name, host_suffix, self._public_base_domain
                ),
                status=ServiceStatus.ACTIVE,
                simulated=self._source_client.simulated,
                organization_id=command.organization_id,
            )
            try:
                view = await self._insert_service(service, actor_user_id)
            except _PublicHostTakenError:
                logger.warning(
                    "service public host collided",
                    extra={"action": "create_service", "attempt": attempt + 1},
                )
                continue
            _log("service created", "create_service", service)
            return view
        raise PublicHostConflictError("public host already in use")

    async def search_services(
        self, organization_id: uuid.UUID, page: int, size: int
    ) -> ServiceSearchResult:
        async with self._session.begin():
            services = await self._services.search_live(
                organization_id, offset=page * size, limit=size
            )
            total = await self._services.count_live(organization_id)
        return ServiceSearchResult(items=services, total=total)

    async def get_service(
        self, organization_id: uuid.UUID, service_id: uuid.UUID
    ) -> ServiceDetailView:
        async with self._session.begin():
            service = await self._services.get_live_by_id(organization_id, service_id)
            return await load_service_detail(self._session, service, self._clock.now())

    async def update_service(
        self,
        organization_id: uuid.UUID,
        service_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        changes: ServiceChanges,
    ) -> ServiceDetailView:
        """바뀐 값은 다음 배포 요청부터 쓴다. 접수된 요청은 접수 때 사본을 쓴다.

        바뀐 칸이 없으면 감사 줄을 쓰지 않고, 줄에는 바뀐 칸 이름만 적는다(값은 적지 않는다).
        """
        async with self._session.begin():
            service = await self._services.get_live_by_id(organization_id, service_id)
            _check_updatable(service, changes)
        repo_private: bool | None = None
        new_repo_url: str | None = None
        if changes.repo_url is not None:
            new_repo_url = normalize_repo_url(changes.repo_url)
            if new_repo_url != service.repo_url:
                access = await self._source_client.check_access(parse_repo_url(new_repo_url))
                repo_private = access.private
        async with self._session.begin():
            service = await self._services.get_live_by_id(organization_id, service_id, lock=True)
            _check_updatable(service, changes)
            changed_fields: list[str] = []
            if changes.description_set and changes.description != service.description:
                changed_fields.append("description")
            if changes.description_set:
                service.description = changes.description
            if new_repo_url is not None and repo_private is not None:
                if new_repo_url != service.repo_url:
                    changed_fields.append("repoUrl")
                service.repo_url = new_repo_url
                service.repo_private = repo_private
            if changes.health_check_path not in (None, service.health_check_path):
                changed_fields.append("healthCheckPath")
            if changes.health_check_path is not None:
                service.health_check_path = changes.health_check_path
            if changes.health_check_timeout_seconds not in (
                None,
                service.health_check_timeout_seconds,
            ):
                changed_fields.append("healthCheckTimeoutSeconds")
            if changes.health_check_timeout_seconds is not None:
                service.health_check_timeout_seconds = changes.health_check_timeout_seconds
            if changes.container_port not in (None, service.container_port):
                changed_fields.append("containerPort")
            if changes.container_port is not None:
                service.container_port = changes.container_port
            await self._session.flush()
            if changed_fields:
                self._record_audit(
                    AuditAction.SERVICE_UPDATED,
                    service,
                    actor_user_id,
                    {"changedFields": changed_fields},
                )
            view = await load_service_detail(self._session, service, self._clock.now())
        _log("service updated", "update_service", service)
        return view

    async def delete_service(
        self, organization_id: uuid.UUID, service_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> ServiceDetailView:
        """DELETING 으로 바꾸고 대기 요청을 취소한 뒤 내리기 할 일을 만든다.

        실행 중인 이전 계획이 있으면 409 MIGRATION_IN_PROGRESS 다. 이미 DELETING 이면 재시도다.
        내리기에 실패해 남은 연결은 teardown_attempt 를 1 늘려 새 멱등 키로 다시 내린다.
        감사 줄은 상태를 DELETING 으로 바꾼 요청만 쓴다(재시도는 쓰지 않는다).
        """
        now = self._clock.now()
        async with self._session.begin():
            service = await self._services.get_live_by_id(organization_id, service_id, lock=True)
            connections = await self._service_environments.lock_live_by_service(service.id)
            await check_no_migration_in_progress(self._session, service.id)
            is_retry = service.status == ServiceStatus.DELETING
            if is_retry:
                for connection in connections:
                    if connection.teardown_error is not None:
                        connection.teardown_attempt += 1
                        connection.teardown_error = None
                        connection.teardown_requested_at = None
                        connection.teardown_external_ref = None
            service.status = ServiceStatus.DELETING
            service.delete_error = None
            if not is_retry:
                self._record_audit(AuditAction.SERVICE_DELETE_REQUESTED, service, actor_user_id)
            for queued in await self._deploy_requests.search_queued_by_service(service.id):
                if await self._deploy_requests.transition(
                    queued.id,
                    DeployRequestStatus.QUEUED,
                    {
                        "status": DeployRequestStatus.CANCELLED,
                        "cancel_reason": DeployRequestCancelReason.SERVICE_DELETED,
                        "finished_at": now,
                        "updated_at": now,
                    },
                ):
                    await self._job_log.append(queued, DeployLogCode.CANCELLED_SERVICE_DELETED, now)
                    await record_deploy_request_finished(
                        self._session,
                        self._clock,
                        queued,
                        status=DeployRequestStatus.CANCELLED,
                        component=AuditComponent.API,
                        cancel_reason=DeployRequestCancelReason.SERVICE_DELETED,
                    )
                    await self._jobs.cancel_queued_for_subject(queued.id, now)
            if await self._jobs.find_open_for_subject(service.id) is None:
                self._jobs.add(
                    Job(
                        kind=JobKind.SERVICE_TEARDOWN,
                        subject_id=service.id,
                        status=JobStatus.QUEUED,
                        run_after=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
            await self._session.flush()
            view = await load_service_detail(self._session, service, self._clock.now())
        _log("service deletion requested", "delete_service", service)
        return view

    async def _insert_service(
        self, service: Service, actor_user_id: uuid.UUID
    ) -> ServiceDetailView:
        """한 트랜잭션에서 이름·주소를 확인하고 넣는다. 주소가 쓰이면 _PublicHostTakenError."""
        async with self._session.begin():
            if (
                await self._services.find_live_by_name(service.organization_id, service.name)
                is not None
            ):
                raise ServiceNameConflictError("service name already in use")
            # 대표·환경별 주소는 같은 `*.도메인` 아래에 있으므로 두 표 모두에서 겹치지 않아야 한다.
            if await self._services.is_public_host_in_use(
                service.public_host
            ) or await self._service_environments.is_public_host_in_use(service.public_host):
                raise _PublicHostTakenError
            self._services.add(service)
            await self._flush()
            self._record_audit(AuditAction.SERVICE_CREATED, service, actor_user_id)
            return await load_service_detail(self._session, service, self._clock.now())

    async def _flush(self) -> None:
        try:
            await self._session.flush()
        except IntegrityError as exc:
            constraint = violated_constraint_name(exc)
            if constraint == NAME_UNIQUE_INDEX:
                raise ServiceNameConflictError("service name already in use") from None
            if constraint == PUBLIC_HOST_UNIQUE_INDEX:
                raise _PublicHostTakenError from None
            raise


def _check_updatable(service: Service, changes: ServiceChanges) -> None:
    if service.status == ServiceStatus.DELETING:
        raise ServiceDeletingError("service is being deleted", service_id=str(service.id))
    if changes.name is not None and changes.name != service.name:
        raise ServiceNameImmutableError(
            "service name cannot be changed", service_id=str(service.id)
        )


def _log(message: str, action: str, service: Service) -> None:
    logger.info(
        message,
        extra={"action": action, "service_id": str(service.id), "status": service.status},
    )
