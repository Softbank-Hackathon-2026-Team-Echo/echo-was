"""이전 계획 비율 단계의 "요청 N건 처리 확인" 규칙(순수 계산, DB·실행기 호출 없음).

비율을 적용한 뒤 일꾼은 출발·도착 환경의 누적 카운터(요청·5xx)를 주기마다 읽어 "센 값"을 쌓는다.

- 센 값 = carried + (last − baseline). 기준값(baseline)이 없으면 0 이다.
- 지금 읽은 값이 last 보다 작으면(요청·5xx 중 하나라도) 진입점이 재시작해 0부터 다시 쌓였다고
  본다: carried += last − baseline, baseline = 0, last = 지금 값. 이미 센 건수는 줄지 않는다.
- 판정은 도착 값만 본다. 5xx 가 허용을 넘으면 그 순간 실패, 필요 건수를 채우면 통과 쪽(헬스체크)으로
  간다. 최대 대기가 끝날 때까지 못 채우면 마감 판정(judge_counting)을 한다.
- 허용 5xx = max(1, floor(필요 건수 × 기준 오류율 / 100)). 부동소수 경계 오류를 피하려고 Decimal 로
  계산하고 필요 건수 기준으로 고정한다(센 요청이 N 을 넘어도 다시 계산하지 않는다).

센 값은 단계 결과 JSON(step_results[i].counting)에 camelCase 키로 저장한다.
"""

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Any

from app.enums import MigrationErrorCode, MigrationJudgeReason

# 도착이 받은 비율이 기대의 이 분의 1 미만이면 "도착 미수신"이다.
NOT_RECEIVING_RATIO = 4

_REQUESTS = "requests"
_ERRORS_5XX = "errors5xx"
_BASELINE = "baseline"
_LAST = "last"
_CARRIED = "carried"
_LAST_READ_AT = "lastReadAt"
_REQUIRED_REQUESTS = "requiredRequests"
_ALLOWED_ERRORS_5XX = "allowedErrors5xx"
_STARTED_AT = "startedAt"
_UPDATED_AT = "updatedAt"
_SOURCE = "source"
_TARGET = "target"


class CounterSide(StrEnum):
    """카운터가 출발 연결 것인지 도착 연결 것인지(COUNTERS_RESET 로그 data.counter)."""

    SOURCE = "source"
    TARGET = "target"


@dataclass(frozen=True)
class CounterValue:
    requests: int
    errors_5xx: int

    def __sub__(self, other: "CounterValue") -> "CounterValue":
        return CounterValue(self.requests - other.requests, self.errors_5xx - other.errors_5xx)

    def __add__(self, other: "CounterValue") -> "CounterValue":
        return CounterValue(self.requests + other.requests, self.errors_5xx + other.errors_5xx)

    def is_below(self, other: "CounterValue") -> bool:
        return self.requests < other.requests or self.errors_5xx < other.errors_5xx

    def to_json(self) -> dict[str, int]:
        return {_REQUESTS: self.requests, _ERRORS_5XX: self.errors_5xx}

    @classmethod
    def from_json(cls, value: dict[str, Any] | None) -> "CounterValue | None":
        if value is None:
            return None
        return cls(int(value[_REQUESTS]), int(value[_ERRORS_5XX]))


ZERO = CounterValue(0, 0)


@dataclass(frozen=True)
class CounterReset:
    """진입점 재시작으로 본 읽기 한 번(COUNTERS_RESET 로그 한 줄)."""

    side: CounterSide
    previous: CounterValue
    current: CounterValue

    def log_data(self) -> dict[str, Any]:
        return {
            "counter": self.side.value,
            "previousRequests": self.previous.requests,
            "previousErrors5xx": self.previous.errors_5xx,
            "requests": self.current.requests,
            "errors5xx": self.current.errors_5xx,
        }


@dataclass(frozen=True)
class CounterState:
    """환경 하나(출발 또는 도착)의 센 값 상태."""

    baseline: CounterValue | None = None
    last: CounterValue | None = None
    carried: CounterValue = ZERO
    last_read_at: datetime | None = None

    @property
    def counted(self) -> CounterValue:
        if self.baseline is None or self.last is None:
            return self.carried
        return self.carried + (self.last - self.baseline)

    def observe(self, reading: CounterValue, at: datetime) -> tuple["CounterState", bool]:
        """성공한 읽기 하나를 반영한다. 두 번째 값은 재시작으로 봤는지다."""
        if self.baseline is None or self.last is None:
            return replace(self, baseline=reading, last=reading, last_read_at=at), False
        if reading.is_below(self.last):
            carried = self.carried + (self.last - self.baseline)
            return CounterState(ZERO, reading, carried, at), True
        return replace(self, last=reading, last_read_at=at), False

    def to_json(self) -> dict[str, Any]:
        return {
            _BASELINE: self.baseline.to_json() if self.baseline else None,
            _LAST: self.last.to_json() if self.last else None,
            _CARRIED: self.carried.to_json(),
            _LAST_READ_AT: self.last_read_at.isoformat() if self.last_read_at else None,
        }

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "CounterState":
        read_at = value.get(_LAST_READ_AT)
        return cls(
            baseline=CounterValue.from_json(value.get(_BASELINE)),
            last=CounterValue.from_json(value.get(_LAST)),
            carried=CounterValue.from_json(value.get(_CARRIED)) or ZERO,
            last_read_at=datetime.fromisoformat(read_at) if read_at else None,
        )


