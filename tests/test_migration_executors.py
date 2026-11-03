"""이전 계획용 가짜 실행기(트래픽·지표·카운터·데이터 확인)가 공통 계약과 결정적 값을 지키는지.

SPEC api-8 AC-18, api-8-1 AC-8-1-13.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.executors import build_executor_set
from app.executors.base import (
    CountsUnavailable,
    DataSyncUnavailable,
    MetricsUnavailable,
    RequestCounts,
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
    assert_counts_contract,
    assert_data_sync_checker_contract,
    assert_metrics_provider_contract,
    assert_traffic_executor_contract,
)


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: float = 7) -> None:
        self.value += timedelta(seconds=seconds)


async def test_fake_traffic_executor_satisfies_contract() -> None:
    clock = _Clock()

    await assert_traffic_executor_contract(FakeTrafficExecutor(clock), clock.advance)


async def test_fake_metrics_provider_satisfies_contract_with_fixed_values() -> None:
    metrics = await assert_metrics_provider_contract(FakeMetricsProvider(_Clock()))

    # 비율을 모르면 100% 로 본다: 50 rps × 1.0 × 5초 = 250.
    assert (metrics.error_rate_percent, metrics.p95_ms, metrics.request_count) == (0.1, 120.0, 250)


async def test_fake_counts_satisfy_contract_and_follow_rps_and_weight() -> None:
    clock = _Clock()
    traffic = FakeTrafficExecutor(clock)
    metrics = FakeMetricsProvider(clock, traffic.weight_of, requests_per_second=50)
    await traffic.set_weights(
        ROUTE, (WeightedTarget(TARGET, 90), WeightedTarget(STANDBY_TARGET, 10)), "p:1"
    )

    first, second = await assert_counts_contract(metrics, clock.advance)
    full_first = await metrics.read_counts(TARGET)
    clock.advance(10)
    standby = await metrics.read_counts(STANDBY_TARGET)
    full = await metrics.read_counts(TARGET)

    # 처음 읽은 순간 0, 10% 연결은 50 × 0.1 × 7초 = 35, 5xx 는 0.
    assert (first, second) == (RequestCounts(0, 0), RequestCounts(35, 0))
    assert isinstance(standby, RequestCounts) and isinstance(full, RequestCounts)
    assert isinstance(full_first, RequestCounts)
    # 다음 10초: 10% 연결 +50, 90% 연결 +450 (비율에 비례).
    assert standby.requests - second.requests == 50
    assert full.requests - full_first.requests == 450


async def test_fake_counts_never_go_down_when_weight_changes() -> None:
    clock = _Clock()
    traffic = FakeTrafficExecutor(clock)
    metrics = FakeMetricsProvider(clock, traffic.weight_of)
    weights = ((50, 50), (100, 0), (0, 100))
    seen: list[int] = []

    for index, (source, target) in enumerate(weights):
        await traffic.set_weights(
            ROUTE,
            (WeightedTarget(TARGET, source), WeightedTarget(STANDBY_TARGET, target)),
            f"p:{index}",
        )
        clock.advance(2)
        counts = await metrics.read_counts(STANDBY_TARGET)
        assert isinstance(counts, RequestCounts)
        seen.append(counts.requests)

    assert seen == sorted(seen)
    assert seen[1] == seen[0]  # 비율 0 동안은 늘지 않는다.


async def test_fake_counts_knobs_freeze_add_errors_set_and_fail() -> None:
    clock = _Clock()
    metrics = FakeMetricsProvider(clock)
    standby_id = STANDBY_TARGET.service_environment_id

    await metrics.read_counts(STANDBY_TARGET)
    clock.advance(2)
    before_freeze = await metrics.read_counts(STANDBY_TARGET)
    metrics.freeze(standby_id)
    clock.advance(5)
    frozen = await metrics.read_counts(STANDBY_TARGET)
    metrics.add_errors(standby_id, 3)
    with_errors = await metrics.read_counts(STANDBY_TARGET)
    metrics.set_counts(standby_id, 4)
    restarted = await metrics.read_counts(STANDBY_TARGET)
    metrics.fail_counts(standby_id)
    failing = await metrics.read_counts(STANDBY_TARGET)
    other = await metrics.read_counts(TARGET)
    metrics.fail_counts(standby_id, is_failing=False)
    metrics.fail_counts()
    all_failing = await metrics.read_counts(TARGET)

    assert before_freeze == RequestCounts(100, 0)
    assert frozen == RequestCounts(100, 0)
    assert with_errors == RequestCounts(100, 3)
    # 줄이면 진입점 재시작처럼 보인다.
    assert restarted == RequestCounts(4, 0)
    assert failing == CountsUnavailable()
    assert isinstance(other, RequestCounts)
    assert all_failing == CountsUnavailable()


async def test_fake_counts_keep_accumulating_while_reads_fail() -> None:
    clock = _Clock()
    metrics = FakeMetricsProvider(clock)
    standby_id = STANDBY_TARGET.service_environment_id

    await metrics.read_counts(STANDBY_TARGET)
    metrics.fail_counts(standby_id)
    clock.advance(1)
    await metrics.read_counts(STANDBY_TARGET)
    metrics.fail_counts(standby_id, is_failing=False)
    clock.advance(1)
    recovered = await metrics.read_counts(STANDBY_TARGET)

    # 읽기만 실패했고 진입점은 계속 셌다: 50 × 2초.
    assert recovered == RequestCounts(100, 0)


async def test_fake_data_sync_checker_satisfies_contract_with_fixed_lag() -> None:
    lag = await assert_data_sync_checker_contract(FakeDataSyncChecker())

    assert lag.seconds == 0.3


async def test_fake_metrics_request_count_follows_applied_weight() -> None:
    clock = _Clock()
    traffic = FakeTrafficExecutor(clock)
    metrics = FakeMetricsProvider(clock, traffic.weight_of, requests_per_second=10)
    since = WINDOW_END - timedelta(seconds=5)

    await traffic.set_weights(
        ROUTE, (WeightedTarget(TARGET, 90), WeightedTarget(STANDBY_TARGET, 10)), "p:1"
    )
    at_ten = await metrics.read(STANDBY_TARGET, since, WINDOW_END)
    await traffic.set_weights(
        ROUTE, (WeightedTarget(TARGET, 100), WeightedTarget(STANDBY_TARGET, 0)), "p:rollback:0"
    )
    at_zero = await metrics.read(STANDBY_TARGET, since, WINDOW_END)

    # max(1, round(rps × 비율/100 × 창 초)).
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
    metrics = await FakeMetricsProvider(_Clock(), unavailable=True).read(
        TARGET, WINDOW_END, WINDOW_END
    )
    lag = await FakeDataSyncChecker(unavailable=True).replication_lag(TARGET, STANDBY_TARGET)

    assert isinstance(metrics, MetricsUnavailable)
    assert isinstance(lag, DataSyncUnavailable)


@pytest.mark.parametrize("weights", [(90, 20), (50, 49), (101, -1)])
def test_check_weights_total_rejects_bad_weights(weights: tuple[int, int]) -> None:
    with pytest.raises(ValueError):
        check_weights_total(
            (WeightedTarget(TARGET, weights[0]), WeightedTarget(STANDBY_TARGET, weights[1]))
        )


async def test_build_executor_set_wires_fake_metrics_to_fake_traffic_and_expected_rps() -> None:
    clock = _Clock()
    executors = build_executor_set(
        Settings(database_url="postgresql+asyncpg://x:y@127.0.0.1:1/z", migration_expected_rps=20),
        clock,
    )

    await executors.traffic.set_weights(
        ROUTE, (WeightedTarget(TARGET, 50), WeightedTarget(STANDBY_TARGET, 50)), "p:2"
    )
    metrics = await executors.metrics.read(
        STANDBY_TARGET, WINDOW_END - timedelta(seconds=4), WINDOW_END
    )
    await executors.metrics.read_counts(STANDBY_TARGET)
    clock.advance(3)
    counts = await executors.metrics.read_counts(STANDBY_TARGET)

    # 20 rps × 50% × 4초 = 40, 카운터는 20 × 50% × 3초 = 30.
    assert getattr(metrics, "request_count", None) == 40
    assert counts == RequestCounts(30, 0)
    assert executors.traffic.simulated
    assert executors.metrics.simulated
    assert executors.data_sync.simulated
