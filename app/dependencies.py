"""FastAPI 의존성 주입 함수. 시험은 app.dependency_overrides 로 바꾼다."""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import State

from app.adapters.registry import get_environment_adapter
from app.clients.app_secret_store import AppSecretStore, build_app_secret_store
from app.clients.github_source_repository import GithubAppSourceRepositoryClient
from app.clients.github_user import GithubUserClient, HttpGithubUserClient
from app.clients.osv import AdvisoryFeed, OsvAdvisoryFeed
from app.clients.prometheus_client import PrometheusClient
from app.clients.secret_client import EnvVarSecretClient, SecretClient
from app.clients.source_repository import FakeSourceRepositoryClient, SourceRepositoryClient
from app.clients.tls_probe import PublicHostTlsChecker
from app.core.clock import Clock, SystemClock
from app.core.config import (
    EnvironmentAdapterMode,
    ExecutorMode,
    Settings,
    SourceClientMode,
    get_settings,
)
from app.core.database import get_session_factory
from app.core.secrets import parse_secret_ref
from app.executors import ExecutorSet, build_executor_set
from app.executors.base import NoCutoverRuns
from app.executors.entrypoint import parse_entrypoint_environment_map
from app.executors.google_metadata import GoogleMetadataClient
from app.executors.kubernetes.access import ClusterAccessProvider
from app.executors.kubernetes.api import KubeHttpClients, default_kube_http_factory
from app.executors.kubernetes.reader import ClusterReader
from app.repositories.environment_repository import EnvironmentRepository
from app.services.agent_run_service import AgentRunService
from app.services.agent_token_service import AgentTokenService
from app.services.ai_residency_service import AiResidencyService
from app.services.audit_log_service import AuditLogService
from app.services.auth_service import AuthService
from app.services.cost_catalog import CostCatalog, get_cost_catalog
from app.services.cost_service import CostService
from app.services.cve_watch_service import CveWatchService
from app.services.deploy_request_service import DeployRequestService
from app.services.deploy_support import HostSuffixGenerator, generate_host_suffix
from app.services.diagnosis_service import DiagnosisService
from app.services.disaster_service import DisasterService
from app.services.environment_connection_service import (
    AdapterResolver,
    EnvironmentConnectionService,
)
from app.services.environment_kind_service import EnvironmentKindService
from app.services.environment_service import EnvironmentService
from app.services.executor_environment_lookup import DbEnvironmentLookup
from app.services.invitation_service import InvitationService
from app.services.migration_plan_result import MigrationPlanResultService, cutover_result_cache
from app.services.migration_plan_service import MigrationPlanService
from app.services.organization_service import OrganizationService
from app.services.precheck_patch_service import PrecheckPatchService
from app.services.precheck_service import PrecheckService
from app.services.security_compare_service import SecurityCompareService
from app.services.security_exception_service import SecurityExceptionService
from app.services.security_gate import SecurityGate, build_public_tls_checker
from app.services.security_matrix_service import SecurityMatrixService
from app.services.service_metrics import ServiceMetricsService, service_metrics_cache
from app.services.service_service import ServiceService
from app.services.service_variable_service import ServiceVariableService


