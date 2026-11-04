"""일꾼이 배포 요청 하나를 한 단계 진행한다(QUEUED → BUILDING → DEPLOYING → VERIFYING → 끝).

규칙
- 한 번 실행 = 한 단계 진행 또는 외부 상태 한 번 확인. 끝나지 않았으면 run_after 를 정해 돌려놓는다.
- 쓰기 트랜잭션은 잠금 순서(서비스 → 연결 → 요청 → 빌드 → 할 일)를 지키고, 맨 끝에 할 일 임대를
  확인한다. 임대를 잃었으면 결과를 버린다(LeaseLostError → 되돌림).
- 상태는 조건부 UPDATE 로만 바꾼다.
- 외부 호출은 트랜잭션 밖에서 하고, 우리 행 id 를 멱등 키로 넘긴다. 순서는 "요청함 시각 저장·커밋 →
  호출 → external_ref 저장"이다. 다시 시작한 일꾼은 요청함 시각만 있고 external_ref 가 없으면 같은
  키로 다시 부른다. 가짜 대표 주소 라우터만 I/O 가 없어 성공 처리 트랜잭션 안에서 부른다.
- 배포 기록이 생긴 뒤의 실패는 모두 되돌리기를 불러 직전 버전을 유지하고 결과를 revert_status 에
  적는다.
- 배포 작업 로그는 그 줄을 일으킨 전환과 같은 트랜잭션에서, 전환이 반영됐을 때만 쓴다(DeployJobLog).
- 되돌리기 요청(trigger = ROLLBACK)은 시작할 때 연결의 현재 배포가 접수 때 값
  (rollback_base_deployment_id)와 다르면 CANCELLED(BASE_CHANGED) 로 끝난다. 빌드는 대상 요청의
  성공한 빌드를 재사용하므로 BUILDING 을 바로 지나 배포 기록(rollback_of_id = 대상의 배포 기록)을
  만든다.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.source_repository import SourceRepositoryClient
from app.core.clock import Clock
from app.core.config import Settings
from app.core.exceptions import RepoNotAccessibleError, SourceProviderUnavailableError
from app.core.repo_url import parse_repo_url
from app.enums import (
    BuildStatus,
    DeployLogCode,
    DeploymentStatus,
    DeployRequestCancelReason,
    DeployRequestErrorCode,
    DeployRequestStatus,
    DeployRequestTrigger,
    HealthCheckResult,
    RevertStatus,
    ServiceStatus,
    SimulatedFailureStage,
)
from app.executors import ExecutorSet
from app.executors.base import (
    BuildFailed,
    BuildRunning,
    BuildSpec,
    BuildStartError,
    BuildSucceeded,
    DeployApplied,
    DeployApplyError,
    DeployFailed,
    DeployPending,
    HealthHttpError,
    HealthOk,
    MainRoute,
    Reverted,
    RouteApplied,
    SourceAccess,
)
from app.models import Build, Deployment, DeployRequest, Environment, Service, ServiceEnvironment
from app.models.build import ACTIVE_BUILD_UNIQUE_INDEX
from app.repositories.build_repository import BuildRepository
from app.repositories.deploy_request_repository import DeployRequestRepository
from app.repositories.deployment_repository import DeploymentRepository
from app.repositories.job_repository import JobRepository
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.repositories.service_repository import ServiceRepository
from app.services.deploy_job_log import DeployJobLog
from app.services.deploy_support import (
    SIMULATE_FAIL_AT,
    SNAPSHOT_ENVIRONMENT_URL,
    SNAPSHOT_HEALTH_CHECK_PATH,
    SNAPSHOT_HEALTH_CHECK_TIMEOUT_SECONDS,
    deploy_target,
    deployment_spec,
    enqueue_deploy_request,
    public_url,
)
from app.services.integrity import violated_constraint_name
from app.services.job_lease import JobLease, JobOutcome
from app.services.security_scan_collector import BuildSecurityScanCollector

logger = logging.getLogger(__name__)

# 배포 기록이 생긴 뒤 이 코드로 끝나면 되돌리기를 부른다(헬스·배포 실패, 일꾼 상실).
_HEALTH_FAILURE_BY_LAST_RESULT = {
    HealthCheckResult.HTTP_ERROR: DeployRequestErrorCode.HEALTH_CHECK_FAILED,
    HealthCheckResult.UNREACHABLE: DeployRequestErrorCode.PUBLIC_URL_UNREACHABLE,
}
_HEALTH_PROBE_LOG_CODES = {
    HealthCheckResult.HTTP_ERROR: DeployLogCode.HEALTH_PROBE_HTTP_ERROR,
    HealthCheckResult.UNREACHABLE: DeployLogCode.HEALTH_PROBE_UNREACHABLE,
}
# 서비스 중 연결에서 성공하면 예비 연결이 따라가는 요청 종류.
_STANDBY_SYNC_SOURCE_TRIGGERS = (DeployRequestTrigger.MANUAL, DeployRequestTrigger.ROLLBACK)


@dataclass(frozen=True)
class _Locked:
    service: Service
    connection: ServiceEnvironment
    request: DeployRequest
    build: Build | None


class DeployRequestProcessor:
    def __init__(
        self,
        session: AsyncSession,
        lease: JobLease,
        settings: Settings,
        clock: Clock,
        executors: ExecutorSet,
        source_client: SourceRepositoryClient,
    ) -> None:
        self._session = session
        self._lease = lease
        self._settings = settings
        self._clock = clock
        self._executors = executors
        self._source_client = source_client
        self._services = ServiceRepository(session)
        self._connections = ServiceEnvironmentRepository(session)
        self._requests = DeployRequestRepository(session)
        self._builds = BuildRepository(session)
        self._deployments = DeploymentRepository(session)
        self._jobs = JobRepository(session)
        self._job_log = DeployJobLog(session)
        self._request_id = lease.job.subject_id

    async def advance(self) -> JobOutcome:
        async with self._session.begin():
            request = await self._requests.find_by_id(self._request_id)
        if request is None:
            return JobOutcome.done()
        status = DeployRequestStatus(request.status)
        if self._lease.is_exhausted:
            return await self._give_up(request)
        if status.is_terminal:
            if _needs_revert(request):
                await self._revert()
            return JobOutcome.done()
        match status:
            case DeployRequestStatus.QUEUED:
                return await self._start()
            case DeployRequestStatus.BUILDING:
                return await self._build(request)
            case DeployRequestStatus.DEPLOYING:
                return await self._deploy(request)
            case _:
                return await self._verify(request)

    # --- QUEUED ---

    async def _start(self) -> JobOutcome:
        now = self._clock.now()
        async with self._session.begin():
            locked = await self._lock()
            request = locked.request
            if request.status != DeployRequestStatus.QUEUED:
                return self._next_run_now()
            service = locked.service
            if service.is_deleted or service.status == ServiceStatus.DELETING:
                await self._cancel_queued(
                    request,
                    DeployRequestCancelReason.SERVICE_DELETED,
                    DeployLogCode.CANCELLED_SERVICE_DELETED,
                    now,
                )
                return JobOutcome.done()
            if await self._requests.find_active_for_connection(locked.connection.id) is not None:
                # 같은 연결에 실행 중 요청이 있다(실행 1 + 대기 1). 끝날 때까지 기다린다.
                return self._next_run_after_poll()
            if (
                request.trigger == DeployRequestTrigger.ROLLBACK
                and locked.connection.current_deployment_id != request.rollback_base_deployment_id
            ):
                # 접수 뒤 다른 배포가 성공해 현재 배포가 바뀌었다. 사용자가 보지 못한 새 배포를
                # 되돌리기로 덮어쓰지 않는다.
                await self._cancel_queued(
                    request,
                    DeployRequestCancelReason.BASE_CHANGED,
                    DeployLogCode.CANCELLED_BASE_CHANGED,
                    now,
                )
                return JobOutcome.done()
            if await self._requests.transition(
                request.id,
                DeployRequestStatus.QUEUED,
                {
                    "status": DeployRequestStatus.BUILDING,
                    "started_at": now,
                    "building_at": now,
                    "updated_at": now,
                },
            ):
                await self._job_log.append(request, DeployLogCode.STARTED, now)
                if request.build_id is not None:
                    # 되돌리기·예비 따라가기는 접수 때 성공한 빌드를 이미 연결했다.
                    await self._job_log.append(request, DeployLogCode.BUILD_REUSED, now)
        self._log("deploy request started", request, DeployRequestStatus.BUILDING)
        return self._next_run_now()

    async def _cancel_queued(
        self,
        request: DeployRequest,
        reason: DeployRequestCancelReason,
        log_code: DeployLogCode,
        now: datetime,
    ) -> None:
        """잠근 트랜잭션 안에서 QUEUED 요청을 취소한다. 반영됐을 때만 로그 줄을 쓴다."""
        if await self._requests.transition(
            request.id,
            DeployRequestStatus.QUEUED,
            {
                "status": DeployRequestStatus.CANCELLED,
                "cancel_reason": reason,
                "finished_at": now,
                "updated_at": now,
            },
        ):
            await self._job_log.append(request, log_code, now)
        logger.info(
            "deploy request cancelled",
            extra={
                "action": "advance_deploy_request",
                "deploy_request_id": str(request.id),
                "service_environment_id": str(request.service_environment_id),
                "status": DeployRequestStatus.CANCELLED,
                "cancel_reason": reason,
            },
        )

    # --- BUILDING ---

    async def _build(self, request: DeployRequest) -> JobOutcome:
        if _simulated_failure(request) is SimulatedFailureStage.BUILDING:
            # 공유 빌드를 건드리지 않는 요청 단위 가짜 실패.
            return await self._fail(DeployRequestErrorCode.BUILD_FAILED)
        if request.build_id is None:
            await self._attach_build()
            return self._next_run_now()
        async with self._session.begin():
            build = await self._builds.find_by_id(request.build_id)
        assert build is not None
        match BuildStatus(build.status):
            case BuildStatus.QUEUED:
                return await self._start_build(build)
            case BuildStatus.RUNNING:
                return await self._poll_build(build)
            case BuildStatus.FAILED:
                code = DeployRequestErrorCode(
                    build.error_code or DeployRequestErrorCode.BUILD_FAILED
                )
                return await self._fail(code)
            case BuildStatus.SUCCEEDED:
                return await self._create_deployment(build)

    async def _attach_build(self) -> None:
        """같은 서비스·저장소·커밋의 실패하지 않은 빌드가 있으면 붙고(재사용), 없으면 만든다."""
        now = self._clock.now()
        async with self._session.begin():
            locked = await self._lock(hold=False)
            request = locked.request
            if request.status != DeployRequestStatus.BUILDING or request.build_id is not None:
                await self._lease.hold()
                return
            build = await self._builds.find_not_failed(
                request.service_id, request.repo_url, request.commit_sha
            )
            reused = build is not None
            if build is None:
                build, reused = await self._insert_build(request, now)
            await self._lease.hold()
            if await self._requests.update_if(
                request.id,
                [
                    DeployRequest.status == DeployRequestStatus.BUILDING,
                    DeployRequest.build_id.is_(None),
                ],
                {"build_id": build.id, "build_reused": reused, "updated_at": now},
            ):
                await self._job_log.append(
                    request,
                    DeployLogCode.BUILD_REUSED if reused else DeployLogCode.BUILD_ATTACHED,
                    now,
                )
        logger.info(
            "build attached",
            extra={
                "action": "attach_build",
                "deploy_request_id": str(request.id),
                "build_id": str(build.id),
                "reused": reused,
            },
        )

    async def _insert_build(self, request: DeployRequest, now: datetime) -> tuple[Build, bool]:
        """새 빌드를 넣는다. 부분 unique 위반이면 그사이 생긴 빌드에 붙는다. (빌드, 재사용 여부)."""
        build = Build(
            id=uuid.uuid4(),
            service_id=request.service_id,
            repo_url=request.repo_url,
            commit_sha=request.commit_sha,
            status=BuildStatus.QUEUED,
            simulated=self._executors.build.simulated,
            created_at=now,
            updated_at=now,
        )
        try:
            async with self._session.begin_nested():
                self._builds.add(build)
                await self._session.flush()
        except IntegrityError as exc:
            if violated_constraint_name(exc) != ACTIVE_BUILD_UNIQUE_INDEX:
                raise
            existing = await self._builds.find_not_failed(
                request.service_id, request.repo_url, request.commit_sha
            )
            assert existing is not None
            return existing, True
        return build, False

    async def _start_build(self, build: Build) -> JobOutcome:
        now = self._clock.now()
        # 요청함 시각이 이미 있으면 다시 시작한 일꾼이다(직전 시작 결과를 못 저장했을 수 있다).
        resumed_after = build.start_requested_at
        if build.start_requested_at is None:
            async with self._session.begin():
                await self._lock(lock_build=True)
                await self._builds.mark_start_requested(
                    build.id, {"start_requested_at": now, "updated_at": now}
                )
        async with self._session.begin():
            service = await self._services.find_by_id(build.service_id)
        assert service is not None
        await self._lease.extend()
        try:
            # 비공개 저장소 접근(App 설치)을 여기서 확인한다. 실제 빌드 실행기는 이 토큰을 쓰지 않고
            # 워크플로가 직접 받는다. 토큰은 메모리로만 넘기고 저장·로그하지 않는다.
            read_token = await self._source_client.issue_read_token(parse_repo_url(build.repo_url))
            external_ref = await self._executors.build.start(
                BuildSpec(
                    build_id=build.id,
                    service_id=service.id,
                    service_name=service.name,
                    repo_url=build.repo_url,
                    commit_sha=build.commit_sha,
                    repo_private=service.repo_private,
                    resumed_after=resumed_after,
                ),
                SourceAccess(
                    repo_url=build.repo_url, commit_sha=build.commit_sha, read_token=read_token
                ),
                idempotency_key=str(build.id),
            )
        except (RepoNotAccessibleError, SourceProviderUnavailableError) as exc:
            await self._close_build(build.id, DeployRequestErrorCode(exc.code))
            return self._next_run_now()
        except BuildStartError as exc:
            # 다시 불러도 소용없는 시작 실패(워크플로·권한 없음).
            # 다음 실행에서 요청이 이 코드로 끝난다.
            await self._close_build(build.id, exc.code)
            return self._next_run_now()
        async with self._session.begin():
            await self._lock(lock_build=True)
            await self._builds.transition(
                build.id,
                BuildStatus.QUEUED,
                {
                    "status": BuildStatus.RUNNING,
                    "external_ref": external_ref,
                    "started_at": self._clock.now(),
                    "updated_at": self._clock.now(),
                },
            )
        logger.info("build started", extra={"action": "start_build", "build_id": str(build.id)})
        return self._next_run_now()

    async def _poll_build(self, build: Build) -> JobOutcome:
        now = self._clock.now()
        timeout = timedelta(seconds=self._settings.build_timeout_seconds)
        if build.started_at is not None and now - build.started_at > timeout:
            await self._close_build(build.id, DeployRequestErrorCode.BUILD_TIMEOUT)
            return self._next_run_now()
        assert build.external_ref is not None
        await self._lease.extend()
        result = await self._executors.build.poll(build.external_ref)
        match result:
            case BuildRunning():
                return self._next_run_after_poll()
            case BuildFailed(code=code):
                await self._close_build(build.id, code)
            case BuildSucceeded(image_repository=image_repository, image_digest=image_digest):
                async with self._session.begin():
                    await self._lock(lock_build=True)
                    succeeded = await self._builds.transition(
                        build.id,
                        BuildStatus.RUNNING,
                        {
                            "status": BuildStatus.SUCCEEDED,
                            "image_repository": image_repository,
                            "image_digest": image_digest,
                            "finished_at": self._clock.now(),
                            "updated_at": self._clock.now(),
                        },
                    )
                logger.info(
                    "build succeeded", extra={"action": "poll_build", "build_id": str(build.id)}
                )
                if succeeded:
                    # 같은 실행의 보안 검사 원문을 증거로 남긴다(보안·거버넌스 2·11).
                    # 실패해도 배포는 계속한다.
                    await BuildSecurityScanCollector(self._session, self._clock).collect(
                        build.id, self._executors.build
                    )
        return self._next_run_now()

    async def _close_build(self, build_id: uuid.UUID, code: DeployRequestErrorCode) -> None:
        """빌드를 FAILED 로 한 번만 닫는다.

        이 빌드를 기다리는 요청은 각자 다음 실행에서 같은 코드로 끝난다.
        """
        now = self._clock.now()
        async with self._session.begin():
            await self._lock(lock_build=True)
            closed = await self._builds.transition(
                build_id,
                (BuildStatus.QUEUED, BuildStatus.RUNNING),
                {"status": BuildStatus.FAILED, "error_code": code, "finished_at": now},
            )
        if closed:
            logger.info(
                "build failed",
                extra={"action": "close_build", "build_id": str(build_id), "error_code": code},
            )

    async def _create_deployment(self, build: Build) -> JobOutcome:
        now = self._clock.now()
        async with self._session.begin():
            locked = await self._lock(hold=False)
            request = locked.request
            if request.status != DeployRequestStatus.BUILDING or request.deployment_id is not None:
                await self._lease.hold()
                return self._next_run_now()
            assert build.image_repository is not None and build.image_digest is not None
            rollback_of_id = await self._rollback_target_deployment_id(request)
            deployment = Deployment(
                id=uuid.uuid4(),
                service_environment_id=locked.connection.id,
                image_repository=build.image_repository,
                image_digest=build.image_digest,
                config_snapshot=dict(request.config_snapshot),
                status=DeploymentStatus.IN_PROGRESS,
                rollback_of_id=rollback_of_id,
                started_at=now,
                created_at=now,
                updated_at=now,
            )
            self._deployments.add(deployment)
            await self._session.flush()
            await self._lease.hold()
            if await self._requests.transition(
                request.id,
                DeployRequestStatus.BUILDING,
                {
                    "status": DeployRequestStatus.DEPLOYING,
                    "deployment_id": deployment.id,
                    "deploying_at": now,
                    "updated_at": now,
                },
            ):
                await self._job_log.append(request, DeployLogCode.DEPLOYMENT_CREATED, now)
        self._log("deploy request deploying", request, DeployRequestStatus.DEPLOYING)
        return self._next_run_now()

    async def _rollback_target_deployment_id(self, request: DeployRequest) -> uuid.UUID | None:
        """되돌리기면 대상 요청이 만든 배포 기록(새 배포 기록의 rollback_of_id). 아니면 None.

        이미지·digest 는 대상과 같은 빌드에서, 설정은 접수 때 복사한 대상 배포 기록의 사본에서 온다.
        """
        if request.trigger != DeployRequestTrigger.ROLLBACK:
            return None
        assert request.rollback_of_request_id is not None
        target = await self._requests.find_by_id(request.rollback_of_request_id)
        assert target is not None and target.deployment_id is not None
        return target.deployment_id

    # --- DEPLOYING ---

    async def _deploy(self, request: DeployRequest) -> JobOutcome:
        if _simulated_failure(request) is SimulatedFailureStage.DEPLOYING:
            return await self._fail(DeployRequestErrorCode.DEPLOY_FAILED)
        now = self._clock.now()
        timeout = timedelta(seconds=self._settings.deploy_timeout_seconds)
        assert request.deploying_at is not None and request.deployment_id is not None
        if now - request.deploying_at > timeout:
            return await self._fail(DeployRequestErrorCode.DEPLOY_TIMEOUT)

        if request.deploy_external_ref is None:
            if request.deploy_requested_at is None:
                async with self._session.begin():
                    await self._lock()
                    if await self._requests.update_if(
                        request.id,
                        [
                            DeployRequest.status == DeployRequestStatus.DEPLOYING,
                            DeployRequest.deploy_requested_at.is_(None),
                        ],
                        {"deploy_requested_at": now, "updated_at": now},
                    ):
                        await self._job_log.append(request, DeployLogCode.DEPLOY_REQUESTED, now)
            async with self._session.begin():
                deployment = await self._deployments.find_by_id(request.deployment_id)
                service, connection, environment = await self._target_rows(request)
            assert deployment is not None
            await self._lease.extend()
            try:
                external_ref = await self._executors.deploy.apply(
                    deployment_spec(deployment),
                    deploy_target(service, connection, environment),
                    idempotency_key=str(deployment.id),
                )
            except DeployApplyError as exc:
                # 권한·환경 설정 누락처럼 다시 불러도 소용없는 실패.
                # 배포 기록이 있어 되돌리기도 부른다.
                return await self._fail(exc.code)
            async with self._session.begin():
                await self._lock()
                await self._requests.update_if(
                    request.id,
                    [
                        DeployRequest.status == DeployRequestStatus.DEPLOYING,
                        DeployRequest.deploy_external_ref.is_(None),
                    ],
                    {"deploy_external_ref": external_ref, "updated_at": self._clock.now()},
                )
            return self._next_run_now()

        await self._lease.extend()
        result = await self._executors.deploy.poll(request.deploy_external_ref)
        match result:
            case DeployPending():
                return self._next_run_after_poll()
            case DeployFailed(code=code):
                return await self._fail(code)
            case DeployApplied():
                async with self._session.begin():
                    await self._lock()
                    applied_at = self._clock.now()
                    if await self._requests.transition(
                        request.id,
                        DeployRequestStatus.DEPLOYING,
                        {
                            "status": DeployRequestStatus.VERIFYING,
                            "verifying_at": applied_at,
                            "updated_at": applied_at,
                        },
                    ):
                        await self._job_log.append(
                            request, DeployLogCode.DEPLOY_APPLIED, applied_at
                        )
                self._log("deploy request verifying", request, DeployRequestStatus.VERIFYING)
                return self._next_run_now()

    # --- VERIFYING ---

    async def _verify(self, request: DeployRequest) -> JobOutcome:
        if _simulated_failure(request) is SimulatedFailureStage.VERIFYING:
            return await self._fail(DeployRequestErrorCode.HEALTH_CHECK_FAILED)
        snapshot = request.config_snapshot
        assert request.verifying_at is not None
        deadline = request.verifying_at + timedelta(
            seconds=int(snapshot[SNAPSHOT_HEALTH_CHECK_TIMEOUT_SECONDS])
        )
        now = self._clock.now()
        if now > deadline:
            last = (
                HealthCheckResult(request.last_health_result)
                if request.last_health_result
                else None
            )
            code = (
                _HEALTH_FAILURE_BY_LAST_RESULT.get(last)
                if last is not None
                else DeployRequestErrorCode.HEALTH_CHECK_TIMEOUT
            )
            return await self._fail(code or DeployRequestErrorCode.HEALTH_CHECK_TIMEOUT)

        await self._lease.extend()
        result = await self._executors.health.probe(
            str(snapshot[SNAPSHOT_ENVIRONMENT_URL]),
            str(snapshot[SNAPSHOT_HEALTH_CHECK_PATH]),
            self._settings.health_check_request_timeout_seconds,
        )
        if isinstance(result, HealthOk):
            return await self._succeed()
        last_result = (
            HealthCheckResult.HTTP_ERROR
            if isinstance(result, HealthHttpError)
            else HealthCheckResult.UNREACHABLE
        )
        async with self._session.begin():
            await self._lock()
            probed_at = self._clock.now()
            # 결과 종류가 바뀔 때만 적고 로그 줄을 쓴다(같은 실패가 이어져도 한 줄).
            if await self._requests.update_if(
                request.id,
                [
                    DeployRequest.status == DeployRequestStatus.VERIFYING,
                    DeployRequest.last_health_result.is_distinct_from(last_result),
                ],
                {"last_health_result": last_result, "updated_at": probed_at},
            ):
                await self._job_log.append(request, _HEALTH_PROBE_LOG_CODES[last_result], probed_at)
        return JobOutcome.again(
            self._clock.now() + timedelta(seconds=self._settings.health_check_interval_seconds)
        )

    # --- 성공 처리: 현재 배포 교체, 역할 판정, 예비 연결 따라가기 ---

    async def _succeed(self) -> JobOutcome:
        now = self._clock.now()
        created_syncs: list[uuid.UUID] = []
        async with self._session.begin():
            snapshot = await self._requests.find_by_id(self._request_id)
            assert snapshot is not None
            service = await self._services.find_by_id(snapshot.service_id, lock=True)
            assert service is not None
            connections = await self._connections.lock_live_by_service(service.id)
            request = await self._requests.find_by_id(self._request_id, lock=True)
            assert request is not None and request.deployment_id is not None
            await self._lease.hold()
            if request.status != DeployRequestStatus.VERIFYING:
                return self._next_run_now()
            connection = next(
                item for item in connections if item.id == request.service_environment_id
            )
            if not await self._requests.transition(
                request.id,
                DeployRequestStatus.VERIFYING,
                {
                    "status": DeployRequestStatus.SUCCEEDED,
                    "last_health_result": HealthCheckResult.OK,
                    "finished_at": now,
                    "updated_at": now,
                },
            ):
                return self._next_run_now()
            await self._job_log.append(request, DeployLogCode.HEALTH_PROBE_OK, now)
            await self._job_log.append(request, DeployLogCode.SUCCEEDED, now)
            await self._deployments.transition(
                request.deployment_id,
                DeploymentStatus.IN_PROGRESS,
                {"status": DeploymentStatus.SUCCEEDED, "finished_at": now, "updated_at": now},
            )
            connection.current_deployment_id = request.deployment_id

            others = [item for item in connections if item.id != connection.id]
            if connection.traffic_weight == 0 and not any(
                item.traffic_weight > 0 for item in others
            ):
                await self._route_main(service, connection, request.deployment_id)

            is_serving = connection.traffic_weight > 0
            if (
                is_serving
                and service.status == ServiceStatus.ACTIVE
                and not service.is_deleted
                and request.trigger in _STANDBY_SYNC_SOURCE_TRIGGERS
            ):
                # 예비 연결을 직접 되돌린 경우(비율 0)는 따라가기를 만들지 않는다.
                for standby in others:
                    if standby.traffic_weight == 0 and standby.current_deployment_id is not None:
                        created_syncs.append(
                            await self._enqueue_standby_sync(request, standby, now)
                        )
            await self._session.flush()
        self._log("deploy request succeeded", request, DeployRequestStatus.SUCCEEDED)
        for sync_id in created_syncs:
            logger.info(
                "standby sync requested",
                extra={
                    "action": "enqueue_standby_sync",
                    "deploy_request_id": str(sync_id),
                    "source_request_id": str(request.id),
                },
            )
        return JobOutcome.done()

    async def _route_main(
        self, service: Service, connection: ServiceEnvironment, deployment_id: uuid.UUID
    ) -> None:
        """서비스 중 연결이 없으면 대표 주소를 이 연결로 보낸다. 적용됐을 때만 비율을 적는다."""
        environment = await self._session.get_one(Environment, connection.environment_id)
        result = await self._executors.main_route.route_main(
            MainRoute(
                service_id=service.id, service_name=service.name, public_host=service.public_host
            ),
            deploy_target(service, connection, environment),
            idempotency_key=str(deployment_id),
        )
        if isinstance(result, RouteApplied):
            connection.traffic_weight = 100
            connection.traffic_applied_at = result.applied_at
            return
        logger.warning(
            "main route not applied",
            extra={
                "action": "route_main",
                "service_environment_id": str(connection.id),
                "error_code": result.code,
            },
        )

    async def _enqueue_standby_sync(
        self, source: DeployRequest, standby: ServiceEnvironment, now: datetime
    ) -> uuid.UUID:
        """예비 연결이 같은 빌드(같은 이미지)·같은 설정으로 따라간다.

        설정 사본에서 환경별 주소만 그 연결의 것으로 바꾼다. 원본 요청에 STANDBY_SYNC_REQUESTED 줄을
        쓴다(새 요청의 REQUEST_ACCEPTED 는 enqueue_deploy_request 가 쓴다).
        """
        request = DeployRequest(
            id=uuid.uuid4(),
            service_id=source.service_id,
            environment_id=standby.environment_id,
            service_environment_id=standby.id,
            trigger=DeployRequestTrigger.STANDBY_SYNC,
            source_request_id=source.id,
            ref_type=source.ref_type,
            ref_value=source.ref_value,
            repo_url=source.repo_url,
            commit_sha=source.commit_sha,
            status=DeployRequestStatus.QUEUED,
            build_id=source.build_id,
            build_reused=True,
            config_snapshot={
                **source.config_snapshot,
                SNAPSHOT_ENVIRONMENT_URL: public_url(standby.public_host),
            },
            simulate=None,
            simulated=source.simulated,
            created_at=now,
            updated_at=now,
        )
        await enqueue_deploy_request(
            self._session, self._requests, self._jobs, self._job_log, request, now
        )
        await self._job_log.append(source, DeployLogCode.STANDBY_SYNC_REQUESTED, now)
        return request.id

    # --- 실패·되돌리기·일꾼 상실 ---

    async def _fail(self, code: DeployRequestErrorCode) -> JobOutcome:
        now = self._clock.now()
        async with self._session.begin():
            locked = await self._lock(lock_build=True)
            request = locked.request
            status = DeployRequestStatus(request.status)
            if status.is_terminal:
                return self._next_run_now()
            if await self._requests.transition(
                request.id,
                status,
                {
                    "status": DeployRequestStatus.FAILED,
                    "error_code": code,
                    "finished_at": now,
                    "updated_at": now,
                },
            ):
                await self._job_log.append(request, DeployLogCode.FAILED, now, error_code=code)
            if request.deployment_id is not None:
                await self._deployments.transition(
                    request.deployment_id,
                    DeploymentStatus.IN_PROGRESS,
                    {"status": DeploymentStatus.FAILED, "finished_at": now, "updated_at": now},
                )
            build = locked.build
            if (
                build is not None
                and not BuildStatus(build.status).is_terminal
                and not await self._requests.has_open_on_build(build.id, excluding=request.id)
            ):
                # 이 빌드를 기다리는 마지막 요청이 끝난다. 다음 요청이 막히지 않게 빌드도 닫는다.
                await self._builds.transition(
                    build.id,
                    (BuildStatus.QUEUED, BuildStatus.RUNNING),
                    {"status": BuildStatus.FAILED, "error_code": code, "finished_at": now},
                )
        logger.info(
            "deploy request failed",
            extra={
                "action": "fail_deploy_request",
                "deploy_request_id": str(request.id),
                "service_environment_id": str(request.service_environment_id),
                "error_code": code,
            },
        )
        if request.deployment_id is not None:
            await self._revert()
        return JobOutcome.done()

    async def _revert(self) -> None:
        """실패한 새 버전을 거두고 연결의 현재 배포(직전 버전)를 유지한다. 결과는 revert_status."""
        async with self._session.begin():
            request = await self._requests.find_by_id(self._request_id)
            assert request is not None and request.deployment_id is not None
            if request.revert_status is not None:
                return
            service, connection, environment = await self._target_rows(request)
            previous = (
                await self._deployments.find_by_id(connection.current_deployment_id)
                if connection.current_deployment_id
                else None
            )
        await self._lease.extend()
        result = await self._executors.deploy.revert(
            deploy_target(service, connection, environment),
            deployment_spec(previous) if previous is not None else None,
            idempotency_key=str(request.deployment_id),
        )
        revert_status = (
            RevertStatus.SUCCEEDED if isinstance(result, Reverted) else RevertStatus.FAILED
        )
        async with self._session.begin():
            await self._lock()
            reverted_at = self._clock.now()
            if await self._requests.update_if(
                request.id,
                [DeployRequest.revert_status.is_(None)],
                {
                    "revert_status": revert_status,
                    "revert_external_ref": (
                        result.external_ref if isinstance(result, Reverted) else None
                    ),
                    "updated_at": reverted_at,
                },
            ):
                await self._job_log.append(
                    request,
                    DeployLogCode.REVERT_SUCCEEDED
                    if revert_status is RevertStatus.SUCCEEDED
                    else DeployLogCode.REVERT_FAILED,
                    reverted_at,
                )
        logger.info(
            "deploy request reverted",
            extra={
                "action": "revert_deploy_request",
                "deploy_request_id": str(request.id),
                "revert_status": revert_status,
            },
        )

    async def _give_up(self, request: DeployRequest) -> JobOutcome:
        """임대를 3번 잃었다.

        진행 중이면 WORKER_LOST 로 끝내고(배포 기록이 있으면 되돌리기) 할 일을 FAILED 로 닫는다.
        """
        if not DeployRequestStatus(request.status).is_terminal:
            await self._fail(DeployRequestErrorCode.WORKER_LOST)
        elif _needs_revert(request):
            await self._revert()
        return JobOutcome.failed(DeployRequestErrorCode.WORKER_LOST)

    # --- 공통 ---

    async def _lock(self, *, lock_build: bool = False, hold: bool = True) -> _Locked:
        """서비스 → 연결 → 요청 → (빌드) 순서로 잠근다.

        hold=True 면 마지막에 할 일 임대를 확인한다.
        """
        snapshot = await self._requests.find_by_id(self._request_id)
        assert snapshot is not None
        service = await self._services.find_by_id(snapshot.service_id, lock=True)
        connection = await self._connections.lock_by_id(snapshot.service_environment_id)
        request = await self._requests.find_by_id(self._request_id, lock=True)
        assert service is not None and connection is not None and request is not None
        build = (
            await self._builds.find_by_id(request.build_id, lock=True)
            if lock_build and request.build_id is not None
            else None
        )
        if hold:
            await self._lease.hold()
        return _Locked(service=service, connection=connection, request=request, build=build)

    async def _target_rows(
        self, request: DeployRequest
    ) -> tuple[Service, ServiceEnvironment, Environment]:
        service = await self._services.find_by_id(request.service_id)
        connection = await self._connections.find_by_id(request.service_environment_id)
        assert service is not None and connection is not None
        return service, connection, connection.environment

    def _next_run_now(self) -> JobOutcome:
        return JobOutcome.again(self._clock.now())

    def _next_run_after_poll(self) -> JobOutcome:
        return JobOutcome.again(
            self._clock.now() + timedelta(seconds=self._settings.worker_poll_interval_seconds)
        )

    @staticmethod
    def _log(message: str, request: DeployRequest, status: DeployRequestStatus) -> None:
        logger.info(
            message,
            extra={
                "action": "advance_deploy_request",
                "deploy_request_id": str(request.id),
                "service_environment_id": str(request.service_environment_id),
                "status": status,
            },
        )


def _needs_revert(request: DeployRequest) -> bool:
    return (
        request.status == DeployRequestStatus.FAILED
        and request.deployment_id is not None
        and request.revert_status is None
    )


def _simulated_failure(request: DeployRequest) -> SimulatedFailureStage | None:
    simulate: dict[str, Any] | None = request.simulate
    if not simulate or SIMULATE_FAIL_AT not in simulate:
        return None
    return SimulatedFailureStage(simulate[SIMULATE_FAIL_AT])