@dataclass(frozen=True)
class StepCounting:
    """비율 단계 하나의 건수 세기 상태(step_results[i].counting)."""

    required_requests: int
    allowed_errors_5xx: int
    started_at: datetime
    updated_at: datetime
    source: CounterState = CounterState()
    target: CounterState = CounterState()

    @classmethod
    def start(
        cls, required_requests: int, max_error_rate_percent: float, at: datetime
    ) -> "StepCounting":
        return cls(
            required_requests=required_requests,
            allowed_errors_5xx=allowed_errors_5xx(required_requests, max_error_rate_percent),
            started_at=at,
            updated_at=at,
        )

    def observe(
        self, source: CounterValue | None, target: CounterValue | None, at: datetime
    ) -> tuple["StepCounting", list[CounterReset]]:
        """한 번 읽은 결과를 반영한다. 읽지 못한 쪽(None)은 그대로 둔다(출발·도착을 따로 갱신)."""
        resets: list[CounterReset] = []
        states: dict[CounterSide, CounterState] = {}
        for side, state, reading in (
            (CounterSide.SOURCE, self.source, source),
            (CounterSide.TARGET, self.target, target),
        ):
            if reading is None:
                states[side] = state
                continue
            updated, is_reset = state.observe(reading, at)
            if is_reset and state.last is not None:
                resets.append(CounterReset(side, state.last, reading))
            states[side] = updated
        return (
            replace(
                self,
                source=states[CounterSide.SOURCE],
                target=states[CounterSide.TARGET],
                updated_at=at,
            ),
            resets,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            _REQUIRED_REQUESTS: self.required_requests,
            _ALLOWED_ERRORS_5XX: self.allowed_errors_5xx,
            _STARTED_AT: self.started_at.isoformat(),
            _UPDATED_AT: self.updated_at.isoformat(),
            _SOURCE: self.source.to_json(),
            _TARGET: self.target.to_json(),
        }

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "StepCounting":
        return cls(
            required_requests=int(value[_REQUIRED_REQUESTS]),
            allowed_errors_5xx=int(value[_ALLOWED_ERRORS_5XX]),
            started_at=datetime.fromisoformat(value[_STARTED_AT]),
            updated_at=datetime.fromisoformat(value[_UPDATED_AT]),
            source=CounterState.from_json(value[_SOURCE]),
            target=CounterState.from_json(value[_TARGET]),
        )


@dataclass(frozen=True)
class CountingVerdict:
    """한 번 읽은 뒤의 결론. 실패 원인이나 판정 이유 중 하나만 있고, 둘 다 없으면 계속 센다."""

    failure: MigrationErrorCode | None = None
    judge_reason: MigrationJudgeReason | None = None

    @property
    def is_waiting(self) -> bool:
        return self.failure is None and self.judge_reason is None


def allowed_errors_5xx(required_requests: int, max_error_rate_percent: float) -> int:
    """허용 5xx = max(1, floor(N × 기준 오류율 / 100)). 0.57% × 10000 = 57 처럼 정확히 계산한다."""
    allowed = Decimal(required_requests) * Decimal(str(max_error_rate_percent)) / Decimal(100)
    return max(1, int(allowed.to_integral_value(rounding=ROUND_FLOOR)))


def error_rate_percent(errors_5xx: int, requests: int) -> float | None:
    """min(100, 5xx ÷ 요청 × 100). 요청이 0이면 None."""
    if requests <= 0:
        return None
    return min(100.0, errors_5xx * 100 / requests)


def is_target_not_receiving(
    source_requests: int, target_requests: int, required_requests: int, target_weight: int
) -> bool:
    """도착에 N건 이상 갔어야 할 만큼 출발에 요청이 왔는데 도착이 기대의 4분의 1도 못 받았는가.

    정수식으로만 비교한다. w = 100 이면 출발 비율이 0인데 출발로 요청이 계속 가는 상황이다.
    """
    if target_weight >= 100:
        return (
            source_requests >= required_requests
            and NOT_RECEIVING_RATIO * target_requests < source_requests
        )
    source_share = 100 - target_weight
    return (
        source_requests * target_weight >= required_requests * source_share
        and NOT_RECEIVING_RATIO * target_requests * source_share < source_requests * target_weight
    )


def judge_counting(
    counting: StepCounting,
    *,
    now: datetime,
    deadline: datetime,
    target_weight: int,
    stale: timedelta,
) -> CountingVerdict:
    """센 값으로 지금 결론을 낸다(5xx 초과 → 건수 채움 → 마감 전 대기 → 마감 판정 순서)."""
    target = counting.target.counted
    if target.errors_5xx > counting.allowed_errors_5xx:
        return CountingVerdict(failure=MigrationErrorCode.THRESHOLD_EXCEEDED)
    if target.requests >= counting.required_requests:
        return CountingVerdict(judge_reason=MigrationJudgeReason.COUNT_REACHED)
    if now < deadline:
        return CountingVerdict()
    last_read_at = counting.target.last_read_at
    if counting.target.baseline is None or last_read_at is None or now - last_read_at > stale:
        return CountingVerdict(failure=MigrationErrorCode.METRICS_UNAVAILABLE)
    if counting.source.baseline is not None and is_target_not_receiving(
        counting.source.counted.requests,
        target.requests,
        counting.required_requests,
        target_weight,
    ):
        return CountingVerdict(failure=MigrationErrorCode.TARGET_NOT_RECEIVING)
    return CountingVerdict(judge_reason=MigrationJudgeReason.LOW_TRAFFIC)


def expected_shift_seconds(
    required_requests: int, max_wait_seconds: int, requests_per_second: int, target_weight: int
) -> int:
    """건수를 채우는 데 걸릴 예상 초 = min(최대 대기, ceil(N ÷ (rps × 비율/100)))."""
    needed = math.ceil(required_requests * 100 / (requests_per_second * target_weight))
    return min(max_wait_seconds, needed)
