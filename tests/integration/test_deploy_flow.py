"""원터치 배포 흐름 통합 시험(SPEC api-3 AC-2~AC-6, AC-11). 가짜 실행기·주입 시계·run_once()."""

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import ExecutorMode
from app.enums import DeployRequestStatus
from app.executors.base import HealthHttpError, HealthUnreachable
from app.executors.fake import FakeBuildExecutor, FakeDeployExecutor
from app.models import Build, Deployment, DeployRequest, Environment, ServiceEnvironment
from tests.integration.deploy_harness import (
    DeployHarness,
    ScriptedHealthChecker,
    connection_of,
)

TERMINAL = {DeployRequestStatus.SUCCEEDED, DeployRequestStatus.FAILED}
# _setup 이 shop-api 를 만들 때 주입하는 주소 suffix.
SUFFIX = "a1b2c3d4"
MAIN_URL = f"https://shop-api-{SUFFIX}.apps.echo.test"
AWS_URL = f"https://shop-api-aws-seoul-{SUFFIX}.apps.echo.test"
DETAIL_KEYS = {
    "id",
    "serviceId",
    "environment",
    "serviceEnvironmentId",
    "trigger",
    "sourceRequestId",
    "ref",
    "repoUrl",
    "commitSha",
    "status",
    "cancelReason",
    "supersededById",
    "errorCode",
    "errorMessage",
    "revertStatus",
    "build",
    "deploymentId",
    "urls",
    "stageTimes",
    "simulated",
    # API 서버 4: 이력 칸
    "isCurrent",
    "rollbackable",
    "rollbackOfRequestId",
    # API 서버 4-1: 실패 원인·컨테이너 진단
    "failure",
    # 이슈 #98: AI 진단 실행 연결
    "diagnosis",
    # 이슈 #99: 예비 환경 함께 띄우기(칸 이름 가칭)
    "standbyEnvironmentIds",
    "standbyDeployRequestIds",
}


async def _setup(harness: DeployHarness) -> tuple[dict[str, Any], Environment, Environment]:
    harness.host_suffixes.queue(SUFFIX)
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    onprem = await harness.add_environment("onprem-k3s")
    return service, aws, onprem


async def _deploy_and_finish(
    harness: DeployHarness, service_id: str, environment: Environment, **ref: Any
) -> dict[str, Any]:
    accepted = await harness.deploy(service_id, environment.id, **ref)
    await harness.run_until_idle()
    return await harness.get_request(accepted["id"])


async def _count(harness: DeployHarness, model: type[Any], *conditions: Any) -> int:
    async with harness.session_factory() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


# --- AC-2 원터치 배포 ---


async def test_deploy_branch_to_aws_runs_all_stages_and_serves_main_url(
    harness: DeployHarness,
) -> None:
    service, aws, _ = await _setup(harness)

    accepted = await harness.deploy(service["id"], aws.id, branch="main")
    trace = await harness.run_until(accepted["id"], TERMINAL)
    await harness.run_until_idle()
    finished = await harness.get_request(accepted["id"])
    detail = await harness.get_service(service["id"])

    assert set(accepted) == DETAIL_KEYS
    assert accepted["status"] == "QUEUED"
    assert len(accepted["commitSha"]) == 40
    assert accepted["ref"] == {"type": "BRANCH", "value": "main"}
    assert trace == ["QUEUED", "BUILDING", "DEPLOYING", "VERIFYING", "SUCCEEDED"]
    assert finished["urls"] == {"main": MAIN_URL, "environment": AWS_URL}
    assert finished["simulated"] is True
    assert finished["errorCode"] is None and finished["revertStatus"] is None
    assert finished["build"]["status"] == "SUCCEEDED"
    assert finished["build"]["reused"] is False
    assert finished["build"]["imageDigest"].startswith("sha256:")
    assert finished["build"]["imageRepository"] == "registry.echo.test/shop-api"
    assert all(finished["stageTimes"][key] for key in finished["stageTimes"])

    connection = connection_of(detail, "aws-seoul")
    assert connection["role"] == "SERVING"
    assert connection["trafficWeight"] == 100
    assert connection["url"] == AWS_URL
    assert connection["currentDeployment"]["id"] == finished["deploymentId"]
    assert connection["currentDeployment"]["commitSha"] == accepted["commitSha"]
    assert connection["simulated"] is True
    async with harness.session_factory() as session:
        link = await session.get_one(
            ServiceEnvironment, uuid.UUID(connection["serviceEnvironmentId"])
        )
        deployment = await session.get_one(Deployment, uuid.UUID(finished["deploymentId"]))
    assert link.traffic_applied_at == harness.clock.now()
    assert deployment.status == "SUCCEEDED"
    assert deployment.image_digest == finished["build"]["imageDigest"]
    assert deployment.config_snapshot["environmentUrl"] == finished["urls"]["environment"]
    assert harness.executors.main_route.calls[0].idempotency_key == finished["deploymentId"]


