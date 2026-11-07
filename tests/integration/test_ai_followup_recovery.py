"""후속 복구의 규범 PG 시험. 외부 효과는 합성이며 PG와 별개다."""

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import select, text

from app.clients.repo_commit_writer import FakeRepoCommitWriter
from app.enums import AgentRunStatus
from app.models import AgentRun
from app.repositories.agent_run_repository import AgentRunRepository
from app.services.apply_commit_service import COMMIT_CLAIM_TTL, ApplyCommitService, CommitOutcome
from app.services.diagnosis_service import DiagnosisService
from tests.integration.migration_support import Migration
from tests.integration.test_failed_deploy_diagnosis import (
    _approve,
    _data,
    _failed_deploy,
    _request_diagnosis,
)


class RuntimeInterruption(BaseException):
    """HTTP middleware가 삼키지 않는 합성 중단. OS kill 증거 아님."""


async def pending_apply(migration: Migration, monkeypatch: pytest.MonkeyPatch) -> AgentRun:
    failed = await _failed_deploy(migration)
    diagnosis = _data(await _request_diagnosis(migration, failed["id"]))

    async def cut(self: DiagnosisService, run_id: uuid.UUID) -> None:
        raise RuntimeInterruption

    with monkeypatch.context() as context:
        context.setattr(DiagnosisService, "redeploy_after_apply", cut)
        with pytest.raises(RuntimeInterruption):
            await _approve(migration, diagnosis["id"], diagnosis["patches"][0])
    async with migration.harness.session_factory() as session:
        run = await session.scalar(
            select(AgentRun).where(AgentRun.parent_run_id == uuid.UUID(diagnosis["id"]))
        )
        assert run is not None and run.status == AgentRunStatus.SUCCEEDED
        return run


