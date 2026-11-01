"""배포 겹침·일꾼 복구·제한 시간 통합 시험(SPEC api-3 AC-7 순차 부분, AC-8, AC-9)."""

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.enums import DeployRequestStatus, JobStatus
from app.executors.base import BuildSpec, SourceAccess
from app.executors.fake import FakeBuildExecutor, FakeDeployExecutor
from app.models import Build, DeployRequest, Environment, Job
from app.repositories.deploy_request_repository import DeployRequestRepository
from tests.integration.deploy_harness import DeployHarness

LEASE_SECONDS = 30


class WorkerCrashError(Exception):
    """시험용: 외부 호출 직후(external_ref 저장 전) 일꾼이 죽은 것처럼 멈춘다."""


class CrashAfterFirstStartBuildExecutor(FakeBuildExecutor):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._crashed = False

    async def start(self, build: BuildSpec, source: SourceAccess, idempotency_key: str) -> str:
        external_ref = await super().start(build, source, idempotency_key)
        if not self._crashed:
            self._crashed = True
            raise WorkerCrashError
        return external_ref


async def _setup(harness: DeployHarness) -> tuple[dict[str, Any], Environment]:
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    return service, aws


async def _request_row(harness: DeployHarness, request_id: str) -> DeployRequest:
    async with harness.session_factory() as session:
        return await session.get_one(DeployRequest, uuid.UUID(request_id))


async def _job_for(harness: DeployHarness, subject_id: str) -> Job:
    async with harness.session_factory() as session:
        job = await session.scalar(
            select(Job)
            .where(Job.subject_id == uuid.UUID(subject_id))
            .order_by(Job.created_at.desc())
        )
    assert job is not None
    return job


# --- AC-7 실행 1 + 대기 1 ---


async def test_new_request_supersedes_queued_and_waits_for_active(harness: DeployHarness) -> None:
    service, aws = await _setup(harness)
    first = await harness.deploy(service["id"], aws.id, branch="main")
    harness.replace_executors(
        build=FakeBuildExecutor(harness.clock, 0, stall_commits={first["commitSha"]})
    )
    await harness.run_until(first["id"], {DeployRequestStatus.BUILDING})
    for _ in range(3):
        await harness.worker().run_once()

    second = await harness.deploy(service["id"], aws.id, branch="second")
    third = await harness.deploy(service["id"], aws.id, branch="third")
    for _ in range(5):
        await harness.worker().run_once()
        harness.clock.advance(1)
    states_while_first_builds = (
        await harness.request_status(first["id"]),
        await harness.request_status(third["id"]),
    )
    harness.replace_executors(build=FakeBuildExecutor(harness.clock, 0))
    await harness.run_until_idle()
    first_row, second_row, third_row = [
        await _request_row(harness, item["id"]) for item in (first, second, third)
    ]

    assert states_while_first_builds == ("BUILDING", "QUEUED")
    assert (second_row.status, second_row.cancel_reason) == ("CANCELLED", "SUPERSEDED")
    assert second_row.superseded_by_id == third_row.id
    assert (await _job_for(harness, second["id"])).status == JobStatus.CANCELLED
    assert first_row.status == third_row.status == "SUCCEEDED"
    assert first_row.finished_at is not None and third_row.building_at is not None
    assert third_row.building_at >= first_row.finished_at


async def test_worker_start_and_supersede_race_applies_only_one(harness: DeployHarness) -> None:
    service, aws = await _setup(harness)
    accepted = await harness.deploy(service["id"], aws.id)
    request_id = uuid.UUID(accepted["id"])
    now = harness.clock.now()

    async def transition(next_status: DeployRequestStatus, values: dict[str, Any]) -> bool:
        async with harness.session_factory() as session, session.begin():
            return await DeployRequestRepository(session).transition(
                request_id,
                DeployRequestStatus.QUEUED,
                {"status": next_status, "updated_at": now, **values},
            )

    results = await asyncio.gather(
        transition(DeployRequestStatus.BUILDING, {"started_at": now, "building_at": now}),
        transition(
            DeployRequestStatus.CANCELLED, {"cancel_reason": "SUPERSEDED", "finished_at": now}
        ),
    )
    row = await _request_row(harness, accepted["id"])

    assert sorted(results) == [False, True]
    assert row.status == ("BUILDING" if results[0] else "CANCELLED")