async def test_deploy_short_commit_sha_is_fixed_to_40_chars(harness: DeployHarness) -> None:
    service, aws, _ = await _setup(harness)

    accepted = await harness.deploy(service["id"], aws.id, commitSha="abc1234")

    assert accepted["ref"] == {"type": "COMMIT", "value": "abc1234"}
    assert len(accepted["commitSha"]) == 40


# --- AC-3 예비 ---


async def test_deploy_second_environment_same_commit_becomes_standby_and_reuses_build(
    harness: DeployHarness,
) -> None:
    service, aws, onprem = await _setup(harness)
    first = await _deploy_and_finish(harness, service["id"], aws)

    second = await _deploy_and_finish(harness, service["id"], onprem)
    detail = await harness.get_service(service["id"])

    assert second["status"] == "SUCCEEDED"
    assert second["build"]["id"] == first["build"]["id"]
    assert second["build"]["reused"] is True
    assert await _count(harness, Build) == 1
    assert connection_of(detail, "aws-seoul")["role"] == "SERVING"
    standby = connection_of(detail, "onprem-k3s")
    assert (standby["role"], standby["trafficWeight"]) == ("STANDBY", 0)
    assert standby["versionStatus"] == "IN_SYNC"
    assert len(harness.executors.main_route.calls) == 1


# --- AC-4 자동 따라가기 ---


async def test_new_commit_on_serving_creates_standby_sync_with_same_build(
    harness: DeployHarness,
) -> None:
    service, aws, onprem = await _setup(harness)
    await _deploy_and_finish(harness, service["id"], aws)
    await _deploy_and_finish(harness, service["id"], onprem)

    source = await _deploy_and_finish(harness, service["id"], aws, branch="release")
    onprem_requests = await harness.list_requests(service["id"], environmentId=str(onprem.id))
    syncs = [item for item in onprem_requests if item["trigger"] == "STANDBY_SYNC"]
    assert len(syncs) == 1
    sync = await harness.get_request(syncs[0]["id"])
    detail = await harness.get_service(service["id"])

    assert source["status"] == "SUCCEEDED"
    assert sync["trigger"] == "STANDBY_SYNC"
    assert sync["sourceRequestId"] == source["id"]
    assert sync["status"] == "SUCCEEDED"
    assert sync["commitSha"] == source["commitSha"]
    assert sync["build"]["id"] == source["build"]["id"] and sync["build"]["reused"] is True
    assert await _count(harness, Build) == 2
    async with harness.session_factory() as session:
        source_row = await session.get_one(DeployRequest, uuid.UUID(source["id"]))
        sync_row = await session.get_one(DeployRequest, uuid.UUID(sync["id"]))
    differing = {
        key
        for key in source_row.config_snapshot
        if source_row.config_snapshot[key] != sync_row.config_snapshot[key]
    }
    assert differing == {"environmentUrl"}
    assert (
        sync_row.config_snapshot["environmentUrl"]
        == f"https://shop-api-onprem-k3s-{SUFFIX}.apps.echo.test"
    )
    standby = connection_of(detail, "onprem-k3s")
    assert standby["versionStatus"] == "IN_SYNC"
    assert standby["lastSyncError"] is None
    assert standby["currentDeployment"]["commitSha"] == source["commitSha"]


