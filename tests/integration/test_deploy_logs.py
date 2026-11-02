"""배포 작업 로그·빌드·실행 로그 통합 시험(SPEC api-4 AC-8~AC-13)."""

import logging
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text, update

from app.core.config import ExecutorMode
from app.dependencies import get_executor_set
from app.enums import DeployRequestStatus, JobStatus
from app.executors.base import DeploymentSpec, DeployTarget, HealthHttpError
from app.executors.fake import FakeBuildExecutor, FakeDeployExecutor, FakeTeardownExecutor
from app.main import app
from app.models import DeployRequest, DeployRequestLog, Job, Service
from app.services.deploy_job_log import MAX_DEPLOY_LOG_LINES
from tests.github_fake import TEST_APP_PRIVATE_KEY_PEM
from tests.integration.auth_support import create_member, create_organization, make_client, org_path
from tests.integration.conftest import TABLES
from tests.integration.deploy_harness import DeployHarness, ScriptedHealthChecker
from tests.integration.rollback_support import (
    SUFFIX,
    deploy_and_finish,
    get_logs,
    log_codes,
    log_rows,
    read_all_logs,
    rollback,
)

LEASE_SECONDS = 30
SUCCESS_TAIL = [
    "DEPLOYMENT_CREATED",
    "DEPLOY_REQUESTED",
    "DEPLOY_APPLIED",
    "HEALTH_PROBE_OK",
    "SUCCEEDED",
]
NEW_BUILD = ["REQUEST_ACCEPTED", "STARTED", "BUILD_ATTACHED", *SUCCESS_TAIL]
REUSED_BUILD = ["REQUEST_ACCEPTED", "STARTED", "BUILD_REUSED", *SUCCESS_TAIL]


async def _service(harness: DeployHarness) -> tuple[dict[str, Any], Any, Any]:
    harness.host_suffixes.queue(SUFFIX)
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    gcp = await harness.add_environment("gcp-seoul")
    return service, aws, gcp


async def _seqs(harness: DeployHarness, request_id: str) -> list[int]:
    return [row.seq for row in await log_rows(harness, request_id)]


# --- AC-8 배포 작업 로그 순서 ---


async def test_deploy_job_log_follows_normal_order_for_each_request_kind(
    harness: DeployHarness,
) -> None:
    service, aws, gcp = await _service(harness)
    new_build = await deploy_and_finish(harness, service["id"], aws.id, branch="main")
    reused = await deploy_and_finish(harness, service["id"], gcp.id, branch="main")
    serving = await deploy_and_finish(harness, service["id"], aws.id, branch="release")
    syncs = await harness.list_requests(
        service["id"], environmentId=str(gcp.id), trigger="STANDBY_SYNC"
    )
    back = await rollback(harness, new_build["id"])
    await harness.run_until_idle()
    rollback_syncs = [
        item
        for item in await harness.list_requests(service["id"], trigger="STANDBY_SYNC")
        if item["sourceRequestId"] == back["id"]
    ]

    assert await log_codes(harness, new_build["id"]) == NEW_BUILD
    assert await log_codes(harness, reused["id"]) == REUSED_BUILD
    assert await log_codes(harness, serving["id"]) == [*NEW_BUILD, "STANDBY_SYNC_REQUESTED"]
    assert await log_codes(harness, syncs[0]["id"]) == REUSED_BUILD
    assert await log_codes(harness, back["id"]) == [*REUSED_BUILD, "STANDBY_SYNC_REQUESTED"]
    assert await log_codes(harness, rollback_syncs[0]["id"]) == REUSED_BUILD
    for request in (new_build, reused, serving, back):
        seqs = await _seqs(harness, request["id"])
        assert seqs == list(range(1, len(seqs) + 1))

    rows = await log_rows(harness, new_build["id"])
    items = await read_all_logs(harness, new_build["id"], "DEPLOY_JOB")
    assert [(item["level"], item["message"]) for item in items] == [
        (row.level, row.message) for row in rows
    ]
    assert all(row.simulated for row in rows)
    assert [row.stage for row in rows] == [
        "QUEUED",
        "BUILDING",
        "BUILDING",
        "DEPLOYING",
        "DEPLOYING",
        "DEPLOYING",
        "VERIFYING",
        "SUCCEEDED",
    ]


