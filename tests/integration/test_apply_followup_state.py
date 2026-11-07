"""실 PostgreSQL + ASGI HTTP. GitHub/빌드/클라우드는 기존 합성 대역만 사용한다."""

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

from app.clients.repo_commit_writer import FakeRepoCommitWriter
from app.enums import OrganizationRole
from app.models import AgentRun, AuditLog, Job
from tests.integration.auth_support import create_member, create_organization, make_client, org_path
from tests.integration.migration_support import Migration
from tests.integration.runner_support import prechecks_path
from tests.integration.test_apply_commit_to_repo import REPO_SHA, _runner_apply, _use_writer
from tests.integration.test_failed_deploy_diagnosis import (
    _approve,
    _data,
    _detail,
    _failed_deploy,
    _request_diagnosis,
)


@pytest.mark.parametrize(
    ("enabled", "error", "repository_state"),
    [
        (True, None, "SUCCEEDED"),
        (False, None, "NOT_REQUIRED"),
        (True, "REPO_WRITE_FORBIDDEN", "FAILED"),
        (True, "REPO_WRITE_FAILED", "UNKNOWN"),
    ],
)
async def test_callback_persists_only_followup_markers(
    migration: Migration, enabled: bool, error: str | None, repository_state: str
) -> None:
    writer = FakeRepoCommitWriter(
        files={"Dockerfile": "FROM a\n"}, commit_sha=REPO_SHA, fail_code=error
    )
    _use_writer(writer)
    failed, patch, apply, _ = await _runner_apply(migration, commit_enabled=enabled)
    followup = apply["followUp"]
    assert followup["repository"]["state"] == repository_state
    assert followup["repository"]["finishedAt"] is not None
    assert followup["deadlineAt"] is None
    assert followup["updatedAt"] is not None
    run_id = apply["runId"]
    standalone = await _detail(migration, run_id)
    assert standalone["apply"]["followUp"] == followup
    assert standalone["apply"]["redeployRequestId"] == apply["redeployRequestId"]
    async with migration.harness.session_factory() as session:
        row = await session.get_one(AgentRun, uuid.UUID(run_id))
        stored = dict(row.result)
        finished_at = row.finished_at
        assert stored["repositoryFinishedAt"] is not None
        if not enabled:
            assert stored["repositorySkipReason"] == "COMMIT_DISABLED"

    # 再GETは行/イベント/ジョブを作らない。ここでは人のGETを使いagent読取監査とは分ける。
    async def counts() -> tuple[int, int]:
        async with migration.harness.session_factory() as session:
            return (
                await session.scalar(select(func.count()).select_from(Job)),
                await session.scalar(select(func.count()).select_from(AuditLog)),
            )

    before_counts = await counts()
    await _detail(migration, str(patch["apply"]["runId"]))
    async with migration.harness.session_factory() as session:
        row = await session.get_one(AgentRun, uuid.UUID(run_id))
        assert row.result == stored
        assert row.finished_at == finished_at
    assert await counts() == before_counts
    if not error:
        assert followup["redeploy"]["state"] == "CREATED"
        assert stored["redeployFinishedAt"] is not None
        assert followup["redeploy"]["finishedAt"] is not None
        request = await migration.harness.get_request(apply["redeployRequestId"])
        assert request["commitSha"] == (REPO_SHA if enabled else failed["commitSha"])
    else:
        assert followup["redeploy"]["state"] == "NOT_REQUIRED"
        assert apply["failureCode"] == error
        assert apply["status"] == "FAILED"


async def test_simulated_approval_receipt_keeps_original_commit_and_org_guards(
    migration: Migration,
) -> None:
    writer = FakeRepoCommitWriter(files={"Dockerfile": "FROM a\n"})
    _use_writer(writer)
    migration.harness.use_settings(ai_apply_commit_enabled=True)
    failed = await _failed_deploy(migration)
    run = _data(await _request_diagnosis(migration, failed["id"]))
    approved = await _approve(migration, run["id"], run["patches"][0])
    assert approved.status_code == 200
    apply = _data(approved)["apply"]
    assert apply["simulated"] is True
    assert apply["followUp"]["repository"]["reasonCode"] == "SIMULATED"
    assert apply["followUp"]["redeploy"]["state"] == "CREATED"
    assert apply["followUp"]["redeploy"]["finishedAt"] is not None
    assert (await _detail(migration, run["id"]))["patches"][0]["apply"] == apply
    assert (await migration.harness.get_request(apply["redeployRequestId"]))["commitSha"] == failed[
        "commitSha"
    ]
    assert writer.requests == [] and writer.reads == []
    factory = migration.harness.session_factory
    other = await create_organization(factory, "other-followup")
    member = await create_member(
        factory, other, OrganizationRole.VIEWER, migration.harness.clock.now()
    )
    async with make_client(session_token=member.session_token) as client:
        # 조직 멤버가 자기 org URL에 타 org 실행 id를 넣어도 404.
        response = await client.get(org_path(other.id, f"/prechecks/{apply['runId']}"))
        assert response.status_code == 404
        assert "followUp" not in response.text
    async with make_client() as client:
        assert (
            await client.get(prechecks_path(migration, f"/{apply['runId']}"))
        ).status_code == 401