async def test_standby_sync_failure_marks_out_of_sync_and_keeps_serving(
    harness: DeployHarness,
) -> None:
    service, aws, onprem = await _setup(harness)
    await _deploy_and_finish(harness, service["id"], aws)
    await _deploy_and_finish(harness, service["id"], onprem)
    standby_id = uuid.UUID(
        connection_of(await harness.get_service(service["id"]), "onprem-k3s")[
            "serviceEnvironmentId"
        ]
    )
    harness.executors.deploy.fail_service_environment(standby_id)

    source = await _deploy_and_finish(harness, service["id"], aws, branch="release")
    detail = await harness.get_service(service["id"])

    standby = connection_of(detail, "onprem-k3s")
    serving = connection_of(detail, "aws-seoul")
    assert source["status"] == "SUCCEEDED"
    assert standby["versionStatus"] == "OUT_OF_SYNC"
    assert standby["lastSyncError"] == "DEPLOY_FAILED"
    assert standby["role"] == "STANDBY"
    assert serving["role"] == "SERVING" and serving["trafficWeight"] == 100
    assert serving["currentDeployment"]["id"] == source["deploymentId"]


async def test_standby_sync_supersedes_queued_manual_request_on_standby(
    harness: DeployHarness,
) -> None:
    service, aws, onprem = await _setup(harness)
    await _deploy_and_finish(harness, service["id"], aws)
    await _deploy_and_finish(harness, service["id"], onprem)
    standby_link = uuid.UUID(
        connection_of(await harness.get_service(service["id"]), "onprem-k3s")[
            "serviceEnvironmentId"
        ]
    )
    # 예비 연결에 실행 중 요청을 하나 붙잡아 두어, 수동 요청이 확실히 대기(QUEUED)로 남게 한다.
    harness.replace_executors(
        deploy=FakeDeployExecutor(harness.clock, 0, stall_service_environment_ids={standby_link})
    )
    busy = await harness.deploy(service["id"], onprem.id, branch="slow")
    await harness.run_until(busy["id"], {DeployRequestStatus.DEPLOYING})
    serving = await harness.deploy(service["id"], aws.id, branch="release")
    manual = await harness.deploy(service["id"], onprem.id, branch="feature-x")

    await harness.run_until(serving["id"], {DeployRequestStatus.SUCCEEDED})
    manual_after = await harness.get_request(manual["id"])

    assert manual_after["status"] == "CANCELLED"
    assert manual_after["cancelReason"] == "SUPERSEDED"
    sync = await harness.get_request(manual_after["supersededById"])
    assert (sync["trigger"], sync["status"]) == ("STANDBY_SYNC", "QUEUED")
    assert sync["sourceRequestId"] == serving["id"]


# --- AC-5 헬스체크 ---


async def test_simulated_health_failure_reverts_and_keeps_current_deployment(
    harness: DeployHarness,
) -> None:
    service, aws, onprem = await _setup(harness)
    first = await _deploy_and_finish(harness, service["id"], aws)
    await _deploy_and_finish(harness, service["id"], onprem)
    requests_before = len(await harness.list_requests(service["id"]))

    failed = await _deploy_and_finish(
        harness, service["id"], aws, branch="bad", simulate={"failAt": "VERIFYING"}
    )
    detail = await harness.get_service(service["id"])

    assert (failed["status"], failed["errorCode"]) == ("FAILED", "HEALTH_CHECK_FAILED")
    assert failed["errorMessage"] == "The health check did not return a 2xx response."
    assert failed["revertStatus"] == "SUCCEEDED"
    serving = connection_of(detail, "aws-seoul")
    assert serving["currentDeployment"]["id"] == first["deploymentId"]
    assert serving["trafficWeight"] == 100
    assert len(await harness.list_requests(service["id"])) == requests_before + 1
    async with harness.session_factory() as session:
        deployment = await session.get_one(Deployment, uuid.UUID(failed["deploymentId"]))
    assert deployment.status == "FAILED"
    assert harness.executors.deploy.reverts[-1].idempotency_key == failed["deploymentId"]


