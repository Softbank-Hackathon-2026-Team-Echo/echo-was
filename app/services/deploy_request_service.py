"""배포 요청 접수·되돌리기 접수·조회·로그 유스케이스. 접수만 동기로 하고 실행은 일꾼이 한다.

서비스·환경·배포 요청은 모두 경로의 조직 안에서만 찾는다. 환경이 서비스와 다른 조직이면 그 환경은
없는 것과 같다(404 ENVIRONMENT_NOT_FOUND).

접수 순서(잠금 순서: 환경 → 서비스 → 연결 → 요청 → 할 일):
1. simulate 는 가짜 실행기 모드에서만 받는다.
2. 서비스·환경을 읽어 거절할 것을 먼저 거절한다(트랜잭션 밖 GitHub 호출 전에).
3. 브랜치·커밋을 40자 SHA 로 고정한다(트랜잭션 밖).
4. 환경 행을 FOR SHARE 로 잠그고 다시 확인한 뒤(환경 삭제와 경쟁 방지), 서비스·연결을 잠그고
   연결이 없으면 만든다(첫 배포 전 변수 저장이 만든 연결이 있으면 그것을 쓴다). 같은 연결의
   대기 요청은 SUPERSEDED 로 밀어내고 새 요청과 할 일을 넣는다.

서비스에 실행 중인 이전 계획이 있으면 409 MIGRATION_IN_PROGRESS 다(서비스 행을 잠근 뒤 본다).

예비 환경 함께 띄우기(이슈 #99 2번): standbyEnvironmentIds 는 접수 때 확인만 하고 요청
config_snapshot 에 적는다(표 칸을 더하지 않는다). 연결 만들기·STANDBY_SYNC 는 일꾼이 주 환경
성공 처리 때 한다(DeployRequestProcessor._succeed). 상세 응답 standbyDeployRequestIds 는 그
결과를 읽는다.

되돌리기 접수는 같은 순서로 잠그고, 이력에서 고른 성공 요청의 빌드·커밋·배포 기록 설정 사본으로
trigger = ROLLBACK 요청을 넣는다. 접수 순간 연결의 현재 배포를 rollback_base_deployment_id 에
적어 두고, 일꾼이 시작할 때 다시 비교한다.
"""

