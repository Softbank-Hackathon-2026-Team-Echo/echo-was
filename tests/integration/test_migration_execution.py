"""이전 계획 실행: 판정 실패 롤백·중단·사전 확인 실패·롤백 실패·일꾼 재시작.

SPEC api-8 AC-7~AC-12.
"""

import dataclasses
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select, text

from app.enums import JobKind, JobStatus, MigrationErrorCode, MigrationLogCode
from app.executors.base import HealthHttpError, MainRoute, TrafficResult, WeightedTarget
from app.executors.fake import FakeTrafficExecutor
from app.models import Job, MigrationPlan, MigrationPlanLog
from app.services.migration_log_messages import MIGRATION_ERROR_MESSAGES
from app.services.migration_plan_log import MAX_MIGRATION_LOG_LINES, MigrationPlanLogWriter
from app.worker.runner import UnknownJobKindError
from tests.integration.deploy_harness import ScriptedHealthChecker
from tests.integration.migration_support import Migration
from tests.integration.test_migration_plans import SUCCESS_LOG_CODES


def _error(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["code"]


async def _approved(migration: Migration, **body: Any) -> dict[str, Any]:
    plan = await migration.create_plan(**body)
    await migration.approve(plan["id"])
    return plan


def _step_statuses(plan: dict[str, Any]) -> list[str]:
    return [step["result"]["status"] for step in plan["steps"]]


async def _job(migration: Migration, plan_id: str) -> Job:
    job = await migration.plan_job(plan_id)
    assert job is not None
    return job


async def _lose_lease(migration: Migration, times: int = 3) -> None:
    """할 일을 집기만 하고 결과를 적지 않은 채 임대를 times 번 만료시킨다(일꾼이 죽은 것처럼)."""
    harness = migration.harness
    worker = harness.worker("dying-worker")
    for _ in range(times):
        assert await worker.claim_next_job() is not None
        harness.clock.advance(harness.settings.worker_lease_seconds + 1)


# --- AC-7·AC-12: 판정 실패 → 자동 롤백, 원인 코드 구분 ---


@pytest.mark.parametrize(
    ("step", "reason", "error_code", "failure_line", "metric", "value"),
    [
        # 필요 50건 · 기준 1% → 허용 1, 만든 5xx = 허용 + 1.
        (2, "ERROR_RATE", "THRESHOLD_EXCEEDED", "JUDGE_FAILED", "errors5xx", 2),
        (4, "HEALTH", "HEALTH_CHECK_FAILED", "JUDGE_FAILED", "healthStatus", "HTTP_ERROR"),
        (3, "DATA_LAG", "DATA_LAG_EXCEEDED", "DATA_SYNC_FAILED", "replicationLagSeconds", 30.0),
        (1, "TARGET_NOT_RECEIVING", "TARGET_NOT_RECEIVING", "JUDGE_FAILED", "requestCount", 0),
        (2, "TARGET_NOT_RECEIVING", "TARGET_NOT_RECEIVING", "JUDGE_FAILED", "requestCount", 0),
    ],
)
async def test_simulated_judge_failure_rolls_back_to_source(
    migration: Migration,
    step: int,
    reason: str,
    error_code: str,
    failure_line: str,
    metric: str,
    value: object,
) -> None:
    plan = await _approved(migration, simulate={"failAtStep": step, "reason": reason})

    await migration.harness.run_until_idle()
    final = await migration.get_plan(plan["id"])
    codes = await migration.log_codes(plan["id"])

    assert (final["status"], final["errorCode"], final["rollbackStatus"]) == (
        "ROLLED_BACK",
        error_code,
        "SUCCEEDED",
    )
    assert final["errorMessage"] == MIGRATION_ERROR_MESSAGES[MigrationErrorCode(error_code)]
    assert await migration.weights() == (100, 0)
    result = final["steps"][step]["result"]
    assert (result["status"], result["failureCode"]) == ("FAILED", error_code)
    assert result["metrics"][metric] == value
    assert _step_statuses(final) == ["PASSED"] * step + ["FAILED"] + ["SKIPPED"] * (4 - step)
    assert codes[-3:] == [failure_line, "ROLLBACK_STARTED", "ROLLBACK_APPLIED"]
    assert migration.traffic_keys()[-1] == f"{plan['id']}:rollback:0"


async def test_traffic_apply_failure_rolls_back_without_metrics(migration: Migration) -> None:
    plan = await _approved(migration, simulate={"failAtStep": 2, "reason": "TRAFFIC"})

    await migration.harness.run_until_idle()
    final = await migration.get_plan(plan["id"])
    rows = await migration.log_rows(plan["id"])

    assert (final["status"], final["errorCode"], final["rollbackStatus"]) == (
        "ROLLED_BACK",
        "TRAFFIC_APPLY_FAILED",
        "SUCCEEDED",
    )
    assert final["steps"][2]["result"] | {"startedAt": None, "finishedAt": None} == {
        "status": "FAILED",
        "startedAt": None,
        "finishedAt": None,
        "metrics": None,
        "failureCode": "TRAFFIC_APPLY_FAILED",
    }
    assert [row.code for row in rows][-3:] == [
        "WEIGHT_APPLY_FAILED",
        "ROLLBACK_STARTED",
        "ROLLBACK_APPLIED",
    ]
    failed_line = rows[-3]
    assert (failed_line.step, failed_line.data) == (2, {"sourceWeight": 50, "targetWeight": 50})
    # 처리기가 실패를 만들었으므로 단계 2 의 비율 호출은 없다.
    assert migration.traffic_keys() == [f"{plan['id']}:1", f"{plan['id']}:rollback:0"]
    assert await migration.weights() == (100, 0)


@pytest.mark.parametrize("unavailable", ["counts", "data_sync"])
async def test_unavailable_counts_or_replication_lag_rolls_back(
    migration: Migration, unavailable: str
) -> None:
    executors = migration.harness.executors
    if unavailable == "counts":
        # 카운터를 단계 내내 읽지 못하면 최대 대기 끝에 판단 불가로 롤백한다.
        executors.metrics.fail_counts()
    else:
        executors.data_sync.unavailable = True
    plan = await _approved(migration)

    await migration.harness.run_until_idle()
    final = await migration.get_plan(plan["id"])

    failed_step = 1 if unavailable == "counts" else 3
    assert (final["status"], final["errorCode"]) == ("ROLLED_BACK", "METRICS_UNAVAILABLE")
    assert final["steps"][failed_step]["result"]["failureCode"] == "METRICS_UNAVAILABLE"
    assert await migration.weights() == (100, 0)


def test_error_codes_have_distinct_fixed_messages() -> None:
    messages = [MIGRATION_ERROR_MESSAGES[code] for code in MigrationErrorCode]

    assert len(set(messages)) == len(MigrationErrorCode)


# --- AC-8: 중단 ---


async def test_abort_during_counting_rolls_back_on_next_run(migration: Migration) -> None:
    harness = migration.harness
    plan = await _approved(migration)
    assert await harness.worker().run_once()
    observing = await migration.weights()
    counting = await migration.get_plan(plan["id"])

    first = await migration.act(plan["id"], "abort")
    job = await _job(migration, plan["id"])
    second = await migration.act(plan["id"], "abort")
    # 시계를 움직이지 않은 다음 실행(카운터 읽기 주기 1초 안)에서 바로 롤백한다.
    assert await harness.worker().run_once()
    rolled_back_at_once = (await migration.plan_row(plan["id"])).status
    await harness.run_until_idle()
    final = await migration.get_plan(plan["id"])
    codes = await migration.log_codes(plan["id"])

    assert observing == (90, 10)
    assert counting["progress"]["step"] == 1
    assert rolled_back_at_once == "ROLLED_BACK"
    assert (first.status_code, second.status_code) == (202, 202)
    assert first.json()["data"]["abortRequestedAt"] is not None
    assert job.run_after == harness.clock.now()
    assert (final["status"], final["errorCode"], final["rollbackStatus"]) == (
        "ROLLED_BACK",
        "ABORTED",
        "SUCCEEDED",
    )
    assert final["abortedBy"]["login"] == "dev-b"
    assert codes.count("ABORT_REQUESTED") == 1
    assert codes[-2:] == ["ROLLBACK_STARTED", "ROLLBACK_APPLIED"]
    assert final["steps"][1]["result"]["failureCode"] == "ABORTED"
    assert _step_statuses(final) == ["PASSED", "FAILED", "SKIPPED", "SKIPPED", "SKIPPED"]
    assert await migration.weights() == (100, 0)


async def test_abort_before_start_cancels_without_traffic(migration: Migration) -> None:
    plan = await _approved(migration)

    aborted = await migration.act(plan["id"], "abort")
    await migration.harness.run_until_idle()
    final = await migration.get_plan(plan["id"])

    assert aborted.status_code == 202
    assert final["status"] == "CANCELLED"
    assert final["cancelledBy"]["login"] == "dev-b"
    assert migration.traffic_keys() == []
    assert await migration.log_codes(plan["id"]) == [
        "PLAN_CREATED",
        "APPROVED",
        "ABORT_REQUESTED",
        "CANCELLED",
    ]


async def test_abort_pending_or_finished_plan_is_not_running(migration: Migration) -> None:
    pending = await migration.create_plan()
    on_pending = await migration.act(pending["id"], "abort")
    await migration.act(pending["id"], "cancel", client=migration.author_client)
    finished = await _approved(migration)
    await migration.harness.run_until_idle()
    on_finished = await migration.act(finished["id"], "abort")

    assert _error(on_pending) == (409, "PLAN_NOT_RUNNING")
    assert (await migration.get_plan(finished["id"]))["status"] == "SUCCEEDED"
    assert _error(on_finished) == (409, "PLAN_NOT_RUNNING")


async def test_abort_without_open_job_recovers(migration: Migration) -> None:
    harness = migration.harness
    approved = await _approved(migration)
    await _close_jobs(migration)
    cancelled = await migration.act(approved["id"], "abort")

    running = await _approved(migration)
    assert await harness.worker().run_once()
    await _close_jobs(migration)
    aborted = await migration.act(running["id"], "abort")
    async with harness.session_factory() as session:
        reopened = await session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.subject_id == uuid.UUID(running["id"]), Job.status == JobStatus.QUEUED)
        )
    await harness.run_until_idle()

    # 할 일이 없는(비정상) APPROVED 는 그 자리에서 취소, RUNNING 은 할 일을 다시 넣는다.
    assert cancelled.json()["data"]["status"] == "CANCELLED"
    assert aborted.status_code == 202
    assert reopened == 1
    assert (await migration.get_plan(running["id"]))["status"] == "ROLLED_BACK"


