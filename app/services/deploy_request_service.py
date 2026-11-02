"""배포 요청 접수·되돌리기 접수·조회·로그 유스케이스. 접수만 동기로 하고 실행은 일꾼이 한다.

서비스·환경·배포 요청은 모두 경로의 조직 안에서만 찾는다. 환경이 서비스와 다른 조직이면 그 환경은
없는 것과 같다(404 ENVIRONMENT_NOT_FOUND).

접수 순서(잠금 순서: 환경 → 서비스 → 연결 → 요청 → 할 일):
1. simulate 는 가짜 실행기 모드에서만 받는다.
2. 서비스·환경을 읽어 거절할 것을 먼저 거절한다(트랜잭션 밖 GitHub 호출 전에).
3. 브랜치·커밋을 40자 SHA 로 고정한다(트랜잭션 밖).
4. 환경 행을 FOR SHARE 로 잠그고 다시 확인한 뒤(환경 삭제와 경쟁 방지), 서비스·연결을 잠그고
   연결이 없으면 만든다. 같은 연결의 대기 요청은 SUPERSEDED 로 밀어내고 새 요청과 할 일을 넣는다.

되돌리기 접수는 같은 순서로 잠그고, 이력에서 고른 성공 요청의 빌드·커밋·배포 기록 설정 사본으로
trigger = ROLLBACK 요청을 넣는다. 접수 순간 연결의 현재 배포를 rollback_base_deployment_id 에
적어 두고, 일꾼이 시작할 때 다시 비교한다.
"""

import logging
import uuid
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.source_repository import SourceRepositoryClient
from app.core.clock import Clock
from app.core.config import ExecutorMode
from app.core.exceptions import (
    AlreadyCurrentError,
    DeployRequestNotFoundError,
    EnvironmentUnhealthyError,
    NotRollbackableError,
    PublicHostConflictError,
    RequestFieldInvalidError,
    ServiceDeletingError,
    SimulateNotAllowedError,
)
from app.core.log_masking import mask_secrets
from app.core.repo_url import parse_repo_url
from app.enums import (
    DeployRequestStatus,
    DeployRequestTrigger,
    EnvironmentConnectionStatus,
    LogSource,
    RefType,
    ServiceStatus,
    SimulatedFailureStage,
)
from app.executors import ExecutorSet
from app.executors.base import InvalidLogCursor, LogChunk, LogLine
from app.models import Build, DeployRequest, Environment, Service, ServiceEnvironment
from app.models.service_environment import PUBLIC_HOST_UNIQUE_INDEX
from app.repositories.build_repository import BuildRepository
from app.repositories.deploy_request_log_repository import DeployRequestLogRepository
from app.repositories.deploy_request_repository import DeployRequestRepository
from app.repositories.deployment_repository import DeploymentRepository
from app.repositories.environment_repository import EnvironmentRepository
from app.repositories.job_repository import JobRepository
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.repositories.service_repository import ServiceRepository
from app.services.deploy_job_log import DeployJobLog
from app.services.deploy_support import (
    SIMULATE_FAIL_AT,
    build_config_snapshot,
    connection_public_host,
    deploy_target,
    deployment_spec,
    enqueue_deploy_request,
)
from app.services.integrity import violated_constraint_name

logger = logging.getLogger(__name__)

INVALID_CURSOR_REASON = "INVALID_CURSOR"


@dataclass(frozen=True)
class CreateDeployCommand:
    environment_id: uuid.UUID
    ref_type: RefType
    ref_value: str
    simulate_fail_at: SimulatedFailureStage | None


@dataclass(frozen=True)
class DeployRequestFilter:
    """이력 목록 조건. 빈 목록은 조건 없음."""

    environment_id: uuid.UUID | None = None
    triggers: Sequence[DeployRequestTrigger] = ()
    statuses: Sequence[DeployRequestStatus] = ()


@dataclass(frozen=True)
class DeployRequestView:
    """응답 한 건. is_current·rollbackable 은 연결·서비스의 지금 상태로 계산한다(저장 안 함)."""

    deploy_request: DeployRequest
    environment: Environment
    build: Build | None
    is_current: bool = False
    rollbackable: bool = False