async def test_parent_context_reads_legacy_but_get_does_not_repair_it(migration: Migration) -> None:
    _use_writer(FakeRepoCommitWriter(files={"Dockerfile": "FROM a\n"}))
    _, patch, apply, _ = await _runner_apply(migration)
    run_id = uuid.UUID(apply["runId"])
    async with migration.harness.session_factory() as session, session.begin():
        row = await session.get_one(AgentRun, run_id)
        row.input_snapshot = {}
        row.result = {"appliedCommitSha": "a" * 40}
        parent_id = str(row.parent_run_id)
    parent = await _detail(migration, parent_id)
    assert parent["patches"][0]["apply"]["followUp"]["applicable"] is True
    single = await _detail(migration, str(run_id))
    assert single["apply"]["followUp"]["applicable"] is None
    assert single["apply"]["followUp"]["repository"]["state"] == "UNKNOWN"
    async with migration.harness.session_factory() as session:
        row = await session.get_one(AgentRun, run_id)
        assert row.result == {"appliedCommitSha": "a" * 40}
        assert row.input_snapshot == {}


async def test_http_observes_live_claim_then_receipt_without_advancing_callback(
    migration: Migration,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class PausedWriter(FakeRepoCommitWriter):
        async def commit_and_open_pull_request(self, request: Any) -> Any:
            entered.set()
            await release.wait()
            return await super().commit_and_open_pull_request(request)

    writer = PausedWriter(files={"Dockerfile": "FROM a\n"})
    _use_writer(writer)
    task = asyncio.create_task(_runner_apply(migration))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        async with migration.harness.session_factory() as session:
            row = await session.scalar(select(AgentRun).where(AgentRun.kind == "APPLY_PATCH"))
            apply_id, parent_id = str(row.id), str(row.parent_run_id)
            stored = dict(row.result)
        for path in (apply_id, parent_id):
            data = await _detail(migration, path)
            apply = data["apply"] if path == apply_id else data["patches"][0]["apply"]
            assert apply["followUp"]["repository"]["state"] == "IN_PROGRESS"
            assert apply["followUp"]["redeploy"]["state"] == "PENDING"
            assert apply.get("redeployRequestId") is None
        async with migration.harness.session_factory() as session:
            row = await session.get_one(AgentRun, uuid.UUID(apply_id))
            assert row.result == stored
        assert not task.done()
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=10)
    assert (await _detail(migration, parent_id))["patches"][0]["apply"]["followUp"]["redeploy"][
        "state"
    ] == "CREATED"


@pytest.mark.parametrize(
    ("result", "redeploy_state", "rejected"),
    [
        ({"redeployRequestId": "not-a-uuid"}, "UNKNOWN", None),
        ({"repositoryFinishedAt": "0001-01-01T00:00:00+01:00"}, "PENDING", None),
        ({"redeployRejectedCode": "EXTERNAL\nSECRET_TOKEN=example"}, "UNKNOWN", "UNKNOWN"),
        ({"redeployRejectedCode": "raw response body"}, "UNKNOWN", "UNKNOWN"),
        ({"redeployRejectedCode": 42}, "UNKNOWN", "UNKNOWN"),
        ({"redeployRejectedCode": "MIGRATION_IN_PROGRESS"}, "REJECTED", "MIGRATION_IN_PROGRESS"),
        (
            {"redeployRequestId": None, "redeployRejectedCode": "INTERNAL_ERROR"},
            "REJECTED",
            "INTERNAL_ERROR",
        ),
    ],
)
async def test_corrupt_receipt_real_get_is_read_only(
    migration: Migration, result: dict[str, Any], redeploy_state: str, rejected: str | None
) -> None:
    for payload in await _stored_result_gets(migration, result):
        assert payload["redeployRequestId"] is None
        assert payload["redeployRejectedCode"] == rejected
        assert payload["followUp"]["redeploy"]["state"] == redeploy_state