async def _close_jobs(migration: Migration) -> None:
    async with migration.harness.session_factory() as session, session.begin():
        await session.execute(text("UPDATE jobs SET status = 'DONE' WHERE kind = 'MIGRATION_PLAN'"))


# --- AC-9: 사전 확인 실패 ---


async def test_preflight_health_failure_fails_without_traffic_change(
    migration: Migration,
) -> None:
    harness = migration.harness
    harness.replace_executors(health=ScriptedHealthChecker(default=HealthHttpError(503)))
    plan = await _approved(migration)

    await harness.run_until_idle()
    final = await migration.get_plan(plan["id"])

    assert (final["status"], final["errorCode"], final["rollbackStatus"]) == (
        "FAILED",
        "PREFLIGHT_FAILED",
        "NOT_NEEDED",
    )
    assert final["steps"][0]["result"]["metrics"]["healthStatus"] == "HTTP_ERROR"
    assert _step_statuses(final) == ["FAILED"] + ["SKIPPED"] * 4
    assert migration.traffic_keys() == []
    assert await migration.weights() == (100, 0)
    assert (await migration.log_codes(plan["id"]))[-1] == "PREFLIGHT_FAILED"
    assert harness.executors.health.calls[-1].startswith(
        f"https://shop-api-gcp-seoul-m1g2r3t4.{harness.settings.public_base_domain}/"
    )


