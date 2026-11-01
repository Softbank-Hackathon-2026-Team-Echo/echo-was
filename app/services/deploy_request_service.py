"""배포 요청 접수·조회 유스케이스. 접수만 동기로 하고 실행은 일꾼이 한다.

접수 순서(잠금 순서: 환경 → 서비스 → 연결 → 요청 → 할 일):
1. simulate 는 가짜 실행기 모드에서만 받는다.
2. 서비스·환경을 읽어 거절할 것을 먼저 거절한다(트랜잭션 밖 GitHub 호출 전에).
3. 브랜치·커밋을 40자 SHA 로 고정한다(트랜잭션 밖).
4. 환경 행을 FOR UPDATE 로 잠그고 다시 확인한 뒤(환경 삭제와 경쟁 방지), 서비스·연결을 잠그고
   연결이 없으면 만든다. 같은 연결의 대기 요청은 SUPERSEDED 로 밀어내고 새 요청과 할 일을 넣는다.
"""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.source_repository import SourceRepositoryClient
from app.core.clock import Clock
from app.core.config import ExecutorMode
from app.core.exceptions import (
    DeployRequestNotFoundError,
    EnvironmentUnhealthyError,
    PublicHostConflictError,
    ServiceDeletingError,
    SimulateNotAllowedError,
)
from app.core.repo_url import parse_repo_url
from app.enums import (
    DeployRequestStatus,
    DeployRequestTrigger,
    EnvironmentConnectionStatus,
    RefType,
    ServiceStatus,
    SimulatedFailureStage,
)
from app.models import Build, DeployRequest, Environment, Service, ServiceEnvironment
from app.models.service_environment import PUBLIC_HOST_UNIQUE_INDEX
from app.repositories.build_repository import BuildRepository
from app.repositories.deploy_request_repository import DeployRequestRepository
from app.repositories.environment_repository import EnvironmentRepository
from app.repositories.job_repository import JobRepository
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.repositories.service_repository import ServiceRepository
from app.services.deploy_support import (
    SIMULATE_FAIL_AT,
    build_config_snapshot,
    connection_public_host,
    enqueue_deploy_request,
)
from app.services.integrity import violated_constraint_name

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CreateDeployCommand:
    environment_id: uuid.UUID
    ref_type: RefType
    ref_value: str
    simulate_fail_at: SimulatedFailureStage | None


@dataclass(frozen=True)
class DeployRequestView:
    deploy_request: DeployRequest
    environment: Environment
    build: Build | None


@dataclass(frozen=True)
class DeployRequestSearchResult:
    items: list[DeployRequestView]
    total: int


