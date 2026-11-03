"""이전 비율 단계 "요청 N건 처리 확인" 규칙(순수 계산). SPEC api-8-1 AC-8-1-3·5·6·7·17."""

from datetime import UTC, datetime, timedelta

import pytest

from app.enums import MigrationErrorCode, MigrationJudgeReason
from app.services.migration_counting import (
    CounterSide,
    CounterState,
    CounterValue,
    StepCounting,
    allowed_errors_5xx,
    error_rate_percent,
    expected_shift_seconds,
    is_target_not_receiving,
    judge_counting,
)

START = datetime(2026, 11, 3, 3, 0, tzinfo=UTC)
DEADLINE = START + timedelta(seconds=30)
STALE = timedelta(seconds=5)


@pytest.mark.parametrize(
    ("required", "rate", "allowed"),
    [
        (20, 1.0, 1),  # floor(0.2) = 0 → 최소 1
        (50, 1.0, 1),
        (50, 10.0, 5),
        (10000, 0.57, 57),  # 부동소수로 곱하면 56.99999… → 56 이 나오는 경계
        (100, 0.0, 1),
    ],
)
def test_allowed_errors_5xx_uses_exact_decimal_floor_with_minimum_one(
    required: int, rate: float, allowed: int
) -> None:
    assert allowed_errors_5xx(required, rate) == allowed


def test_float_multiplication_would_miss_the_057_percent_boundary() -> None:
    # 이 시험이 지키는 경계: float 로 계산하면 56 이다.
    assert int(10000 * 0.57 / 100) == 56


@pytest.mark.parametrize(
    ("errors", "requests", "rate"),
    [(0, 20, 0.0), (1, 20, 5.0), (3, 1, 100.0), (2, 0, None)],
)
def test_error_rate_percent_caps_at_100_and_is_none_without_requests(
    errors: int, requests: int, rate: float | None
) -> None:
    assert error_rate_percent(errors, requests) == rate


@pytest.mark.parametrize(
    ("source", "target", "required", "weight", "expected"),
    [
        (180, 4, 20, 10, True),  # 180 × 10 ≥ 20 × 90, 4 × 4 × 90 < 1800
        (180, 5, 20, 10, False),  # 4 × 5 × 90 = 1800 은 미만이 아니다
        (179, 0, 20, 10, False),  # 출발이 아직 기대만큼 오지 않았다(서비스 전체가 조용함)
        (60, 14, 50, 100, True),  # 출발 비율 0인데 출발로 계속 간다
        (60, 15, 50, 100, False),
        (49, 0, 50, 100, False),
        (50, 12, 50, 50, True),
        (50, 13, 50, 50, False),
    ],
)
def test_is_target_not_receiving_uses_integer_formula(
    source: int, target: int, required: int, weight: int, expected: bool
) -> None:
    assert is_target_not_receiving(source, target, required, weight) is expected


def test_counter_state_starts_at_zero_then_counts_from_baseline() -> None:
    state = CounterState()
    assert state.counted == CounterValue(0, 0)

    state, reset = state.observe(CounterValue(12000, 3), START)
    assert (state.counted, reset) == (CounterValue(0, 0), False)
    state, reset = state.observe(CounterValue(12014, 4), START + timedelta(seconds=1))

    assert (state.counted, reset) == (CounterValue(14, 1), False)
    assert state.last_read_at == START + timedelta(seconds=1)


def test_counter_restart_carries_counted_and_counts_from_zero() -> None:
    state, _ = CounterState().observe(CounterValue(12000, 0), START)
    state, _ = state.observe(CounterValue(12014, 0), START)

    state, reset = state.observe(CounterValue(3, 0), START)
    after_restart = state.counted
    state, _ = state.observe(CounterValue(6, 0), START)

    # 재시작 뒤 0부터 다시 쌓였다고 본다: carried 14 + 3 = 17, 이어서 + 3.
    assert reset is True
    assert (state.baseline, state.carried) == (CounterValue(0, 0), CounterValue(14, 0))
    assert after_restart == CounterValue(17, 0)
    assert state.counted == CounterValue(20, 0)