async def test_health_check_retries_after_interval_and_then_succeeds(
    harness: DeployHarness,
) -> None:
    service, aws, _ = await _setup(harness)
    checker = ScriptedHealthChecker([HealthHttpError(503), HealthUnreachable()])
    harness.replace_executors(health=checker)

    accepted = await harness.deploy(service["id"], aws.id)
    await harness.run_until(accepted["id"], {DeployRequestStatus.VERIFYING})
    worker = harness.worker()
    await worker.run_once()
    harness.clock.advance(4)
    picked_early = await worker.run_once()
    harness.clock.advance(1)
    await harness.run_until_idle()

    assert picked_early is False
    assert (await harness.get_request(accepted["id"]))["status"] == "SUCCEEDED"
    assert checker.calls == [f"{AWS_URL}/healthz"] * 3


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ([HealthHttpError(500)], "HEALTH_CHECK_FAILED"),
        ([HealthUnreachable()], "PUBLIC_URL_UNREACHABLE"),
        ([HealthHttpError(500), HealthUnreachable()], "PUBLIC_URL_UNREACHABLE"),
        ([HealthUnreachable(), HealthHttpError(404)], "HEALTH_CHECK_FAILED"),
    ],
)
async def test_health_check_deadline_uses_last_result(
    harness: DeployHarness, script: list[Any], expected: str
) -> None:
    service, aws, _ = await _setup(harness)
    harness.replace_executors(health=ScriptedHealthChecker(script, default=script[-1]))
    response = await harness.client.patch(
        f"{harness.services}/{service['id']}", json={"healthCheckTimeoutSeconds": 10}
    )
    assert response.status_code == 200

    failed = await _deploy_and_finish(harness, service["id"], aws)

    assert (failed["status"], failed["errorCode"]) == ("FAILED", expected)
    assert failed["revertStatus"] == "SUCCEEDED"


async def test_health_check_never_probed_before_deadline_times_out(
    harness: DeployHarness,
) -> None:
    service, aws, _ = await _setup(harness)
    accepted = await harness.deploy(service["id"], aws.id)
    await harness.run_until(accepted["id"], {DeployRequestStatus.VERIFYING})

    harness.clock.advance(301)
    await harness.run_until_idle()
    failed = await harness.get_request(accepted["id"])

    assert (failed["status"], failed["errorCode"]) == ("FAILED", "HEALTH_CHECK_TIMEOUT")
    assert harness.executors.health.calls == []


# --- AC-6 빌드·배포 실패 ---


async def test_simulated_build_failure_creates_no_build_and_leaves_shared_build(
    harness: DeployHarness,
) -> None:
    service, aws, onprem = await _setup(harness)
    good = await harness.deploy(service["id"], aws.id, branch="main")
    bad = await harness.deploy(
        service["id"], onprem.id, branch="main", simulate={"failAt": "BUILDING"}
    )

    await harness.run_until_idle()
    good_after = await harness.get_request(good["id"])
    bad_after = await harness.get_request(bad["id"])

    assert (bad_after["status"], bad_after["errorCode"]) == ("FAILED", "BUILD_FAILED")
    assert bad_after["build"] is None and bad_after["revertStatus"] is None
    assert good_after["status"] == "SUCCEEDED"
    assert await _count(harness, Build) == 1