@dataclass(frozen=True)
class DeployRequestSearchResult:
    items: list[DeployRequestView]
    total: int


@dataclass(frozen=True)
class LogPageView:
    source: LogSource
    simulated: bool
    lines: list[LogLine] = field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


class DeployRequestService:
    def __init__(
        self,
        session: AsyncSession,
        source_client: SourceRepositoryClient,
        clock: Clock,
        executor_mode: ExecutorMode,
        public_base_domain: str,
        executors: ExecutorSet | None = None,
    ) -> None:
        self._session = session
        self._source_client = source_client
        self._clock = clock
        self._executor_mode = executor_mode
        self._public_base_domain = public_base_domain
        # 로그 읽기에만 쓴다. 실제 실행기가 아직 없는 모드면 None(빌드·실행 로그는 빈 페이지).
        self._executors = executors
        self._services = ServiceRepository(session)
        self._environments = EnvironmentRepository(session)
        self._service_environments = ServiceEnvironmentRepository(session)
        self._deploy_requests = DeployRequestRepository(session)
        self._deployments = DeploymentRepository(session)
        self._builds = BuildRepository(session)
        self._jobs = JobRepository(session)
        self._logs = DeployRequestLogRepository(session)
        self._job_log = DeployJobLog(session)

    async def create_deploy_request(
        self, organization_id: uuid.UUID, service_id: uuid.UUID, command: CreateDeployCommand
    ) -> DeployRequestView:
        self._check_simulate_allowed(command.simulate_fail_at)

        async with self._session.begin():
            service = await self._services.get_live_by_id(organization_id, service_id)
            _check_service_accepts(service)
            environment = await self._environments.get_live_by_id(
                organization_id, command.environment_id
            )
            _check_environment_accepts(environment)
            repo = parse_repo_url(service.repo_url)

        commit_sha = await self._source_client.resolve_ref(repo, command.ref_value)

        now = self._clock.now()
        async with self._session.begin():
            # FOR SHARE: 환경 삭제(FOR UPDATE)와는 줄을 세우되, 일꾼이 서비스 행을 잡은 채
            # 이 환경을 FK 로 가리키는 행을 쓸 때 잡는 FOR KEY SHARE 와는 부딪히지 않게 한다
            # (FOR UPDATE 면 접수 ↔ 성공 처리 사이에 교착이 생긴다).
            environment = await self._environments.get_live_by_id(
                organization_id, command.environment_id, for_share=True
            )
            _check_environment_accepts(environment)
            service = await self._services.get_live_by_id(organization_id, service_id, lock=True)
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
                simulate=_simulate(command.simulate_fail_at),
                simulated=self._executor_mode is ExecutorMode.FAKE,
                created_at=now,
                updated_at=now,
            )
            superseded_id = await enqueue_deploy_request(
                self._session, self._deploy_requests, self._jobs, self._job_log, request, now
            )
        _log_accepted("deploy request accepted", "create_deploy_request", request, superseded_id)
        return DeployRequestView(deploy_request=request, environment=environment, build=None)

    async def create_rollback(
        self,
        organization_id: uuid.UUID,
        deploy_request_id: uuid.UUID,
        simulate_fail_at: SimulatedFailureStage | None,
    ) -> DeployRequestView:
        """이력에서 고른 성공 요청(deploy_request_id)의 이미지·설정으로 되돌리는 요청을 접수한다.

        거절 순서: 대상 요청(404) → 서비스·환경(404·409) → 상태(NOT_ROLLBACKABLE·ALREADY_CURRENT).
        새 빌드를 만들지 않고 대상의 빌드를 build_reused = true 로 연결한다.
        """
        self._check_simulate_allowed(simulate_fail_at)

        async with self._session.begin():
            target = await self._get_in_organization(organization_id, deploy_request_id)
            service = await self._services.get_live_by_id(organization_id, target.service_id)
            _check_service_accepts(service)
            environment = await self._environments.get_live_by_id(
                organization_id, target.environment_id
            )
            _check_environment_accepts(environment)

        now = self._clock.now()
        async with self._session.begin():
            environment = await self._environments.get_live_by_id(
                organization_id, target.environment_id, for_share=True
            )
            _check_environment_accepts(environment)
            service = await self._services.get_live_by_id(
                organization_id, target.service_id, lock=True
            )
            _check_service_accepts(service)
            connections = await self._service_environments.lock_live_by_service(service.id)
            connection = next(
                (item for item in connections if item.id == target.service_environment_id), None
            )
            if (
                target.status != DeployRequestStatus.SUCCEEDED
                or target.deployment_id is None
                or target.build_id is None
                or connection is None
                or connection.current_deployment_id is None
            ):
                raise NotRollbackableError(
                    "deploy request is not rollbackable", deploy_request_id=str(target.id)
                )
            if connection.current_deployment_id == target.deployment_id:
                raise AlreadyCurrentError(
                    "deploy request is already current", deploy_request_id=str(target.id)
                )
            deployment = await self._deployments.find_by_id(target.deployment_id)
            assert deployment is not None
            request = DeployRequest(
                id=uuid.uuid4(),
                service_id=service.id,
                environment_id=environment.id,
                service_environment_id=connection.id,
                trigger=DeployRequestTrigger.ROLLBACK,
                rollback_of_request_id=target.id,
                rollback_base_deployment_id=connection.current_deployment_id,
                ref_type=RefType.COMMIT,
                ref_value=target.commit_sha,
                repo_url=target.repo_url,
                commit_sha=target.commit_sha,
                status=DeployRequestStatus.QUEUED,
                build_id=target.build_id,
                build_reused=True,
                config_snapshot=dict(deployment.config_snapshot),
                simulate=_simulate(simulate_fail_at),
                simulated=self._executor_mode is ExecutorMode.FAKE,
                created_at=now,
                updated_at=now,
            )
            superseded_id = await enqueue_deploy_request(
                self._session, self._deploy_requests, self._jobs, self._job_log, request, now
            )
            view = (await self._views([request]))[0]
        _log_accepted("rollback accepted", "create_rollback", request, superseded_id)
        return view

    async def search_deploy_requests(
        self,
        organization_id: uuid.UUID,
        service_id: uuid.UUID,
        filter_: DeployRequestFilter,
        page: int,
        size: int,
    ) -> DeployRequestSearchResult:
        async with self._session.begin():
            await self._services.get_live_by_id(organization_id, service_id)
            requests = await self._deploy_requests.search_by_service(
                organization_id,
                service_id,
                filter_.environment_id,
                filter_.triggers,
                filter_.statuses,
                offset=page * size,
                limit=size,
            )
            total = await self._deploy_requests.count_by_service(
                organization_id,
                service_id,
                filter_.environment_id,
                filter_.triggers,
                filter_.statuses,
            )
            views = await self._views(requests)
        return DeployRequestSearchResult(items=views, total=total)

    async def get_deploy_request(
        self, organization_id: uuid.UUID, deploy_request_id: uuid.UUID
    ) -> DeployRequestView:
        async with self._session.begin():
            request = await self._get_in_organization(organization_id, deploy_request_id)
            return (await self._views([request]))[0]

    async def get_deploy_request_logs(
        self,
        organization_id: uuid.UUID,
        deploy_request_id: uuid.UUID,
        source: LogSource,
        cursor: str | None,
        limit: int,
    ) -> LogPageView:
        """로그 한 페이지. 서비스가 삭제된 뒤에도 읽는다. 모든 줄은 응답 직전에 마스킹한다.

        - DEPLOY_JOB: DB 의 배포 작업 로그(cursor = 마지막으로 받은 seq).
        - BUILD·RUNTIME: 실행기 즉석 조회. 읽을 대상(빌드 번호·배포 번호)이 없거나, 실행기가 없거나,
          실행기의 simulated 가 기록과 다르면 빈 페이지다.
        """
        if source is LogSource.DEPLOY_JOB:
            return await self._read_deploy_job_logs(
                organization_id, deploy_request_id, cursor, limit
            )
        if source is LogSource.BUILD:
            page = await self._read_build_logs(organization_id, deploy_request_id, cursor, limit)
        else:
            page = await self._read_runtime_logs(organization_id, deploy_request_id, cursor, limit)
        return _masked(page)

    async def _read_deploy_job_logs(
        self,
        organization_id: uuid.UUID,
        deploy_request_id: uuid.UUID,
        cursor: str | None,
        limit: int,
    ) -> LogPageView:
        after_seq = _parse_seq_cursor(cursor)
        async with self._session.begin():
            request = await self._get_in_organization(organization_id, deploy_request_id)
            rows = await self._logs.search_after(request.id, after_seq, limit + 1)
        page = rows[:limit]
        return _masked(
            LogPageView(
                source=LogSource.DEPLOY_JOB,
                simulated=request.simulated,
                lines=[
                    LogLine(at=row.logged_at, level=row.level, message=row.message) for row in page
                ],
                next_cursor=str(page[-1].seq) if page else cursor,
                has_more=len(rows) > limit,
            )
        )

    async def _read_build_logs(
        self,
        organization_id: uuid.UUID,
        deploy_request_id: uuid.UUID,
        cursor: str | None,
        limit: int,
    ) -> LogPageView:
        async with self._session.begin():
            request = await self._get_in_organization(organization_id, deploy_request_id)
            build = (
                await self._builds.find_by_id(request.build_id)
                if request.build_id is not None
                else None
            )
        if build is None:
            return LogPageView(LogSource.BUILD, request.simulated, next_cursor=cursor)
        empty = LogPageView(LogSource.BUILD, build.simulated, next_cursor=cursor)
        executors = self._executors
        if (
            build.external_ref is None
            or executors is None
            or executors.build.simulated != build.simulated
        ):
            return empty
        chunk = await _read_chunk(executors.build.read_logs(build.external_ref, cursor, limit))
        return _page_view(LogSource.BUILD, build.simulated, chunk)

    async def _read_runtime_logs(
        self,
        organization_id: uuid.UUID,
        deploy_request_id: uuid.UUID,
        cursor: str | None,
        limit: int,
    ) -> LogPageView:
        async with self._session.begin():
            request = await self._get_in_organization(organization_id, deploy_request_id)
            empty = LogPageView(LogSource.RUNTIME, request.simulated, next_cursor=cursor)
            external_ref = request.deploy_external_ref
            executors = self._executors
            if (
                request.deployment_id is None
                or external_ref is None
                or executors is None
                or executors.deploy.simulated != request.simulated
            ):
                return empty
            deployment = await self._deployments.find_by_id(request.deployment_id)
            service = await self._services.find_by_id(request.service_id)
            connection = await self._service_environments.find_by_id(request.service_environment_id)
            if deployment is None or service is None or connection is None:
                return empty
            if connection.is_deleted:
                # 내린 연결은 실행 대상을 만들 수 없다.
                return empty
            target = deploy_target(service, connection, connection.environment)
        chunk = await _read_chunk(
            executors.deploy.read_runtime_logs(
                target, deployment_spec(deployment), external_ref, cursor, limit
            )
        )
        return _page_view(LogSource.RUNTIME, request.simulated, chunk)

    async def _get_in_organization(
        self, organization_id: uuid.UUID, deploy_request_id: uuid.UUID
    ) -> DeployRequest:
        request = await self._deploy_requests.find_in_organization(
            organization_id, deploy_request_id
        )
        if request is None:
            raise DeployRequestNotFoundError(
                "deploy request not found", deploy_request_id=str(deploy_request_id)
            )
        return request

    def _check_simulate_allowed(self, simulate_fail_at: SimulatedFailureStage | None) -> None:
        if simulate_fail_at is not None and self._executor_mode is not ExecutorMode.FAKE:
            raise SimulateNotAllowedError("simulate is only accepted with fake executors")

    async def _create_connection(
        self, service: Service, environment: Environment
    ) -> ServiceEnvironment:
        """그 환경의 첫 요청 때 연결을 만든다. 주소는 이때 한 번 정해 저장한다.

        주소 `{서비스}-{환경}-{suffix}.{도메인}` 은 서비스의 suffix 를 쓴다. 살아 있는 연결과 겹치면
        409 PUBLIC_HOST_CONFLICT 다(환경 이름을 바꾼 뒤 옛 이름으로 새 환경을 만든 경우, 63자
        자르기로 같은 레이블이 된 경우). 서비스 이름이 53자 이상이면 자르기 때문에 환경별 주소가
        그 서비스의 대표 주소와 같아진다. 연결이 하나뿐이라 둘 다 같은 곳으로 가므로 막지 않는다.
        """
        public_host = connection_public_host(
            service.name, environment.name, service.host_suffix, self._public_base_domain
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
        """요청 수와 상관없이 쿼리 4번(환경·빌드·연결·서비스)으로 응답 값을 모은다."""
        if not requests:
            return []
        environment_ids = {request.environment_id for request in requests}
        build_ids = {request.build_id for request in requests if request.build_id is not None}
        connection_ids = {request.service_environment_id for request in requests}
        service_ids = {request.service_id for request in requests}
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
        connections = {
            connection.id: connection
            for connection in await self._session.scalars(
                select(ServiceEnvironment)
                .where(ServiceEnvironment.id.in_(connection_ids))
                .execution_options(populate_existing=True)
            )
        }
        services = {
            service.id: service
            for service in await self._session.scalars(
                select(Service)
                .where(Service.id.in_(service_ids))
                .execution_options(populate_existing=True)
            )
        }
        views = []
        for request in requests:
            connection = connections[request.service_environment_id]
            service = services[request.service_id]
            is_current = (
                request.deployment_id is not None
                and request.deployment_id == connection.current_deployment_id
            )
            rollbackable = (
                request.status == DeployRequestStatus.SUCCEEDED
                and request.deployment_id is not None
                and not connection.is_deleted
                and not is_current
                and not service.is_deleted
                and service.status == ServiceStatus.ACTIVE
            )
            views.append(
                DeployRequestView(
                    deploy_request=request,
                    environment=environments[request.environment_id],
                    build=builds.get(request.build_id) if request.build_id else None,
                    is_current=is_current,
                    rollbackable=rollbackable,
                )
            )
        return views


def _check_service_accepts(service: Service) -> None:
    if service.status == ServiceStatus.DELETING:
        raise ServiceDeletingError("service is being deleted", service_id=str(service.id))


def _check_environment_accepts(environment: Environment) -> None:
    if environment.connection_status == EnvironmentConnectionStatus.FAILED:
        raise EnvironmentUnhealthyError(
            "environment connection check failed", environment_id=str(environment.id)
        )


def _simulate(simulate_fail_at: SimulatedFailureStage | None) -> dict[str, str] | None:
    return {SIMULATE_FAIL_AT: simulate_fail_at.value} if simulate_fail_at is not None else None


def _parse_seq_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not (cursor.isascii() and cursor.isdigit()):
        raise RequestFieldInvalidError("cursor", INVALID_CURSOR_REASON)
    return int(cursor)


async def _read_chunk(reading: Awaitable[LogChunk]) -> LogChunk:
    try:
        return await reading
    except InvalidLogCursor:
        raise RequestFieldInvalidError("cursor", INVALID_CURSOR_REASON) from None


def _page_view(source: LogSource, simulated: bool, chunk: LogChunk) -> LogPageView:
    return LogPageView(
        source=source,
        simulated=simulated,
        lines=list(chunk.lines),
        next_cursor=chunk.next_cursor,
        has_more=chunk.has_more,
    )


def _masked(page: LogPageView) -> LogPageView:
    """응답 직전 한 번 더 비밀값 모양을 가린다(실행기 마스킹의 방어용 이중 검사)."""
    return LogPageView(
        source=page.source,
        simulated=page.simulated,
        lines=[
            LogLine(at=line.at, level=line.level, message=mask_secrets(line.message))
            for line in page.lines
        ],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


def _log_accepted(
    message: str, action: str, request: DeployRequest, superseded_id: uuid.UUID | None
) -> None:
    logger.info(
        message,
        extra={
            "action": action,
            "deploy_request_id": str(request.id),
            "service_id": str(request.service_id),
            "environment_id": str(request.environment_id),
            "service_environment_id": str(request.service_environment_id),
            "rollback_of_request_id": (
                str(request.rollback_of_request_id) if request.rollback_of_request_id else None
            ),
            "superseded_request_id": str(superseded_id) if superseded_id else None,
        },
    )
