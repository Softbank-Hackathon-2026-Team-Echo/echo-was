"""이전 비율 단계의 "요청 N건 처리 확인" 판정(SPEC api-8-1 AC-8-1-3~8, 10~12, 16, 17).

대부분의 시험은 출발·도착 가짜 카운터를 0 에 고정해 두고(freeze) 시험이 값을 직접 넣은 뒤
(set_counts) 시계를 1초씩 움직이며 run_once 한 번 = 카운터 읽기 한 번으로 경계를 본다.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from app.enums import JobStatus, MigrationStepPhase
from app.executors.base import (
    DeployTarget,
    HealthProbeResult,
    Metrics,
    MetricsResult,
)
from app.executors.fake import FakeMetricsProvider
from app.schemas.migration_plan import MigrationProgressResponse
from app.services.migration_log_messages import LOW_TRAFFIC_PASS_SUFFIX
from tests.integration.deploy_harness import ScriptedHealthChecker
from tests.integration.migration_support import Migration, store_legacy_steps


@dataclass
class Counting:
    """세는 중인 계획 하나와 그 출발·도착 연결 id."""

    migration: Migration
    plan_id: str
    source_id: uuid.UUID
    target_id: uuid.UUID

    @property
    def metrics(self) -> FakeMetricsProvider:
        return self.migration.harness.executors.metrics

    async def tick(
        self,
        *,
        source: int | None = None,
        target: int | None = None,
        target_errors: int = 0,
        seconds: float = 1,
    ) -> None:
        """카운터 값을 바꾸고(주면) 시계를 seconds 만큼 움직인 뒤 일꾼을 한 번 돌린다."""
        if source is not None:
            self.metrics.set_counts(self.source_id, source)
        if target is not None:
            self.metrics.set_counts(self.target_id, target, target_errors)
        self.migration.harness.clock.advance(seconds)
        assert await self.migration.harness.worker().run_once()

    async def wait_until(self, seconds_after_apply: int) -> None:
        """적용 시각 + seconds_after_apply 까지 1초씩 진행한다(값은 그대로)."""
        row = await self.migration.plan_row(self.plan_id)
        assert row.phase_started_at is not None
        while self.migration.harness.clock.now() < row.phase_started_at + timedelta(
            seconds=seconds_after_apply
        ):
            await self.tick()

    async def position(self) -> tuple[int, str | None, str]:
        row = await self.migration.plan_row(self.plan_id)
        return row.current_step, row.step_phase, row.status

    async def plan(self) -> dict[str, Any]:
        return await self.migration.get_plan(self.plan_id)


async def _start_counting(migration: Migration, **body: Any) -> Counting:
    """계획을 만들고 승인한 뒤 두 카운터를 0 에 고정하고 단계 1 세기(기준값 0)까지 진행한다."""
    plan = await migration.create_plan(**body)
    await migration.approve(plan["id"])
    counting = Counting(
        migration=migration,
        plan_id=plan["id"],
        source_id=uuid.UUID(plan["sourceServiceEnvironmentId"]),
        target_id=uuid.UUID(plan["targetServiceEnvironmentId"]),
    )
    counting.metrics.freeze(counting.source_id)
    counting.metrics.freeze(counting.target_id)
    assert await migration.harness.worker().run_once()
    assert await counting.position() == (1, "OBSERVE", "RUNNING")
    return counting


# --- AC-8-1-3·17: 5xx 허용 ---


async def test_errors_within_allowance_keep_counting_and_one_more_rolls_back(
    migration: Migration,
) -> None:
    counting = await _start_counting(migration)

    await counting.tick(target=1, target_errors=1)
    with_one_error = await counting.position()
    progress = (await counting.plan())["progress"]
    # 건수를 채우기 전이라도 허용(1)을 넘는 순간 실패한다. 요청 1건에 5xx 2건이면 오류율은 100%.
    await counting.tick(target=1, target_errors=2)
    final = await counting.plan()
    rows = await migration.log_rows(counting.plan_id)

    assert with_one_error == (1, "OBSERVE", "RUNNING")
    assert (progress["errors5xx"], progress["allowedErrors5xx"]) == (1, 1)
    assert (final["status"], final["errorCode"]) == ("ROLLED_BACK", "THRESHOLD_EXCEEDED")
    assert await migration.weights() == (100, 0)
    metrics = final["steps"][1]["result"]["metrics"]
    assert (metrics["errors5xx"], metrics["allowedErrors5xx"]) == (2, 1)
    assert metrics["errorRatePercent"] == 100.0
    failed = next(row for row in rows if row.code == "JUDGE_FAILED")
    assert failed.data is not None and failed.data["errors5xx"] == 2


async def test_allowed_errors_follow_required_requests_and_error_rate(
    migration: Migration,
) -> None:
    counting = await _start_counting(
        migration,
        requiredRequests=[10000, 50, 50],
        rollbackConditions={"maxErrorRatePercent": 0.57},
    )

    progress = (await counting.plan())["progress"]

    # 0.57% × 10000 = 57 (부동소수로 곱하면 56).
    assert (progress["requiredRequests"], progress["allowedErrors5xx"]) == (10000, 57)


# --- AC-8-1-4: 트래픽 적음 ---


async def test_quiet_service_passes_at_max_wait_as_low_traffic(migration: Migration) -> None:
    counting = await _start_counting(migration)

    await counting.wait_until(29)
    at_29 = await counting.position()
    await counting.tick()
    at_30 = await counting.position()
    final_step = (await counting.plan())["steps"][1]["result"]
    rows = await migration.log_rows(counting.plan_id)
    passed = next(row for row in rows if row.code == "JUDGE_PASSED")

    assert at_29 == (1, "OBSERVE", "RUNNING")
    assert at_30 == (2, "OBSERVE", "RUNNING")
    assert final_step["status"] == "PASSED"
    assert (final_step["metrics"]["lowTraffic"], final_step["metrics"]["requestCount"]) == (
        True,
        0,
    )
    assert final_step["metrics"]["healthStatus"] == "OK"
    assert final_step["metrics"]["errorRatePercent"] is None
    assert passed.data is not None and passed.data["lowTraffic"] is True
    assert passed.message.endswith(LOW_TRAFFIC_PASS_SUFFIX)
    assert await migration.weights() == (90, 10)


# --- AC-8-1-5: 도착 미수신 ---


@pytest.mark.parametrize(
    ("source", "target", "status", "error_code"),
    [
        (190, 2, "ROLLED_BACK", "TARGET_NOT_RECEIVING"),
        (190, 3, "RUNNING", None),
        (189, 0, "RUNNING", None),
    ],
)
async def test_target_not_receiving_is_judged_at_max_wait_with_integer_formula(
    migration: Migration, source: int, target: int, status: str, error_code: str | None
) -> None:
    counting = await _start_counting(migration)

    await counting.tick(source=source, target=target)
    await counting.wait_until(30)
    final = await counting.plan()

    assert (final["status"], final["errorCode"]) == (status, error_code)
    result = final["steps"][1]["result"]
    assert (result["metrics"]["sourceRequestCount"], result["metrics"]["requestCount"]) == (
        source,
        target,
    )
    if error_code is None:
        assert result["metrics"]["lowTraffic"] is True
        assert final["currentStep"] == 2
    else:
        assert result["failureCode"] == error_code
        assert final["errorMessage"].startswith("The target environment did not receive")
        assert await migration.weights() == (100, 0)


# --- AC-8-1-6: 카운터 재시작 ---


async def test_counter_restart_keeps_counted_requests_and_continues(migration: Migration) -> None:
    migration.harness.use_settings(migration_min_observe_seconds=0)
    counting = await _start_counting(migration, requiredRequests=[20] * 6)

    await counting.tick(target=14)
    await counting.tick(target=3)
    after_restart = (await counting.plan())["progress"]
    await counting.tick(target=6)
    rows = await migration.log_rows(counting.plan_id)
    resets = [row for row in rows if row.code == "COUNTERS_RESET"]

    assert after_restart["requests"] == 17
    assert len(resets) == 1
    assert resets[0].step == 1
    assert resets[0].data == {
        "counter": "target",
        "previousRequests": 14,
        "previousErrors5xx": 0,
        "requests": 3,
        "errors5xx": 0,
    }
    assert await counting.position() == (2, "OBSERVE", "RUNNING")
    step = (await counting.plan())["steps"][1]["result"]
    assert (step["status"], step["metrics"]["requestCount"]) == ("PASSED", 20)


# --- AC-8-1-7: 카운터 읽기 실패 ---


async def test_target_counts_unavailable_all_step_rolls_back_as_metrics_unavailable(
    migration: Migration,
) -> None:
    migration.harness.executors.metrics.fail_counts()
    plan = await migration.create_plan()
    await migration.approve(plan["id"])

    await migration.harness.run_until_idle()
    final = await migration.get_plan(plan["id"])
    rows = await migration.log_rows(plan["id"])
    failed = next(row for row in rows if row.code == "JUDGE_FAILED")
    applied = next(row for row in rows if row.code == "WEIGHT_APPLIED")

    assert (final["status"], final["errorCode"]) == ("ROLLED_BACK", "METRICS_UNAVAILABLE")
    assert final["steps"][1]["result"]["failureCode"] == "METRICS_UNAVAILABLE"
    assert failed.logged_at - applied.logged_at == timedelta(seconds=30)
    assert await migration.weights() == (100, 0)


async def test_occasional_count_failures_keep_counting_and_pass(migration: Migration) -> None:
    harness = migration.harness
    plan = await migration.create_plan()
    await migration.approve(plan["id"])
    target_id = uuid.UUID(plan["targetServiceEnvironmentId"])
    worker = harness.worker()
    assert await worker.run_once()

    for second in range(1, 5):
        # 2·3초째 읽기만 실패한다(진입점은 계속 센다).
        harness.executors.metrics.fail_counts(target_id, is_failing=second in (2, 3))
        harness.clock.advance(1)
        assert await worker.run_once()
    final = await migration.get_plan(plan["id"])

    assert final["steps"][1]["result"]["status"] == "PASSED"
    assert final["steps"][1]["result"]["metrics"]["requestCount"] == 10
    assert final["steps"][1]["result"]["metrics"]["lowTraffic"] is False


@pytest.mark.parametrize(
    ("fail_from", "status", "error_code"),
    [(24, "ROLLED_BACK", "METRICS_UNAVAILABLE"), (26, "RUNNING", None)],
)
async def test_stale_target_counts_at_max_wait_cannot_be_judged(
    migration: Migration, fail_from: int, status: str, error_code: str | None
) -> None:
    counting = await _start_counting(migration)

    await counting.tick(target=5)
    await counting.wait_until(fail_from - 1)
    counting.metrics.fail_counts(counting.target_id)
    await counting.wait_until(30)
    final = await counting.plan()

    # 24초부터 실패: 마지막 성공 23초 → 7초 전 > 5 → 판단 불가.
    # 26초부터 실패: 마지막 성공 25초 → 5초 전(넘지 않음) → 트래픽 적음 통과.
    assert (final["status"], final["errorCode"]) == (status, error_code)


async def test_source_counts_unavailable_skips_not_receiving_check(migration: Migration) -> None:
    plan = await migration.create_plan()
    await migration.approve(plan["id"])
    metrics = migration.harness.executors.metrics
    counting = Counting(
        migration=migration,
        plan_id=plan["id"],
        source_id=uuid.UUID(plan["sourceServiceEnvironmentId"]),
        target_id=uuid.UUID(plan["targetServiceEnvironmentId"]),
    )
    # 출발은 단계 내내 읽지 못하고(기준값 없음), 도착은 0건이다.
    metrics.fail_counts(counting.source_id)
    metrics.freeze(counting.target_id)
    assert await migration.harness.worker().run_once()

    await counting.wait_until(29)
    stored = (await migration.plan_row(plan["id"])).step_results[1]["counting"]
    await counting.tick()
    step = (await counting.plan())["steps"][1]["result"]

    assert stored["source"]["baseline"] is None
    assert step["status"] == "PASSED"
    assert (step["metrics"]["lowTraffic"], step["metrics"]["sourceRequestCount"]) == (True, 0)


async def test_source_keeps_updating_while_target_reads_fail(migration: Migration) -> None:
    counting = await _start_counting(migration)
    await counting.tick(source=10, target=3)
    counting.metrics.fail_counts(counting.target_id)

    await counting.tick(source=40, target=15)
    progress = (await counting.plan())["progress"]

    assert (progress["sourceRequests"], progress["requests"]) == (40, 3)


# --- AC-8-1-8: p95 참고 ---


class _SlowP95Metrics(FakeMetricsProvider):
    """카운터는 가짜 그대로, p95 만 5000ms 로 답한다."""

    async def read(self, target: DeployTarget, since: datetime, until: datetime) -> MetricsResult:
        await super().read(target, since, until)
        return Metrics(error_rate_percent=0.1, p95_ms=5000.0, request_count=10)


@pytest.mark.parametrize(("slow", "p95"), [(True, 5000.0), (False, None)])
async def test_p95_is_recorded_for_reference_and_never_fails_a_step(
    migration: Migration, slow: bool, p95: float | None
) -> None:
    harness = migration.harness
    if slow:
        harness.replace_executors(
            metrics=_SlowP95Metrics(harness.clock, harness.executors.traffic.weight_of)
        )
    else:
        harness.executors.metrics.unavailable = True
    plan = await migration.create_plan()
    await migration.approve(plan["id"])

    await harness.run_until_idle()
    final = await migration.get_plan(plan["id"])
    windows = [until - since for _, since, until in harness.executors.metrics.calls]

    assert final["status"] == "SUCCEEDED"
    assert [
        final["steps"][index]["result"]["metrics"]["p95Ms"] for index in (1, 2, 3, 4, 6, 7)
    ] == [p95] * 6
    assert final["rollbackConditions"]["p95Advisory"] is True
    assert windows == [timedelta(seconds=30)] * 6


# --- AC-8-1-10: 진행 응답 ---


async def test_progress_is_shown_only_while_counting_a_shift_step(migration: Migration) -> None:
    harness = migration.harness
    plan = await migration.create_plan()
    pending = await migration.get_plan(plan["id"])
    await migration.approve(plan["id"])
    worker = harness.worker()
    assert await worker.run_once()
    harness.clock.advance(4)
    assert await worker.run_once()
    first = await migration.get_plan(plan["id"])
    harness.clock.advance(1)
    assert await worker.run_once()
    second = await migration.get_plan(plan["id"])
    row = await migration.plan_row(plan["id"])
    listed = await harness.client.get(migration.plans_path)
    # 데이터 확인 단계(3)에서는 진행 숫자가 없다(같은 행을 단계 3 위치로 바꿔 본다).
    row.current_step, row.step_phase = 5, MigrationStepPhase.JUDGE
    on_data_step = MigrationProgressResponse.from_plan(row)
    await harness.run_until_idle()
    finished = await migration.get_plan(plan["id"])

    assert pending["progress"] is None
    progress = first["progress"]
    assert (progress["step"], progress["targetWeight"]) == (2, 10)
    assert (progress["requiredRequests"], progress["allowedErrors5xx"]) == (10, 1)
    assert (progress["requests"], progress["errors5xx"]) == (0, 0)
    started = datetime.fromisoformat(progress["startedAt"])
    assert datetime.fromisoformat(progress["deadlineAt"]) == started + timedelta(seconds=30)
    assert second["progress"]["requests"] == 5
    assert second["progress"]["sourceRequests"] == 45
    assert second["progress"]["updatedAt"] != progress["updatedAt"]
    assert on_data_step is None
    assert listed.status_code == 200
    assert "progress" not in listed.json()["data"]["items"][0]
    assert finished["progress"] is None


# --- AC-11(8-1): 세는 중·판정 직후 일꾼 재시작 ---


async def test_counting_resumes_from_stored_counts_after_lease_expires(
    migration: Migration,
) -> None:
    harness = migration.harness
    harness.use_settings(worker_lease_seconds=3)
    counting = await _start_counting(migration, requiredRequests=[20] * 6)
    await counting.tick(target=10)

    # 다른 일꾼이 집기만 하고 죽는다. 임대가 끝난 뒤 새 일꾼이 저장된 센 값(10)에서 이어 센다.
    harness.clock.advance(1)
    assert await harness.worker("dying-worker").claim_next_job() is not None
    harness.clock.advance(4)
    counting.metrics.set_counts(counting.target_id, 20)
    assert await harness.worker().run_once()
    counting.metrics.freeze(counting.target_id, is_frozen=False)
    counting.metrics.freeze(counting.source_id, is_frozen=False)
    await harness.run_until_idle()
    final = await counting.plan()
    codes = await migration.log_codes(counting.plan_id)
    job = await migration.plan_job(counting.plan_id)

    assert final["status"] == "SUCCEEDED"
    assert final["steps"][1]["result"]["metrics"]["requestCount"] == 20
    assert codes.count("JUDGE_PASSED") == 6
    assert "COUNTERS_RESET" not in codes
    assert job is not None and (job.status, job.lease_losses) == (JobStatus.DONE, 1)


class _CrashOnceHealth(ScriptedHealthChecker):
    """armed 이면 다음 probe 한 번에서 일꾼이 죽은 것처럼 예외를 낸다."""

    armed = False

    async def probe(self, url: str, path: str, timeout_seconds: float) -> HealthProbeResult:
        if self.armed:
            self.armed = False
            raise RuntimeError("worker process died")
        return await super().probe(url, path, timeout_seconds)


async def test_restarted_worker_passes_judge_with_same_stored_reason(
    migration: Migration,
) -> None:
    harness = migration.harness
    health = _CrashOnceHealth()
    harness.replace_executors(health=health)
    counting = await _start_counting(migration)
    await counting.wait_until(29)

    health.armed = True
    harness.clock.advance(1)
    with pytest.raises(RuntimeError, match="worker process died"):
        await harness.worker().run_once()
    stuck = await migration.plan_row(counting.plan_id)
    harness.clock.advance(harness.settings.worker_lease_seconds + 1)
    assert await harness.worker().run_once()
    final = await counting.plan()
    passed = [row for row in await migration.log_rows(counting.plan_id) if row.step == 1]

    assert (stuck.step_phase, stuck.step_results[1]["judgeReason"]) == ("JUDGE", "LOW_TRAFFIC")
    # 다시 시작한 일꾼은 카운터를 다시 읽지 않고 저장된 이유(트래픽 적음)로 통과한다.
    assert final["steps"][1]["result"]["metrics"]["lowTraffic"] is True
    assert [row.code for row in passed] == ["WEIGHT_APPLIED", "JUDGE_PASSED"]
    assert final["currentStep"] == 2


# --- AC-8-1-12: simulate ---


async def test_simulated_error_rate_fails_on_first_read(migration: Migration) -> None:
    plan = await migration.create_plan(simulate={"failAtStep": 1, "reason": "ERROR_RATE"})
    await migration.approve(plan["id"])

    await migration.harness.run_until_idle()
    final = await migration.get_plan(plan["id"])
    rows = await migration.log_rows(plan["id"])
    applied = next(row for row in rows if row.code == "WEIGHT_APPLIED")
    failed = next(row for row in rows if row.code == "JUDGE_FAILED")

    assert (final["status"], final["errorCode"]) == ("ROLLED_BACK", "THRESHOLD_EXCEEDED")
    assert failed.logged_at - applied.logged_at == timedelta(seconds=1)
    assert final["steps"][1]["result"]["metrics"]["errors5xx"] == 2
    # 처리기가 값을 만들었으므로 카운터 실행기를 부르지 않는다.
    assert migration.harness.executors.metrics.count_calls == []


async def test_simulated_low_traffic_on_last_step_passes_and_succeeds(
    migration: Migration,
) -> None:
    plan = await migration.create_plan(simulate={"failAtStep": 7, "reason": "LOW_TRAFFIC"})
    await migration.approve(plan["id"])

    await migration.harness.run_until_idle()
    final = await migration.get_plan(plan["id"])

    assert final["status"] == "SUCCEEDED"
    assert final["steps"][7]["result"]["metrics"]["lowTraffic"] is True
    assert final["steps"][1]["result"]["metrics"]["lowTraffic"] is False


async def test_stored_legacy_latency_simulation_runs_without_failure(
    migration: Migration,
) -> None:
    plan = await migration.create_plan()
    async with migration.harness.session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE migration_plans SET simulate = "
                '\'{"failAtStep": 1, "reason": "LATENCY"}\'::jsonb WHERE id = :id'
            ),
            {"id": plan["id"]},
        )
    await migration.approve(plan["id"])

    await migration.harness.run_until_idle()

    assert (await migration.get_plan(plan["id"]))["status"] == "SUCCEEDED"


# --- AC-8-1-16: 8-1 전에 만든 계획 ---


@pytest.mark.parametrize("phase", ["OBSERVE", "JUDGE"])
async def test_legacy_plan_without_counts_restarts_counting_in_place(
    migration: Migration, phase: str
) -> None:
    harness = migration.harness
    plan = await migration.create_plan()
    stored_steps = await store_legacy_steps(migration, plan["id"], before_counting=True)
    await migration.approve(plan["id"])
    assert await harness.worker().run_once()
    # 옛 계획처럼 만든다: 센 값·필요 건수·최대 대기 키 없음, 지켜보기 5초.
    async with harness.session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE migration_plans SET observe_seconds = 5, step_phase = :phase, "
                "step_results = jsonb_set(step_results, '{1}', "
                "(step_results->1) - 'counting' - 'judgeReason'), "
                "steps = jsonb_set(jsonb_set(jsonb_set(steps, '{1}', "
                "(steps->1) - 'requiredRequests' - 'maxWaitSeconds'), '{2}', "
                "(steps->2) - 'requiredRequests' - 'maxWaitSeconds'), '{4}', "
                "(steps->4) - 'requiredRequests' - 'maxWaitSeconds') "
                "WHERE id = :id"
            ),
            {"id": plan["id"], "phase": phase},
        )
    harness.clock.advance(1)
    restarted_at = harness.clock.now()
    assert await harness.worker().run_once()
    restarted = await migration.plan_row(plan["id"])
    await harness.run_until_idle()
    final = await migration.get_plan(plan["id"])

    assert [step.get("observeSeconds") for step in stored_steps] == [None, 30, 30, None, 30]
    assert (restarted.step_phase, restarted.observe_until) == (
        "OBSERVE",
        restarted_at + timedelta(seconds=5),
    )
    # 필요 건수는 설정 기본값(20)이다.
    assert restarted.step_results[1]["counting"]["requiredRequests"] == 20
    assert final["status"] == "SUCCEEDED"
    assert final["maxWaitSeconds"] == 5
    assert [step["requiredRequests"] for step in final["steps"]] == [None] * 5
    assert final["steps"][1]["maxWaitSeconds"] == 5
    assert all("observeSeconds" not in step for step in final["steps"])