# --- AC-10: 롤백 실패 ---


async def test_rollback_that_never_applies_fails_and_keeps_last_weights(
    migration: Migration,
) -> None:
    harness = migration.harness
    harness.executors.traffic.fail_when(lambda key: ":rollback:" in key)
    plan = await _approved(migration, simulate={"failAtStep": 2, "reason": "ERROR_RATE"})

    await harness.run_until_idle()
    final = await migration.get_plan(plan["id"])
    codes = await migration.log_codes(plan["id"])
    row = await migration.plan_row(plan["id"])

    assert (final["status"], final["errorCode"], final["rollbackStatus"]) == (
        "FAILED",
        "THRESHOLD_EXCEEDED",
        "FAILED",
    )
    assert await migration.weights() == (50, 50)
    assert row.rollback_attempts == harness.settings.migration_rollback_max_attempts == 3
    assert [key for key in migration.traffic_keys() if ":rollback:" in key] == [
        f"{plan['id']}:rollback:{attempt}" for attempt in range(3)
    ]
    assert codes[-4:] == [
        "ROLLBACK_STARTED",
        "ROLLBACK_APPLY_FAILED",
        "ROLLBACK_APPLY_FAILED",
        "ROLLBACK_FAILED",
    ]


# --- AC-11: 일꾼 재시작·임대 상실·디스패치 ---