async def test_new_pg_claim_survives_old_callback_cancellation(
    migration: Migration, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = await pending_apply(migration, monkeypatch)
    harness = migration.harness
    async with harness.session_factory() as session, session.begin():
        row = await session.get_one(AgentRun, run.id)
        row.simulated = False
    entered = asyncio.Event()
    release = asyncio.Event()

    async def interrupted(*args: Any) -> CommitOutcome:
        entered.set()
        await release.wait()
        raise RuntimeInterruption

    async def paused(*args: Any) -> CommitOutcome:
        return CommitOutcome(pending=True)

    async with harness.session_factory() as first, harness.session_factory() as second:
        # 두 독립 PG 연결에서 저장한다. 각 연결의 synchronous_commit 실효값도 확인한다.
        for session in (first, second):
            async with session.begin():
                await session.execute(text("SET synchronous_commit = on"))
                assert await session.scalar(text("SHOW synchronous_commit")) == "on"
        old = ApplyCommitService(first, harness.clock, FakeRepoCommitWriter(), enabled=True)
        new = ApplyCommitService(second, harness.clock, FakeRepoCommitWriter(), enabled=True)
        monkeypatch.setattr(old, "_commit", interrupted)
        monkeypatch.setattr(new, "_commit", paused)
        task = asyncio.create_task(old.prepare_redeploy_commit(run.id))
        await asyncio.wait_for(entered.wait(), timeout=10)
        harness.clock.advance(COMMIT_CLAIM_TTL.total_seconds())
        await new.prepare_redeploy_commit(run.id)
        async with second.begin():
            newer = await AgentRunRepository(second).find_by_id(run.id)
            assert newer is not None
            expected_claimed = newer.result["commitClaimedAt"]
        release.set()
        with pytest.raises(RuntimeInterruption):
            await task
    async with harness.session_factory() as session:
        fresh = await session.get_one(AgentRun, run.id)
        assert fresh.result["commitState"] == "IN_PROGRESS"
        assert fresh.result["commitClaimedAt"] == expected_claimed
        assert "redeployRequestId" not in fresh.result


async def test_stale_repository_outcome_cannot_publish_or_enqueue(
    migration: Migration, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.dependencies import get_deploy_request_service
    from app.models import DeployRequest, Job

    run = await pending_apply(migration, monkeypatch)
    harness = migration.harness
    async with harness.session_factory() as session, session.begin():
        repo = AgentRunRepository(session)
        assert await repo.claim_commit(run.id, harness.clock.now(), "old-owner", None)
        assert await repo.claim_commit(run.id, harness.clock.now(), "new-owner", "old-owner")
        before_requests = set(await session.scalars(select(DeployRequest.id)))
        before_jobs = set(await session.scalars(select(Job.id)))

    class OldOutcome:
        async def prepare_redeploy_commit(self, *args: Any, **kwargs: Any) -> CommitOutcome:
            return CommitOutcome(
                ref="f" * 40,
                owner="old-owner",
                result_fields={
                    "repoCommitted": True,
                    "appliedCommitSha": "f" * 40,
                    "commitState": "DONE",
                },
            )

    async with harness.session_factory() as session:
        deploy = get_deploy_request_service(
            session,
            harness.settings,
            harness.clock,
            harness.source_client,
            harness.executors.as_set(),
        )
        diagnosis = DiagnosisService(
            session, harness.clock, deploy, simulate_enabled=True, apply_commit=OldOutcome()
        )
        await diagnosis.redeploy_after_apply(run.id)
    async with harness.session_factory() as session:
        fresh = await session.get_one(AgentRun, run.id)
        assert fresh.result["commitOwner"] == "new-owner"
        assert fresh.result["commitState"] == "IN_PROGRESS"
        assert not fresh.result.get("repoCommitted")
        assert "redeployRequestId" not in fresh.result
        assert set(await session.scalars(select(DeployRequest.id))) == before_requests
        assert set(await session.scalars(select(Job.id))) == before_jobs


async def test_lease_loss_rolls_back_commit_claim_without_external_write(
    migration: Migration, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.job_lease import LeaseLostError

    class LostLease:
        async def hold(self) -> None:
            raise LeaseLostError

    run = await pending_apply(migration, monkeypatch)
    harness = migration.harness
    async with harness.session_factory() as session, session.begin():
        row = await session.get_one(AgentRun, run.id)
        row.simulated = False
    writer = FakeRepoCommitWriter()
    async with harness.session_factory() as session:
        with pytest.raises(LeaseLostError):
            await ApplyCommitService(
                session, harness.clock, writer, enabled=True, lease=LostLease()
            ).prepare_redeploy_commit(run.id)
    assert writer.requests == []
    async with harness.session_factory() as session:
        fresh = await session.get_one(AgentRun, run.id)
        assert "commitOwner" not in fresh.result
        assert "commitState" not in fresh.result


async def test_expired_claim_uses_read_only_reconcile_never_reposts(
    migration: Migration, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = await pending_apply(migration, monkeypatch)
    harness = migration.harness
    async with harness.session_factory() as session, session.begin():
        row = await session.get_one(AgentRun, run.id)
        row.simulated = False
        row.result = {
            **row.result,
            "commitState": "IN_PROGRESS",
            "commitClaimedAt": (harness.clock.now() - COMMIT_CLAIM_TTL).isoformat(),
        }
    writer = FakeRepoCommitWriter(
        files={
            "app/static/index.html": "\n" * 118
            + "    <script>\n      renderStatus(data;\n    </script>\n"
        }
    )
    async with harness.session_factory() as session:
        outcome = await ApplyCommitService(
            session, harness.clock, writer, enabled=True
        ).prepare_redeploy_commit(run.id)
    assert writer.requests == []
    assert outcome.stopped
    async with harness.session_factory() as session:
        fresh = await session.get_one(AgentRun, run.id)
        assert "redeployRequestId" not in fresh.result


@pytest.mark.parametrize("operation", ["release", "fail", "publish", "link"])
async def test_sql_owner_mismatch_changes_zero_rows(
    migration: Migration, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    run = await pending_apply(migration, monkeypatch)
    harness = migration.harness
    async with harness.session_factory() as first, first.begin():
        repo = AgentRunRepository(first)
        assert await repo.claim_commit(run.id, harness.clock.now(), "old", None)
    async with harness.session_factory() as second, second.begin():
        repo = AgentRunRepository(second)
        assert not await repo.claim_commit(run.id, harness.clock.now(), "wrong", "not-owner")
        assert await repo.claim_commit(run.id, harness.clock.now(), "new", "old")
    fields = {
        "release": {"commitState": None},
        "fail": {"commitState": "FAILED"},
        "publish": {"commitState": "DONE", "repoCommitted": True},
        "link": {"redeployRequestId": str(uuid.uuid4())},
    }[operation]
    async with harness.session_factory() as first, first.begin():
        assert not await AgentRunRepository(first).patch_owned_commit(
            run.id,
            harness.clock.now(),
            "old",
            fields,
            fail_code="REPO_WRITE_FAILED" if operation == "fail" else None,
        )
    async with harness.session_factory() as session:
        fresh = await session.get_one(AgentRun, run.id)
        assert fresh.status == AgentRunStatus.SUCCEEDED
        assert fresh.result["commitOwner"] == "new"
        assert fresh.result["commitState"] == "IN_PROGRESS"
        assert "redeployRequestId" not in fresh.result