async def get_session(
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> AsyncIterator[AsyncSession]:
    """트랜잭션을 열지 않은 세션. 서비스가 session.begin() 블록 단위로 커밋한다."""
    async with session_factory() as session:
        yield session


def get_secret_client() -> SecretClient:
    return EnvVarSecretClient()


def get_clock() -> Clock:
    return SystemClock()


class ClusterReaderHolder:
    """환경 접속 확인 어댑터가 쓰는 클러스터 읽기 구현(API 서버 12). 프로세스당 하나를 재사용한다.

    ExecutorSetHolder 와 같은 모양이다. 만들 때 네트워크·비밀값 조회가 없다(첫 호출 때 한다).
    설정 객체가 바뀌면(시험의 설정 교체) 옛 것을 닫고 새로 만든다. 읽기 호출의 제한 시간은
    환경 접속 확인 전체 제한 시간과 같다.
    """

    def __init__(self) -> None:
        self._settings: Settings | None = None
        self._reader: ClusterReader | None = None
        self._closers: tuple[Callable[[], Awaitable[None]], ...] = ()

    async def get(self, settings: Settings, clock: Clock) -> ClusterReader:
        if self._reader is None or self._settings is not settings:
            await self.aclose()
            timeout = settings.environment_check_timeout_seconds
            metadata = GoogleMetadataClient(settings.google_metadata_base_url, clock)
            provider = ClusterAccessProvider(
                metadata=metadata,
                secret_client=EnvVarSecretClient(),
                clock=clock,
                default_aws_role_arn=settings.executor_aws_role_arn,
                timeout_seconds=timeout,
            )
            clients = KubeHttpClients(default_kube_http_factory, timeout)
            self._reader = ClusterReader(provider, clients)
            self._closers = (clients.aclose, provider.aclose, metadata.aclose)
            self._settings = settings
        return self._reader

    async def aclose(self) -> None:
        closers, self._closers = self._closers, ()
        self._reader, self._settings = None, None
        for close in closers:
            await close()


def cluster_reader_holder(state: State) -> ClusterReaderHolder:
    """앱 state 의 클러스터 읽기 보관함(없으면 만든다)."""
    holder = getattr(state, "cluster_reader_holder", None)
    if not isinstance(holder, ClusterReaderHolder):
        holder = ClusterReaderHolder()
        state.cluster_reader_holder = holder
    return holder


async def get_adapter_resolver(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> AdapterResolver:
    """환경 종류 → 어댑터. simulated(기본)면 가짜 값, real 이면 실제 클러스터 읽기 구현을 넣는다."""
    if settings.environment_adapter_mode is EnvironmentAdapterMode.SIMULATED:
        return get_environment_adapter
    reader = await cluster_reader_holder(request.app.state).get(settings, clock)
    return lambda kind: get_environment_adapter(kind, reader=reader)


def get_environment_connection_service(
    settings: Annotated[Settings, Depends(get_settings)],
    secret_client: Annotated[SecretClient, Depends(get_secret_client)],
    adapter_resolver: Annotated[AdapterResolver, Depends(get_adapter_resolver)],
) -> EnvironmentConnectionService:
    return EnvironmentConnectionService(
        secret_client=secret_client,
        adapter_resolver=adapter_resolver,
        timeout_seconds=settings.environment_check_timeout_seconds,
    )


def get_environment_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    connection_service: Annotated[
        EnvironmentConnectionService, Depends(get_environment_connection_service)
    ],
) -> EnvironmentService:
    return EnvironmentService(
        session=session,
        environment_repository=EnvironmentRepository(session),
        connection_service=connection_service,
        public_base_domain=settings.public_base_domain,
        clock=clock,
    )


def get_environment_kind_service() -> EnvironmentKindService:
    return EnvironmentKindService()


def build_source_repository_client(settings: Settings, clock: Clock) -> SourceRepositoryClient:
    """설정에 맞는 소스 저장소 클라이언트. 일꾼 진입점도 이 함수를 쓴다."""
    if settings.source_client_mode is SourceClientMode.GITHUB:
        return GithubAppSourceRepositoryClient(
            api_base_url=settings.github_api_base_url,
            app_id=settings.github_app_id,
            private_key_ref=(
                parse_secret_ref(settings.github_app_private_key_ref)
                if settings.github_app_private_key_ref
                else None
            ),
            secret_client=EnvVarSecretClient(),
            install_url=settings.github_app_install_url,
            timeout_seconds=settings.github_request_timeout_seconds,
            clock=clock,
        )
    return FakeSourceRepositoryClient(install_url=settings.github_app_install_url)


def get_source_repository_client(
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> SourceRepositoryClient:
    return build_source_repository_client(settings, clock)


def get_host_suffix_generator() -> HostSuffixGenerator:
    """대표·환경별 주소에 붙일 무작위 suffix 생성기. 시험은 고정 값 생성기로 바꾼다."""
    return generate_host_suffix


def get_service_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    source_client: Annotated[SourceRepositoryClient, Depends(get_source_repository_client)],
    host_suffix_generator: Annotated[HostSuffixGenerator, Depends(get_host_suffix_generator)],
) -> ServiceService:
    return ServiceService(
        session=session,
        source_client=source_client,
        clock=clock,
        public_base_domain=settings.public_base_domain,
        host_suffix_generator=host_suffix_generator,
    )


class ExecutorSetHolder:
    """API 프로세스의 실제 실행기 묶음. 프로세스당 하나를 만들어 재사용하고 종료 때 닫는다.

    만들 때 네트워크·비밀값 조회가 없다(첫 호출 때 한다). 설정 객체가 바뀌면(시험의 설정 교체)
    옛 묶음을 닫고 새로 만든다.
    """

    def __init__(self) -> None:
        self._settings: Settings | None = None
        self._executors: ExecutorSet | None = None

    async def get(
        self,
        settings: Settings,
        clock: Clock,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> ExecutorSet:
        if self._executors is None or self._settings is not settings:
            await self.aclose()
            self._executors = build_executor_set(
                settings, clock, environment_lookup=DbEnvironmentLookup(session_factory)
            )
            self._settings = settings
        return self._executors

    async def aclose(self) -> None:
        executors, self._executors, self._settings = self._executors, None, None
        if executors is not None:
            await executors.aclose()


def executor_set_holder(state: State) -> ExecutorSetHolder:
    """앱 state 의 실행기 묶음 보관함(없으면 만든다)."""
    holder = getattr(state, "executor_set_holder", None)
    if not isinstance(holder, ExecutorSetHolder):
        holder = ExecutorSetHolder()
        state.executor_set_holder = holder
    return holder


async def get_executor_set(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> ExecutorSet | None:
    """API 프로세스가 로그를 읽을 실행기 묶음(배포는 일꾼만 한다).

    - fake: 요청마다 가짜 묶음(상태 없음, 만드는 비용 없음).
    - real: 프로세스당 하나(app.state, 종료 때 lifespan 이 닫는다).
    시험은 dependency_overrides 로 시험용 가짜 실행기를 넣는다.
    """
    if settings.executor_mode is ExecutorMode.FAKE:
        return build_executor_set(settings, clock)
    return await executor_set_holder(request.app.state).get(settings, clock, session_factory)


def get_migration_plan_result_service(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    executors: Annotated[ExecutorSet | None, Depends(get_executor_set)],
) -> MigrationPlanResultService:
    return MigrationPlanResultService(
        session=session,
        reader=executors.cutover_runs if executors is not None else NoCutoverRuns(),
        cache=cutover_result_cache(request.app.state),
        clock=clock,
        is_enabled=settings.cutover_results_enabled,
        not_before=settings.cutover_results_not_before,
        cache_seconds=settings.cutover_result_cache_seconds,
    )


def get_deploy_request_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    source_client: Annotated[SourceRepositoryClient, Depends(get_source_repository_client)],
    executors: Annotated[ExecutorSet | None, Depends(get_executor_set)],
) -> DeployRequestService:
    return DeployRequestService(
        session=session,
        source_client=source_client,
        clock=clock,
        executor_mode=settings.executor_mode,
        public_base_domain=settings.public_base_domain,
        executors=executors,
    )


class AppSecretStoreHolder:
    """API 프로세스의 고객 앱 환경 변수 보관함. 프로세스당 하나를 재사용하고 종료 때 닫는다.

    만들 때 네트워크 호출이 없다. 설정 객체가 바뀌면(시험의 설정 교체) 옛 것을 닫고 새로 만든다.
    """

    def __init__(self) -> None:
        self._settings: Settings | None = None
        self._store: AppSecretStore | None = None

    async def get(self, settings: Settings, clock: Clock) -> AppSecretStore:
        if self._store is None or self._settings is not settings:
            await self.aclose()
            self._store = build_app_secret_store(settings, clock)
            self._settings = settings
        return self._store

    async def aclose(self) -> None:
        store, self._store, self._settings = self._store, None, None
        if store is not None:
            await store.aclose()


def app_secret_store_holder(state: State) -> AppSecretStoreHolder:
    """앱 state 의 보관함 보관소(없으면 만든다)."""
    holder = getattr(state, "app_secret_store_holder", None)
    if not isinstance(holder, AppSecretStoreHolder):
        holder = AppSecretStoreHolder()
        state.app_secret_store_holder = holder
    return holder


async def get_app_secret_store(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> AppSecretStore:
    return await app_secret_store_holder(request.app.state).get(settings, clock)


def get_service_variable_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    store: Annotated[AppSecretStore, Depends(get_app_secret_store)],
) -> ServiceVariableService:
    return ServiceVariableService(session=session, store=store, clock=clock, settings=settings)


def get_github_user_client(
    settings: Annotated[Settings, Depends(get_settings)],
    secret_client: Annotated[SecretClient, Depends(get_secret_client)],
) -> GithubUserClient:
    """실제 GitHub 사용자 클라이언트. 가짜는 시험의 dependency_overrides 로만 넣는다."""
    return HttpGithubUserClient(
        oauth_base_url=settings.github_oauth_base_url,
        api_base_url=settings.github_api_base_url,
        client_id=settings.github_app_client_id,
        client_secret_ref=(
            parse_secret_ref(settings.github_app_client_secret_ref)
            if settings.github_app_client_secret_ref
            else None
        ),
        secret_client=secret_client,
        timeout_seconds=settings.github_request_timeout_seconds,
    )


def get_auth_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    github_client: Annotated[GithubUserClient, Depends(get_github_user_client)],
) -> AuthService:
    return AuthService(session=session, settings=settings, clock=clock, github_client=github_client)


def get_organization_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> OrganizationService:
    return OrganizationService(
        session=session, clock=clock, reserved_slugs=settings.reserved_org_slugs
    )


def get_invitation_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    github_client: Annotated[GithubUserClient, Depends(get_github_user_client)],
) -> InvitationService:
    return InvitationService(
        session=session,
        clock=clock,
        github_client=github_client,
        invitation_ttl_seconds=settings.invitation_ttl_seconds,
    )


