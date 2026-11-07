"""시연 조직 스냅샷·복귀 API(실 PG). 역할 표는 test_authorization.py 의 ROLE_TABLE 이 본다."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import Response
from sqlalchemy import select

from app.audit.actions import AuditAction
from app.core.config import DbCutoverExecutorMode
from app.enums import MigrationPlanStatus
from app.models import AgentRun, AuditLog, DeployRequest, MigrationPlan
from tests.integration.auth_support import org_path
from tests.integration.deploy_harness import DeployHarness
from tests.integration.disaster_support import (
    NEAR_SEOUL,
    allow_self_approval,
    declare_disaster,
    tokyo_serving_seoul_standby,
)
from tests.integration.migration_support import Migration
from tests.integration.rollback_support import deploy_and_finish
from tests.integration.runner_support import create_precheck


@pytest.fixture
async def tokyo(harness: DeployHarness) -> AsyncIterator[Migration]:
    setup = await tokyo_serving_seoul_standby(harness)
    try:
        yield setup
    finally:
        await setup.author_client.aclose()
        await setup.approver_client.aclose()


def _path(harness: DeployHarness, suffix: str) -> str:
    return org_path(harness.organization.id, f"/demo{suffix}")


async def _take(harness: DeployHarness) -> Response:
    response = await harness.client.post(_path(harness, "/snapshot"))
    harness.responses.append(response.text)
    return response


async def _snapshot(harness: DeployHarness) -> dict[str, Any]:
    response = await _take(harness)
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def _restore(harness: DeployHarness, **body: Any) -> dict[str, Any]:
    url = _path(harness, "/restore")
    response = await (harness.client.post(url, json=body) if body else harness.client.post(url))
    harness.responses.append(response.text)
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


def _steps(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {step["step"]: step for step in result["steps"]}


def _status(result: dict[str, Any]) -> dict[str, str]:
    return {step["step"]: step["status"] for step in result["steps"]}


def _items(result: dict[str, Any], step: str) -> list[tuple[str, str, str | None]]:
    return [
        (item["action"], item["status"], item["code"]) for item in _steps(result)[step]["items"]
    ]


async def _audit(harness: DeployHarness, action: AuditAction) -> list[AuditLog]:
    async with harness.session_factory() as session:
        rows = await session.scalars(
            select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.seq)
        )
        return list(rows.all())


async def _plan_status(harness: DeployHarness, plan_id: str | uuid.UUID) -> str:
    async with harness.session_factory() as session:
        plan = await session.get_one(MigrationPlan, uuid.UUID(str(plan_id)))
        return str(plan.status)


async def _plans(harness: DeployHarness) -> list[MigrationPlan]:
    async with harness.session_factory() as session:
        rows = await session.scalars(select(MigrationPlan).order_by(MigrationPlan.created_at))
        return list(rows.all())


CLEAN_STATUSES = {
    "MIGRATION_PLANS": "OK",
    "DISASTERS": "OK",
    "AI_RUNS": "OK",
    "DEPLOY_REQUESTS": "OK",
    "TRAFFIC": "OK",
    "DEPLOYMENTS": "OK",
    "DB_WRITER": "OK",
    "DEMO_APP_DATA": "MANUAL_REQUIRED",
    "AI_FIX_PULL_REQUESTS": "OK",
    "KEPT_SETTINGS": "KEPT",
    "AUDIT_LOGS": "KEPT",
}


# --- 스냅샷 만들기·조회 ---


async def test_get_without_snapshot_is_404_and_restore_too(migration: Migration) -> None:
    harness = migration.harness

    got = await harness.client.get(_path(harness, "/snapshot"))
    restored = await harness.client.post(_path(harness, "/restore"))

    assert (got.status_code, got.json()["code"]) == (404, "DEMO_SNAPSHOT_NOT_FOUND")
    assert (restored.status_code, restored.json()["code"]) == (404, "DEMO_SNAPSHOT_NOT_FOUND")


async def test_snapshot_records_state_in_audit_line_and_get_returns_latest(
    migration: Migration,
) -> None:
    harness = migration.harness

    first = await _snapshot(harness)

    (service,) = first["services"]
    assert service["serviceId"] == migration.service_id
    assert service["servingEnvironmentId"] == str(migration.aws.id)
    by_env = {item["environmentName"]: item for item in service["connections"]}
    assert set(by_env) == {"aws-seoul", "gcp-seoul"}
    assert (by_env["aws-seoul"]["trafficWeight"], by_env["gcp-seoul"]["trafficWeight"]) == (100, 0)
    for item in by_env.values():
        assert item["currentDeploymentId"] is not None
        assert item["deployRequestId"] is not None
        assert len(item["commitSha"]) == 40
        assert item["imageDigest"]
    assert by_env["aws-seoul"]["imageDigest"] == by_env["gcp-seoul"]["imageDigest"]
    assert {item["name"] for item in first["environments"]} == {"aws-seoul", "gcp-seoul"}
    assert first["allowSelfApproval"] is False
    assert first["takenByUserId"] == str(harness.admin.user.id)
    (row,) = await _audit(harness, AuditAction.DEMO_SNAPSHOT)
    assert str(row.id) == first["id"]
    assert row.target_type == "organization"
    assert row.target_id == harness.organization.id
    assert row.route == "POST /api/v1/orgs/{org_id}/demo/snapshot"
    assert row.details["snapshot"]["services"][0]["serviceId"] == migration.service_id

    harness.clock.advance(5)
    second = await _snapshot(harness)
    got = await harness.client.get(_path(harness, "/snapshot"))

    assert got.status_code == 200, got.text
    assert got.json()["data"]["id"] == second["id"] != first["id"]
    assert len(await _audit(harness, AuditAction.DEMO_SNAPSHOT)) == 2


async def test_snapshot_refuses_open_plan_and_writes_nothing(migration: Migration) -> None:
    harness = migration.harness
    plan = await migration.create_plan()

    response = await _take(harness)

    assert (response.status_code, response.json()["code"]) == (409, "DEMO_STATE_NOT_CLEAN")
    assert response.json()["details"] == [
        {"field": "migrationPlans", "reason": "OPEN_MIGRATION_PLAN", "value": plan["id"]}
    ]
    assert await _audit(harness, AuditAction.DEMO_SNAPSHOT) == []


async def test_snapshot_refuses_split_traffic(migration: Migration) -> None:
    harness = migration.harness
    await migration.set_weights(50, 50)

    response = await _take(harness)

    assert (response.status_code, response.json()["code"]) == (409, "DEMO_STATE_NOT_CLEAN")
    assert response.json()["details"] == [
        {
            "field": "services",
            "reason": "SERVICE_NOT_SINGLE_SERVING",
            "value": migration.service_id,
        }
    ]


async def test_snapshot_refuses_active_disaster(tokyo: Migration) -> None:
    harness = tokyo.harness
    # 서울 근처: aws-tokyo 는 멀어 계획이 생기지 않고, 재난만 진행 중이다.
    disaster = await declare_disaster(tokyo, epicenter=NEAR_SEOUL)

    response = await _take(harness)

    assert (response.status_code, response.json()["code"]) == (409, "DEMO_STATE_NOT_CLEAN")
    reasons = {(item["reason"], item["value"]) for item in response.json()["details"]}
    assert ("ACTIVE_DISASTER", disaster["id"]) in reasons


# --- 복귀 ---


async def test_restore_on_clean_state_changes_nothing_and_writes_no_line(
    migration: Migration,
) -> None:
    harness = migration.harness
    snapshot = await _snapshot(harness)

    result = await _restore(harness)

    assert result["snapshotId"] == snapshot["id"]
    assert (result["restored"], result["callAgain"], result["dryRun"]) == (True, False, False)
    assert _status(result) == CLEAN_STATUSES
    assert result["manualRequired"] == ["DEMO_APP_DATA"]
    assert _items(result, "DEMO_APP_DATA") == [
        ("RESET_APP_DATA", "MANUAL_REQUIRED", "APP_RESET_ENDPOINT_MISSING")
    ]
    assert _items(result, "KEPT_SETTINGS") == [("COMPARE", "KEPT", "UNCHANGED")]
    assert _items(result, "AUDIT_LOGS") == [("NONE", "KEPT", "AUDIT_LOGS_NOT_DELETABLE")]
    assert await _audit(harness, AuditAction.DEMO_RESTORE) == []


async def test_restore_after_emergency_evacuation_ends_disaster_and_fails_back(
    tokyo: Migration,
) -> None:
    harness = tokyo.harness
    await allow_self_approval(harness)
    await _snapshot(harness)
    disaster = await declare_disaster(tokyo)
    await harness.run_until_idle()
    assert await tokyo.weights() == (0, 100)

    first = await _restore(harness)

    assert _status(first)["DISASTERS"] == "DONE"
    assert _items(first, "TRAFFIC") == [("APPROVE_FAILBACK_PLAN", "STARTED", None)]
    assert (first["restored"], first["callAgain"]) == (False, True)
    (line,) = await _audit(harness, AuditAction.DEMO_RESTORE)
    assert line.details["steps"]["DISASTERS"] == "DONE"
    assert line.details["steps"]["TRAFFIC"] == "STARTED"
    # 재난 종료(DONE) + 복귀 계획 승인(STARTED).
    assert line.details["changedItems"] == 2
    # 기존 함수가 자기 줄을 그대로 쓴다.
    (ended,) = await _audit(harness, AuditAction.DISASTER_ENDED)
    assert str(ended.target_id) == disaster["id"]
    assert len(await _audit(harness, AuditAction.MIGRATION_PLAN_APPROVED)) == 2

    await harness.run_until_idle()
    second = await _restore(harness)

    assert await tokyo.weights() == (100, 0)
    assert (second["restored"], second["callAgain"]) == (True, False)
    assert _status(second)["TRAFFIC"] == "OK"
    assert _status(second)["DEPLOYMENTS"] == "OK"
    # 아무것도 바뀌지 않은 두 번째 호출은 줄을 쓰지 않는다.
    assert len(await _audit(harness, AuditAction.DEMO_RESTORE)) == 1
    plans = await _plans(harness)
    assert [str(plan.status) for plan in plans] == ["SUCCEEDED", "SUCCEEDED"]


async def test_failback_with_self_approval_off_is_manual_required_until_other_approves(
    tokyo: Migration,
) -> None:
    harness = tokyo.harness
    await _snapshot(harness)
    disaster = await declare_disaster(tokyo)
    (emergency,) = disaster["plans"]
    await tokyo.approve(emergency["planId"])
    await harness.run_until_idle()
    assert await tokyo.weights() == (0, 100)

    first = await _restore(harness)

    assert _items(first, "TRAFFIC") == [
        ("APPROVE_FAILBACK_PLAN", "MANUAL_REQUIRED", "APPROVAL_REQUIRED")
    ]
    assert "TRAFFIC" in first["manualRequired"]
    assert first["restored"] is False
    failback = [plan for plan in await _plans(harness) if str(plan.id) != emergency["planId"]]
    (plan,) = failback
    assert plan.status == MigrationPlanStatus.PENDING_APPROVAL

    # 다시 불러도 계획을 새로 만들지 않고, 같은 계획을 승인 대기로 둔다.
    again = await _restore(harness)
    assert _items(again, "MIGRATION_PLANS") == [("KEEP_FAILBACK_PLAN", "OK", None)]
    assert _items(again, "TRAFFIC") == [
        ("APPROVE_FAILBACK_PLAN", "MANUAL_REQUIRED", "APPROVAL_REQUIRED")
    ]
    assert len(await _plans(harness)) == 2

    await tokyo.approve(str(plan.id))
    await harness.run_until_idle()
    done = await _restore(harness)

    assert await tokyo.weights() == (100, 0)
    assert (done["restored"], done["callAgain"]) == (True, False)


async def test_restore_cancels_pending_plan(migration: Migration) -> None:
    harness = migration.harness
    await _snapshot(harness)
    plan = await migration.create_plan()

    result = await _restore(harness)

    assert _items(result, "MIGRATION_PLANS") == [("CANCEL_PLAN", "DONE", None)]
    assert result["restored"] is True
    assert await _plan_status(harness, plan["id"]) == "CANCELLED"
    assert len(await _audit(harness, AuditAction.MIGRATION_PLAN_CANCELLED)) == 1
    assert len(await _audit(harness, AuditAction.DEMO_RESTORE)) == 1


async def test_restore_aborts_running_plan_waits_for_rollback_then_ok(
    migration: Migration,
) -> None:
    harness = migration.harness
    await _snapshot(harness)
    plan = await migration.create_plan()
    await migration.approve(plan["id"])
    worker = harness.worker()
    for _ in range(3):
        await worker.run_once()
        harness.clock.advance(1)

    first = await _restore(harness)

    assert _items(first, "MIGRATION_PLANS") == [("ABORT_PLAN", "STARTED", None)]
    assert _items(first, "TRAFFIC")[0][1:] in {
        ("OK", None),
        ("WAITING", "WAITING_FOR_EARLIER_STEP"),
    }
    assert first["callAgain"] is True

    second = await _restore(harness)
    assert _items(second, "MIGRATION_PLANS") == [
        ("WAIT_PLAN", "WAITING", "ABORT_IN_PROGRESS")
    ] or _items(second, "MIGRATION_PLANS") == [("WAIT_PLAN", "WAITING", "ROLLBACK_IN_PROGRESS")]
    assert len(await _audit(harness, AuditAction.MIGRATION_ABORT_REQUESTED)) == 1

    await harness.run_until_idle()
    third = await _restore(harness)

    assert await _plan_status(harness, plan["id"]) in {"CANCELLED", "ROLLED_BACK"}
    assert await migration.weights() == (100, 0)
    assert (third["restored"], third["callAgain"]) == (True, False)
    assert _items(third, "MIGRATION_PLANS") == []


async def test_restore_rolls_back_changed_image_with_build_reuse(migration: Migration) -> None:
    harness = migration.harness
    snapshot = await _snapshot(harness)
    by_env = {item["environmentName"]: item for item in snapshot["services"][0]["connections"]}
    source_request_id = by_env["aws-seoul"]["deployRequestId"]
    changed = await deploy_and_finish(harness, migration.service_id, migration.aws.id, branch="v2")
    assert changed["status"] == "SUCCEEDED"

    first = await _restore(harness)

    (item,) = _steps(first)["DEPLOYMENTS"]["items"][:1]
    assert (item["action"], item["status"]) == ("ROLLBACK_DEPLOYMENT", "STARTED")
    assert item["label"] == "shop-api@aws-seoul"
    assert item["detail"] == f"rollbackOfDeployRequestId={source_request_id}"
    assert first["callAgain"] is True
    assert len(await _audit(harness, AuditAction.ROLLBACK_REQUESTED)) == 1
    # 되돌리기 요청이 끝나기 전에는 배포 요청 단계가 기다린다.
    waiting = await _restore(harness, dryRun=True)
    assert _status(waiting)["DEPLOY_REQUESTS"] == "WAITING"

    await harness.run_until_idle()
    second = await _restore(harness)

    assert (second["restored"], second["callAgain"]) == (True, False)
    async with harness.session_factory() as session:
        rollback = await session.scalar(
            select(DeployRequest).where(
                DeployRequest.rollback_of_request_id == uuid.UUID(source_request_id)
            )
        )
    assert rollback is not None
    assert (str(rollback.status), rollback.build_reused) == ("SUCCEEDED", True)
    assert rollback.commit_sha == by_env["aws-seoul"]["commitSha"]


async def test_dry_run_reports_would_do_and_changes_nothing(migration: Migration) -> None:
    harness = migration.harness
    await _snapshot(harness)
    plan = await migration.create_plan()

    result = await _restore(harness, dryRun=True)

    assert result["dryRun"] is True
    assert _items(result, "MIGRATION_PLANS") == [("CANCEL_PLAN", "WOULD_DO", None)]
    assert result["restored"] is False
    assert await _plan_status(harness, plan["id"]) == "PENDING_APPROVAL"
    assert await _audit(harness, AuditAction.MIGRATION_PLAN_CANCELLED) == []
    assert await _audit(harness, AuditAction.DEMO_RESTORE) == []


async def test_db_cutover_plan_cancelled_before_running_is_db_writer_ok(
    migration: Migration,
) -> None:
    """승인 전에 취소된 DB 쓰기 전환 계획은 Job 이 돌지 않았다: 쓰기 담당은 스냅샷 그대로다.

    (돌았을 수 있는 계획의 MANUAL_REQUIRED 는 test_demo_snapshot_safety.py 가 본다.)
    """
    harness = migration.harness
    harness.use_settings(db_cutover_executor_mode=DbCutoverExecutorMode.FAKE)
    await _snapshot(harness)
    plan = await migration.create_plan(includeCutover=True)

    result = await _restore(harness)

    (item,) = _steps(result)["DB_WRITER"]["items"]
    assert (item["status"], item["code"], item["targetId"]) == (
        "OK",
        "DB_CUTOVER_NOT_STARTED",
        plan["id"],
    )
    assert "DB_WRITER" not in result["manualRequired"]
    # 계획 자체는 1 단계가 정리한다(DB 쓰기 담당은 건드리지 않는다).
    assert await _plan_status(harness, plan["id"]) == "CANCELLED"
    assert result["restored"] is True


async def test_open_precheck_since_snapshot_is_cancelled(migration: Migration) -> None:
    harness = migration.harness
    await _snapshot(harness)
    precheck = await create_precheck(migration)

    result = await _restore(harness)

    assert _items(result, "AI_RUNS") == [("CANCEL_AI_RUN", "DONE", None)]
    async with harness.session_factory() as session:
        run = await session.get_one(AgentRun, uuid.UUID(precheck["id"]))
    assert str(run.status) == "CANCELLED"
    assert len(await _audit(harness, AuditAction.PRECHECK_CANCELLED)) == 1


async def test_kept_settings_are_reported_not_reverted(migration: Migration) -> None:
    harness = migration.harness
    await _snapshot(harness)
    await allow_self_approval(harness)

    result = await _restore(harness)

    assert _items(result, "KEPT_SETTINGS") == [("COMPARE", "KEPT", "ALLOW_SELF_APPROVAL_CHANGED")]
    assert result["restored"] is True
    settings = await harness.client.get(org_path(harness.organization.id))
    assert settings.json()["data"]["allowSelfApproval"] is True
