"""되돌리기 통합 시험(SPEC api-4 AC-1~AC-6, AC-15 CHECK). 가짜 실행기·주입 시계·run_once()."""

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import ExecutorMode
from app.enums import ConnectionErrorCode, DeployRequestStatus, OrganizationRole
from app.executors.fake import FakeBuildExecutor
from app.models import AuditLog, Build, Deployment, DeployRequest, Environment, ServiceEnvironment
from app.worker.runner import Worker
from tests.integration.auth_support import (
    create_agent_token,
    create_member,
    create_organization,
    make_client,
    org_path,
)
from tests.integration.deploy_harness import DeployHarness, connection_of
from tests.integration.rollback_support import (
    SUFFIX,
    deploy_and_finish,
    log_codes,
    request_rollback,
    rollback,
)

TERMINAL = {
    DeployRequestStatus.SUCCEEDED,
    DeployRequestStatus.FAILED,
    DeployRequestStatus.CANCELLED,
}
ACTIVE_PER_CONNECTION_SQL = text(
    "SELECT max(n) FROM (SELECT count(*) AS n FROM deploy_requests "
    "WHERE status IN ('BUILDING', 'DEPLOYING', 'VERIFYING') "
    "GROUP BY service_environment_id) counts"
)


async def _setup(
    harness: DeployHarness,
) -> tuple[dict[str, Any], Environment, dict[str, Any], dict[str, Any]]:
    """aws 에 v1(main)·v2(release) 를 차례로 성공시킨다. v2 가 현재다."""
    harness.host_suffixes.queue(SUFFIX)
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    v1 = await deploy_and_finish(harness, service["id"], aws.id, branch="main")
    v2 = await deploy_and_finish(harness, service["id"], aws.id, branch="release")
    return service, aws, v1, v2


async def _count(harness: DeployHarness, model: type[Any], *conditions: Any) -> int:
    async with harness.session_factory() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


async def _row(harness: DeployHarness, model: type[Any], row_id: str | uuid.UUID) -> Any:
    async with harness.session_factory() as session:
        return await session.get_one(model, uuid.UUID(str(row_id)))


async def _connection(harness: DeployHarness, request: dict[str, Any]) -> ServiceEnvironment:
    row: ServiceEnvironment = await _row(
        harness, ServiceEnvironment, request["serviceEnvironmentId"]
    )
    return row


async def _hold_build(harness: DeployHarness, service_id: str, environment: Environment) -> Any:
    """새 커밋 요청 하나를 BUILDING 에 붙잡아 둔다(같은 연결의 다음 요청이 대기로 남게)."""
    busy = await harness.deploy(service_id, environment.id, branch="next")
    harness.replace_executors(
        build=FakeBuildExecutor(harness.clock, 0, stall_commits={busy["commitSha"]})
    )
    await harness.run_until(busy["id"], {DeployRequestStatus.BUILDING})
    for _ in range(3):
        await harness.worker().run_once()
    assert await harness.request_status(busy["id"]) == "BUILDING"
    return busy


def _release_build(harness: DeployHarness) -> None:
    harness.replace_executors(build=FakeBuildExecutor(harness.clock, 0))


# --- AC-1 되돌리기 성공 ---