def get_agent_token_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> AgentTokenService:
    return AgentTokenService(
        session=session,
        clock=clock,
        default_days=settings.agent_token_default_days,
        max_days=settings.agent_token_max_days,
    )


def get_security_exception_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> SecurityExceptionService:
    return SecurityExceptionService(session, clock)


def get_public_tls_checker(
    settings: Annotated[Settings, Depends(get_settings)],
) -> PublicHostTlsChecker | None:
    """보안 게이트의 실행 직전 TLS 시험기. 게이트가 꺼져 있으면 None(게이트 없음)."""
    return build_public_tls_checker(settings)


def get_audit_log_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuditLogService:
    return AuditLogService(session=session)


def get_migration_plan_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    tls_checker: Annotated[PublicHostTlsChecker | None, Depends(get_public_tls_checker)],
) -> MigrationPlanService:
    return MigrationPlanService(
        session=session,
        clock=clock,
        executor_mode=settings.executor_mode,
        required_requests_default=settings.migration_required_requests_default,
        max_wait_seconds_default=settings.migration_max_wait_seconds_default,
        expected_rps=settings.migration_expected_rps,
        step_overhead_seconds=settings.migration_step_overhead_seconds,
        approval_ttl_seconds=settings.migration_plan_approval_ttl_seconds,
        security_gate=(
            SecurityGate(session, clock, tls_checker, settings.was_version)
            if tls_checker is not None
            else None
        ),
    )