async def test_health_probe_lines_only_when_result_kind_changes(harness: DeployHarness) -> None:
    service, aws, _ = await _service(harness)
    harness.replace_executors(health=ScriptedHealthChecker([HealthHttpError(503)] * 3))

    finished = await deploy_and_finish(harness, service["id"], aws.id)
    rows = await log_rows(harness, finished["id"])

    assert finished["status"] == "SUCCEEDED"
    assert len(harness.executors.health.calls) == 4
    assert [row.code for row in rows][-3:] == [
        "HEALTH_PROBE_HTTP_ERROR",
        "HEALTH_PROBE_OK",
        "SUCCEEDED",
    ]
    assert [row.code for row in rows].count("HEALTH_PROBE_HTTP_ERROR") == 1
    assert rows[-3].level == "WARN"


async def test_simulated_verify_failure_writes_failed_and_revert_without_health_lines(
    harness: DeployHarness,
) -> None:
    service, aws, _ = await _service(harness)

    failed = await deploy_and_finish(
        harness, service["id"], aws.id, simulate={"failAt": "VERIFYING"}
    )
    rows = await log_rows(harness, failed["id"])

    assert [row.code for row in rows] == [
        "REQUEST_ACCEPTED",
        "STARTED",
        "BUILD_ATTACHED",
        "DEPLOYMENT_CREATED",
        "DEPLOY_REQUESTED",
        "DEPLOY_APPLIED",
        "FAILED",
        "REVERT_SUCCEEDED",
    ]
    assert (rows[-2].level, rows[-2].message) == (
        "ERROR",
        "The health check did not return a 2xx response.",
    )
    assert (rows[-1].stage, rows[-1].level) == ("REVERT", "INFO")


async def test_build_failure_and_revert_failure_lines(harness: DeployHarness) -> None:
    service, aws, gcp = await _service(harness)
    harness.replace_executors(deploy=FakeDeployExecutor(harness.clock, 0, fail_revert=True))
    reverted = await deploy_and_finish(
        harness, service["id"], aws.id, simulate={"failAt": "DEPLOYING"}
    )
    build_failed = await deploy_and_finish(
        harness, service["id"], gcp.id, simulate={"failAt": "BUILDING"}
    )
    rows = await log_rows(harness, reverted["id"])

    assert [row.code for row in rows][-2:] == ["FAILED", "REVERT_FAILED"]
    assert rows[-1].level == "ERROR"
    assert await log_codes(harness, build_failed["id"]) == [
        "REQUEST_ACCEPTED",
        "STARTED",
        "FAILED",
    ]
    assert (await log_rows(harness, build_failed["id"]))[-1].message == "The image build failed."


async def test_cancel_lines_for_superseded_and_service_deleted_paths(
    harness: DeployHarness,
) -> None:
    service, aws, gcp = await _service(harness)
    superseded = await harness.deploy(service["id"], aws.id, branch="one")
    queued = await harness.deploy(service["id"], aws.id, branch="two")
    response = await harness.client.delete(f"{harness.services}/{service['id']}")
    assert response.status_code == 202
    await harness.run_until_idle()

    assert await log_codes(harness, superseded["id"]) == [
        "REQUEST_ACCEPTED",
        "CANCELLED_SUPERSEDED",
    ]
    assert await log_codes(harness, queued["id"]) == [
        "REQUEST_ACCEPTED",
        "CANCELLED_SERVICE_DELETED",
    ]


async def _force_deleting(harness: DeployHarness, service_id: str, *, teardown: bool) -> None:
    """API 를 거치지 않고 서비스를 DELETING 으로 둔다(대기 요청이 남은 채로 일꾼 경로를 시험)."""
    async with harness.session_factory() as session, session.begin():
        await session.execute(
            update(Service).where(Service.id == uuid.UUID(service_id)).values(status="DELETING")
        )
        if teardown:
            now = harness.clock.now()
            await session.execute(
                update(Job)
                .where(Job.kind == "DEPLOY_REQUEST", Job.status == "QUEUED")
                .values(status=JobStatus.DONE, updated_at=now)
            )
            session.add(
                Job(
                    kind="SERVICE_TEARDOWN",
                    subject_id=uuid.UUID(service_id),
                    status=JobStatus.QUEUED,
                    run_after=now,
                    created_at=now,
                    updated_at=now,
                )
            )


