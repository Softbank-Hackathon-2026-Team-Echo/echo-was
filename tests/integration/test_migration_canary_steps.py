"""API-24: 최소 관찰 시간 경계와 요청·저장 계획의 하위 호환."""

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import update

from app.core.config import Settings
from app.executors.base import HealthHttpError
from app.models import MigrationPlan
from app.schemas.migration_plan import CreateMigrationPlanRequest
from tests.integration.deploy_harness import DeployHarness, ScriptedHealthChecker
from tests.integration.disaster_support import declare_disaster, tokyo_serving_seoul_standby
from tests.integration.migration_support import Migration, store_legacy_steps
from tests.integration.test_migration_counting import _start_counting


@pytest.mark.parametrize("minimum", [0, 4, 60])
async def test_count_reached_waits_from_weight_apply_and_keeps_snapshot(
    migration: Migration, minimum: int
) -> None:
    harness = migration.harness
    harness.use_settings(migration_min_observe_seconds=minimum)
    counting = await _start_counting(migration, requiredRequests=[1] * 6)
    started = harness.clock.now()
    # 계획을 만든 뒤 설정이 바뀌어도 저장된 단계값은 유지한다.
    harness.use_settings(migration_min_observe_seconds=60 if minimum == 0 else 0)
    await counting.tick(target=1)
    if minimum:
        assert await counting.position() == (1, "JUDGE", "RUNNING")
        await counting.tick(seconds=minimum - 1.001)
        assert await counting.position() == (1, "JUDGE", "RUNNING")
        # 새 일꾼으로 다시 시작해도 비율 적용 시각은 유지한다.
        await counting.tick(seconds=0.001)
    assert await counting.position() == (2, "OBSERVE", "RUNNING")
    plan = await counting.plan()
    passed = next(
        row for row in await migration.log_rows(counting.plan_id) if row.code == "JUDGE_PASSED"
    )
    assert passed.logged_at == started + timedelta(seconds=max(1, minimum))
    assert plan["steps"][1]["minObserveSeconds"] == minimum
    assert plan["steps"][1]["result"]["metrics"]["requestCount"] == 1


async def test_low_traffic_deadline_before_minimum_waits_without_busy_loop(
    migration: Migration,
) -> None:
    migration.harness.use_settings(migration_min_observe_seconds=8)
    counting = await _start_counting(migration, maxWaitSeconds=5)
    await counting.wait_until(5)
    assert await counting.position() == (1, "JUDGE", "RUNNING")
    job = await migration.plan_job(counting.plan_id)
    assert job is not None and job.run_after > migration.harness.clock.now()
    await counting.tick(seconds=3)
    assert await counting.position() == (2, "OBSERVE", "RUNNING")
    result = (await counting.plan())["steps"][1]["result"]
    assert result["metrics"]["lowTraffic"] is True
    assert (await counting.plan())["steps"][1]["expectedSeconds"] == 10


async def test_health_failure_during_minimum_wait_rolls_back_immediately(
    migration: Migration,
) -> None:
    counting = await _start_counting(migration, requiredRequests=[1] * 6)
    await counting.tick(target=1)
    assert await counting.position() == (1, "JUDGE", "RUNNING")
    migration.harness.replace_executors(health=ScriptedHealthChecker(default=HealthHttpError(503)))
    await counting.tick()
    final = await counting.plan()
    assert (final["status"], final["errorCode"]) == ("ROLLED_BACK", "HEALTH_CHECK_FAILED")
    assert await migration.weights() == (100, 0)


@pytest.mark.parametrize(
    ("reason", "error", "elapsed"),
    [
        ("ERROR_RATE", "THRESHOLD_EXCEEDED", 1),
        ("TARGET_NOT_RECEIVING", "TARGET_NOT_RECEIVING", 5),
        ("TRAFFIC", "TRAFFIC_APPLY_FAILED", 0),
        (None, "METRICS_UNAVAILABLE", 5),
    ],
)
async def test_failure_never_waits_for_minimum_observation(
    migration: Migration, reason: str | None, error: str, elapsed: int
) -> None:
    harness = migration.harness
    harness.use_settings(migration_min_observe_seconds=60)
    if reason is None:
        harness.executors.metrics.fail_counts()
    plan = await migration.create_plan(
        maxWaitSeconds=5,
        **({"simulate": {"failAtStep": 1, "reason": reason}} if reason else {}),
    )
    await migration.approve(plan["id"])
    started = harness.clock.now()
    await harness.run_until_idle()
    final = await migration.get_plan(plan["id"])
    assert (final["status"], final["errorCode"]) == ("ROLLED_BACK", error)
    assert harness.clock.now() - started == timedelta(seconds=elapsed)


