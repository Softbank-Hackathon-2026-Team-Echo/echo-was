"""서비스 삭제(모든 환경에서 내리기 → 소프트 삭제) 통합 시험(SPEC api-3 AC-10, AC-9 내리기)."""

import uuid
from typing import Any

from sqlalchemy import select

from app.enums import DeployRequestStatus
from app.executors.fake import FakeBuildExecutor, FakeTeardownExecutor
from app.models import Deployment, DeployRequest, Environment, Service, ServiceEnvironment
from tests.integration.deploy_harness import DeployHarness, connection_of


async def _two_environment_service(
    harness: DeployHarness,
) -> tuple[dict[str, Any], Environment, Environment]:
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    onprem = await harness.add_environment("onprem-k3s")
    for environment in (aws, onprem):
        await harness.deploy(service["id"], environment.id)
        await harness.run_until_idle()
    return service, aws, onprem


async def _delete(harness: DeployHarness, service_id: str) -> dict[str, Any]:
    response = await harness.client.delete(f"{harness.services}/{service_id}")
    assert response.status_code == 202, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def _connections(harness: DeployHarness, service_id: str) -> list[ServiceEnvironment]:
    async with harness.session_factory() as session:
        rows = await session.scalars(
            select(ServiceEnvironment)
            .where(ServiceEnvironment.service_id == uuid.UUID(service_id))
            .order_by(ServiceEnvironment.created_at)
        )
        return list(rows.all())


async def test_delete_service_cancels_queued_waits_active_then_removes_everything(
    harness: DeployHarness,
) -> None:
    service, aws, _ = await _two_environment_service(harness)
    harness.replace_executors(build=FakeBuildExecutor(harness.clock, 5))
    active = await harness.deploy(service["id"], aws.id, branch="next")
    await harness.run_until(active["id"], {DeployRequestStatus.BUILDING})
    queued = await harness.deploy(service["id"], aws.id, branch="later")

    deleting = await _delete(harness, service["id"])
    rejected_patch = await harness.client.patch(
        f"{harness.services}/{service['id']}", json={"description": "x"}
    )
    rejected_deploy = await harness.request_deploy(service["id"], aws.id)
    queued_after = await harness.get_request(queued["id"])
    await harness.run_until_idle()
    active_after = await harness.get_request(active["id"])
    gone = await harness.client.get(f"{harness.services}/{service['id']}")
    recreated = await harness.client.post(
        harness.services,
        json={"name": "shop-api", "repoUrl": "https://github.com/team-echo/shop-api"},
    )

    assert (deleting["status"], deleting["deleteError"]) == ("DELETING", None)
    assert (rejected_patch.status_code, rejected_patch.json()["code"]) == (409, "SERVICE_DELETING")
    assert (rejected_deploy.status_code, rejected_deploy.json()["code"]) == (
        409,
        "SERVICE_DELETING",
    )
    assert (queued_after["status"], queued_after["cancelReason"]) == (
        "CANCELLED",
        "SERVICE_DELETED",
    )
    assert active_after["status"] == "SUCCEEDED"
    assert (gone.status_code, gone.json()["code"]) == (404, "SERVICE_NOT_FOUND")
    assert recreated.status_code == 201
    connections = await _connections(harness, service["id"])
    assert [item.is_deleted for item in connections] == [True, True]
    assert [call.idempotency_key for call in harness.executors.teardown.calls] == [
        f"{item.id}:0" for item in sorted(connections, key=lambda item: item.id)
    ]
    async with harness.session_factory() as session:
        stored = await session.get_one(Service, uuid.UUID(service["id"]))
        deployments = (await session.scalars(select(Deployment))).all()
    assert stored.is_deleted and stored.deleted_at is not None
    assert len(deployments) == 3


async def test_success_while_deleting_creates_no_standby_sync(harness: DeployHarness) -> None:
    service, aws, onprem = await _two_environment_service(harness)
    active = await harness.deploy(service["id"], aws.id, branch="next")
    await harness.run_until(active["id"], {DeployRequestStatus.VERIFYING})

    await _delete(harness, service["id"])
    await harness.run_until_idle()

    async with harness.session_factory() as session:
        syncs = (
            await session.scalars(
                select(DeployRequest).where(DeployRequest.trigger == "STANDBY_SYNC")
            )
        ).all()
    assert (await harness.request_status(active["id"])) == "SUCCEEDED"
    assert syncs == []