@pytest.mark.parametrize(
    ("status", "index"),
    [
        ("BUILDING", "uq_deploy_requests_service_environment_id_active"),
        ("QUEUED", "uq_deploy_requests_service_environment_id_queued"),
    ],
)
async def test_direct_insert_bypassing_rules_is_rejected_by_partial_unique(
    harness: DeployHarness, status: str, index: str
) -> None:
    service, aws = await _setup(harness)
    accepted = await harness.deploy(service["id"], aws.id)
    if status == "BUILDING":
        await harness.run_until(accepted["id"], {DeployRequestStatus.BUILDING})

    async with harness.session_factory() as session:
        with pytest.raises(IntegrityError, match=index):
            await session.execute(
                text(
                    "INSERT INTO deploy_requests (service_id, environment_id, "
                    "service_environment_id, trigger, ref_type, ref_value, repo_url, commit_sha, "
                    "status, config_snapshot, simulated, building_at, started_at) "
                    "SELECT service_id, environment_id, service_environment_id, trigger, "
                    "ref_type, ref_value, repo_url, commit_sha, :status, config_snapshot, "
                    "simulated, now(), now() FROM deploy_requests WHERE id = :id"
                ),
                {"status": status, "id": accepted["id"]},
            )


# --- AC-8 복구 ---


async def test_worker_crash_after_build_start_resumes_with_same_idempotency_key(
    harness: DeployHarness,
) -> None:
    service, aws = await _setup(harness)
    crashing = CrashAfterFirstStartBuildExecutor(harness.clock, 0)
    harness.replace_executors(build=crashing)
    accepted = await harness.deploy(service["id"], aws.id)
    first_worker = harness.worker("worker-a")

    with pytest.raises(WorkerCrashError):
        for _ in range(10):
            await first_worker.run_once()
    crashed_job = await _job_for(harness, accepted["id"])
    harness.clock.advance(LEASE_SECONDS + 1)
    second_worker = harness.worker("worker-b")
    resumed = await second_worker.claim_next_job()
    assert resumed is not None
    await second_worker.process(resumed)
    await harness.run_until_idle()
    finished = await harness.get_request(accepted["id"])

    assert crashed_job.status == JobStatus.RUNNING
    assert (resumed.id, resumed.lease_losses) == (crashed_job.id, 1)
    assert finished["status"] == "SUCCEEDED"
    build_id = finished["build"]["id"]
    assert [call.idempotency_key for call in crashing.calls] == [build_id, build_id]
    assert crashing.operation_keys == [build_id]


async def test_stale_lease_token_result_is_discarded(harness: DeployHarness) -> None:
    service, aws = await _setup(harness)
    accepted = await harness.deploy(service["id"], aws.id)
    first_worker = harness.worker("worker-a")
    second_worker = harness.worker("worker-b")

    stale = await first_worker.claim_next_job()
    harness.clock.advance(LEASE_SECONDS + 1)
    fresh = await second_worker.claim_next_job()
    assert stale is not None and fresh is not None
    fresh_applied = await second_worker.process(fresh)
    stale_applied = await first_worker.process(stale)
    row = await _request_row(harness, accepted["id"])
    job = await _job_for(harness, accepted["id"])

    assert fresh.lease_token != stale.lease_token
    assert (fresh_applied, stale_applied) == (True, False)
    assert row.status == "BUILDING"
    assert row.build_id is None
    assert job.status == JobStatus.QUEUED