def get_disaster_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    migration_plans: Annotated[MigrationPlanService, Depends(get_migration_plan_service)],
) -> DisasterService:
    return DisasterService(
        session=session,
        clock=clock,
        migration_plans=migration_plans,
        entrypoint_environment_keys=frozenset(
            item.partition("=")[0].strip() for item in settings.entrypoint_environment_map
        ),
    )


def get_security_matrix_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> SecurityMatrixService:
    return SecurityMatrixService(session=session, clock=clock, was_version=settings.was_version)


def get_security_compare_service(
    matrix: Annotated[SecurityMatrixService, Depends(get_security_matrix_service)],
) -> SecurityCompareService:
    return SecurityCompareService(matrix)


def get_cost_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
    catalog: Annotated[CostCatalog, Depends(get_cost_catalog)],
) -> CostService:
    return CostService(session, clock, catalog)


def get_prometheus_client(
    settings: Annotated[Settings, Depends(get_settings)],
    secret_client: Annotated[SecretClient, Depends(get_secret_client)],
) -> PrometheusClient | None:
    """진입점 Prometheus 읽기(운영 2). 주소가 없으면 None(기능 꺼짐).

    만들 때 네트워크·비밀값 조회가 없다(호출 때 한다).
    """
    if not settings.entrypoint_prometheus_url:
        return None
    password_ref = settings.entrypoint_prometheus_basic_auth_password_ref
    return PrometheusClient(
        settings.entrypoint_prometheus_url,
        secret_client=secret_client,
        username=settings.entrypoint_prometheus_basic_auth_user,
        password_ref=parse_secret_ref(password_ref) if password_ref else None,
        timeout_seconds=settings.entrypoint_request_timeout_seconds,
    )