class _CrashAfterCall:
    """crash_key 로 처음 부를 때 실행기 호출은 끝내고 결과를 돌려주기 전에 일꾼이 죽는다."""

    def __init__(self, inner: FakeTrafficExecutor, crash_key: str) -> None:
        self.inner = inner
        self.crash_key = crash_key
        self.crashed = False

    @property
    def simulated(self) -> bool:
        return True

    @property
    def calls(self) -> list[Any]:
        return self.inner.calls

    async def set_weights(
        self, route: MainRoute, weights: tuple[WeightedTarget, ...], idempotency_key: str
    ) -> TrafficResult:
        result = await self.inner.set_weights(route, weights, idempotency_key)
        if idempotency_key == self.crash_key and not self.crashed:
            self.crashed = True
            raise RuntimeError("worker process died")
        return result


async def test_restarted_worker_resumes_with_same_key_without_duplicate_lines(
    migration: Migration,
) -> None:
    harness = migration.harness
    plan = await _approved(migration)
    inner = harness.executors.traffic
    harness.replace_executors(traffic=_CrashAfterCall(inner, f"{plan['id']}:2"))

    with pytest.raises(RuntimeError, match="worker process died"):
        await harness.run_until_idle()
    stuck = await migration.plan_row(plan["id"])
    stuck_weights = await migration.weights()
    harness.clock.advance(harness.settings.worker_lease_seconds + 1)
    await harness.run_until_idle()
    final = await migration.get_plan(plan["id"])
    job = await _job(migration, plan["id"])

    assert (stuck.current_step, stuck.step_phase) == (2, "APPLY")
    assert stuck.traffic_requested_at is not None
    assert stuck_weights == (90, 10)
    assert final["status"] == "SUCCEEDED"
    assert [key for key, _ in inner.applied].count(f"{plan['id']}:2") == 1
    assert migration.traffic_keys().count(f"{plan['id']}:2") == 2
    assert await migration.log_codes(plan["id"]) == SUCCESS_LOG_CODES
    assert (job.status, job.lease_losses) == (JobStatus.DONE, 1)


async def test_running_plan_rolls_back_when_lease_is_lost_three_times(
    migration: Migration,
) -> None:
    harness = migration.harness
    plan = await _approved(migration)
    worker = harness.worker()
    assert await worker.run_once()
    # 5초면 10% 단계가 20건(5 rps)을 넘겨 50% 단계로 간다.
    harness.clock.advance(5)
    assert await worker.run_once()
    at_fifty = await migration.weights()
    harness.clock.advance(5)

    await _lose_lease(migration)
    await harness.run_until_idle()
    final = await migration.get_plan(plan["id"])
    job = await _job(migration, plan["id"])

    assert at_fifty == (50, 50)
    assert (final["status"], final["errorCode"], final["rollbackStatus"]) == (
        "ROLLED_BACK",
        "WORKER_LOST",
        "SUCCEEDED",
    )
    assert final["steps"][2]["result"]["failureCode"] == "WORKER_LOST"
    assert await migration.weights() == (100, 0)
    assert (job.status, job.last_error_code) == (JobStatus.FAILED, "WORKER_LOST")