@pytest.mark.parametrize(
    ("old_step", "new_step", "reason"),
    [
        (1, 2, "ERROR_RATE"),
        (2, 4, "TRAFFIC"),
        (3, 5, "DATA_LAG"),
        (4, 7, "HEALTH"),
        (5, 8, "DB_CUTOVER_FAIL"),
    ],
)
async def test_legacy_request_expands_counts_and_translates_simulation_before_validation(
    migration: Migration, old_step: int, new_step: int, reason: str
) -> None:
    migration.harness.use_settings(db_cutover_executor_mode="fake")
    plan = await migration.create_plan(
        requiredRequests=[11, 22, 33],
        includeCutover=old_step == 5,
        simulate={"failAtStep": old_step, "reason": reason},
    )
    stored = await migration.plan_row(plan["id"])
    assert [s["requiredRequests"] for s in plan["steps"] if s["kind"] == "SHIFT"] == [
        11,
        11,
        22,
        22,
        33,
        33,
    ]
    assert stored.simulate == {"failAtStep": new_step, "reason": reason}
    await migration.approve(plan["id"])
    await migration.harness.run_until_idle()
    final = await migration.get_plan(plan["id"])
    assert final["status"] == "ROLLED_BACK"
    assert final["steps"][new_step]["result"]["status"] == "FAILED"


@pytest.mark.parametrize("counts", [None, [1] * 6, [1] * 3])
@pytest.mark.parametrize(
    ("reason", "valid_steps"),
    [
        ("ERROR_RATE", {1, 2, 3, 4, 6, 7}),
        ("HEALTH", {1, 2, 3, 4, 6, 7}),
        ("TRAFFIC", {1, 2, 3, 4, 6, 7}),
        ("LOW_TRAFFIC", {1, 2, 3, 4, 6, 7}),
        ("TARGET_NOT_RECEIVING", {1, 2}),
        ("DATA_LAG", {5}),
        ("DB_CUTOVER_FAIL", {8}),
        ("DB_CUTOVER_PARTIAL", {8}),
        ("DB_CUTOVER_TIMEOUT", {8}),
        ("DATA_INTEGRITY", {8}),
    ],
)
def test_simulate_reason_matrix_uses_request_version(
    counts: list[int] | None, reason: str, valid_steps: set[int]
) -> None:
    legacy_mapping = {1: 2, 2: 4, 3: 5, 4: 7, 5: 8}
    for index in range(1, 9):
        data: dict[str, Any] = {
            "targetEnvironmentId": str(uuid.uuid4()),
            "includeCutover": True,
            "simulate": {"failAtStep": index, "reason": reason},
        }
        if counts is not None:
            data["requiredRequests"] = counts
        mapped = legacy_mapping.get(index) if counts is not None and len(counts) == 3 else index
        if mapped in valid_steps:
            assert CreateMigrationPlanRequest.model_validate(data).simulate is not None
        else:
            with pytest.raises(ValidationError):
                CreateMigrationPlanRequest.model_validate(data)


@pytest.mark.parametrize("minimum", [-1, 61])
def test_minimum_observation_setting_rejects_out_of_range(minimum: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, migration_min_observe_seconds=minimum)


@pytest.mark.parametrize("cutover", [False, True])
@pytest.mark.parametrize("before_counting", [False, True])
@pytest.mark.parametrize("fail", [False, True])
async def test_stored_legacy_steps_are_canonical_for_response_execution_and_rollback(
    migration: Migration, cutover: bool, before_counting: bool, fail: bool
) -> None:
    migration.harness.use_settings(
        db_cutover_executor_mode="fake", migration_min_observe_seconds=60
    )
    plan = await migration.create_plan(includeCutover=cutover)
    steps = await store_legacy_steps(migration, plan["id"], before_counting=before_counting)
    if fail:
        # 저장된 계획의 simulate는 옛 번호 그대로 읽는다.
        async with migration.harness.session_factory() as session, session.begin():
            await session.execute(
                update(MigrationPlan)
                .where(MigrationPlan.id == uuid.UUID(plan["id"]))
                .values(simulate={"failAtStep": 3, "reason": "DATA_LAG"})
            )
    read = await migration.get_plan(plan["id"])
    assert [s["kind"] for s in read["steps"]] == [s["kind"] for s in steps]
    assert [s["minObserveSeconds"] for s in read["steps"] if s["kind"] == "SHIFT"] == [0] * 3
    assert [s["requiredRequests"] for s in read["steps"] if s["kind"] == "SHIFT"] == (
        [None] * 3 if before_counting else [20, 50, 50]
    )
    await migration.approve(plan["id"])
    await migration.harness.run_until_idle()
    final = await migration.get_plan(plan["id"])
    assert final["status"] == ("ROLLED_BACK" if fail else "SUCCEEDED")
    applied = [row for row in await migration.log_rows(plan["id"]) if row.code == "WEIGHT_APPLIED"]
    assert [row.step for row in applied] == ([1, 2] if fail else [1, 2, 4])
    passed = [row for row in await migration.log_rows(plan["id"]) if row.code == "JUDGE_PASSED"]
    # 새 설정이 60초여도 옛 동작(4·2·1초)으로 진행한다.
    assert [
        (b.logged_at - a.logged_at).total_seconds() for a, b in zip(applied, passed, strict=True)
    ] == ([4, 2] if fail else [4, 2, 1])
    assert await migration.weights() == ((100, 0) if fail else (0, 100))