import logging
import uuid
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.actions import AuditAction, AuditComponent
from app.audit.lifecycle import deploy_request_target
from app.audit.recorder import AuditActor, AuditRecorder
from app.clients.source_repository import SourceRepositoryClient
from app.core.clock import Clock
from app.core.config import ExecutorMode
from app.core.exceptions import (
    AlreadyCurrentError,
    DeployRequestNotFoundError,
    EnvironmentUnhealthyError,
    NotRollbackableError,
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
from app.models import AgentRun, Build, DeployRequest, Environment, Service, ServiceEnvironment
from app.repositories.agent_run_repository import AgentRunRepository
from app.repositories.build_repository import BuildRepository
from app.repositories.deploy_request_log_repository import DeployRequestLogRepository
from app.repositories.deploy_request_repository import DeployRequestRepository
from app.repositories.deployment_repository import DeploymentRepository
from app.repositories.environment_repository import EnvironmentRepository
from app.repositories.job_repository import JobRepository
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.repositories.service_repository import ServiceRepository
from app.services.container_diagnostics import (
    FAILURE_LOG_CODES,
    FailureDetail,
    build_failure_detail,
)
from app.services.deploy_job_log import DeployJobLog
from app.services.deploy_support import (
    SIMULATE_FAIL_AT,
    SNAPSHOT_STANDBY_ENVIRONMENT_IDS,
    build_config_snapshot,
    create_service_connection,
    deploy_target,
    deployment_spec,
    enqueue_deploy_request,
)
from app.services.migration_guard import check_no_migration_in_progress

logger = logging.getLogger(__name__)

INVALID_CURSOR_REASON = "INVALID_CURSOR"
STANDBY_SAME_AS_ENVIRONMENT_REASON = "SAME_AS_ENVIRONMENT"
STANDBY_DUPLICATE_REASON = "DUPLICATE"


@dataclass(frozen=True)
class CreateDeployCommand:
    environment_id: uuid.UUID
    ref_type: RefType
    ref_value: str
    simulate_fail_at: SimulatedFailureStage | None
    # 주 환경 성공 뒤 함께 띄울 예비 환경(이슈 #99 2번). 비면 예전과 같다.
    standby_environment_ids: tuple[uuid.UUID, ...] = ()
    # 배포 실패 진단의 패치 반영 뒤 자동 재배포면 그 반영 실행(SPEC api-98). 감사 details 에 싣는다.
    diagnosis_apply_run_id: uuid.UUID | None = None


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
    # 상세만 채운다. 실패한 요청의 원인·컨테이너 진단(API 서버 4-1).
    failure: FailureDetail | None = None
    # 상세만 채운다. 이 요청의 AI 진단 실행(CHANGE_PROPOSAL, SPEC api-98). 진단 전이면 None.
    diagnosis: AgentRun | None = None
    # 상세만 채운다. 이 요청이 성공해 만든 예비 따라가기(STANDBY_SYNC) 요청 id(이슈 #99).
    standby_request_ids: tuple[uuid.UUID, ...] = ()


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
        self._audit = AuditRecorder(session, clock)

    async def create_deploy_request(
        self,
        organization_id: uuid.UUID,
        service_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        command: CreateDeployCommand,
    ) -> DeployRequestView:
        self._check_simulate_allowed(command.simulate_fail_at)
        _check_standby_command(command)

        async with self._session.begin():
            service = await self._services.get_live_by_id(organization_id, service_id)
            _check_service_accepts(service)
            environment = await self._environments.get_live_by_id(
                organization_id, command.environment_id
            )
            _check_environment_accepts(environment)
            for standby_id in command.standby_environment_ids:
                # 같은 조직의 살아 있는 환경만(아니면 404 ENVIRONMENT_NOT_FOUND),
                # 접속 확인 실패면 409. 성공 처리 때 다시 본다(그 사이 지워졌으면 그 환경만
                # STANDBY_SKIPPED 줄).
                _check_environment_accepts(
                    await self._environments.get_live_by_id(organization_id, standby_id)
                )
            repo = parse_repo_url(service.repo_url)

        commit_sha = await self._source_client.resolve_ref(repo, command.ref_value)

        async with self._session.begin():
            request, environment, superseded_id = await self._enqueue_manual(
                organization_id, service_id, actor_user_id, command, commit_sha
            )
        _log_accepted("deploy request accepted", "create_deploy_request", request, superseded_id)
        return DeployRequestView(deploy_request=request, environment=environment, build=None)

    async def create_redeploy_in_transaction(
        self,
        organization_id: uuid.UUID,
        service_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        command: CreateDeployCommand,
        commit_sha: str,
    ) -> DeployRequestView:
        """이미 고정된 커밋(commit_sha)을 다시 배포하는 접수. 부르는 쪽 트랜잭션 안에서 돈다.

        배포 실패 진단의 반영 뒤 재배포(SPEC api-98)가 쓴다. 부르는 쪽이 반영 실행 행을
        FOR UPDATE 로 잡은 채 접수와 연결을 한 트랜잭션으로 묶는다(같은 반영의 재배포는 한 번).
        ref 해석(GitHub 호출)은 없고, 접수 규칙(서비스 삭제 중·환경 접속·이전 진행 중)은
        create_deploy_request 의 두 번째 단계와 같다. 거절(AppError)이면 아무 것도 쓰지 않고
        그대로 던진다 — 부르는 쪽이 savepoint 로 감싸 되돌린다.
        """
        self._check_simulate_allowed(command.simulate_fail_at)
        _check_standby_command(command)
        request, environment, superseded_id = await self._enqueue_manual(
            organization_id, service_id, actor_user_id, command, commit_sha
        )
        _log_accepted("deploy request accepted", "create_redeploy", request, superseded_id)
        return DeployRequestView(deploy_request=request, environment=environment, build=None)

    async def _enqueue_manual(
        self,
        organization_id: uuid.UUID,
        service_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        command: CreateDeployCommand,
        commit_sha: str,
    ) -> tuple[DeployRequest, Environment, uuid.UUID | None]:
        """접수 두 번째 단계(잠그고 다시 확인 → 요청·할 일 넣기 → 감사). 열린 트랜잭션 안에서."""
        now = self._clock.now()
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
        await check_no_migration_in_progress(self._session, service.id)
        connection = next(
            (item for item in connections if item.environment_id == environment.id), None
        )
        if connection is None:
            # 첫 배포 전에 변수를 저장했으면(API 서버 10-4) 그때 만든 연결을 그대로 쓴다.
            connection = await create_service_connection(
                self._session, service, environment, self._public_base_domain
            )
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
            config_snapshot=_with_standby_ids(
                build_config_snapshot(service, connection), command.standby_environment_ids
            ),
            simulate=_simulate(command.simulate_fail_at),
            simulated=self._executor_mode is ExecutorMode.FAKE,
            created_at=now,
            updated_at=now,
        )
        superseded_id = await enqueue_deploy_request(
            self._session,
            self._deploy_requests,
            self._jobs,
            self._job_log,
            request,
            now,
            self._clock,
            AuditComponent.API,
        )
        details: dict[str, object] = {
            "environmentId": str(environment.id),
            "ref": command.ref_value,
            "commitSha": commit_sha,
        }
        if command.standby_environment_ids:
            details["standbyEnvironmentIds"] = [
                str(item) for item in command.standby_environment_ids
            ]
        if command.diagnosis_apply_run_id is not None:
            details["diagnosisApplyRunId"] = str(command.diagnosis_apply_run_id)
        self._audit.record(
            organization_id=organization_id,
            actor=AuditActor.user(actor_user_id),
            action=AuditAction.DEPLOY_REQUESTED,
            target=await deploy_request_target(self._session, request),
            details=details,
        )
        return request, environment, superseded_id

    async def create_rollback(
        self,
        organization_id: uuid.UUID,
        deploy_request_id: uuid.UUID,
        actor_user_id: uuid.UUID,
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
            await check_no_migration_in_progress(self._session, service.id)
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
                self._session,
                self._deploy_requests,
                self._jobs,
                self._job_log,
                request,
                now,
                self._clock,
                AuditComponent.API,
            )
            self._audit.record(
                organization_id=organization_id,
                actor=AuditActor.user(actor_user_id),
                action=AuditAction.ROLLBACK_REQUESTED,
                target=await deploy_request_target(self._session, request),
                details={"rollbackOfId": str(target.id), "environmentId": str(environment.id)},
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
            view = (await self._views([request]))[0]
            diagnosis = await AgentRunRepository(self._session).find_diagnosis(request.id)
            view = replace(view, diagnosis=diagnosis)
            standby_ids = await self._deploy_requests.search_standby_sync_ids_by_source(request.id)
            if standby_ids:
                view = replace(view, standby_request_ids=tuple(standby_ids))
            if request.status != DeployRequestStatus.FAILED:
                return view
            rows = await self._logs.search_by_codes(request.id, FAILURE_LOG_CODES)
        failure = build_failure_detail(
            request.status, request.error_code, [(row.code, row.message) for row in rows]
        )
        return replace(view, failure=failure)

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
                    LogLine(at=row.logged_at, level=row.level, message=row.message, code=row.code)
                    for row in page
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


def _check_standby_command(command: CreateDeployCommand) -> None:
    """예비 환경 목록 규칙(스키마 밖). 주 환경과 같거나 중복이면 VALIDATION_ERROR."""
    standby_ids = command.standby_environment_ids
    if command.environment_id in standby_ids:
        raise RequestFieldInvalidError("standbyEnvironmentIds", STANDBY_SAME_AS_ENVIRONMENT_REASON)
    if len(set(standby_ids)) != len(standby_ids):
        raise RequestFieldInvalidError("standbyEnvironmentIds", STANDBY_DUPLICATE_REASON)


def _with_standby_ids(
    snapshot: dict[str, Any], standby_environment_ids: Sequence[uuid.UUID]
) -> dict[str, Any]:
    if standby_environment_ids:
        snapshot[SNAPSHOT_STANDBY_ENVIRONMENT_IDS] = [str(item) for item in standby_environment_ids]
    return snapshot


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
            LogLine(
                at=line.at, level=line.level, message=mask_secrets(line.message), code=line.code
            )
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
