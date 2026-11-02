"""빌드·배포·대표 주소·헬스체크·내리기·트래픽·지표·데이터 확인 실행기.

인터페이스는 base, 가짜 구현은 fake.
"""

from dataclasses import dataclass

from app.core.clock import Clock
from app.core.config import ExecutorMode, HealthCheckerMode, Settings
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


def build_executor_set(settings: Settings, clock: Clock) -> ExecutorSet:
    """설정에 맞는 실행기 묶음.

    실제 빌드·배포·라우터·내리기 실행기는 인프라 3·5 이후, 트래픽(Envoy)·지표(OpenTelemetry)·데이터
    확인은 인프라 6·9·7 이후에 더한다. 가짜 지표는 가짜 트래픽 실행기가 적용한 비율을 읽는다.
    """
    if settings.executor_mode is not ExecutorMode.FAKE:
        raise NotImplementedError("real executors are not available yet")
    step_seconds = settings.fake_executor_step_seconds
    health: HealthChecker = (
        HttpHealthChecker()
        if settings.health_checker_mode is HealthCheckerMode.HTTP
        else FakeHealthChecker()
    )
    traffic = FakeTrafficExecutor(clock)
    return ExecutorSet(
        build=FakeBuildExecutor(clock, step_seconds),
        deploy=FakeDeployExecutor(clock, step_seconds),
        main_route=FakeMainRouteExecutor(clock),
        health=health,
        teardown=FakeTeardownExecutor(clock, step_seconds),
        traffic=traffic,
        metrics=FakeMetricsProvider(traffic.weight_of),
        data_sync=FakeDataSyncChecker(),
    )