async def test_simulated_deploy_failure_reverts(harness: DeployHarness) -> None:
    service, aws, _ = await _setup(harness)

    failed = await _deploy_and_finish(harness, service["id"], aws, simulate={"failAt": "DEPLOYING"})
    detail = await harness.get_service(service["id"])

    assert (failed["status"], failed["errorCode"]) == ("FAILED", "DEPLOY_FAILED")
    assert failed["revertStatus"] == "SUCCEEDED"
    assert connection_of(detail, "aws-seoul")["role"] == "NONE"


async def test_injected_build_failure_fails_all_waiting_requests_then_new_build(
    harness: DeployHarness,
) -> None:
    service, aws, onprem = await _setup(harness)
    first = await harness.deploy(service["id"], aws.id, branch="main")
    # 빌드가 5초 걸리게 해 두 요청이 같은 빌드에 붙은 뒤 실패하게 한다.
    harness.replace_executors(
        build=FakeBuildExecutor(harness.clock, 5, fail_commits={first["commitSha"]})
    )
    second = await harness.deploy(service["id"], onprem.id, branch="main")

    await harness.run_until_idle()
    results = [await harness.get_request(item["id"]) for item in (first, second)]
    harness.replace_executors(build=FakeBuildExecutor(harness.clock, 0))
    retried = await _deploy_and_finish(harness, service["id"], aws, branch="main")

    assert [(item["status"], item["errorCode"]) for item in results] == [
        ("FAILED", "BUILD_FAILED"),
        ("FAILED", "BUILD_FAILED"),
    ]
    assert results[0]["build"]["id"] == results[1]["build"]["id"]
    assert results[0]["build"]["status"] == "FAILED"
    assert results[0]["revertStatus"] is None
    assert retried["status"] == "SUCCEEDED"
    assert retried["build"]["id"] != results[0]["build"]["id"]
    assert await _count(harness, Build, Build.commit_sha == first["commitSha"]) == 2


async def test_injected_deploy_failure_fails_with_deploy_failed(harness: DeployHarness) -> None:
    service, aws, _ = await _setup(harness)
    accepted = await harness.deploy(service["id"], aws.id)
    async with harness.session_factory() as session:
        request = await session.get_one(DeployRequest, uuid.UUID(accepted["id"]))
    harness.replace_executors(
        deploy=FakeDeployExecutor(
            harness.clock, 0, fail_service_environment_ids={request.service_environment_id}
        )
    )

    await harness.run_until_idle()
    failed = await harness.get_request(accepted["id"])

    assert (failed["errorCode"], failed["revertStatus"]) == ("DEPLOY_FAILED", "SUCCEEDED")


async def test_revert_failure_is_recorded_without_changing_error_code(
    harness: DeployHarness,
) -> None:
    service, aws, _ = await _setup(harness)
    harness.replace_executors(deploy=FakeDeployExecutor(harness.clock, 0, fail_revert=True))

    failed = await _deploy_and_finish(harness, service["id"], aws, simulate={"failAt": "VERIFYING"})

    assert (failed["errorCode"], failed["revertStatus"]) == ("HEALTH_CHECK_FAILED", "FAILED")


# --- AC-11 접수 거절 ---


async def test_deploy_to_failed_environment_returns_409(harness: DeployHarness) -> None:
    service = await harness.create_service("shop-api")
    failed = await harness.add_environment("aws-seoul", failed=True)

    response = await harness.request_deploy(service["id"], failed.id)

    assert response.status_code == 409
    assert response.json()["code"] == "ENVIRONMENT_UNHEALTHY"


async def test_deploy_to_deleted_or_unknown_environment_returns_404(
    harness: DeployHarness,
) -> None:
    service = await harness.create_service("shop-api")
    deleted = await harness.add_environment("aws-seoul", deleted=True)

    responses = [
        await harness.request_deploy(service["id"], deleted.id),
        await harness.request_deploy(service["id"], uuid.uuid4()),
    ]

    assert [(item.status_code, item.json()["code"]) for item in responses] == [
        (404, "ENVIRONMENT_NOT_FOUND"),
        (404, "ENVIRONMENT_NOT_FOUND"),
    ]