async def test_rollback_restores_image_and_config_of_selected_success(
    harness: DeployHarness,
) -> None:
    harness.host_suffixes.queue(SUFFIX)
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    v1 = await deploy_and_finish(harness, service["id"], aws.id, branch="main")
    # v2 는 설정(헬스체크 제한 시간)도 다르다. 되돌리기는 v1 의 설정을 함께 복원해야 한다.
    patched = await harness.client.patch(
        f"{harness.services}/{service['id']}", json={"healthCheckTimeoutSeconds": 120}
    )
    assert patched.status_code == 200
    v2 = await deploy_and_finish(harness, service["id"], aws.id, branch="release")
    builds_before = await _count(harness, Build)

    accepted = await rollback(harness, v1["id"])
    trace = await harness.run_until(accepted["id"], TERMINAL)
    await harness.run_until_idle()
    finished = await harness.get_request(accepted["id"])
    v1_after = await harness.get_request(v1["id"])
    v2_after = await harness.get_request(v2["id"])
    v1_deployment = await _row(harness, Deployment, v1["deploymentId"])
    v2_deployment = await _row(harness, Deployment, v2["deploymentId"])
    restored = await _row(harness, Deployment, finished["deploymentId"])
    link = await _connection(harness, finished)

    assert (accepted["status"], accepted["trigger"]) == ("QUEUED", "ROLLBACK")
    assert accepted["rollbackOfRequestId"] == v1["id"]
    assert accepted["ref"] == {"type": "COMMIT", "value": v1["commitSha"]}
    assert accepted["commitSha"] == v1["commitSha"]
    assert accepted["build"]["id"] == v1["build"]["id"] and accepted["build"]["reused"] is True
    assert (accepted["isCurrent"], accepted["rollbackable"]) == (False, False)
    assert trace == ["QUEUED", "BUILDING", "DEPLOYING", "VERIFYING", "SUCCEEDED"]
    assert finished["status"] == "SUCCEEDED" and finished["simulated"] is True
    assert restored.image_digest == v1_deployment.image_digest
    assert restored.image_repository == v1_deployment.image_repository
    assert restored.config_snapshot == v1_deployment.config_snapshot
    assert restored.config_snapshot != v2_deployment.config_snapshot
    assert restored.rollback_of_id == v1_deployment.id
    assert restored.status == "SUCCEEDED"
    assert link.current_deployment_id == restored.id
    assert link.traffic_weight == 100
    assert await _count(harness, Build) == builds_before
    assert [call.idempotency_key for call in harness.executors.build.calls].count(
        v1["build"]["id"]
    ) == 1
    assert (finished["isCurrent"], finished["rollbackable"]) == (True, False)
    assert (v1_after["isCurrent"], v1_after["rollbackable"]) == (False, True)
    assert (v2_after["isCurrent"], v2_after["rollbackable"]) == (False, True)


async def test_rollback_of_a_rollback_or_standby_sync_restores_its_deployment(
    harness: DeployHarness,
) -> None:
    service, aws, v1, v2 = await _setup(harness)
    first = await rollback(harness, v1["id"])
    await harness.run_until_idle()

    again = await rollback(harness, v2["id"])
    await harness.run_until_idle()
    back = await rollback(harness, first["id"])
    await harness.run_until_idle()
    restored = await harness.get_request(back["id"])
    first_row = await _row(harness, DeployRequest, first["id"])
    deployment = await _row(harness, Deployment, restored["deploymentId"])

    assert (await harness.get_request(again["id"]))["status"] == "SUCCEEDED"
    assert restored["status"] == "SUCCEEDED"
    assert deployment.rollback_of_id == first_row.deployment_id
    assert restored["commitSha"] == v1["commitSha"]


# --- AC-2 접수 거절 ---


async def test_rollback_rejects_current_failed_cancelled_and_queued_requests(
    harness: DeployHarness,
) -> None:
    service, aws, v1, v2 = await _setup(harness)
    failed = await deploy_and_finish(
        harness, service["id"], aws.id, branch="bad", simulate={"failAt": "VERIFYING"}
    )
    replaced = await harness.deploy(service["id"], aws.id, branch="one")
    queued = await harness.deploy(service["id"], aws.id, branch="two")
    cancelled = await harness.get_request(replaced["id"])
    assert (cancelled["status"], queued["status"]) == ("CANCELLED", "QUEUED")

    responses = {
        "current": await request_rollback(harness, v2["id"]),
        "failed": await request_rollback(harness, failed["id"]),
        "cancelled": await request_rollback(harness, cancelled["id"]),
        "queued": await request_rollback(harness, queued["id"]),
    }

    assert {name: (r.status_code, r.json()["code"]) for name, r in responses.items()} == {
        "current": (409, "ALREADY_CURRENT"),
        "failed": (409, "NOT_ROLLBACKABLE"),
        "cancelled": (409, "NOT_ROLLBACKABLE"),
        "queued": (409, "NOT_ROLLBACKABLE"),
    }
    assert await _count(harness, DeployRequest, DeployRequest.trigger == "ROLLBACK") == 0