def get_service_metrics_service(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    prometheus: Annotated[PrometheusClient | None, Depends(get_prometheus_client)],
    executors: Annotated[ExecutorSet | None, Depends(get_executor_set)],
) -> ServiceMetricsService:
    return ServiceMetricsService(
        session,
        prometheus=prometheus,
        data_sync=executors.data_sync if executors is not None else None,
        cache=service_metrics_cache(request.app.state),
        clock=clock,
        environment_map=parse_entrypoint_environment_map(settings),
        cache_seconds=settings.service_metrics_cache_seconds,
    )


def get_precheck_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> PrecheckService:
    return PrecheckService(session, clock)


def get_ai_residency_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> AiResidencyService:
    return AiResidencyService(session, clock)


def get_precheck_patch_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> PrecheckPatchService:
    return PrecheckPatchService(session, clock, patch_apply_enabled=settings.patch_apply_enabled)


def get_agent_run_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> AgentRunService:
    return AgentRunService(session, clock)


def get_diagnosis_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    deploy_requests: Annotated[DeployRequestService, Depends(get_deploy_request_service)],
) -> DiagnosisService:
    return DiagnosisService(
        session,
        clock,
        deploy_requests,
        simulate_enabled=settings.ai_diagnosis_simulate_enabled,
        runner_enabled=settings.ai_diagnosis_runner_enabled,
    )


def get_advisory_feed(settings: Annotated[Settings, Depends(get_settings)]) -> AdvisoryFeed:
    """CVE 감시 피드(OSV.dev, SPEC sec-6-cve-watch). 만들 때 네트워크를 쓰지 않는다."""
    return OsvAdvisoryFeed(
        settings.cve_watch_osv_url,
        settings.cve_watch_timeout_seconds,
        settings.cve_watch_max_advisories,
    )


def get_cve_watch_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> CveWatchService:
    return CveWatchService(session, clock)