async def _stored_result_gets(migration: Migration, result: dict[str, Any]) -> list[dict[str, Any]]:
    import copy

    from httpx import ASGITransport, AsyncClient

    from app.main import app
    from app.models import AgentRunEvent

    _use_writer(FakeRepoCommitWriter(files={"Dockerfile": "FROM a\n"}))
    _, _, apply, _ = await _runner_apply(migration)
    run_id = uuid.UUID(apply["runId"])
    # Synthetic legacy/corruption fixture, not a normal writer or runner input.
    async with migration.harness.session_factory() as session, session.begin():
        row = await session.get_one(AgentRun, run_id)
        row.result = copy.deepcopy(result)
        parent_id = row.parent_run_id
    async with migration.harness.session_factory() as session:
        row = await session.get_one(AgentRun, run_id)
        before = (copy.deepcopy(row.result), row.updated_at, row.finished_at, row.status)
        counts = [
            await session.scalar(select(func.count()).select_from(model))
            for model in (AgentRunEvent, Job, AuditLog)
        ]
    original = migration.author_client
    payloads = []
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url=original.base_url,
        headers=original.headers,
        cookies=original.cookies,
    ) as client:
        for target in (parent_id, run_id):
            response = await client.get(prechecks_path(migration, f"/{target}"))
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            payload = data["apply"] if target == run_id else data["patches"][0]["apply"]
            payloads.append(payload)
    async with migration.harness.session_factory() as session:
        row = await session.get_one(AgentRun, run_id)
        assert (row.result, row.updated_at, row.finished_at, row.status) == before
        assert [
            await session.scalar(select(func.count()).select_from(model))
            for model in (AgentRunEvent, Job, AuditLog)
        ] == counts
    assert payloads[0]["followUp"] == payloads[1]["followUp"]
    return payloads


@pytest.mark.parametrize(
    ("timestamp", "normalized"),
    [
        ("0001-01-01T00:00:00+01:00", None),
        ("9999-12-31T23:59:59-01:00", None),
        ("0001-01-01T00:00:00Z", "0001-01-01T00:00:00Z"),
        ("0001-01-01T01:00:00+01:00", "0001-01-01T00:00:00Z"),
        ("9999-12-31T22:59:59-01:00", "9999-12-31T23:59:59Z"),
        ("9999-12-31T23:59:59Z", "9999-12-31T23:59:59Z"),
        ("invalid", None),
        ("2026-11-07T10:00:00", None),
        ("2099-01-01T00:00:00Z", "2099-01-01T00:00:00Z"),
        ("2026-11-07T19:00:00+09:00", "2026-11-07T10:00:00Z"),
    ],
)
async def test_timestamp_extremes_real_get_keeps_unknown_claim(
    migration: Migration, timestamp: str, normalized: str | None
) -> None:
    result = {
        key: timestamp for key in ("commitClaimedAt", "repositoryFinishedAt", "redeployFinishedAt")
    }
    result["commitState"] = "IN_PROGRESS"
    for payload in await _stored_result_gets(migration, result):
        followup = payload["followUp"]
        assert followup["repository"]["startedAt"] == normalized
        assert followup["repository"]["finishedAt"] == normalized
        assert followup["redeploy"]["finishedAt"] == normalized
        assert followup["updatedAt"] == normalized
        assert followup["repository"]["state"] == "UNKNOWN"
        assert followup["repository"]["reasonCode"] == "CLAIM_UNCONFIRMED"
        assert followup["deadlineAt"] is None


async def test_conflicting_valid_id_and_rejection_real_get_stays_unknown(
    migration: Migration,
) -> None:
    request_id = str(uuid.UUID(int=9))
    for payload in await _stored_result_gets(
        migration, {"redeployRequestId": request_id, "redeployRejectedCode": "INTERNAL_ERROR"}
    ):
        assert payload["redeployRequestId"] == request_id
        assert payload["redeployRejectedCode"] == "INTERNAL_ERROR"
        assert payload["followUp"]["redeploy"]["state"] == "UNKNOWN"