async def test_rollback_unknown_or_other_organization_request_returns_404(
    harness: DeployHarness,
) -> None:
    _, _, v1, _ = await _setup(harness)
    other = await create_organization(harness.session_factory, "other-org")
    outsider = await create_member(
        harness.session_factory, other, OrganizationRole.ADMIN, harness.clock.now()
    )

    unknown = await request_rollback(harness, str(uuid.uuid4()))
    async with make_client(session_token=outsider.session_token) as client:
        foreign = await client.post(org_path(other.id, f"/deploy-requests/{v1['id']}/rollback"))

    assert (unknown.status_code, unknown.json()["code"]) == (404, "DEPLOY_REQUEST_NOT_FOUND")
    assert (foreign.status_code, foreign.json()["code"]) == (404, "DEPLOY_REQUEST_NOT_FOUND")


async def test_rollback_on_deleting_or_deleted_service(harness: DeployHarness) -> None:
    service, _, v1, _ = await _setup(harness)
    deleting = await harness.client.delete(f"{harness.services}/{service['id']}")
    assert deleting.status_code == 202

    while_deleting = await request_rollback(harness, v1["id"])
    await harness.run_until_idle()
    after_delete = await request_rollback(harness, v1["id"])

    assert (while_deleting.status_code, while_deleting.json()["code"]) == (
        409,
        "SERVICE_DELETING",
    )
    assert (after_delete.status_code, after_delete.json()["code"]) == (404, "SERVICE_NOT_FOUND")


async def test_rollback_to_unhealthy_environment_returns_409(harness: DeployHarness) -> None:
    _, aws, v1, _ = await _setup(harness)
    async with harness.session_factory() as session, session.begin():
        environment = await session.get_one(Environment, aws.id)
        environment.record_connection_check(harness.clock.now(), ConnectionErrorCode.AUTH_FAILED)

    response = await request_rollback(harness, v1["id"])

    assert (response.status_code, response.json()["code"]) == (409, "ENVIRONMENT_UNHEALTHY")


@pytest.mark.parametrize(
    "body",
    [
        {"simulate": {"failAt": "BUILDING"}},
        {"simulate": {"failAt": "TEARDOWN"}},
        {"simulate": {}},
        {"reason": "oops"},
    ],
)
async def test_rollback_invalid_body_returns_422(
    harness: DeployHarness, body: dict[str, Any]
) -> None:
    _, _, v1, _ = await _setup(harness)

    response = await request_rollback(harness, v1["id"], **body)

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


async def test_rollback_simulate_with_non_fake_executor_returns_422(
    harness: DeployHarness,
) -> None:
    _, _, v1, _ = await _setup(harness)
    harness.use_settings(executor_mode=ExecutorMode.REAL)

    response = await request_rollback(harness, v1["id"], simulate={"failAt": "VERIFYING"})

    assert (response.status_code, response.json()["code"]) == (422, "SIMULATE_NOT_ALLOWED")


# --- AC-3 권한 ---