@pytest.mark.parametrize("teardown", [True, False], ids=["teardown-worker", "deploy-worker"])
async def test_worker_paths_cancel_for_service_deleted_write_one_line(
    harness: DeployHarness, teardown: bool
) -> None:
    service, aws, _ = await _service(harness)
    harness.replace_executors(teardown=FakeTeardownExecutor(harness.clock, 0))
    queued = await harness.deploy(service["id"], aws.id)

    await _force_deleting(harness, service["id"], teardown=teardown)
    await harness.run_until_idle()

    assert await harness.request_status(queued["id"]) == "CANCELLED"
    assert await log_codes(harness, queued["id"]) == [
        "REQUEST_ACCEPTED",
        "CANCELLED_SERVICE_DELETED",
    ]


async def test_shared_build_waiters_get_same_log_structure(harness: DeployHarness) -> None:
    service, aws, gcp = await _service(harness)
    harness.replace_executors(build=FakeBuildExecutor(harness.clock, 5))
    first = await harness.deploy(service["id"], aws.id, branch="main")
    second = await harness.deploy(service["id"], gcp.id, branch="main")

    await harness.run_until_idle()
    first_codes = await log_codes(harness, first["id"])
    second_codes = await log_codes(harness, second["id"])

    # 어느 쪽이 먼저 빌드를 붙였는지는 일꾼 순서에 따른다. 빌드 진행 줄은 어느 쪽에도 없다.
    assert sorted([first_codes, second_codes]) == sorted([NEW_BUILD, REUSED_BUILD])


# --- AC-9 재시작·중복·상한 ---


class WorkerCrashError(Exception):
    pass


