"""이전 계획 만들기·승인·실행·조회(SPEC api-8 AC-1~AC-6, AC-13~AC-17)."""

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text

from app.core.config import ExecutorMode
from app.enums import ConnectionErrorCode, DeployRequestStatus, OrganizationRole
from app.models import AuditLog, Environment
from tests.integration.auth_support import (
    create_agent_token,
    create_member,
    create_organization,
    make_client,
    org_path,
)
from tests.integration.deploy_harness import DeployHarness, connection_of
from tests.integration.factories import secret_env_name
from tests.integration.migration_support import Migration
from tests.integration.rollback_support import deploy_and_finish, request_rollback

DEFAULT_CONDITIONS = {
    "maxErrorRatePercent": 1.0,
    "maxP95Ms": 800,
    "maxReplicationLagSeconds": 1.0,
    "healthCheck": True,
    "p95Advisory": True,
}
SUCCESS_LOG_CODES = [
    "PLAN_CREATED",
    "APPROVED",
    "STARTED",
    "PREFLIGHT_OK",
    "WEIGHT_APPLIED",
    "JUDGE_PASSED",
    "WEIGHT_APPLIED",
    "JUDGE_PASSED",
    "DATA_SYNC_OK",
    "WEIGHT_APPLIED",
    "JUDGE_PASSED",
    "SUCCEEDED",
]
DAY_PLUS_ONE_SECOND = 24 * 60 * 60 + 1