async def test_teardown_failure_keeps_deleting_and_retry_removes_only_remaining(
    harness: DeployHarness,
) -> None:
    service, aws, onprem = await _two_environment_service(harness)
    detail = await harness.get_service(service["id"])
    onprem_link = uuid.UUID(connection_of(detail, "onprem-k3s")["serviceEnvironmentId"])
    aws_link = uuid.UUID(connection_of(detail, "aws-seoul")["serviceEnvironmentId"])
    teardown = FakeTeardownExecutor(harness.clock, 0, fail_service_environment_ids={onprem_link})
    harness.replace_executors(teardown=teardown)

    await _delete(harness, service["id"])
    await harness.run_until_idle()
    stuck = await harness.get_service(service["id"])
    teardown.recover_service_environment(onprem_link)
    retried = await _delete(harness, service["id"])
    await harness.run_until_idle()
    gone = await harness.client.get(f"{harness.services}/{service['id']}")

    assert (stuck["status"], stuck["deleteError"]) == ("DELETING", "TEARDOWN_FAILED")
    assert [item["environment"]["name"] for item in stuck["environments"]] == ["onprem-k3s"]
    assert stuck["environments"][0]["teardownError"] == "TEARDOWN_FAILED"
    assert (retried["deleteError"], retried["environments"][0]["teardownError"]) == (None, None)
    assert gone.status_code == 404
    keys = [call.idempotency_key for call in teardown.calls]
    assert keys.count(f"{aws_link}:0") == 1
    assert keys.count(f"{onprem_link}:0") == 1
    assert keys.count(f"{onprem_link}:1") == 1


async def test_teardown_timeout_records_delete_error(harness: DeployHarness) -> None:
    service, _, onprem = await _two_environment_service(harness)
    onprem_link = uuid.UUID(
        connection_of(await harness.get_service(service["id"]), "onprem-k3s")[
            "serviceEnvironmentId"
        ]
    )
    harness.replace_executors(
        teardown=FakeTeardownExecutor(harness.clock, 0, stall_service_environment_ids={onprem_link})
    )

    await _delete(harness, service["id"])
    for _ in range(4):
        await harness.worker().run_once()
    harness.clock.advance(harness.settings.teardown_timeout_seconds + 1)
    await harness.run_until_idle()
    stuck = await harness.get_service(service["id"])

    assert (stuck["status"], stuck["deleteError"]) == ("DELETING", "TEARDOWN_TIMEOUT")
    assert stuck["environments"][0]["teardownError"] == "TEARDOWN_TIMEOUT"


async def test_teardown_job_losing_lease_three_times_records_worker_lost(
    harness: DeployHarness,
) -> None:
    service, _, onprem = await _two_environment_service(harness)
    onprem_link = uuid.UUID(
        connection_of(await harness.get_service(service["id"]), "onprem-k3s")[
            "serviceEnvironmentId"
        ]
    )
    harness.replace_executors(
        teardown=FakeTeardownExecutor(harness.clock, 0, stall_service_environment_ids={onprem_link})
    )
    await _delete(harness, service["id"])
    worker = harness.worker()

    for _ in range(3):
        assert await worker.claim_next_job() is not None
        harness.clock.advance(31)
    last = await worker.claim_next_job()
    assert last is not None
    await worker.process(last)
    stuck = await harness.get_service(service["id"])

    assert last.lease_losses == 3
    assert (stuck["status"], stuck["deleteError"]) == ("DELETING", "WORKER_LOST")
    assert await harness.open_job_count() == 0


async def test_delete_unknown_service_returns_404(harness: DeployHarness) -> None:
    response = await harness.client.delete(f"{harness.services}/{uuid.uuid4()}")

    assert (response.status_code, response.json()["code"]) == (404, "SERVICE_NOT_FOUND")


async def test_deleting_service_is_listed_and_readable(harness: DeployHarness) -> None:
    service, _, _ = await _two_environment_service(harness)
    await _delete(harness, service["id"])

    listed = await harness.client.get(harness.services)
    detail = await harness.get_service(service["id"])

    assert [item["status"] for item in listed.json()["data"]["items"]] == ["DELETING"]
    assert detail["status"] == "DELETING"
