"""simulate 카운터 주입이 실제처럼 흐르는 시계에서도 의도한 결과로 끝나는가(SPEC api-8-4 §1.1).

리허설에서 ERROR_RATE 계획이 SUCCEEDED 로 끝났다. 가짜 카운터가 세기 시작 뒤 첫 읽기부터 5xx 를
허용 + 1 로 줬는데, 첫 읽기는 기준값이 되므로 센 5xx 가 늘 0 이었다. 다른 시험은 시계가 한 실행
안에서 멈춰 있어(비율 적용과 첫 읽기가 같은 시각) 첫 읽기의 5xx 가 0 이라 이 문제를 보지 못했다.
여기서는 now() 를 부를 때마다 1ms 씩 흐르는 시계로 실제 일꾼과 같은 순서를 만든다.
"""

from typing import Any

import httpx
import pytest

from app.executors.entrypoint import (
    EntrypointClient,
    EntrypointMainRouteExecutor,
    EntrypointMetricsProvider,
    EntrypointTrafficExecutor,
)
from tests.integration.migration_support import Migration
from tests.integration.test_migration_entrypoint_counts import (
    BASE_URL,
    TOKEN_REF,
    _CountingEntrypoint,
    _Secrets,
)

TICK_SECONDS = 0.001


def _use_entrypoint(migration: Migration) -> _CountingEntrypoint:
    """트래픽·대표 주소·지표를 http 진입점 실행기(MockTransport)로 바꾼다."""
    harness = migration.harness
    entrypoint = _CountingEntrypoint(harness.clock)
    client = EntrypointClient(
        BASE_URL, TOKEN_REF, _Secrets(), transport=httpx.MockTransport(entrypoint.handle)
    )
    harness.replace_executors(
        traffic=EntrypointTrafficExecutor(client, harness.clock),
        main_route=EntrypointMainRouteExecutor(client, harness.clock),
        metrics=EntrypointMetricsProvider(client),
    )
    return entrypoint


async def _run_with_running_clock(migration: Migration, simulate: dict[str, Any]) -> str:
    plan = await migration.create_plan(simulate=simulate)
    await migration.approve(plan["id"])
    migration.harness.clock.tick_on_read(TICK_SECONDS)
    await migration.harness.run_until_idle()
    return str(plan["id"])


# --- AC-8-4-1: ERROR_RATE → 첫 비율 단계 THRESHOLD_EXCEEDED → 롤백 ---


@pytest.mark.parametrize("executors", ["fake", "entrypoint"])
async def test_simulated_error_rate_with_running_clock_rolls_back_first_shift(
    migration: Migration, executors: str
) -> None:
    entrypoint = _use_entrypoint(migration) if executors == "entrypoint" else None

    plan_id = await _run_with_running_clock(migration, {"failAtStep": 1, "reason": "ERROR_RATE"})
    final = await migration.get_plan(plan_id)
    codes = await migration.log_codes(plan_id)

    assert (final["status"], final["errorCode"], final["rollbackStatus"]) == (
        "ROLLED_BACK",
        "THRESHOLD_EXCEEDED",
        "SUCCEEDED",
    )
    step = final["steps"][1]["result"]
    assert (step["status"], step["failureCode"]) == ("FAILED", "THRESHOLD_EXCEEDED")
    # 필요 20건 · 기준 1% → 허용 1, 기준값 뒤에 만든 5xx = 허용 + 1.
    assert (step["metrics"]["errors5xx"], step["metrics"]["allowedErrors5xx"]) == (2, 1)
    assert [s["result"]["status"] for s in final["steps"]] == ["PASSED", "FAILED"] + ["SKIPPED"] * 3
    assert codes[-3:] == ["JUDGE_FAILED", "ROLLBACK_STARTED", "ROLLBACK_APPLIED"]
    assert await migration.weights() == (100, 0)
    if entrypoint is not None:
        # 비율은 실제로 진입점에 적용됐다가 롤백으로 출발 100 으로 돌아온다.
        assert entrypoint.weights == {"aws": 100, "gcp": 0}
        assert entrypoint.paths.count("PUT /weights") == 2