def _error(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["code"]


async def _run_and_trace_weights(migration: Migration) -> list[tuple[int, int]]:
    """할 일이 없을 때까지 일꾼을 돌리며 (aws, gcp) 비율이 바뀐 순서를 모은다. 매번 합계 100."""
    harness = migration.harness
    worker = harness.worker()
    seen = [await migration.weights()]
    for _ in range(400):
        worked = await worker.run_once()
        weights = await migration.weights()
        assert sum(weights) == 100, weights
        if weights != seen[-1]:
            seen.append(weights)
        if not worked:
            if await harness.open_job_count() == 0:
                return seen
            harness.clock.advance(1.0)
    raise AssertionError("worker did not become idle")


# --- AC-1·AC-2: 성공 경로와 실행 로그 = 계획 ---


async def test_migration_succeeds_step_by_step_and_logs_match_plan(migration: Migration) -> None:
    harness = migration.harness
    plan = await migration.create_plan()
    approved = await migration.approve(plan["id"])
    traced = await _run_and_trace_weights(migration)
    final = await migration.get_plan(plan["id"])
    service = await harness.get_service(migration.service_id)
    rows = await migration.log_rows(plan["id"])

    assert plan["status"] == "PENDING_APPROVAL"
    assert [step["kind"] for step in plan["steps"]] == [
        "PREFLIGHT",
        "SHIFT",
        "SHIFT",
        "DATA_SYNC_CHECK",
        "SHIFT",
    ]
    assert [step["targetWeight"] for step in plan["steps"]] == [None, 10, 50, None, 100]
    assert [step["sourceWeight"] for step in plan["steps"]] == [None, 90, 50, None, 0]
    # 예상 = 여유 2 + ceil(필요 건수 ÷ (50 rps × 비율)): 20 ÷ 5 = 4, 50 ÷ 25 = 2, 50 ÷ 50 = 1.
    assert [step["expectedSeconds"] for step in plan["steps"]] == [2, 6, 4, 2, 3]
    assert plan["expectedSeconds"] == 17
    assert [step["requiredRequests"] for step in plan["steps"]] == [None, 20, 50, None, 50]
    assert [step["maxWaitSeconds"] for step in plan["steps"]] == [None, 30, 30, None, 30]
    assert plan["maxWaitSeconds"] == 30
    assert "observeSeconds" not in plan
    assert all("observeSeconds" not in step for step in plan["steps"])
    assert plan["progress"] is None
    assert plan["rollbackConditions"] == DEFAULT_CONDITIONS
    assert plan["createdBy"]["login"] == "dev-a"
    assert plan["sourceEnvironmentId"] == str(migration.aws.id)
    assert plan["targetEnvironmentId"] == str(migration.gcp.id)
    assert all(step["result"]["status"] == "PENDING" for step in plan["steps"])
    assert (approved["status"], approved["approvedBy"]["login"], approved["selfApproved"]) == (
        "APPROVED",
        "dev-b",
        False,
    )
    # 비율이 (90,10) → (50,50) → (0,100) 순서로 바뀌고 매번 합계 100.
    assert traced == [(100, 0), (90, 10), (50, 50), (0, 100)]
    assert (final["status"], final["currentStep"], final["errorCode"]) == ("SUCCEEDED", 4, None)
    assert [step["result"]["status"] for step in final["steps"]] == ["PASSED"] * 5
    for index in (1, 2, 4):
        metrics = final["steps"][index]["result"]["metrics"]
        assert metrics["requestCount"] >= final["steps"][index]["requiredRequests"]
        assert (metrics["errors5xx"], metrics["allowedErrors5xx"]) == (0, 1)
        assert (metrics["errorRatePercent"], metrics["lowTraffic"]) == (0.0, False)
        assert metrics["healthStatus"] == "OK"
        # p95 는 참고 값(가짜 지표 120ms)으로만 남는다.
        assert metrics["p95Ms"] == 120.0
    assert final["steps"][1]["result"]["metrics"]["requestCount"] == 20
    assert final["steps"][1]["result"]["metrics"]["sourceRequestCount"] == 180
    assert final["steps"][3]["result"]["metrics"]["replicationLagSeconds"] == 0.3
    assert final["progress"] is None
    assert final["startedAt"] is not None and final["finishedAt"] is not None
    gcp, aws = connection_of(service, "gcp-seoul"), connection_of(service, "aws-seoul")
    assert (gcp["role"], gcp["trafficWeight"]) == ("SERVING", 100)
    assert (aws["role"], aws["trafficWeight"], aws["versionStatus"]) == ("STANDBY", 0, "IN_SYNC")
    assert service["activeMigrationPlanId"] is None

    # AC-2: 줄 순서·seq 빈틈 없음·단계 번호와 비율이 계획 값과 같음.
    assert [row.code for row in rows] == SUCCESS_LOG_CODES
    assert [row.seq for row in rows] == list(range(1, len(rows) + 1))
    applied = [row for row in rows if row.code == "WEIGHT_APPLIED"]
    assert [row.step for row in applied] == [1, 2, 4]
    for row in applied:
        assert row.step is not None
        step = plan["steps"][row.step]
        assert row.data == {
            "sourceWeight": step["sourceWeight"],
            "targetWeight": step["targetWeight"],
        }
    judged = [row for row in rows if row.code == "JUDGE_PASSED"]
    assert [row.step for row in judged] == [1, 2, 4]
    # AC-2(8-1): 각 비율 단계는 필요 건수 이상을 센 뒤에 통과했다.
    for judge_row in judged:
        assert judge_row.step is not None and judge_row.data is not None
        required = plan["steps"][judge_row.step]["requiredRequests"]
        assert judge_row.data["requestCount"] >= required
        assert judge_row.data["requiredRequests"] == required
        assert judge_row.data["lowTraffic"] is False
    # 예상(4·2·1초)대로 셌다: 적용 → 통과 시각 차.
    assert [
        (judge_row.logged_at - weight_row.logged_at).total_seconds()
        for weight_row, judge_row in zip(applied, judged, strict=True)
    ] == [4.0, 2.0, 1.0]
    assert "COUNTERS_RESET" not in [row.code for row in rows]
    assert migration.traffic_keys() == [f"{plan['id']}:1", f"{plan['id']}:2", f"{plan['id']}:4"]


# --- AC-8-1-2: 건수 경계 ---


async def test_shift_step_waits_until_target_counts_required_requests(
    migration: Migration,
) -> None:
    harness = migration.harness
    plan = await migration.create_plan()
    await migration.approve(plan["id"])
    metrics = harness.executors.metrics
    target_id = uuid.UUID(plan["targetServiceEnvironmentId"])
    metrics.freeze(target_id)
    worker = harness.worker()

    assert await worker.run_once()
    after_apply = await migration.plan_row(plan["id"])
    metrics.set_counts(target_id, 19)
    harness.clock.advance(1)
    assert await worker.run_once()
    at_nineteen = await migration.plan_row(plan["id"])
    probes_before = len(harness.executors.health.calls)
    metrics.set_counts(target_id, 20)
    harness.clock.advance(1)
    assert await worker.run_once()
    at_twenty = await migration.plan_row(plan["id"])

    assert (after_apply.current_step, after_apply.step_phase) == (1, "OBSERVE")
    assert after_apply.observe_until == after_apply.phase_started_at + timedelta(seconds=30)
    assert (at_nineteen.current_step, at_nineteen.step_phase) == (1, "OBSERVE")
    # 20건이 되는 실행에서 헬스체크 한 번 뒤 통과하고 다음 단계(50%)로 간다.
    assert len(harness.executors.health.calls) == probes_before + 1
    assert (at_twenty.current_step, at_twenty.step_phase) == (2, "OBSERVE")
    assert at_twenty.step_results[1]["metrics"]["requestCount"] == 20
    assert at_twenty.step_results[1]["judgeReason"] == "COUNT_REACHED"
    assert await migration.weights() == (50, 50)


# --- AC-8-1-9: 요청 검증 ---


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"observeSeconds": 5}, "observeSeconds"),
        ({"requiredRequests": [20, 50]}, "requiredRequests"),
        ({"requiredRequests": [20, 50, 50, 50]}, "requiredRequests"),
        ({"requiredRequests": [0, 50, 50]}, "requiredRequests.0"),
        ({"requiredRequests": [20, 50, 10001]}, "requiredRequests.2"),
        ({"maxWaitSeconds": 4}, "maxWaitSeconds"),
        ({"maxWaitSeconds": 301}, "maxWaitSeconds"),
        ({"simulate": {"failAtStep": 1, "reason": "LATENCY"}}, "simulate.reason"),
    ],
)
async def test_create_rejects_invalid_counting_fields(
    migration: Migration, body: dict[str, Any], field: str
) -> None:
    response = await migration.request_plan(**body)

    assert _error(response) == (422, "VALIDATION_ERROR"), response.text
    assert response.json()["details"][0]["field"] == field