class DeployRequestService:
    def __init__(
        self,
        session: AsyncSession,
        source_client: SourceRepositoryClient,
        clock: Clock,
        executor_mode: ExecutorMode,
        public_base_domain: str,
    ) -> None:
        self._session = session
        self._source_client = source_client
        self._clock = clock
        self._executor_mode = executor_mode
        self._public_base_domain = public_base_domain
        self._services = ServiceRepository(session)
        self._environments = EnvironmentRepository(session)
        self._service_environments = ServiceEnvironmentRepository(session)
        self._deploy_requests = DeployRequestRepository(session)
        self._builds = BuildRepository(session)
        self._jobs = JobRepository(session)

    async def create_deploy_request(
        self, service_id: uuid.UUID, command: CreateDeployCommand
    ) -> DeployRequestView:
        if command.simulate_fail_at is not None and self._executor_mode is not ExecutorMode.FAKE:
            raise SimulateNotAllowedError("simulate is only accepted with fake executors")

        async with self._session.begin():
            service = await self._services.get_live_by_id(service_id)
            _check_service_accepts(service)
            environment = await self._environments.get_live_by_id(command.environment_id)
            _check_environment_accepts(environment)
            repo = parse_repo_url(service.repo_url)

        commit_sha = await self._source_client.resolve_ref(repo, command.ref_value)

        now = self._clock.now()
        async with self._session.begin():
            environment = await self._environments.get_live_by_id(
                command.environment_id, for_update=True
            )
            _check_environment_accepts(environment)
            service = await self._services.get_live_by_id(service_id, lock=True)
            _check_service_accepts(service)
            connections = await self._service_environments.lock_live_by_service(service.id)
            connection = next(
                (item for item in connections if item.environment_id == environment.id), None
            )
            if connection is None:
                connection = await self._create_connection(service, environment)
            request = DeployRequest(
                id=uuid.uuid4(),
                service_id=service.id,
                environment_id=environment.id,
                service_environment_id=connection.id,
                trigger=DeployRequestTrigger.MANUAL,
                ref_type=command.ref_type,
                ref_value=command.ref_value,
                repo_url=service.repo_url,
                commit_sha=commit_sha,
                status=DeployRequestStatus.QUEUED,
                config_snapshot=build_config_snapshot(service, connection),
                simulate=(
                    {SIMULATE_FAIL_AT: command.simulate_fail_at.value}
                    if command.simulate_fail_at is not None
                    else None
                ),
                simulated=self._executor_mode is ExecutorMode.FAKE,
                created_at=now,
                updated_at=now,
            )
            superseded_id = await enqueue_deploy_request(
                self._session, self._deploy_requests, self._jobs, request, now
            )
        logger.info(
            "deploy request accepted",
            extra={
                "action": "create_deploy_request",
                "deploy_request_id": str(request.id),
                "service_id": str(service.id),
                "environment_id": str(environment.id),
                "service_environment_id": str(connection.id),
                "superseded_request_id": str(superseded_id) if superseded_id else None,
            },
        )
        return DeployRequestView(deploy_request=request, environment=environment, build=None)

    async def search_deploy_requests(
        self,
        service_id: uuid.UUID,
        environment_id: uuid.UUID | None,
        page: int,
        size: int,
    ) -> DeployRequestSearchResult:
        async with self._session.begin():
            await self._services.get_live_by_id(service_id)
            requests = await self._deploy_requests.search_by_service(
                service_id, environment_id, offset=page * size, limit=size
            )
            total = await self._deploy_requests.count_by_service(service_id, environment_id)
            views = await self._views(requests)
        return DeployRequestSearchResult(items=views, total=total)

    async def get_deploy_request(self, deploy_request_id: uuid.UUID) -> DeployRequestView:
        async with self._session.begin():
            request = await self._deploy_requests.find_by_id(deploy_request_id)
            if request is None:
                raise DeployRequestNotFoundError(
                    "deploy request not found", deploy_request_id=str(deploy_request_id)
                )
            return (await self._views([request]))[0]

    async def _create_connection(
        self, service: Service, environment: Environment
    ) -> ServiceEnvironment:
        """그 환경의 첫 요청 때 연결을 만든다. 주소는 이때 한 번 정해 저장한다."""
        public_host = connection_public_host(
            service.name, environment.name, self._public_base_domain
        )
        if await self._service_environments.is_public_host_in_use(public_host):
            raise PublicHostConflictError(
                "public host already in use", environment_id=str(environment.id)
            )
        connection = ServiceEnvironment(
            service_id=service.id,
            environment_id=environment.id,
            traffic_weight=0,
            public_host=public_host,
        )
        self._service_environments.add(connection)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if violated_constraint_name(exc) == PUBLIC_HOST_UNIQUE_INDEX:
                raise PublicHostConflictError(
                    "public host already in use", environment_id=str(environment.id)
                ) from None
            raise
        return connection

    async def _views(self, requests: list[DeployRequest]) -> list[DeployRequestView]:
        environment_ids = {request.environment_id for request in requests}
        build_ids = {request.build_id for request in requests if request.build_id is not None}
        environments = {
            environment.id: environment
            for environment in await self._session.scalars(
                select(Environment).where(Environment.id.in_(environment_ids))
            )
        }
        builds = {
            build.id: build
            for build in await self._session.scalars(select(Build).where(Build.id.in_(build_ids)))
        }
        return [
            DeployRequestView(
                deploy_request=request,
                environment=environments[request.environment_id],
                build=builds.get(request.build_id) if request.build_id else None,
            )
            for request in requests
        ]


def _check_service_accepts(service: Service) -> None:
    if service.status == ServiceStatus.DELETING:
        raise ServiceDeletingError("service is being deleted", service_id=str(service.id))


def _check_environment_accepts(environment: Environment) -> None:
    if environment.connection_status == EnvironmentConnectionStatus.FAILED:
        raise EnvironmentUnhealthyError(
            "environment connection check failed", environment_id=str(environment.id)
        )