async def test_rollback_permissions_by_role_and_agent(harness: DeployHarness) -> None:
    service, _, v1, v2 = await _setup(harness)
    now = harness.clock.now()
    team = harness.organization
    viewer = await create_member(harness.session_factory, team, OrganizationRole.VIEWER, now)
    developer = await create_member(harness.session_factory, team, OrganizationRole.DEVELOPER, now)
    _, agent_token = await create_agent_token(
        harness.session_factory, team, harness.admin.user, now
    )
    path = f"{harness.deploy_requests}/{v1['id']}/rollback"

    async with (
        make_client(session_token=viewer.session_token) as viewer_client,
        make_client(session_token=developer.session_token) as developer_client,
        make_client(agent_token=agent_token) as agent_client,
    ):
        denied_viewer = await viewer_client.post(path)
        denied_agent = await agent_client.post(path)
        by_developer = await developer_client.post(path)
        by_admin = await harness.client.post(path)
        reads = [
            await client.get(url)
            for client in (viewer_client, agent_client)
            for url in (
                f"{harness.services}/{service['id']}/deploy-requests",
                f"{harness.deploy_requests}/{v1['id']}",
                f"{harness.deploy_requests}/{v2['id']}/logs?source=DEPLOY_JOB",
                f"{harness.deploy_requests}/{v2['id']}/logs?source=BUILD",
                f"{harness.deploy_requests}/{v2['id']}/logs?source=RUNTIME",
            )
        ]
    async with harness.session_factory() as session:
        audits = list((await session.scalars(select(AuditLog))).all())

    assert (denied_viewer.status_code, denied_viewer.json()["code"]) == (403, "ROLE_FORBIDDEN")
    assert (denied_agent.status_code, denied_agent.json()["code"]) == (403, "AGENT_READ_ONLY")
    assert (by_developer.status_code, by_admin.status_code) == (202, 202)
    assert [response.status_code for response in reads] == [200] * len(reads)
    assert sorted(audit.reason for audit in audits) == ["AGENT_READ_ONLY", "ROLE_FORBIDDEN"]
    assert {audit.action for audit in audits} == {
        "POST /api/v1/orgs/{org_id}/deploy-requests/{deploy_request_id}/rollback"
    }
    # 개발자가 접수한 대기 요청은 어드민의 새 요청에 밀린다(실행 1 + 대기 1).
    first = await harness.get_request(by_developer.json()["data"]["id"])
    assert (first["status"], first["cancelReason"]) == ("CANCELLED", "SUPERSEDED")


# --- AC-4 예비 따라가기 ---


async def _serving_and_standby(
    harness: DeployHarness,
) -> tuple[dict[str, Any], Environment, Environment, dict[str, Any], dict[str, Any]]:
    """aws 서비스 중 + gcp 예비. 둘 다 v2(release). (서비스, aws, gcp, aws v1, gcp v1)."""
    harness.host_suffixes.queue(SUFFIX)
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    gcp = await harness.add_environment("gcp-seoul")
    aws_v1 = await deploy_and_finish(harness, service["id"], aws.id, branch="main")
    gcp_v1 = await deploy_and_finish(harness, service["id"], gcp.id, branch="main")
    await deploy_and_finish(harness, service["id"], aws.id, branch="release")
    detail = await harness.get_service(service["id"])
    assert connection_of(detail, "gcp-seoul")["versionStatus"] == "IN_SYNC"
    assert connection_of(detail, "gcp-seoul")["role"] == "STANDBY"
    return service, aws, gcp, aws_v1, gcp_v1


async def test_serving_rollback_success_makes_standby_follow_same_build(
    harness: DeployHarness,
) -> None:
    service, aws, gcp, aws_v1, _ = await _serving_and_standby(harness)
    builds_before = await _count(harness, Build)

    accepted = await rollback(harness, aws_v1["id"])
    await harness.run_until_idle()
    syncs = await harness.list_requests(
        service["id"], environmentId=str(gcp.id), trigger="STANDBY_SYNC"
    )
    following = [item for item in syncs if item["sourceRequestId"] == accepted["id"]]
    assert len(following) == 1
    sync = await harness.get_request(following[0]["id"])
    detail = await harness.get_service(service["id"])

    assert sync["status"] == "SUCCEEDED"
    assert sync["build"]["id"] == aws_v1["build"]["id"] and sync["build"]["reused"] is True
    assert sync["commitSha"] == aws_v1["commitSha"]
    assert await _count(harness, Build) == builds_before
    standby = connection_of(detail, "gcp-seoul")
    assert standby["versionStatus"] == "IN_SYNC"
    assert standby["currentDeployment"]["commitSha"] == aws_v1["commitSha"]
    assert connection_of(detail, "aws-seoul")["role"] == "SERVING"