async def test_create_uses_given_required_requests_and_max_wait(migration: Migration) -> None:
    plan = await migration.create_plan(requiredRequests=[100, 100, 100], maxWaitSeconds=10)
    row = await migration.plan_row(plan["id"])

    # 10%: min(10, ceil(100 ÷ 5)) = 10, 50%: ceil(100 ÷ 25) = 4, 100%: ceil(100 ÷ 50) = 2.
    assert [step["expectedSeconds"] for step in plan["steps"]] == [2, 12, 6, 2, 4]
    assert plan["expectedSeconds"] == 26
    assert [step["requiredRequests"] for step in plan["steps"]] == [None, 100, 100, None, 100]
    assert plan["maxWaitSeconds"] == 10
    # DB 칸 observe_seconds 에는 최대 대기를, steps JSON 에는 옛 파드용 observeSeconds 도 적는다.
    assert row.observe_seconds == 10
    assert [step.get("observeSeconds") for step in row.steps] == [None, 10, 10, None, 10]


# --- AC-4: 자기 승인 ---


async def test_self_approval_is_forbidden_by_default_and_allowed_by_admin_switch(
    migration: Migration,
) -> None:
    harness = migration.harness
    plan = await migration.create_plan()
    org_settings = org_path(harness.organization.id, "/settings")

    self_approve = await migration.act(plan["id"], "approve", client=migration.author_client)
    self_reject = await migration.act(plan["id"], "reject", client=migration.author_client)
    still_pending = await migration.get_plan(plan["id"])
    by_developer = await migration.author_client.patch(
        org_settings, json={"allowSelfApproval": True}
    )
    by_admin = await harness.client.patch(org_settings, json={"allowSelfApproval": True})
    organization = await harness.client.get(org_path(harness.organization.id))
    approved = await migration.act(plan["id"], "approve", client=migration.author_client)
    async with harness.session_factory() as session:
        reasons = list(await session.scalars(select(AuditLog.reason).order_by(AuditLog.created_at)))

    assert _error(self_approve) == (403, "SELF_APPROVAL_FORBIDDEN")
    assert _error(self_reject) == (403, "SELF_APPROVAL_FORBIDDEN")
    assert still_pending["status"] == "PENDING_APPROVAL"
    assert _error(by_developer) == (403, "ROLE_FORBIDDEN")
    assert by_admin.status_code == 200
    assert by_admin.json()["data"]["allowSelfApproval"] is True
    assert organization.json()["data"]["allowSelfApproval"] is True
    assert approved.status_code == 202, approved.text
    assert approved.json()["data"]["selfApproved"] is True
    assert approved.json()["data"]["approvedBy"]["login"] == "dev-a"
    assert await migration.log_codes(plan["id"]) == ["PLAN_CREATED", "SELF_APPROVED"]
    assert reasons == ["SELF_APPROVAL_FORBIDDEN", "SELF_APPROVAL_FORBIDDEN", "ROLE_FORBIDDEN"]


