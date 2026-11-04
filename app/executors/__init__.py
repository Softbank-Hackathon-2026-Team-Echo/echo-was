"""빌드·배포·대표 주소·헬스체크·내리기·트래픽·지표·데이터 확인 실행기.

인터페이스는 base, 가짜 구현은 fake, 실제 구현은 http_health_checker(헬스체크)·entrypoint(트래픽·
대표 주소·지표)·github_actions(빌드)·kubernetes(배포·내리기).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx

from app.clients.secret_client import EnvVarSecretClient, SecretClient
from app.core.clock import Clock
from app.core.config import (
    DataSyncCheckerMode,
    EntrypointExecutorMode,
    ExecutorMode,
    HealthCheckerMode,
    Settings,
)
from app.core.secrets import parse_secret_ref
from app.executors.base import (
    BuildExecutor,
    DataSyncChecker,
    DeployExecutor,
    EnvironmentLookup,
    HealthChecker,
    MainRouteExecutor,
    MetricsProvider,
    TeardownExecutor,
    TrafficExecutor,
)
from app.executors.cloudflare_dns import CloudflareDnsClient
from app.executors.entrypoint import (
    EntrypointMainRouteExecutor,
    EntrypointMetricsProvider,
    EntrypointTrafficExecutor,
    build_entrypoint_client,
)
from app.executors.fake import (
    FakeBuildExecutor,
    FakeDataSyncChecker,
    FakeDeployExecutor,
    FakeHealthChecker,
    FakeMainRouteExecutor,
    FakeMetricsProvider,
    FakeTeardownExecutor,
    FakeTrafficExecutor,
)
from app.executors.github_actions import GithubActionsBuildExecutor
from app.executors.google_metadata import GoogleMetadataClient
from app.executors.http_health_checker import HttpHealthChecker
from app.executors.kubernetes.access import ClusterAccessProvider
from app.executors.kubernetes.api import KubeHttpClients, KubeHttpFactory, default_kube_http_factory
from app.executors.kubernetes.executors import (
    KubernetesDeployExecutor,
    KubernetesRuntime,
    KubernetesTeardownExecutor,
)
from app.executors.postgres_data_sync import PostgresDataSyncChecker, parse_replication_endpoints
from app.executors.retry import Sleep, real_sleep

Closer = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class ExecutorSet:
    build: BuildExecutor
    deploy: DeployExecutor
    main_route: MainRouteExecutor
    health: HealthChecker
    teardown: TeardownExecutor
    traffic: TrafficExecutor
    metrics: MetricsProvider
    data_sync: DataSyncChecker
    # 프로세스가 끝날 때 닫을 HTTP 클라이언트(실제 실행기만).
    closers: tuple[Closer, ...] = field(default=(), repr=False)

    async def aclose(self) -> None:
        for close in self.closers:
            await close()


@dataclass(frozen=True)
class RealExecutorTransports:
    """시험이 바꿔 끼우는 전송(httpx.MockTransport)과 sleep. 운영은 기본값(실제 네트워크)."""

    github: httpx.AsyncBaseTransport | None = None
    cloud: httpx.AsyncBaseTransport | None = None
    metadata: httpx.AsyncBaseTransport | None = None
    cloudflare: httpx.AsyncBaseTransport | None = None
    kube_http_factory: KubeHttpFactory = default_kube_http_factory
    sleep: Sleep = real_sleep


def build_executor_set(
    settings: Settings,
    clock: Clock,
    secret_client: SecretClient | None = None,
    *,
    environment_lookup: EnvironmentLookup | None = None,
    transports: RealExecutorTransports | None = None,
) -> ExecutorSet:
    """설정에 맞는 실행기 묶음.

    `executor_mode = real` 이면 빌드·배포·내리기가 실제(GitHub Actions + 쿠버네티스 직접 반영)다.
    만들 때 네트워크·비밀값 조회를 하지 않는다(처음 호출할 때 한다). 데이터 확인은
    `data_sync_checker_mode = postgres` 면 출발 PostgreSQL 의 복제 지연을 읽는다(인프라 7).
    트래픽·대표 주소·지표는 `entrypoint_executor_mode = http` 면 고정 진입점을 부른다.
    가짜 지표·카운터는 가짜 트래픽 실행기가 적용한 비율과 설정 MIGRATION_EXPECTED_RPS 로 만든다.
    """
    secrets = secret_client or EnvVarSecretClient()
    step_seconds = settings.fake_executor_step_seconds
    health: HealthChecker = (
        HttpHealthChecker()
        if settings.health_checker_mode is HealthCheckerMode.HTTP
        else FakeHealthChecker()
    )
    traffic: TrafficExecutor
    main_route: MainRouteExecutor
    metrics: MetricsProvider
    if settings.entrypoint_executor_mode is EntrypointExecutorMode.HTTP:
        client = build_entrypoint_client(settings, secrets)
        traffic = EntrypointTrafficExecutor(client, clock)
        main_route = EntrypointMainRouteExecutor(client, clock)
        metrics = EntrypointMetricsProvider(client)
    else:
        fake_traffic = FakeTrafficExecutor(clock)
        traffic = fake_traffic
        main_route = FakeMainRouteExecutor(clock)
        metrics = FakeMetricsProvider(
            clock, fake_traffic.weight_of, requests_per_second=settings.migration_expected_rps
        )
    build: BuildExecutor
    deploy: DeployExecutor
    teardown: TeardownExecutor
    closers: tuple[Closer, ...] = ()
    if settings.executor_mode is ExecutorMode.REAL:
        real_build, runtime = _real_executors(
            settings, clock, secrets, environment_lookup, transports or RealExecutorTransports()
        )
        build = real_build
        deploy = KubernetesDeployExecutor(runtime)
        teardown = KubernetesTeardownExecutor(runtime)
        closers = (real_build.aclose, runtime.aclose)
    else:
        build = FakeBuildExecutor(clock, step_seconds)
        deploy = FakeDeployExecutor(clock, step_seconds)
        teardown = FakeTeardownExecutor(clock, step_seconds)
    data_sync: DataSyncChecker = FakeDataSyncChecker()
    if settings.data_sync_checker_mode is DataSyncCheckerMode.POSTGRES:
        if not settings.data_sync_password_ref:
            raise ValueError("data_sync_password_ref is required")
        data_sync = PostgresDataSyncChecker(
            parse_replication_endpoints(settings.data_sync_databases),
            user=settings.data_sync_user,
            database=settings.data_sync_database,
            password_ref=parse_secret_ref(settings.data_sync_password_ref),
            secret_client=secrets,
            ssl_mode=settings.data_sync_ssl_mode,
            timeout_seconds=settings.data_sync_timeout_seconds,
        )
    return ExecutorSet(
        build=build,
        deploy=deploy,
        main_route=main_route,
        health=health,
        teardown=teardown,
        traffic=traffic,
        metrics=metrics,
        data_sync=data_sync,
        closers=closers,
    )


def _real_executors(
    settings: Settings,
    clock: Clock,
    secrets: SecretClient,
    environment_lookup: EnvironmentLookup | None,
    transports: RealExecutorTransports,
) -> tuple[GithubActionsBuildExecutor, KubernetesRuntime]:
    build = GithubActionsBuildExecutor(
        api_base_url=settings.github_api_base_url,
        repository=settings.build_github_repository,
        workflow=settings.build_github_workflow,
        ref=settings.build_github_ref,
        app_id=settings.github_app_id,
        private_key_ref=(
            parse_secret_ref(settings.github_app_private_key_ref)
            if settings.github_app_private_key_ref
            else None
        ),
        secret_client=secrets,
        clock=clock,
        timeout_seconds=settings.github_request_timeout_seconds,
        transport=transports.github,
        sleep=transports.sleep,
    )
    metadata = GoogleMetadataClient(
        settings.google_metadata_base_url, clock, transport=transports.metadata
    )
    dns = (
        CloudflareDnsClient(
            api_base_url=settings.cloudflare_api_base_url,
            zone_id=settings.cloudflare_zone_id,
            token_ref=parse_secret_ref(settings.cloudflare_api_token_ref),
            secret_client=secrets,
            transport=transports.cloudflare,
        )
        if settings.cloudflare_api_token_ref and settings.cloudflare_zone_id
        else None
    )
    runtime = KubernetesRuntime(
        provider=ClusterAccessProvider(
            metadata=metadata,
            secret_client=secrets,
            clock=clock,
            default_aws_role_arn=settings.executor_aws_role_arn,
            timeout_seconds=settings.kube_request_timeout_seconds,
            transport=transports.cloud,
        ),
        clients=KubeHttpClients(
            transports.kube_http_factory, settings.kube_request_timeout_seconds
        ),
        metadata=metadata,
        dns=dns,
        environment_lookup=environment_lookup,
        image_mirrors=tuple(
            (source.strip().rstrip("/"), mirror.strip().rstrip("/"))
            for source, _, mirror in (
                item.partition("=") for item in settings.image_registry_mirrors
            )
        ),
        clock=clock,
        sleep=transports.sleep,
        image_pull_grace_seconds=settings.deploy_image_pull_grace_seconds,
    )
    return build, runtime