async def test_standby_direct_rollback_does_not_create_standby_sync(
    harness: DeployHarness,
) -> None:
    service, aws, gcp, _, gcp_v1 = await _serving_and_standby(harness)
    syncs_before = await _count(harness, DeployRequest, DeployRequest.trigger == "STANDBY_SYNC")
    aws_requests_before = len(await harness.list_requests(service["id"], environmentId=str(aws.id)))

    accepted = await rollback(harness, gcp_v1["id"])
    await harness.run_until_idle()
    finished = await harness.get_request(accepted["id"])
    detail = await harness.get_service(service["id"])

    assert finished["status"] == "SUCCEEDED"
    assert await _count(harness, DeployRequest, DeployRequest.trigger == "STANDBY_SYNC") == (
        syncs_before
    )
    aws_requests = await harness.list_requests(service["id"], environmentId=str(aws.id))
    assert len(aws_requests) == aws_requests_before
    assert connection_of(detail, "gcp-seoul")["role"] == "STANDBY"
    assert connection_of(detail, "gcp-seoul")["versionStatus"] == "OUT_OF_SYNC"
    assert "STANDBY_SYNC_REQUESTED" not in await log_codes(harness, accepted["id"])


async def test_serving_rollback_while_service_deleting_creates_no_standby_sync(
    harness: DeployHarness,
) -> None:
    service, _, _, aws_v1, _ = await _serving_and_standby(harness)
    accepted = await rollback(harness, aws_v1["id"])
    await harness.run_until(accepted["id"], {DeployRequestStatus.VERIFYING})
    deleting = await harness.client.delete(f"{harness.services}/{service['id']}")
    assert deleting.status_code == 202

    await harness.run_until_idle()
    finished = await _row(harness, DeployRequest, accepted["id"])

    assert finished.status == "SUCCEEDED"
    assert (
        await _count(
            harness,
            DeployRequest,
            DeployRequest.source_request_id == uuid.UUID(accepted["id"]),
        )
        == 0
    )


# --- AC-5 되돌린 버전이 안 뜸 ---


@pytest.mark.parametrize(
    ("fail_at", "error_code"),
    [("VERIFYING", "HEALTH_CHECK_FAILED"), ("DEPLOYING", "DEPLOY_FAILED")],
)
async def test_rollback_failure_keeps_current_deployment_and_records_failure(
    harness: DeployHarness, fail_at: str, error_code: str
) -> None:
    service, aws, gcp, aws_v1, _ = await _serving_and_standby(harness)
    current_before = (await _connection(harness, aws_v1)).current_deployment_id
    syncs_before = await _count(harness, DeployRequest, DeployRequest.trigger == "STANDBY_SYNC")

    accepted = await rollback(harness, aws_v1["id"], simulate={"failAt": fail_at})
    await harness.run_until_idle()
    failed = await harness.get_request(accepted["id"])
    link = await _connection(harness, failed)
    deployment = await _row(harness, Deployment, failed["deploymentId"])
    codes = await log_codes(harness, accepted["id"])

    assert (failed["status"], failed["errorCode"]) == ("FAILED", error_code)
    assert failed["revertStatus"] == "SUCCEEDED"
    assert deployment.status == "FAILED"
    assert (link.current_deployment_id, link.traffic_weight) == (current_before, 100)
    assert await _count(harness, DeployRequest, DeployRequest.trigger == "STANDBY_SYNC") == (
        syncs_before
    )
    assert codes[-2:] == ["FAILED", "REVERT_SUCCEEDED"]
    assert not any(code.startswith("HEALTH_PROBE") for code in codes)
    assert (await harness.get_request(aws_v1["id"]))["rollbackable"] is True