async def test_all_default_shift_steps_observe_three_seconds(migration: Migration) -> None:
    plan = await migration.create_plan()
    await migration.approve(plan["id"])
    await migration.harness.run_until_idle()
    final = await migration.get_plan(plan["id"])
    assert final["status"] == "SUCCEEDED"
    assert (
        datetime.fromisoformat(final["finishedAt"]) - datetime.fromisoformat(final["startedAt"])
    ).total_seconds() == 18
    for step in final["steps"]:
        assert step["minObserveSeconds"] == (3 if step["kind"] == "SHIFT" else None)


async def test_legacy_cutover_failure_rolls_back_using_stored_step_five(
    migration: Migration,
) -> None:
    migration.harness.use_settings(db_cutover_executor_mode="fake")
    plan = await migration.create_plan(includeCutover=True)
    await store_legacy_steps(migration, plan["id"])
    async with migration.harness.session_factory() as session, session.begin():
        await session.execute(
            update(MigrationPlan)
            .where(MigrationPlan.id == uuid.UUID(plan["id"]))
            .values(simulate={"failAtStep": 5, "reason": "DB_CUTOVER_FAIL"})
        )
    await migration.approve(plan["id"])
    await migration.harness.run_until_idle()
    final = await migration.get_plan(plan["id"])
    assert (final["status"], final["errorCode"]) == ("ROLLED_BACK", "DB_CUTOVER_FAILED")
    assert final["steps"][5]["kind"] == "DB_CUTOVER"
    assert final["steps"][5]["result"]["status"] == "FAILED"
    assert await migration.weights() == (100, 0)


@pytest.mark.parametrize("mode", ["PLANNED", "FAILBACK", "EMERGENCY"])
@pytest.mark.parametrize("cutover", [False, True])
async def test_all_plan_modes_create_same_canary_sequence(
    harness: DeployHarness, mode: str, cutover: bool
) -> None:
    migration = await tokyo_serving_seoul_standby(harness)
    harness.use_settings(db_cutover_executor_mode="fake", migration_db_cutover_on_disaster=cutover)
    try:
        if mode == "EMERGENCY":
            disaster = await declare_disaster(migration)
            [result] = disaster["plans"]
            plan = await migration.get_plan(result["planId"])
        else:
            if mode == "FAILBACK":
                first = await migration.create_plan()
                await migration.approve(first["id"])
                await harness.run_until_idle()
            plan = await migration.create_plan(
                targetEnvironmentId=str(
                    migration.aws.id if mode == "FAILBACK" else migration.gcp.id
                ),
                includeCutover=cutover,
            )
        assert plan["mode"] == mode
        assert [step["index"] for step in plan["steps"]] == list(range(9 if cutover else 8))
        assert [step["kind"] for step in plan["steps"]] == (
            ["PREFLIGHT", "SHIFT", "SHIFT", "SHIFT", "SHIFT", "DATA_SYNC_CHECK", "SHIFT", "SHIFT"]
            + (["DB_CUTOVER"] if cutover else [])
        )
        shifts = [step for step in plan["steps"] if step["kind"] == "SHIFT"]
        assert [step["targetWeight"] for step in shifts] == [5, 10, 25, 50, 75, 100]
        assert [step["requiredRequests"] for step in shifts] == [7, 10, 20, 30, 30, 30]
        assert [step["minObserveSeconds"] for step in shifts] == [3] * 6
        assert [step["expectedSeconds"] for step in shifts] == [5] * 6
        assert plan["expectedSeconds"] == (66 if cutover else 34)
    finally:
        await migration.author_client.aclose()
        await migration.approver_client.aclose()