class CrashAfterFirstApplyDeployExecutor(FakeDeployExecutor):
    """배포 요청을 보낸 직후(external_ref 저장 전) 일꾼이 죽은 것처럼 한 번 멈춘다."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._crashed = False

    async def apply(
        self, deployment: DeploymentSpec, target: DeployTarget, idempotency_key: str
    ) -> str:
        external_ref = await super().apply(deployment, target, idempotency_key)
        if not self._crashed:
            self._crashed = True
            raise WorkerCrashError
        return external_ref


async def test_lease_loss_and_crash_mid_stage_never_duplicate_or_skip_lines(
    harness: DeployHarness,
) -> None:
    service, aws, _ = await _service(harness)
    crashing = CrashAfterFirstApplyDeployExecutor(harness.clock, 0)
    harness.replace_executors(deploy=crashing)
    accepted = await harness.deploy(service["id"], aws.id)

    # 1) 임대를 잃은 일꾼의 QUEUED → BUILDING 결과는 버려지고, 새 일꾼이 다시 한다.
    stale_worker, fresh_worker = harness.worker("worker-a"), harness.worker("worker-b")
    stale = await stale_worker.claim_next_job()
    harness.clock.advance(LEASE_SECONDS + 1)
    fresh = await fresh_worker.claim_next_job()
    assert stale is not None and fresh is not None
    assert await fresh_worker.process(fresh) is True
    assert await stale_worker.process(stale) is False
    # 2) 배포 요청 직후 일꾼이 죽고, 임대가 지난 뒤 다른 일꾼이 같은 멱등 키로 이어 간다.
    with pytest.raises(WorkerCrashError):
        for _ in range(10):
            await harness.worker("worker-c").run_once()
    harness.clock.advance(LEASE_SECONDS + 1)
    await harness.run_until_idle()
    rows = await log_rows(harness, accepted["id"])

    assert await harness.request_status(accepted["id"]) == "SUCCEEDED"
    assert [row.code for row in rows] == NEW_BUILD
    assert [row.seq for row in rows] == list(range(1, len(NEW_BUILD) + 1))
    assert len(crashing.calls) == 2 and len(set(crashing.operation_keys)) == 1


async def test_log_line_limit_writes_truncated_once_and_request_still_succeeds(
    harness: DeployHarness,
) -> None:
    service, aws, _ = await _service(harness)
    accepted = await harness.deploy(service["id"], aws.id)
    request_id = uuid.UUID(accepted["id"])
    now = harness.clock.now()
    async with harness.session_factory() as session, session.begin():
        session.add_all(
            DeployRequestLog(
                deploy_request_id=request_id,
                seq=seq,
                logged_at=now,
                level="INFO",
                stage="QUEUED",
                code="REQUEST_ACCEPTED",
                message="filler",
                simulated=True,
                created_at=now,
            )
            for seq in range(2, MAX_DEPLOY_LOG_LINES)
        )

    await harness.run_until_idle()
    rows = await log_rows(harness, accepted["id"])

    assert await harness.request_status(accepted["id"]) == "SUCCEEDED"
    assert len(rows) == MAX_DEPLOY_LOG_LINES
    assert rows[-1].seq == MAX_DEPLOY_LOG_LINES
    assert (rows[-1].code, rows[-1].stage) == ("LOG_TRUNCATED", "BUILDING")
    assert [row.code for row in rows].count("LOG_TRUNCATED") == 1


async def test_deploy_request_log_rows_cannot_be_deleted(harness: DeployHarness) -> None:
    service, aws, _ = await _service(harness)
    await harness.deploy(service["id"], aws.id)

    async with harness.session_factory() as session:
        with pytest.raises(Exception, match="deploy_request_logs rows are append-only"):
            await session.execute(text("DELETE FROM deploy_request_logs"))


# --- AC-10 페이징 ---


async def test_deploy_job_paging_covers_every_line_once(harness: DeployHarness) -> None:
    service, aws, _ = await _service(harness)
    finished = await deploy_and_finish(harness, service["id"], aws.id)

    first = (await get_logs(harness, finished["id"], source="DEPLOY_JOB", limit=2)).json()["data"]
    paged = await read_all_logs(harness, finished["id"], "DEPLOY_JOB", limit=2)
    whole = await read_all_logs(harness, finished["id"], "DEPLOY_JOB", limit=500)
    last = (
        await get_logs(harness, finished["id"], source="DEPLOY_JOB", cursor="8", limit=2)
    ).json()["data"]

    assert (first["source"], first["nextCursor"], first["hasMore"]) == ("DEPLOY_JOB", "2", True)
    assert len(first["items"]) == 2
    assert paged == whole and len(whole) == len(NEW_BUILD)
    assert (last["items"], last["nextCursor"], last["hasMore"]) == ([], "8", False)


@pytest.mark.parametrize("source", ["BUILD", "RUNTIME"])
async def test_executor_log_paging_covers_every_line_once(
    harness: DeployHarness, source: str
) -> None:
    service, aws, _ = await _service(harness)
    finished = await deploy_and_finish(harness, service["id"], aws.id)

    first = (await get_logs(harness, finished["id"], source=source, limit=2)).json()["data"]
    paged = await read_all_logs(harness, finished["id"], source, limit=2)
    whole = await read_all_logs(harness, finished["id"], source, limit=500)
    again = await read_all_logs(harness, finished["id"], source, limit=2)

    assert (first["nextCursor"], first["hasMore"], first["simulated"]) == ("2", True, True)
    assert paged == whole == again
    assert 5 <= len(whole) <= 8
    times = [item["at"] for item in whole]
    assert times == sorted(times)


@pytest.mark.parametrize(
    "params",
    [
        {"source": "DEPLOY_JOB", "limit": 0},
        {"source": "DEPLOY_JOB", "limit": 501},
        {"source": "AUDIT"},
        {},
        {"source": "DEPLOY_JOB", "cursor": "abc"},
        {"source": "DEPLOY_JOB", "cursor": "-1"},
        {"source": "BUILD", "cursor": "x1"},
        {"source": "RUNTIME", "cursor": "x1"},
    ],
)
async def test_invalid_log_query_returns_422(
    harness: DeployHarness, params: dict[str, Any]
) -> None:
    service, aws, _ = await _service(harness)
    finished = await deploy_and_finish(harness, service["id"], aws.id)

    response = await get_logs(harness, finished["id"], **params)

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")
    if "cursor" in params:
        assert response.json()["details"] == [{"field": "cursor", "reason": "INVALID_CURSOR"}]


async def test_logs_without_build_or_deployment_are_empty_pages(harness: DeployHarness) -> None:
    service, aws, _ = await _service(harness)
    queued = await harness.deploy(service["id"], aws.id)
    built = await harness.deploy(service["id"], aws.id, branch="other")
    await harness.run_until(built["id"], {DeployRequestStatus.BUILDING})
    await harness.worker().run_once()

    pages = {
        (name, source): (await get_logs(harness, item["id"], source=source)).json()["data"]
        for name, item in (("queued", queued), ("built", built))
        for source in ("BUILD", "RUNTIME")
    }
    with_cursor = (await get_logs(harness, queued["id"], source="BUILD", cursor="3")).json()

    assert pages[("queued", "BUILD")] == {
        "source": "BUILD",
        "simulated": True,
        "items": [],
        "nextCursor": None,
        "hasMore": False,
    }
    assert pages[("queued", "RUNTIME")]["items"] == []
    assert pages[("built", "RUNTIME")]["items"] == []
    assert with_cursor["data"]["nextCursor"] == "3"


# --- AC-11 권한·범위 ---


async def test_logs_of_other_organization_are_not_found(harness: DeployHarness) -> None:
    service, aws, _ = await _service(harness)
    finished = await deploy_and_finish(harness, service["id"], aws.id)
    other = await create_organization(harness.session_factory, "other-org")
    outsider = await create_member(harness.session_factory, other, "ADMIN", harness.clock.now())

    async with make_client(session_token=outsider.session_token) as client:
        response = await client.get(
            org_path(other.id, f"/deploy-requests/{finished['id']}/logs"),
            params={"source": "DEPLOY_JOB"},
        )

    assert (response.status_code, response.json()["code"]) == (404, "DEPLOY_REQUEST_NOT_FOUND")


async def test_logs_of_deleted_service_are_still_readable(harness: DeployHarness) -> None:
    service, aws, _ = await _service(harness)
    finished = await deploy_and_finish(harness, service["id"], aws.id)
    response = await harness.client.delete(f"{harness.services}/{service['id']}")
    assert response.status_code == 202
    await harness.run_until_idle()

    job = await read_all_logs(harness, finished["id"], "DEPLOY_JOB")
    build = await read_all_logs(harness, finished["id"], "BUILD")
    runtime = await read_all_logs(harness, finished["id"], "RUNTIME")

    assert len(job) == len(NEW_BUILD)
    assert build
    # 연결을 내려 실행 대상을 만들 수 없다.
    assert runtime == []


class _RealLikeBuildExecutor(FakeBuildExecutor):
    @property
    def simulated(self) -> bool:
        return False


class _RealLikeDeployExecutor(FakeDeployExecutor):
    @property
    def simulated(self) -> bool:
        return False


async def test_executor_simulated_mismatch_returns_empty_page(harness: DeployHarness) -> None:
    service, aws, _ = await _service(harness)
    finished = await deploy_and_finish(harness, service["id"], aws.id)
    harness.replace_executors(
        build=_RealLikeBuildExecutor(harness.clock, 0),
        deploy=_RealLikeDeployExecutor(harness.clock, 0),
    )

    build = (await get_logs(harness, finished["id"], source="BUILD")).json()["data"]
    runtime = (await get_logs(harness, finished["id"], source="RUNTIME")).json()["data"]

    assert (build["items"], build["simulated"]) == ([], True)
    assert (runtime["items"], runtime["simulated"]) == ([], True)


# --- AC-12 simulated ---


async def test_simulated_comes_from_rows_not_settings(harness: DeployHarness) -> None:
    service, aws, _ = await _service(harness)
    finished = await deploy_and_finish(harness, service["id"], aws.id)
    before = {
        source: (await get_logs(harness, finished["id"], source=source)).json()["data"]
        for source in ("DEPLOY_JOB", "BUILD", "RUNTIME")
    }

    harness.use_settings(executor_mode=ExecutorMode.REAL)
    # 실제 의존성: 가짜가 아닌 모드면 실행기를 만들지 않는다(빌드·실행 로그는 빈 페이지).
    app.dependency_overrides.pop(get_executor_set)
    after = {
        source: (await get_logs(harness, finished["id"], source=source)).json()["data"]
        for source in ("DEPLOY_JOB", "BUILD", "RUNTIME")
    }
    detail = await harness.get_request(finished["id"])

    assert all(page["simulated"] is True and page["items"] for page in before.values())
    assert all(page["simulated"] is True for page in after.values())
    assert after["DEPLOY_JOB"]["items"] == before["DEPLOY_JOB"]["items"]
    assert after["BUILD"]["items"] == after["RUNTIME"]["items"] == []
    assert detail["simulated"] is True


# --- AC-13 비밀값 ---

GITHUB_TOKEN = "ghs_" + "A1b2C3d4E5f6G7h8I9j0" * 2
AWS_KEY = "AKIA" + "ABCDEFGHIJ234567"
BEARER_TOKEN = "eyJhbGciOiJIUzI1NiJ9.test-bearer_value"
API_KEY = "sk-" + "live_" + "x" * 24
PEM_LINE = TEST_APP_PRIVATE_KEY_PEM.strip()


def _private_key_fragments() -> list[str]:
    body = [line for line in PEM_LINE.splitlines() if not line.startswith("-----")]
    return [PEM_LINE, "".join(body), *body]


async def test_secret_like_executor_lines_are_masked_everywhere(
    harness: DeployHarness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    injections = [
        f"cloning with token {GITHUB_TOKEN}",
        f"aws access key {AWS_KEY} loaded",
        f"Authorization: Bearer {BEARER_TOKEN}",
        f"OPENAI_API_KEY={API_KEY}",
        f"key file:\n{PEM_LINE}\nend of key",
    ]
    harness.replace_executors(
        build=FakeBuildExecutor(harness.clock, 0, log_injections=injections),
        deploy=FakeDeployExecutor(harness.clock, 0, log_injections=injections),
    )
    service, aws, _ = await _service(harness)
    finished = await deploy_and_finish(harness, service["id"], aws.id)

    build = await read_all_logs(harness, finished["id"], "BUILD", limit=3)
    runtime = await read_all_logs(harness, finished["id"], "RUNTIME", limit=3)
    await read_all_logs(harness, finished["id"], "DEPLOY_JOB")
    async with harness.session_factory() as session:
        dumps = [
            await session.scalar(
                text(f"SELECT coalesce(string_agg(t::text, '\n'), '') FROM {table} t")
            )
            for table in TABLES
        ]

    masked = [item["message"] for item in build + runtime if "***" in item["message"]]
    assert len(masked) == 2 * len(injections)
    assert "Authorization: Bearer ***" in masked
    secrets = [GITHUB_TOKEN, AWS_KEY, BEARER_TOKEN, API_KEY, *_private_key_fragments()]
    haystacks = {
        "responses": "\n".join(harness.responses),
        "logs": caplog.text,
        "db": "\n".join(dump or "" for dump in dumps),
    }
    assert finished["id"] in haystacks["responses"] and finished["id"] in haystacks["db"]
    leaks = [
        (where, index)
        for where, haystack in haystacks.items()
        for index, secret in enumerate(secrets)
        if secret and secret in haystack
    ]
    assert leaks == []


async def test_log_times_come_from_injected_clock(harness: DeployHarness) -> None:
    service, aws, _ = await _service(harness)
    accepted_at = harness.clock.now()
    accepted = await harness.deploy(service["id"], aws.id)
    harness.clock.advance(3600)
    await harness.run_until_idle()

    rows = await log_rows(harness, accepted["id"])

    assert rows[0].logged_at == accepted_at
    assert rows[1].logged_at == accepted_at + timedelta(seconds=3600)


async def test_request_rows_keep_simulated_flag_for_logs(harness: DeployHarness) -> None:
    service, aws, _ = await _service(harness)
    accepted = await harness.deploy(service["id"], aws.id)
    async with harness.session_factory() as session:
        request = await session.get_one(DeployRequest, uuid.UUID(accepted["id"]))

    assert request.simulated is True