def test_errors_going_down_alone_is_also_a_restart() -> None:
    state, _ = CounterState().observe(CounterValue(10, 5), START)
    state, _ = state.observe(CounterValue(20, 6), START)

    state, reset = state.observe(CounterValue(25, 0), START)

    assert reset is True
    assert state.counted == CounterValue(10 + 25, 1)


def test_step_counting_updates_sides_separately_and_reports_resets() -> None:
    counting = StepCounting.start(20, 1.0, START)
    counting, _ = counting.observe(CounterValue(100, 0), CounterValue(50, 0), START)

    counting, resets = counting.observe(None, CounterValue(10, 0), START + timedelta(seconds=1))
    round_trip = StepCounting.from_json(counting.to_json())

    assert counting.source.counted == CounterValue(0, 0)
    assert counting.source.last_read_at == START
    # 도착은 50 → 10 으로 줄어 재시작으로 봤다: 그때까지 센 0 + 재시작 뒤 10.
    assert counting.target.counted == CounterValue(10, 0)
    assert [(item.side, item.previous, item.current) for item in resets] == [
        (CounterSide.TARGET, CounterValue(50, 0), CounterValue(10, 0))
    ]
    assert round_trip == counting


def _counting(source: int | None, target: int | None, *, read_at: datetime) -> StepCounting:
    counting = StepCounting.start(20, 1.0, START)
    counting, _ = counting.observe(
        None if source is None else CounterValue(0, 0),
        None if target is None else CounterValue(0, 0),
        START,
    )
    counting, _ = counting.observe(
        None if source is None else CounterValue(source, 0),
        None if target is None else CounterValue(target, 0),
        read_at,
    )
    return counting


def _judge(counting: StepCounting, now: datetime, weight: int = 10) -> object:
    verdict = judge_counting(
        counting, now=now, deadline=DEADLINE, target_weight=weight, stale=STALE
    )
    return verdict.failure or verdict.judge_reason


def test_judge_counting_order_errors_then_count_then_deadline() -> None:
    reached = _counting(0, 20, read_at=START + timedelta(seconds=4))
    almost = _counting(0, 19, read_at=START + timedelta(seconds=4))
    errors = StepCounting.start(20, 1.0, START)
    errors, _ = errors.observe(CounterValue(0, 0), CounterValue(0, 0), START)
    errors, _ = errors.observe(CounterValue(0, 0), CounterValue(25, 2), START)

    assert _judge(reached, START + timedelta(seconds=4)) is MigrationJudgeReason.COUNT_REACHED
    assert _judge(almost, START + timedelta(seconds=29)) is None
    # 건수를 채웠어도 5xx 초과가 먼저다.
    assert _judge(errors, START) is MigrationErrorCode.THRESHOLD_EXCEEDED


def test_judge_counting_at_deadline_table() -> None:
    no_target = _counting(100, None, read_at=DEADLINE)
    stale = _counting(0, 5, read_at=DEADLINE - timedelta(seconds=6))
    fresh_enough = _counting(0, 5, read_at=DEADLINE - timedelta(seconds=5))
    not_receiving = _counting(180, 4, read_at=DEADLINE)
    quiet = _counting(180, 5, read_at=DEADLINE)
    no_source = _counting(None, 0, read_at=DEADLINE)

    assert _judge(no_target, DEADLINE) is MigrationErrorCode.METRICS_UNAVAILABLE
    assert _judge(stale, DEADLINE) is MigrationErrorCode.METRICS_UNAVAILABLE
    assert _judge(fresh_enough, DEADLINE) is MigrationJudgeReason.LOW_TRAFFIC
    assert _judge(not_receiving, DEADLINE) is MigrationErrorCode.TARGET_NOT_RECEIVING
    assert _judge(quiet, DEADLINE) is MigrationJudgeReason.LOW_TRAFFIC
    # 출발을 읽지 못했으면 미수신 판정을 건너뛴다.
    assert _judge(no_source, DEADLINE) is MigrationJudgeReason.LOW_TRAFFIC


@pytest.mark.parametrize(
    ("required", "weight", "seconds"), [(20, 10, 4), (50, 50, 2), (50, 100, 1), (1000, 10, 30)]
)
def test_expected_shift_seconds_is_capped_by_max_wait(
    required: int, weight: int, seconds: int
) -> None:
    assert expected_shift_seconds(required, 30, 50, weight) == seconds