# --- AC-8-4-2: TARGET_NOT_RECEIVING·LOW_TRAFFIC ---


@pytest.mark.parametrize("step", [1, 2])
async def test_simulated_target_not_receiving_with_running_clock_rolls_back(
    migration: Migration, step: int
) -> None:
    plan_id = await _run_with_running_clock(
        migration, {"failAtStep": step, "reason": "TARGET_NOT_RECEIVING"}
    )
    final = await migration.get_plan(plan_id)

    assert (final["status"], final["errorCode"], final["rollbackStatus"]) == (
        "ROLLED_BACK",
        "TARGET_NOT_RECEIVING",
        "SUCCEEDED",
    )
    metrics = final["steps"][step]["result"]["metrics"]
    assert metrics["requestCount"] == 0
    assert metrics["sourceRequestCount"] > 0
    assert await migration.weights() == (100, 0)


async def test_simulated_low_traffic_with_running_clock_passes_step(
    migration: Migration,
) -> None:
    plan_id = await _run_with_running_clock(migration, {"failAtStep": 1, "reason": "LOW_TRAFFIC"})
    final = await migration.get_plan(plan_id)

    assert (final["status"], final["errorCode"]) == ("SUCCEEDED", None)
    step = final["steps"][1]["result"]
    assert step["status"] == "PASSED"
    assert step["metrics"]["lowTraffic"] is True
    assert step["metrics"]["requestCount"] == 0
    assert await migration.weights() == (0, 100)


# --- AC-8-4-3: /summary 의 p95 null → 단계 결과 p95Ms null, 숫자 → 그 값 ---


class _SummaryEntrypoint(_CountingEntrypoint):
    """카운터 대본에 `GET /summary` 를 더한다. 도착(gcp) p95 는 생성자로 받은 값이다."""

    def __init__(self, migration: Migration, target_p95_ms: float | None) -> None:
        super().__init__(migration.harness.clock)
        self._target_p95_ms = target_p95_ms

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/summary":
            return super().handle(request)
        self.paths.append(f"{request.method} {request.url.path}")
        entry = {"rps": 5.0, "error_ratio": 0.0, "hosts": 1, "healthy_hosts": 1}
        envs = {
            "aws": {**entry, "p95_ms": 90.0},
            "gcp": {**entry, "p95_ms": self._target_p95_ms},
        }
        return httpx.Response(
            200,
            json={
                "ts": self._clock.now().isoformat(),
                "window": request.url.params["window"],
                "envs": envs,
                "prometheus": "ok",
            },
        )


@pytest.mark.parametrize("target_p95_ms", [None, 87.5])
async def test_entrypoint_p95_null_stays_null_and_judgement_is_unchanged(
    migration: Migration, target_p95_ms: float | None
) -> None:
    harness = migration.harness
    entrypoint = _SummaryEntrypoint(migration, target_p95_ms)
    client = EntrypointClient(
        BASE_URL, TOKEN_REF, _Secrets(), transport=httpx.MockTransport(entrypoint.handle)
    )
    harness.replace_executors(
        traffic=EntrypointTrafficExecutor(client, harness.clock),
        main_route=EntrypointMainRouteExecutor(client, harness.clock),
        metrics=EntrypointMetricsProvider(client),
    )
    plan = await migration.create_plan()
    await migration.approve(plan["id"])

    await harness.run_until_idle()
    final = await migration.get_plan(plan["id"])

    # 판정은 p95 와 상관없이 같다(참고 값).
    assert (final["status"], final["errorCode"]) == ("SUCCEEDED", None)
    assert "GET /summary" in entrypoint.paths
    for step in (1, 2, 4):
        assert final["steps"][step]["result"]["metrics"]["p95Ms"] == target_p95_ms