async def test_rollback_injected_deploy_failure_keeps_current(harness: DeployHarness) -> None:
    _, _, v1, v2 = await _setup(harness)
    link_before = await _connection(harness, v2)
    harness.executors.deploy.fail_service_environment(link_before.id)

    accepted = await rollback(harness, v1["id"])
    await harness.run_until_idle()
    failed = await harness.get_request(accepted["id"])
    link = await _connection(harness, v2)

    assert (failed["errorCode"], failed["revertStatus"]) == ("DEPLOY_FAILED", "SUCCEEDED")
    assert link.current_deployment_id == link_before.current_deployment_id
    assert (await harness.get_request(v2["id"]))["isCurrent"] is True


# --- AC-6 상황 변화 ---


async def test_rollback_started_after_another_success_is_cancelled_base_changed(
    harness: DeployHarness,
) -> None:
    service, aws, v1, _ = await _setup(harness)
    busy = await _hold_build(harness, service["id"], aws)
    accepted = await rollback(harness, v1["id"])
    deployments_before = await _count(harness, Deployment)

    _release_build(harness)
    await harness.run_until_idle()
    busy_after = await harness.get_request(busy["id"])
    cancelled = await harness.get_request(accepted["id"])
    link = await _connection(harness, busy_after)

    assert busy_after["status"] == "SUCCEEDED"
    assert (cancelled["status"], cancelled["cancelReason"]) == ("CANCELLED", "BASE_CHANGED")
    assert cancelled["deploymentId"] is None
    assert link.current_deployment_id == uuid.UUID(busy_after["deploymentId"])
    assert await _count(harness, Deployment) == deployments_before + 1
    assert await log_codes(harness, accepted["id"]) == [
        "REQUEST_ACCEPTED",
        "CANCELLED_BASE_CHANGED",
    ]


async def test_queued_rollback_is_superseded_by_newer_request(harness: DeployHarness) -> None:
    service, aws, v1, _ = await _setup(harness)
    await _hold_build(harness, service["id"], aws)
    queued_rollback = await rollback(harness, v1["id"])

    newer = await harness.deploy(service["id"], aws.id, branch="newer")
    superseded = await harness.get_request(queued_rollback["id"])

    assert (superseded["status"], superseded["cancelReason"]) == ("CANCELLED", "SUPERSEDED")
    assert superseded["supersededById"] == newer["id"]
    assert await log_codes(harness, queued_rollback["id"]) == [
        "REQUEST_ACCEPTED",
        "CANCELLED_SUPERSEDED",
    ]


async def test_two_rollbacks_in_a_row_keep_one_waiting(harness: DeployHarness) -> None:
    service, aws, v1, v2 = await _setup(harness)
    third = await deploy_and_finish(harness, service["id"], aws.id, branch="third")
    await _hold_build(harness, service["id"], aws)

    first = await rollback(harness, v1["id"])
    second = await rollback(harness, v2["id"])
    queued = await _count(
        harness,
        DeployRequest,
        DeployRequest.service_environment_id == uuid.UUID(third["serviceEnvironmentId"]),
        DeployRequest.status == "QUEUED",
    )

    assert queued == 1
    assert (await harness.get_request(first["id"]))["cancelReason"] == "SUPERSEDED"
    assert (await harness.get_request(second["id"]))["status"] == "QUEUED"


async def test_accept_and_worker_start_race_applies_only_one_with_one_log_line(
    harness: DeployHarness,
) -> None:
    service, aws, v1, _ = await _setup(harness)
    queued = await harness.deploy(service["id"], aws.id, branch="next")

    results = await asyncio.gather(harness.worker().run_once(), request_rollback(harness, v1["id"]))
    codes = await log_codes(harness, queued["id"])
    status = await harness.request_status(queued["id"])

    assert results[1].status_code == 202
    if status == "CANCELLED":
        assert codes == ["REQUEST_ACCEPTED", "CANCELLED_SUPERSEDED"]
    else:
        assert status == "BUILDING"
        assert codes[:2] == ["REQUEST_ACCEPTED", "STARTED"]
        assert "CANCELLED_SUPERSEDED" not in codes