async def test_organization_settings_requires_body_and_new_orgs_default_to_false(
    harness: DeployHarness,
) -> None:
    org_settings = org_path(harness.organization.id, "/settings")

    missing = await harness.client.patch(org_settings, json={})
    created = await harness.client.post("/api/v1/orgs", json={"slug": "acme", "name": "Acme"})
    me = await harness.client.get("/api/v1/me")

    assert _error(missing) == (422, "VALIDATION_ERROR")
    assert created.json()["data"]["allowSelfApproval"] is False
    assert {item["allowSelfApproval"] for item in me.json()["data"]["organizations"]} == {False}


# --- AC-5: 낡은 계획 ---


async def test_new_deploy_after_plan_makes_approval_stale(migration: Migration) -> None:
    harness = migration.harness
    plan = await migration.create_plan()
    await deploy_and_finish(harness, migration.service_id, migration.aws.id, branch="release")

    response = await migration.act(plan["id"], "approve")
    stored = await migration.get_plan(plan["id"])
    rows = await migration.log_rows(plan["id"])

    assert _error(response) == (409, "PLAN_STALE")
    assert {"field": "connections", "reason": "CHANGED"} in response.json()["details"]
    assert stored["status"] == "STALE"
    assert stored["finishedAt"] is not None
    assert rows[-1].code == "STALE"
    assert rows[-1].data is not None and "connections" in rows[-1].data["changed"]
    assert await migration.plan_job(plan["id"]) is None


async def test_change_between_approval_and_start_makes_plan_stale_without_traffic(
    migration: Migration,
) -> None:
    plan = await migration.create_plan()
    await migration.approve(plan["id"])
    await migration.set_weights(99, 1)

    await migration.harness.run_until_idle()
    stored = await migration.get_plan(plan["id"])

    assert stored["status"] == "STALE"
    assert migration.traffic_keys() == []
    assert await migration.log_codes(plan["id"]) == ["PLAN_CREATED", "APPROVED", "STALE"]
    assert all(step["result"]["status"] == "SKIPPED" for step in stored["steps"])


async def test_target_environment_config_change_makes_approval_stale(
    migration: Migration,
) -> None:
    harness = migration.harness
    plan = await migration.create_plan()
    async with harness.session_factory() as session, session.begin():
        gcp = await session.get_one(Environment, migration.gcp.id)
        gcp.config = {**gcp.config, "cluster_name": "echo-gke-2"}

    response = await migration.act(plan["id"], "approve")

    assert _error(response) == (409, "PLAN_STALE")
    assert response.json()["details"] == [{"field": "targetEnvironment", "reason": "CHANGED"}]
    assert (await migration.get_plan(plan["id"]))["status"] == "STALE"


