"""이전 계획용 가짜 실행기(트래픽·지표·데이터 확인)가 공통 계약과 결정적 값을 지키는지.

SPEC api-8 AC-18.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.executors import build_executor_set
from app.executors.base import (
    DataSyncUnavailable,
    MetricsUnavailable,
    TrafficApplied,
    TrafficFailed,
    WeightedTarget,
    check_weights_total,
)
from app.executors.fake import (
    FakeDataSyncChecker,
    FakeMetricsProvider,
    FakeTrafficExecutor,
)
from tests.executor_contract import (
    ROUTE,
    STANDBY_TARGET,
    TARGET,
    WINDOW_END,
    assert_data_sync_checker_contract,
    assert_metrics_provider_contract,
    assert_traffic_executor_contract,
)


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self) -> None:
        self.value += timedelta(seconds=7)


async def test_fake_traffic_executor_satisfies_contract() -> None:
    clock = _Clock()

    await assert_traffic_executor_contract(FakeTrafficExecutor(clock), clock.advance)


async def test_fake_metrics_provider_satisfies_contract_with_fixed_values() -> None:
    metrics = await assert_metrics_provider_contract(FakeMetricsProvider())

    # 비율을 모르면 100% 로 본다: 10 × 1.0 × 5초 = 50.
    assert (metrics.error_rate_percent, metrics.p95_ms, metrics.request_count) == (0.1, 120.0, 50)


async def test_fake_data_sync_checker_satisfies_contract_with_fixed_lag() -> None:
    lag = await assert_data_sync_checker_contract(FakeDataSyncChecker())

    assert lag.seconds == 0.3


async def test_fake_metrics_request_count_follows_applied_weight() -> None:
    clock = _Clock()
    traffic = FakeTrafficExecutor(clock)
    metrics = FakeMetricsProvider(traffic.weight_of)
    since = WINDOW_END - timedelta(seconds=5)

    await traffic.set_weights(
        ROUTE, (WeightedTarget(TARGET, 90), WeightedTarget(STANDBY_TARGET, 10)), "p:1"
    )
    at_ten = await metrics.read(STANDBY_TARGET, since, WINDOW_END)
    await traffic.set_weights(
        ROUTE, (WeightedTarget(TARGET, 100), WeightedTarget(STANDBY_TARGET, 0)), "p:rollback:0"
    )
    at_zero = await metrics.read(STANDBY_TARGET, since, WINDOW_END)

    # max(1, round(10 × 비율/100 × 창 초)).
    assert getattr(at_ten, "request_count", None) == 5
    assert getattr(at_zero, "request_count", None) == 1


async def test_fake_traffic_failure_hook_fails_only_matching_keys_and_keeps_result() -> None:
    clock = _Clock()
    traffic = FakeTrafficExecutor(clock, fail_when=lambda key: ":rollback:" in key)
    weights = (WeightedTarget(TARGET, 100), WeightedTarget(STANDBY_TARGET, 0))

    failed = await traffic.set_weights(ROUTE, weights, "p:rollback:0")
    failed_again = await traffic.set_weights(ROUTE, weights, "p:rollback:0")
    applied = await traffic.set_weights(ROUTE, weights, "p:4")

    assert isinstance(failed, TrafficFailed) and failed_again == failed
    assert isinstance(applied, TrafficApplied)
    assert [key for key, _ in traffic.applied] == ["p:4"]
    assert traffic.weight_of(STANDBY_TARGET.service_environment_id) == 0


async def test_fake_unavailable_switches_return_unavailable() -> None:
    metrics = await FakeMetricsProvider(unavailable=True).read(TARGET, WINDOW_END, WINDOW_END)
    lag = await FakeDataSyncChecker(unavailable=True).replication_lag(TARGET, STANDBY_TARGET)

    assert isinstance(metrics, MetricsUnavailable)
    assert isinstance(lag, DataSyncUnavailable)


@pytest.mark.parametrize("weights", [(90, 20), (50, 49), (101, -1)])
def test_check_weights_total_rejects_bad_weights(weights: tuple[int, int]) -> None:
    with pytest.raises(ValueError):
        check_weights_total(
            (WeightedTarget(TARGET, weights[0]), WeightedTarget(STANDBY_TARGET, weights[1]))
        )


async def test_build_executor_set_wires_fake_metrics_to_fake_traffic() -> None:
    clock = _Clock()
    executors = build_executor_set(
        Settings(database_url="postgresql+asyncpg://x:y@127.0.0.1:1/z"), clock
    )

    await executors.traffic.set_weights(
        ROUTE, (WeightedTarget(TARGET, 50), WeightedTarget(STANDBY_TARGET, 50)), "p:2"
    )
    metrics = await executors.metrics.read(
        STANDBY_TARGET, WINDOW_END - timedelta(seconds=4), WINDOW_END
    )

    assert getattr(metrics, "request_count", None) == 20
    assert executors.traffic.simulated
    assert executors.metrics.simulated
    assert executors.data_sync.simulated