async def test_three_lease_losses_fail_with_worker_lost_and_revert(
    harness: DeployHarness,
) -> None:
    service, aws = await _setup(harness)
    accepted = await harness.deploy(service["id"], aws.id)
    request = await _request_row(harness, accepted["id"])
    harness.replace_executors(
        deploy=FakeDeployExecutor(
            harness.clock, 0, stall_service_environment_ids={request.service_environment_id}
        )
    )
    await harness.run_until(accepted["id"], {DeployRequestStatus.DEPLOYING})
    worker = harness.worker()

    losses = []
    for _ in range(3):
        claimed = await worker.claim_next_job()
        assert claimed is not None
        losses.append(claimed.lease_losses)
        harness.clock.advance(LEASE_SECONDS + 1)
    last = await worker.claim_next_job()
    assert last is not None
    await worker.process(last)
    failed = await harness.get_request(accepted["id"])
    job = await _job_for(harness, accepted["id"])

    assert losses == [0, 1, 2]
    assert last.lease_losses == 3
    assert (failed["status"], failed["errorCode"]) == ("FAILED", "WORKER_LOST")
    assert failed["revertStatus"] == "SUCCEEDED"
    assert (job.status, job.last_error_code) == (JobStatus.FAILED, "WORKER_LOST")


async def test_worker_lost_while_building_closes_orphan_build_for_next_request(
    harness: DeployHarness,
) -> None:
    service, aws = await _setup(harness)
    accepted = await harness.deploy(service["id"], aws.id)
    harness.replace_executors(
        build=FakeBuildExecutor(harness.clock, 0, stall_commits={accepted["commitSha"]})
    )
    await harness.run_until(accepted["id"], {DeployRequestStatus.BUILDING})
    for _ in range(3):
        await harness.worker().run_once()
    harness.clock.advance(harness.settings.worker_poll_interval_seconds)
    worker = harness.worker()
    for _ in range(3):
        assert await worker.claim_next_job() is not None
        harness.clock.advance(LEASE_SECONDS + 1)
    last = await worker.claim_next_job()
    assert last is not None
    await worker.process(last)
    lost = await harness.get_request(accepted["id"])
    harness.replace_executors(build=FakeBuildExecutor(harness.clock, 0))
    retried = await harness.deploy(service["id"], aws.id)
    await harness.run_until_idle()
    retried_after = await harness.get_request(retried["id"])

    assert (lost["errorCode"], lost["revertStatus"]) == ("WORKER_LOST", None)
    assert lost["build"]["status"] == "FAILED"
    assert retried_after["status"] == "SUCCEEDED"
    assert retried_after["build"]["id"] != lost["build"]["id"]


# --- AC-9 제한 시간 ---


async def test_build_timeout_closes_build_and_next_request_creates_new_build(
    harness: DeployHarness,
) -> None:
    service, aws = await _setup(harness)
    accepted = await harness.deploy(service["id"], aws.id)
    harness.replace_executors(
        build=FakeBuildExecutor(harness.clock, 0, stall_commits={accepted["commitSha"]})
    )
    await harness.run_until(accepted["id"], {DeployRequestStatus.BUILDING})
    for _ in range(3):
        await harness.worker().run_once()

    harness.clock.advance(harness.settings.build_timeout_seconds + 1)
    await harness.run_until_idle()
    timed_out = await harness.get_request(accepted["id"])
    harness.replace_executors(build=FakeBuildExecutor(harness.clock, 0))
    retried = await harness.deploy(service["id"], aws.id)
    await harness.run_until_idle()
    retried_after = await harness.get_request(retried["id"])

    assert (timed_out["errorCode"], timed_out["build"]["status"]) == ("BUILD_TIMEOUT", "FAILED")
    assert retried_after["status"] == "SUCCEEDED"
    assert retried_after["build"]["id"] != timed_out["build"]["id"]
    async with harness.session_factory() as session:
        builds = await session.scalar(
            select(func.count()).select_from(Build).where(Build.commit_sha == accepted["commitSha"])
        )
    assert builds == 2


async def test_deploy_timeout_fails_and_reverts(harness: DeployHarness) -> None:
    service, aws = await _setup(harness)
    accepted = await harness.deploy(service["id"], aws.id)
    request = await _request_row(harness, accepted["id"])
    harness.replace_executors(
        deploy=FakeDeployExecutor(
            harness.clock, 0, stall_service_environment_ids={request.service_environment_id}
        )
    )
    await harness.run_until(accepted["id"], {DeployRequestStatus.DEPLOYING})

    harness.clock.advance(harness.settings.deploy_timeout_seconds + 1)
    await harness.run_until_idle()
    failed = await harness.get_request(accepted["id"])

    assert (failed["errorCode"], failed["revertStatus"]) == ("DEPLOY_TIMEOUT", "SUCCEEDED")