async def test_connection_recheck_alone_does_not_make_plan_stale(
    migration: Migration, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = migration.harness
    monkeypatch.setenv(secret_env_name("gcp-seoul"), "fake-gke-kubeconfig")
    plan = await migration.create_plan()
    harness.clock.advance(30)

    checked = await harness.client.post(
        f"{harness.environments}/{migration.gcp.id}/connection-check"
    )
    approved = await migration.act(plan["id"], "approve")

    assert checked.status_code == 200, checked.text
    assert checked.json()["data"]["connectionStatus"] == "CONNECTED"
    assert approved.status_code == 202, approved.text


# --- AC-6: 만들기 거절 ---


async def test_create_rejects_target_without_app(harness: DeployHarness) -> None:
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    gcp = await harness.add_environment("gcp-seoul")
    await deploy_and_finish(harness, service["id"], aws.id)

    response = await harness.client.post(
        f"{harness.services}/{service['id']}/migration-plans",
        json={"targetEnvironmentId": str(gcp.id)},
    )

    assert _error(response) == (409, "TARGET_NOT_READY")
    assert response.json()["details"] == [
        {"field": "targetEnvironmentId", "reason": "NO_DEPLOYMENT"}
    ]


async def test_create_rejects_target_on_other_version(migration: Migration) -> None:
    harness = migration.harness
    await deploy_and_finish(harness, migration.service_id, migration.gcp.id, commitSha="f" * 40)

    response = await migration.request_plan()

    assert _error(response) == (409, "TARGET_NOT_READY")
    assert response.json()["details"] == [
        {"field": "targetEnvironmentId", "reason": "VERSION_MISMATCH"}
    ]


async def test_create_rejects_target_whose_last_sync_failed_with_sync_error(
    migration: Migration,
) -> None:
    harness = migration.harness
    service = await harness.get_service(migration.service_id)
    gcp_connection = uuid.UUID(connection_of(service, "gcp-seoul")["serviceEnvironmentId"])
    harness.executors.deploy.fail_service_environment(gcp_connection)
    await deploy_and_finish(harness, migration.service_id, migration.aws.id, branch="release")

    response = await migration.request_plan()

    assert _error(response) == (409, "TARGET_NOT_READY")
    assert response.json()["details"] == [
        {"field": "targetEnvironmentId", "reason": "VERSION_MISMATCH"},
        {"field": "lastSyncError", "reason": "LAST_SYNC_FAILED", "value": "DEPLOY_FAILED"},
    ]


async def test_create_rejects_while_target_standby_is_syncing(migration: Migration) -> None:
    harness = migration.harness
    accepted = await harness.deploy(migration.service_id, migration.aws.id, branch="release")
    await harness.run_until(accepted["id"], {DeployRequestStatus.SUCCEEDED})
    service = await harness.get_service(migration.service_id)
    sync_id = connection_of(service, "gcp-seoul")["queuedRequestId"]

    response = await migration.request_plan()

    assert sync_id is not None
    assert _error(response) == (409, "TARGET_SYNCING")
    assert response.json()["details"] == [
        {"field": "deployRequestId", "reason": "TARGET_SYNCING", "value": sync_id}
    ]


async def test_create_rejects_open_deploy_request_on_source_or_target(
    migration: Migration,
) -> None:
    harness = migration.harness
    source_request = await harness.deploy(migration.service_id, migration.aws.id, branch="x")

    on_source = await migration.request_plan()
    await harness.run_until_idle()
    target_request = await harness.deploy(migration.service_id, migration.gcp.id, branch="main")
    on_target = await migration.request_plan()

    assert _error(on_source) == (409, "DEPLOY_IN_PROGRESS")
    assert on_source.json()["details"] == [
        {"field": "deployRequestId", "reason": "DEPLOY_IN_PROGRESS", "value": source_request["id"]}
    ]
    assert _error(on_target) == (409, "DEPLOY_IN_PROGRESS")
    assert on_target.json()["details"][0]["value"] == target_request["id"]


async def test_create_rejects_unless_exactly_one_source_at_full_weight(
    migration: Migration, harness: DeployHarness
) -> None:
    await migration.set_weights(90, 10)
    two_serving = await migration.request_plan()
    await migration.set_weights(0, 0)
    none_serving = await migration.request_plan()

    assert _error(two_serving) == (409, "SOURCE_NOT_SINGLE")
    assert _error(none_serving) == (409, "SOURCE_NOT_SINGLE")


async def test_create_rejects_same_environment_as_source(migration: Migration) -> None:
    response = await migration.request_plan(targetEnvironmentId=str(migration.aws.id))

    assert _error(response) == (422, "VALIDATION_ERROR")
    assert response.json()["details"] == [
        {"field": "targetEnvironmentId", "reason": "SAME_ENVIRONMENT"}
    ]


async def test_create_rejects_second_open_plan_and_replaces_expired_one(
    migration: Migration,
) -> None:
    harness = migration.harness
    first = await migration.create_plan()

    second = await migration.request_plan()
    harness.clock.advance(DAY_PLUS_ONE_SECOND)
    third = await migration.request_plan()
    expired = await migration.get_plan(first["id"])

    assert _error(second) == (409, "MIGRATION_PLAN_OPEN")
    assert second.json()["details"] == [
        {"field": "planId", "reason": "MIGRATION_PLAN_OPEN", "value": first["id"]}
    ]
    assert third.status_code == 201, third.text
    assert expired["status"] == "EXPIRED"
    assert (await migration.log_codes(first["id"]))[-1] == "EXPIRED"


async def test_create_rejects_deleting_service_and_unhealthy_target(
    migration: Migration,
) -> None:
    harness = migration.harness
    async with harness.session_factory() as session, session.begin():
        gcp = await session.get_one(Environment, migration.gcp.id)
        gcp.record_connection_check(harness.clock.now(), ConnectionErrorCode.AUTH_FAILED)
    unhealthy = await migration.request_plan()
    deleting = await harness.client.delete(f"{harness.services}/{migration.service_id}")
    on_deleting = await migration.request_plan()

    assert _error(unhealthy) == (409, "ENVIRONMENT_UNHEALTHY")
    assert deleting.status_code == 202
    assert _error(on_deleting) == (409, "SERVICE_DELETING")


async def test_create_rejects_unknown_service_or_environment(migration: Migration) -> None:
    harness = migration.harness
    no_service = await migration.author_client.post(
        f"{harness.services}/{uuid.uuid4()}/migration-plans",
        json={"targetEnvironmentId": str(migration.gcp.id)},
    )
    no_environment = await migration.request_plan(targetEnvironmentId=str(uuid.uuid4()))

    assert _error(no_service) == (404, "SERVICE_NOT_FOUND")
    assert _error(no_environment) == (404, "ENVIRONMENT_NOT_FOUND")


async def test_create_simulate_rules(migration: Migration) -> None:
    harness = migration.harness
    mismatch = await migration.request_plan(simulate={"failAtStep": 2, "reason": "DATA_LAG"})
    data_step = await migration.request_plan(simulate={"failAtStep": 3, "reason": "HEALTH"})
    low_traffic_on_data = await migration.request_plan(
        simulate={"failAtStep": 3, "reason": "LOW_TRAFFIC"}
    )
    # 단계 4 는 출발 비율이 0이라 도착 미수신을 만들 수 없다.
    not_receiving_last = await migration.request_plan(
        simulate={"failAtStep": 4, "reason": "TARGET_NOT_RECEIVING"}
    )
    latency = await migration.request_plan(simulate={"failAtStep": 1, "reason": "LATENCY"})
    preflight = await migration.request_plan(simulate={"failAtStep": 0, "reason": "HEALTH"})
    harness.use_settings(executor_mode=ExecutorMode.REAL)
    not_fake = await migration.request_plan(simulate={"failAtStep": 2, "reason": "ERROR_RATE"})

    for response in (mismatch, data_step, low_traffic_on_data, not_receiving_last):
        assert _error(response) == (422, "VALIDATION_ERROR"), response.text
        assert response.json()["details"][0]["reason"] == "SIMULATE_REASON_STEP_MISMATCH"
    assert _error(latency) == (422, "VALIDATION_ERROR")
    latency_detail = latency.json()["details"][0]
    assert (latency_detail["field"], latency_detail["reason"]) == (
        "simulate.reason",
        "SIMULATE_REASON_NOT_SUPPORTED",
    )
    assert _error(preflight) == (422, "VALIDATION_ERROR")
    assert _error(not_fake) == (422, "SIMULATE_NOT_ALLOWED")


@pytest.mark.parametrize(
    ("step", "reason"),
    [(1, "TARGET_NOT_RECEIVING"), (2, "TARGET_NOT_RECEIVING"), (4, "LOW_TRAFFIC")],
)
async def test_create_accepts_new_simulate_reasons_on_their_steps(
    migration: Migration, step: int, reason: str
) -> None:
    response = await migration.request_plan(simulate={"failAtStep": step, "reason": reason})

    assert response.status_code == 201, response.text


# --- AC-13: 실행 중 겹침 ---


async def test_running_migration_blocks_deploy_rollback_and_delete(migration: Migration) -> None:
    harness = migration.harness
    requests = await harness.list_requests(migration.service_id)
    plan = await migration.create_plan()
    await migration.approve(plan["id"])
    assert await harness.worker().run_once()
    running = await migration.get_plan(plan["id"])

    deploy = await harness.request_deploy(migration.service_id, migration.aws.id)
    rollback = await request_rollback(harness, requests[-1]["id"])
    delete = await harness.client.delete(f"{harness.services}/{migration.service_id}")
    service = await harness.get_service(migration.service_id)

    expected_details = [{"field": "planId", "reason": "MIGRATION_IN_PROGRESS", "value": plan["id"]}]
    assert running["status"] == "RUNNING"
    for response in (deploy, rollback, delete):
        assert _error(response) == (409, "MIGRATION_IN_PROGRESS")
        assert response.json()["details"] == expected_details
    assert service["activeMigrationPlanId"] == plan["id"]
    assert service["status"] == "ACTIVE"


async def test_pending_plan_does_not_block_deploy_but_becomes_stale(
    migration: Migration,
) -> None:
    plan = await migration.create_plan()

    deploy = await migration.harness.request_deploy(migration.service_id, migration.aws.id)
    approve = await migration.act(plan["id"], "approve")

    assert deploy.status_code == 202
    assert _error(approve) == (409, "PLAN_STALE")
    assert {"field": "openDeployRequests", "reason": "CHANGED"} in approve.json()["details"]


async def test_concurrent_creates_allow_only_one_plan(migration: Migration) -> None:
    first, second = await asyncio.gather(
        migration.request_plan(client=migration.author_client),
        migration.request_plan(client=migration.approver_client),
    )

    assert sorted([first.status_code, second.status_code]) == [201, 409]
    rejected = first if first.status_code == 409 else second
    assert rejected.json()["code"] == "MIGRATION_PLAN_OPEN"


# --- AC-14: 권한·조직 ---


async def test_roles_for_plan_actions_and_other_organization_is_hidden(
    migration: Migration,
) -> None:
    harness = migration.harness
    now = harness.clock.now()
    viewer = await create_member(
        harness.session_factory, harness.organization, OrganizationRole.VIEWER, now
    )
    _, agent_token = await create_agent_token(
        harness.session_factory, harness.organization, harness.admin.user, now
    )
    other = await create_organization(harness.session_factory, "acme")
    outsider = await create_member(harness.session_factory, other, OrganizationRole.ADMIN, now)
    plan = await migration.create_plan(client=harness.client)

    async with (
        make_client(session_token=viewer.session_token) as viewer_client,
        make_client(agent_token=agent_token) as agent,
        make_client(session_token=outsider.session_token) as outsider_client,
    ):
        viewer_create = await migration.request_plan(client=viewer_client)
        agent_create = await migration.request_plan(client=agent)
        agent_approve = await migration.act(plan["id"], "approve", client=agent)
        viewer_approve = await migration.act(plan["id"], "approve", client=viewer_client)
        reads = [
            await client.get(path)
            for client in (viewer_client, agent)
            for path in (
                migration.plan_path(plan["id"]),
                migration.plan_path(plan["id"], "/logs"),
                migration.plans_path,
            )
        ]
        hidden = await outsider_client.get(org_path(other.id, f"/migration-plans/{plan['id']}"))
    approved_by_admin = await migration.act(plan["id"], "approve", client=harness.client)

    assert _error(viewer_create) == (403, "ROLE_FORBIDDEN")
    assert _error(agent_create) == (403, "AGENT_READ_ONLY")
    assert _error(agent_approve) == (403, "AGENT_READ_ONLY")
    assert _error(viewer_approve) == (403, "ROLE_FORBIDDEN")
    assert [response.status_code for response in reads] == [200] * 6
    assert _error(hidden) == (404, "MIGRATION_PLAN_NOT_FOUND")
    # 어드민이 만든 계획을 같은 어드민이 승인하면 자기 승인이라 막힌다.
    assert _error(approved_by_admin) == (403, "SELF_APPROVAL_FORBIDDEN")
    approved_by_developer = await migration.act(plan["id"], "approve")
    assert approved_by_developer.status_code == 202


async def test_list_plans_newest_first_with_status_filter(migration: Migration) -> None:
    first = await migration.create_plan()
    await migration.act(first["id"], "cancel", client=migration.author_client)
    migration.harness.clock.advance(1)
    second = await migration.create_plan()

    listed = await migration.harness.client.get(migration.plans_path)
    pending = await migration.harness.client.get(
        migration.plans_path, params={"status": "PENDING_APPROVAL"}
    )

    page = listed.json()["data"]
    assert [item["id"] for item in page["items"]] == [second["id"], first["id"]]
    assert page["total"] == 2
    assert [item["status"] for item in page["items"]] == ["PENDING_APPROVAL", "CANCELLED"]
    assert [item["id"] for item in pending.json()["data"]["items"]] == [second["id"]]
    assert "steps" not in page["items"][0]


# --- AC-15: 만료 ---


@pytest.mark.parametrize("action", ["approve", "reject", "cancel"])
async def test_expired_plan_actions_return_409_and_store_expired(
    migration: Migration, action: str
) -> None:
    harness = migration.harness
    plan = await migration.create_plan()
    harness.clock.advance(DAY_PLUS_ONE_SECOND)

    before = await harness.get_service(migration.service_id)
    client = migration.author_client if action == "cancel" else migration.approver_client
    response = await migration.act(plan["id"], action, client=client)
    stored = await migration.get_plan(plan["id"])

    # 조회는 상태를 바꾸지 않지만 만료된 대기 계획은 세지 않는다.
    assert before["activeMigrationPlanId"] is None
    assert _error(response) == (409, "PLAN_EXPIRED")
    assert stored["status"] == "EXPIRED"
    assert await migration.log_codes(plan["id"]) == ["PLAN_CREATED", "EXPIRED"]


async def test_reject_and_cancel_close_pending_plan(migration: Migration) -> None:
    rejected = await migration.create_plan()
    reject = await migration.act(rejected["id"], "reject")
    cancelled = await migration.create_plan()
    cancel = await migration.act(cancelled["id"], "cancel", client=migration.author_client)
    again = await migration.act(cancelled["id"], "cancel", client=migration.author_client)

    assert reject.status_code == 200
    assert (reject.json()["data"]["status"], reject.json()["data"]["rejectedBy"]["login"]) == (
        "REJECTED",
        "dev-b",
    )
    assert cancel.status_code == 200
    assert cancel.json()["data"]["cancelledBy"]["login"] == "dev-a"
    assert _error(again) == (409, "PLAN_NOT_PENDING")
    assert await migration.log_codes(rejected["id"]) == ["PLAN_CREATED", "REJECTED"]


# --- AC-16: 예비 따라가기 표시 ---


async def test_standby_sync_timing_and_follow_after_migration(migration: Migration) -> None:
    harness = migration.harness
    accepted = await harness.deploy(migration.service_id, migration.aws.id, branch="release")
    await harness.run_until(accepted["id"], {DeployRequestStatus.SUCCEEDED})
    source = await harness.get_request(accepted["id"])
    service = await harness.get_service(migration.service_id)
    sync_id = connection_of(service, "gcp-seoul")["queuedRequestId"]
    async with harness.session_factory() as session:
        sync_run_after = await session.scalar(
            text("SELECT run_after FROM jobs WHERE subject_id = :id"), {"id": sync_id}
        )
    harness.clock.advance(3)
    await harness.run_until_idle()
    synced = await harness.get_request(sync_id)
    after_sync = await harness.get_service(migration.service_id)

    assert sync_run_after is not None
    assert sync_run_after.isoformat().replace("+00:00", "Z") == source["stageTimes"]["finishedAt"]
    assert synced["status"] == "SUCCEEDED"
    assert connection_of(after_sync, "gcp-seoul")["lastSyncSeconds"] == 3.0
    assert connection_of(after_sync, "aws-seoul")["lastSyncSeconds"] is None

    plan = await migration.create_plan()
    await migration.approve(plan["id"])
    await harness.run_until_idle()
    follow = await harness.deploy(migration.service_id, migration.gcp.id, branch="next")
    await harness.run_until(follow["id"], {DeployRequestStatus.SUCCEEDED})
    aws_syncs = await harness.list_requests(
        migration.service_id, environmentId=str(migration.aws.id), trigger="STANDBY_SYNC"
    )

    # 이전이 끝나 gcp 가 서비스 중이 되면, gcp 배포 성공을 aws(이제 예비)가 따라간다.
    assert (await migration.get_plan(plan["id"]))["status"] == "SUCCEEDED"
    assert [item["sourceRequestId"] for item in aws_syncs][:1] == [follow["id"]]


# --- AC-17: simulated ---


async def test_plan_and_logs_carry_stored_simulated_flag(migration: Migration) -> None:
    plan = await migration.create_plan()
    logs = await migration.logs(plan["id"])
    page = logs.json()["data"]
    paged = await migration.logs(plan["id"], cursor="0", limit=1)
    bad_cursor = await migration.logs(plan["id"], cursor="x")

    assert plan["simulated"] is True
    assert page["simulated"] is True
    assert [(item["seq"], item["code"], item["step"]) for item in page["items"]] == [
        (1, "PLAN_CREATED", None)
    ]
    assert page["items"][0]["data"] == {
        "expectedSeconds": 17,
        "requiredRequests": [20, 50, 50],
        "maxWaitSeconds": 30,
    }
    assert (page["nextCursor"], page["hasMore"]) == ("1", False)
    assert paged.json()["data"]["nextCursor"] == "1"
    assert _error(bad_cursor) == (422, "VALIDATION_ERROR")
    async with migration.harness.session_factory() as session:
        stored = await session.scalar(
            text("SELECT simulated FROM migration_plans WHERE id = :id"), {"id": plan["id"]}
        )
        count = await session.scalar(select(func.count()).select_from(AuditLog))
    assert stored is True
    assert count == 0