async def test_approved_plan_fails_without_traffic_when_lease_is_lost(
    migration: Migration,
) -> None:
    plan = await _approved(migration)

    await _lose_lease(migration)
    await migration.harness.run_until_idle()
    final = await migration.get_plan(plan["id"])
    job = await _job(migration, plan["id"])

    assert (final["status"], final["errorCode"], final["rollbackStatus"]) == (
        "FAILED",
        "WORKER_LOST",
        "NOT_NEEDED",
    )
    assert migration.traffic_keys() == []
    assert (job.status, job.last_error_code) == (JobStatus.FAILED, "WORKER_LOST")
    assert (await migration.log_codes(plan["id"]))[-1] == "FAILED"


async def test_rolling_back_plan_keeps_retrying_after_lease_is_lost(
    migration: Migration,
) -> None:
    harness = migration.harness
    harness.executors.traffic.fail_when(lambda key: key.endswith(":rollback:0"))
    plan = await _approved(migration, simulate={"failAtStep": 1, "reason": "ERROR_RATE"})
    worker = harness.worker()
    for _ in range(50):
        row = await migration.plan_row(plan["id"])
        if row.rollback_attempts == 1:
            break
        if not await worker.run_once():
            harness.clock.advance(1)
    harness.clock.advance(1)

    await _lose_lease(migration)
    await harness.run_until_idle()
    final = await migration.get_plan(plan["id"])
    job = await _job(migration, plan["id"])

    assert row.status == "ROLLING_BACK"
    assert (final["status"], final["errorCode"], final["rollbackStatus"]) == (
        "ROLLED_BACK",
        "THRESHOLD_EXCEEDED",
        "SUCCEEDED",
    )
    assert migration.traffic_keys()[-1] == f"{plan['id']}:rollback:1"
    assert await migration.weights() == (100, 0)
    assert (job.status, job.last_error_code) == (JobStatus.FAILED, "WORKER_LOST")


async def test_migration_job_goes_to_migration_processor_and_unknown_kind_raises(
    migration: Migration,
) -> None:
    harness = migration.harness
    plan = await _approved(migration)
    worker = harness.worker()

    claimed = await worker.claim_next_job()
    assert claimed is not None and claimed.kind is JobKind.MIGRATION_PLAN
    unknown = dataclasses.replace(claimed, kind="SOMETHING_NEW")  # type: ignore[arg-type]
    with pytest.raises(UnknownJobKindError):
        await worker.process(unknown)
    still_running = await _job(migration, plan["id"])
    await worker.process(claimed)
    service = await harness.get_service(migration.service_id)

    assert still_running.status == JobStatus.RUNNING
    assert (await migration.get_plan(plan["id"]))["status"] == "RUNNING"
    assert service["status"] == "ACTIVE"


# --- FR-8: 로그 상한 ---


async def test_log_writer_stops_at_limit_with_truncated_line(migration: Migration) -> None:
    harness = migration.harness
    plan = await migration.create_plan()
    async with harness.session_factory() as session, session.begin():
        row = await session.get_one(MigrationPlan, uuid.UUID(plan["id"]))
        writer = MigrationPlanLogWriter(session)
        for _ in range(MAX_MIGRATION_LOG_LINES + 5):
            await writer.append(row, MigrationLogCode.ABORT_REQUESTED, harness.clock.now())
    async with harness.session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(MigrationPlanLog)
            .where(MigrationPlanLog.migration_plan_id == row.id)
        )
        last = await session.scalar(
            select(MigrationPlanLog.code)
            .where(MigrationPlanLog.migration_plan_id == row.id)
            .order_by(MigrationPlanLog.seq.desc())
        )

    assert count == MAX_MIGRATION_LOG_LINES
    assert last == "LOG_TRUNCATED"


async def test_plan_and_log_rows_cannot_be_deleted(migration: Migration) -> None:
    plan = await migration.create_plan()

    async with migration.harness.session_factory() as session:
        with pytest.raises(Exception, match="append-only"):
            await session.execute(text("DELETE FROM migration_plan_logs"))
    async with migration.harness.session_factory() as session:
        with pytest.raises(Exception, match="append-only"):
            await session.execute(
                text("DELETE FROM migration_plans WHERE id = :id"), {"id": plan["id"]}
            )
