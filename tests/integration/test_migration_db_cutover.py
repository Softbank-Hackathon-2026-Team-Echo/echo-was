"""이전 계획의 DB 쓰기 전환 단계(API 서버 17, SPEC api-17-cutover-step).

includeCutover=false 는 지금과 같은 8단계, true 면 비율 100% 뒤 DB_CUTOVER 단계(8)가 컷오버 Job 을
만들고 기다려 결과 행으로 판정한다. 실패 정책: 종료 1 → 트래픽 롤백, 부분 상태·상한 초과·누락 →
자동으로 되돌리지 않음(rollbackStatus SKIPPED).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from app.audit.actions import AuditAction
from app.core.config import DbCutoverExecutorMode
from app.enums import CutoverKind, MigrationErrorCode
from app.executors.base import CutoverRun, DbCutoverJobKind, DbCutoverStartFailed
from app.executors.fake import (
    FAKE_CUTOVER_ROWS_TOTAL,
    FAKE_CUTOVER_WRITE_PAUSE_MS,
    FakeDbCutoverExecutor,
)
from app.models import AuditLog
from app.services.migration_log_messages import MIGRATION_ERROR_MESSAGES
from tests.integration.migration_support import Migration
from tests.integration.test_migration_plans import SUCCESS_LOG_CODES

CUTOVER_SUCCESS_LOG_CODES = [
    *SUCCESS_LOG_CODES[:-1],
    "DB_CUTOVER_JOB_CREATED",
    "DB_CUTOVER_JOB_FINISHED",
    "DB_CUTOVER_OK",
    "SUCCEEDED",
]


def _enable(migration: Migration, **settings: Any) -> None:
    migration.harness.use_settings(db_cutover_executor_mode=DbCutoverExecutorMode.FAKE, **settings)


def _fake(migration: Migration, **knobs: Any) -> FakeDbCutoverExecutor:
    fake = FakeDbCutoverExecutor(migration.harness.clock, 0, **knobs)
    migration.harness.replace_executors(db_cutover=fake)
    return fake


async def _run(migration: Migration, **body: Any) -> tuple[dict[str, Any], list[str]]:
    plan = await migration.create_plan(**body)
    await migration.approve(plan["id"])
    await migration.harness.run_until_idle()
    return await migration.get_plan(plan["id"]), await migration.log_codes(plan["id"])


def _statuses(plan: dict[str, Any]) -> list[str]:
    return [step["result"]["status"] for step in plan["steps"]]


async def _audit(migration: Migration, plan_id: str) -> list[AuditLog]:
    async with migration.harness.session_factory() as session:
        rows = await session.scalars(
            select(AuditLog)
            .where(
                AuditLog.target_id == uuid.UUID(plan_id),
                AuditLog.action.in_(
                    (
                        AuditAction.MIGRATION_DB_CUTOVER_STARTED,
                        AuditAction.MIGRATION_DB_CUTOVER_FINISHED,
                    )
                ),
            )
            .order_by(AuditLog.seq)
        )
        return list(rows.all())


# --- 기본(false): 지금과 같다 ---


async def test_without_include_cutover_plan_has_eight_steps(migration: Migration) -> None:
    _enable(migration)
    fake = _fake(migration)

    final, codes = await _run(migration)
    created = (await migration.log_rows(final["id"]))[0]

    assert [step["kind"] for step in final["steps"]] == [
        "PREFLIGHT",
        "SHIFT",
        "SHIFT",
        "SHIFT",
        "SHIFT",
        "DATA_SYNC_CHECK",
        "SHIFT",
        "SHIFT",
    ]
    assert final["status"] == "SUCCEEDED"
    assert codes == SUCCESS_LOG_CODES
    assert all(step["cutover"] is None for step in final["steps"])
    assert "includeCutover" not in (created.data or {})
    assert fake.started == []
    assert await _audit(migration, final["id"]) == []


async def test_include_cutover_rejected_when_executor_disabled(migration: Migration) -> None:
    response = await migration.request_plan(includeCutover=True)

    assert (response.status_code, response.json()["code"]) == (409, "DB_CUTOVER_DISABLED")


async def test_cutover_simulate_without_include_cutover_is_422(migration: Migration) -> None:
    _enable(migration)
    response = await migration.request_plan(simulate={"failAtStep": 8, "reason": "DB_CUTOVER_FAIL"})

    assert response.status_code == 422


# --- true: 성공 ---


async def test_include_cutover_runs_job_after_full_shift_and_records_result(
    migration: Migration,
) -> None:
    _enable(migration, migration_db_cutover_timeout_seconds=90)
    fake = _fake(migration)

    final, codes = await _run(migration, includeCutover=True)
    step = final["steps"][8]
    audit = await _audit(migration, final["id"])

    assert final["status"] == "SUCCEEDED"
    assert _statuses(final) == ["PASSED"] * 9
    assert (step["kind"], step["maxWaitSeconds"]) == ("DB_CUTOVER", 90)
    assert codes == CUTOVER_SUCCESS_LOG_CODES
    # 트래픽을 100% 옮긴 뒤에 Job 을 만들었다.
    assert await migration.weights() == (0, 100)
    assert [(item.job_kind, item.allow_promote) for item in fake.started] == [
        (DbCutoverJobKind.CUTOVER, False)
    ]
    assert fake.started[0].plan_id.hex == final["id"].replace("-", "")
    assert fake.started[0].target.environment_id == migration.gcp.id
    cutover = step["cutover"]
    assert (cutover["jobKind"], cutover["exitCode"], cutover["jobFinished"]) == ("cutover", 0, True)
    assert cutover["run"]["writePauseMs"] == FAKE_CUTOVER_WRITE_PAUSE_MS
    assert (cutover["run"]["missingRows"], cutover["run"]["duplicateRows"]) == (0, 0)
    # 계획 단위 result 도 단계가 적어 둔 행을 그대로 싣는다(고객 DB 를 다시 읽지 않는다).
    assert final["result"]["writePauseMs"] == FAKE_CUTOVER_WRITE_PAUSE_MS
    assert final["result"]["rowsTotal"] == FAKE_CUTOVER_ROWS_TOTAL
    assert (final["result"]["missingRows"], final["result"]["duplicateRows"]) == (0, 0)
    assert [row.action for row in audit] == [
        AuditAction.MIGRATION_DB_CUTOVER_STARTED,
        AuditAction.MIGRATION_DB_CUTOVER_FINISHED,
    ]
    assert audit[1].result == "SUCCEEDED"
    assert audit[1].details["missingRows"] == 0


async def test_failback_plan_runs_rollback_job_and_emergency_allows_promote(
    migration: Migration,
) -> None:
    _enable(migration)
    fake = _fake(migration)
    await _run(migration, includeCutover=True)

    back, _ = await _run(migration, includeCutover=True, targetEnvironmentId=str(migration.aws.id))

    assert back["mode"] == "FAILBACK"
    assert back["status"] == "SUCCEEDED"
    assert fake.started[-1].job_kind is DbCutoverJobKind.ROLLBACK
    assert fake.started[-1].result_kind is CutoverKind.ROLLBACK
    assert back["result"]["kind"] == "ROLLBACK"


async def test_already_writer_reason_passes_without_result_row(migration: Migration) -> None:
    _enable(migration)
    _fake(migration, reason="ALREADY_WRITER")

    final, codes = await _run(migration, includeCutover=True)

    assert final["status"] == "SUCCEEDED"
    assert final["steps"][8]["cutover"]["reason"] == "ALREADY_WRITER"
    assert final["steps"][8]["cutover"]["run"] is None
    assert codes[-2:] == ["DB_CUTOVER_OK", "SUCCEEDED"]


# --- true: 실패 정책 ---


async def test_job_exit_one_rolls_traffic_back_to_source(migration: Migration) -> None:
    _enable(migration)
    _fake(migration, exit_code=1)

    final, codes = await _run(migration, includeCutover=True)

    assert (final["status"], final["errorCode"], final["rollbackStatus"]) == (
        "ROLLED_BACK",
        "DB_CUTOVER_FAILED",
        "SUCCEEDED",
    )
    assert final["errorMessage"] == MIGRATION_ERROR_MESSAGES[MigrationErrorCode.DB_CUTOVER_FAILED]
    assert await migration.weights() == (100, 0)
    assert _statuses(final) == ["PASSED"] * 8 + ["FAILED"]
    assert codes[-3:] == ["DB_CUTOVER_FAILED", "ROLLBACK_STARTED", "ROLLBACK_APPLIED"]


async def test_job_create_failure_rolls_traffic_back(migration: Migration) -> None:
    _enable(migration)
    _fake(migration, start_failure=DbCutoverStartFailed("JOB_CREATE_REJECTED"))

    final, _ = await _run(migration, includeCutover=True)

    assert (final["status"], final["errorCode"]) == ("ROLLED_BACK", "DB_CUTOVER_FAILED")
    assert final["steps"][8]["cutover"]["startFailure"] == "JOB_CREATE_REJECTED"
    assert await migration.weights() == (100, 0)


@pytest.mark.parametrize(
    ("knobs", "error_code", "reason"),
    [
        ({"exit_code": 2}, "DB_CUTOVER_PARTIAL", None),
        ({"lost": True}, "DB_CUTOVER_PARTIAL", "JOB_LOST"),
        (
            {
                "run": CutoverRun(
                    kind=CutoverKind.CUTOVER,
                    paused_at=datetime(2026, 11, 7, tzinfo=UTC),
                    resumed_at=datetime(2026, 11, 7, tzinfo=UTC),
                    write_pause_ms=900,
                    drain_ms=None,
                    catchup_ms=None,
                    switch_ms=None,
                    rows_total=10,
                    missing_rows=0,
                    duplicate_rows=2,
                )
            },
            "DATA_INTEGRITY_FAILED",
            "ROWS_MISMATCH",
        ),
        ({"no_run": True}, "DATA_INTEGRITY_FAILED", "RESULT_MISSING"),
    ],
)
async def test_unsafe_cutover_failure_leaves_traffic_and_db_for_a_person(
    migration: Migration, knobs: dict[str, Any], error_code: str, reason: str | None
) -> None:
    _enable(migration)
    _fake(migration, **knobs)

    final, codes = await _run(migration, includeCutover=True)
    audit = await _audit(migration, final["id"])

    assert (final["status"], final["errorCode"], final["rollbackStatus"]) == (
        "FAILED",
        error_code,
        "SKIPPED",
    )
    # 트래픽은 도착 100% 그대로(쓰기 담당이 도착이거나 알 수 없다).
    assert await migration.weights() == (0, 100)
    assert final["steps"][8]["result"]["failureCode"] == error_code
    assert final["steps"][8]["cutover"]["reason"] == reason
    assert codes[-3:] == ["DB_CUTOVER_FAILED", "DB_CUTOVER_MANUAL_REQUIRED", "FAILED"]
    assert audit[-1].result == "FAILED"
    assert audit[-1].details["errorCode"] == error_code


async def test_job_that_never_finishes_times_out_without_rollback(migration: Migration) -> None:
    _enable(migration, migration_db_cutover_timeout_seconds=20)
    fake = _fake(migration, never_finish=True)

    final, codes = await _run(migration, includeCutover=True)

    assert (final["status"], final["errorCode"], final["rollbackStatus"]) == (
        "FAILED",
        "DB_CUTOVER_TIMEOUT",
        "SKIPPED",
    )
    assert final["steps"][8]["cutover"]["reason"] == "TIMED_OUT"
    assert fake.polls >= 2
    assert await migration.weights() == (0, 100)
    assert "DB_CUTOVER_JOB_FINISHED" not in codes


async def test_timed_out_job_with_result_row_passes(migration: Migration) -> None:
    _enable(migration, migration_db_cutover_timeout_seconds=20)
    run = CutoverRun(
        kind=CutoverKind.CUTOVER,
        paused_at=datetime(2026, 11, 7, tzinfo=UTC),
        resumed_at=datetime(2026, 11, 7, tzinfo=UTC),
        write_pause_ms=1200,
        drain_ms=None,
        catchup_ms=None,
        switch_ms=None,
        rows_total=5,
        missing_rows=0,
        duplicate_rows=0,
    )
    _fake(migration, never_finish=True, run=run)

    final, _ = await _run(migration, includeCutover=True)

    assert final["status"] == "SUCCEEDED"
    assert final["result"]["writePauseMs"] == 1200


# --- simulate(가짜 모드 시연) ---


@pytest.mark.parametrize(
    ("reason", "status", "error_code", "rollback_status", "weights"),
    [
        ("DB_CUTOVER_FAIL", "ROLLED_BACK", "DB_CUTOVER_FAILED", "SUCCEEDED", (100, 0)),
        ("DB_CUTOVER_PARTIAL", "FAILED", "DB_CUTOVER_PARTIAL", "SKIPPED", (0, 100)),
        ("DB_CUTOVER_TIMEOUT", "FAILED", "DB_CUTOVER_TIMEOUT", "SKIPPED", (0, 100)),
        ("DATA_INTEGRITY", "FAILED", "DATA_INTEGRITY_FAILED", "SKIPPED", (0, 100)),
    ],
)
async def test_simulated_cutover_failures_do_not_call_executor(
    migration: Migration,
    reason: str,
    status: str,
    error_code: str,
    rollback_status: str,
    weights: tuple[int, int],
) -> None:
    _enable(migration, migration_db_cutover_timeout_seconds=15)
    fake = _fake(migration)

    final, _ = await _run(
        migration, includeCutover=True, simulate={"failAtStep": 8, "reason": reason}
    )

    assert (final["status"], final["errorCode"], final["rollbackStatus"]) == (
        status,
        error_code,
        rollback_status,
    )
    assert await migration.weights() == weights
    assert fake.started == [] and fake.polls == 0
    assert final["steps"][8]["cutover"]["jobName"].startswith("simulated-cutover-")


# --- 중단: Job 을 만든 뒤에는 판정까지 기다린다 ---


async def test_abort_while_job_runs_waits_for_job_result(migration: Migration) -> None:
    _enable(migration, migration_db_cutover_timeout_seconds=60)
    fake = _fake(migration, never_finish=True)
    plan = await migration.create_plan(includeCutover=True)
    await migration.approve(plan["id"])
    worker = migration.harness.worker()
    for _ in range(200):
        await worker.run_once()
        if fake.started:
            break
        migration.harness.clock.advance(1.0)
    assert fake.started

    response = await migration.act(plan["id"], "abort")
    fake.never_finish = False
    await migration.harness.run_until_idle()
    final = await migration.get_plan(plan["id"])

    assert response.status_code == 202, response.text
    # 중단으로 트래픽을 되돌리지 않고 Job 결과로 끝냈다(쓰기 담당 = 도착과 어긋나지 않게).
    assert final["status"] == "SUCCEEDED"
    assert await migration.weights() == (0, 100)