async def test_two_workers_with_concurrent_deploys_and_rollbacks_never_run_two(
    harness: DeployHarness,
) -> None:
    service, aws, v1, _ = await _setup(harness)
    workers = [harness.worker("worker-a"), harness.worker("worker-b")]
    observed: list[int] = []
    stop = asyncio.Event()

    async def sample() -> None:
        while not stop.is_set():
            async with harness.session_factory() as session:
                observed.append(await session.scalar(ACTIVE_PER_CONNECTION_SQL) or 0)
            await asyncio.sleep(0)

    async def drive(worker: Worker) -> None:
        for _ in range(60):
            if not await worker.run_once():
                harness.clock.advance(0.1)

    async def submit() -> list[int]:
        statuses = []
        for index in range(10):
            if index % 2:
                response = await request_rollback(harness, v1["id"])
            else:
                response = await harness.request_deploy(
                    service["id"], aws.id, branch=f"change-{index}"
                )
            statuses.append(response.status_code)
            await asyncio.sleep(0)
        return statuses

    sampler = asyncio.create_task(sample())
    results = await asyncio.gather(drive(workers[0]), drive(workers[1]), submit())
    await harness.run_until_idle()
    stop.set()
    await sampler
    requests = await harness.list_requests(service["id"], size=100)

    assert set(results[2]) <= {202, 409}
    assert observed and max(observed) <= 1
    assert {item["status"] for item in requests} <= {"SUCCEEDED", "CANCELLED"}
    assert {item["cancelReason"] for item in requests if item["status"] == "CANCELLED"} <= {
        "SUPERSEDED",
        "BASE_CHANGED",
    }
    assert sum(item["isCurrent"] for item in requests) == 1


# --- AC-15 CHECK ---


@pytest.mark.parametrize(
    ("trigger", "set_rollback_columns", "constraint"),
    [
        # 두 칸 CHECK 중 어느 쪽이 먼저 걸릴지는 PostgreSQL 이 정한다.
        ("ROLLBACK", False, "ck_deploy_requests_rollback_(of_request|base)_consistency"),
        ("MANUAL", True, "ck_deploy_requests_rollback_(of_request|base)_consistency"),
    ],
)
async def test_direct_insert_with_inconsistent_rollback_columns_is_rejected(
    harness: DeployHarness, trigger: str, set_rollback_columns: bool, constraint: str
) -> None:
    _, _, v1, _ = await _setup(harness)
    columns = (
        ", rollback_of_request_id, rollback_base_deployment_id" if set_rollback_columns else ""
    )
    values = ", id, deployment_id" if set_rollback_columns else ""

    async with harness.session_factory() as session:
        with pytest.raises(IntegrityError, match=constraint):
            await session.execute(
                text(
                    "INSERT INTO deploy_requests (service_id, environment_id, "
                    "service_environment_id, trigger, ref_type, ref_value, repo_url, commit_sha, "
                    f"status, config_snapshot, simulated, finished_at{columns}) "
                    "SELECT service_id, environment_id, service_environment_id, :trigger, "
                    "ref_type, ref_value, repo_url, commit_sha, 'SUCCEEDED', config_snapshot, "
                    f"simulated, now(){values} FROM deploy_requests WHERE id = :id"
                ),
                {"trigger": trigger, "id": v1["id"]},
            )


async def test_direct_insert_rollback_without_base_is_rejected(harness: DeployHarness) -> None:
    _, _, v1, _ = await _setup(harness)

    async with harness.session_factory() as session:
        with pytest.raises(IntegrityError, match="ck_deploy_requests_rollback_base_consistency"):
            await session.execute(
                text(
                    "INSERT INTO deploy_requests (service_id, environment_id, "
                    "service_environment_id, trigger, ref_type, ref_value, repo_url, commit_sha, "
                    "status, config_snapshot, simulated, rollback_of_request_id, finished_at) "
                    "SELECT service_id, environment_id, service_environment_id, 'ROLLBACK', "
                    "ref_type, ref_value, repo_url, commit_sha, 'SUCCEEDED', config_snapshot, "
                    "simulated, id, now() FROM deploy_requests WHERE id = :id"
                ),
                {"id": v1["id"]},
            )