async def test_deploy_unknown_service_returns_404(harness: DeployHarness) -> None:
    aws = await harness.add_environment("aws-seoul")

    response = await harness.request_deploy(str(uuid.uuid4()), aws.id)

    assert (response.status_code, response.json()["code"]) == (404, "SERVICE_NOT_FOUND")


async def test_deploy_missing_ref_returns_422(harness: DeployHarness) -> None:
    service, aws, _ = await _setup(harness)

    response = await harness.request_deploy(service["id"], aws.id, branch="missing-branch")

    assert (response.status_code, response.json()["code"]) == (422, "REF_NOT_FOUND")
    assert await _count(harness, ServiceEnvironment) == 0


@pytest.mark.parametrize(
    "ref",
    [
        {"branch": "main", "commitSha": "abc1234"},
        {"simulate": {"failAt": "BUILDING"}},
        {"branch": "feature..x"},
        {"branch": "bad branch"},
        {"branch": "topic/"},
        {"branch": "x.lock"},
        {"commitSha": "ABC1234"},
        {"commitSha": "abc12"},
        {"branch": "main", "simulate": {"failAt": "TEARDOWN"}},
    ],
)
async def test_deploy_invalid_ref_or_simulate_returns_422(
    harness: DeployHarness, ref: dict[str, Any]
) -> None:
    service, aws, _ = await _setup(harness)

    response = await harness.request_deploy(service["id"], aws.id, **ref)

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


async def test_deploy_simulate_with_non_fake_executor_returns_422(harness: DeployHarness) -> None:
    service, aws, _ = await _setup(harness)
    harness.use_settings(executor_mode=ExecutorMode.REAL)

    response = await harness.request_deploy(
        service["id"], aws.id, branch="main", simulate={"failAt": "BUILDING"}
    )

    assert (response.status_code, response.json()["code"]) == (422, "SIMULATE_NOT_ALLOWED")


async def test_deploy_public_host_conflict_after_environment_rename_returns_409(
    harness: DeployHarness,
) -> None:
    service, aws, _ = await _setup(harness)
    await harness.deploy(service["id"], aws.id)
    async with harness.session_factory() as session, session.begin():
        renamed = await session.get_one(Environment, aws.id)
        renamed.name = "aws-old"
    reused_name = await harness.add_environment("aws-seoul")

    response = await harness.request_deploy(service["id"], reused_name.id)

    assert (response.status_code, response.json()["code"]) == (409, "PUBLIC_HOST_CONFLICT")


# --- 조회·이력 ---


async def test_search_deploy_requests_filters_by_environment_newest_first(
    harness: DeployHarness,
) -> None:
    service, aws, onprem = await _setup(harness)
    first = await harness.deploy(service["id"], aws.id)
    harness.clock.advance(1)
    second = await harness.deploy(service["id"], onprem.id)
    harness.clock.advance(1)
    third = await harness.deploy(service["id"], aws.id, branch="next")

    everything = await harness.list_requests(service["id"])
    aws_only = await harness.list_requests(service["id"], environmentId=str(aws.id))

    assert [item["id"] for item in everything] == [third["id"], second["id"], first["id"]]
    assert [item["id"] for item in aws_only] == [third["id"], first["id"]]


async def test_get_deploy_request_unknown_returns_404(harness: DeployHarness) -> None:
    response = await harness.client.get(f"{harness.deploy_requests}/{uuid.uuid4()}")

    assert (response.status_code, response.json()["code"]) == (404, "DEPLOY_REQUEST_NOT_FOUND")


async def test_deploy_requests_rows_cannot_be_deleted(harness: DeployHarness) -> None:
    service, aws, _ = await _setup(harness)
    await harness.deploy(service["id"], aws.id)

    async with harness.session_factory() as session:
        with pytest.raises(IntegrityError, match="deploy_requests rows are append-only"):
            await session.execute(text("DELETE FROM deploy_requests"))
