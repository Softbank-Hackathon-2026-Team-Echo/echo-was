"""빌드·배포·대표 주소·헬스체크·내리기·트래픽·지표·데이터 확인 실행기.

인터페이스는 base, 가짜 구현은 fake, 실제 구현은 http_health_checker(헬스체크)·entrypoint(트래픽·
대표 주소·지표).
"""

from dataclasses import dataclass

from app.clients.secret_client import EnvVarSecretClient, SecretClient
from app.core.clock import Clock
from app.core.config import EntrypointExecutorMode, ExecutorMode, HealthCheckerMode, Settings
from app.executors.base import (
    BuildExecutor,
    DataSyncChecker,
    DeployExecutor,
    HealthChecker,
    MainRouteExecutor,
    MetricsProvider,
    TeardownExecutor,
    TrafficExecutor,
)
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
from app.executors.http_health_checker import HttpHealthChecker


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


def build_executor_set(
    settings: Settings, clock: Clock, secret_client: SecretClient | None = None
) -> ExecutorSet:
    """설정에 맞는 실행기 묶음.

    실제 빌드·배포·내리기 실행기는 인프라 3 이후, 데이터 확인은 인프라 7 이후에 더한다. 트래픽·대표
    주소·지표는 `entrypoint_executor_mode = http` 면 고정 진입점을 부른다(인프라 5·6·9). 가짜 지표는
    가짜 트래픽 실행기가 적용한 비율을 읽는다.
    """
    if settings.executor_mode is not ExecutorMode.FAKE:
        raise NotImplementedError("real executors are not available yet")
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
        client = build_entrypoint_client(settings, secret_client or EnvVarSecretClient())
        traffic = EntrypointTrafficExecutor(client, clock)
        main_route = EntrypointMainRouteExecutor(client, clock)
        metrics = EntrypointMetricsProvider(client)
    else:
        fake_traffic = FakeTrafficExecutor(clock)
        traffic = fake_traffic
        main_route = FakeMainRouteExecutor(clock)
        metrics = FakeMetricsProvider(fake_traffic.weight_of)
    return ExecutorSet(
        build=FakeBuildExecutor(clock, step_seconds),
        deploy=FakeDeployExecutor(clock, step_seconds),
        main_route=main_route,
        health=health,
        teardown=FakeTeardownExecutor(clock, step_seconds),
        traffic=traffic,
        metrics=metrics,
        data_sync=FakeDataSyncChecker(),
    )
