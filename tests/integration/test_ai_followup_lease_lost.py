"""PR138 교차 검수 F2 회귀: claim TTL 뒤 늦게 끝난 이전 owner 의 결과 제출·승인 경로.

검수자 탐침 P1(실 PG 두 세션, TTL 경과) 조건을 원본 탐침 그대로 다시 쓴다. 이전 owner 는
저장소 쓰기 뒤 소유권을 잃고(LeaseLostError) 재배포를 잇지 못한다. 결과 저장은 이미
커밋됐으므로 응답은 기준선과 같은 2xx 이고, 재배포 연결 0·새 owner 기록은 그대로다.
외부 효과는 합성 writer 이며 실 GitHub 호출은 0이다.
"""

import asyncio
import logging
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.clients.repo_commit_writer import FakeRepoCommitWriter, RepoCommitRequest, RepoCommitResult
from app.dependencies import get_deploy_request_service, get_repo_commit_writer
from app.enums import AgentRunStatus
from app.main import app
from app.models import AgentRun, DeployRequest, Job
from app.services.apply_commit_service import COMMIT_CLAIM_TTL, ApplyCommitService
from app.services.diagnosis_service import DiagnosisService
from tests.integration.migration_support import Migration
from tests.integration.runner_support import RunnerFixture, add_runner, event
from tests.integration.test_ai_followup_recovery import pending_apply
from tests.integration.test_apply_commit_to_repo import PATCH, SANDBOX_SHA, _diagnosis_result
from tests.integration.test_failed_deploy_diagnosis import (
    DIAGNOSIS_CAPABILITIES,
    _approve,
    _claim,
    _data,
    _failed_deploy,
    _request_diagnosis,
)

PROBE_FILES = {
    "app/static/index.html": "\n" * 118 + "    <script>\n      renderStatus(data;\n    </script>\n"
}
SKIP_LOG = "diagnosis redeploy skipped: repository commit ownership lost"


class BlockingWriter(FakeRepoCommitWriter):
    """탐침 P1 의 CountingWriter 와 같다: 첫 POST 를 붙잡아 두고 POST·reconcile 횟수를 센다."""

    def __init__(self, files: dict[str, str], block: asyncio.Event) -> None:
        super().__init__(files=dict(files))
        self.block = block
        self.entered = asyncio.Event()
        self.posts = 0
        self.reconciles = 0

    async def commit_and_open_pull_request(self, request: RepoCommitRequest) -> RepoCommitResult:
        self.posts += 1
        self.requests.append(request)
        self.entered.set()
        await self.block.wait()
        return RepoCommitResult(self.commit_sha, request.branch, self.pull_request_url)

    async def reconcile(self, request: RepoCommitRequest) -> RepoCommitResult:
        self.reconciles += 1
        return await super().reconcile(request)


async def _ids(migration: Migration) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    async with migration.harness.session_factory() as session:
        requests = set(await session.scalars(select(DeployRequest.id)))
        jobs = set(await session.scalars(select(Job.id)))
    return requests, jobs


async def _assert_new_owner_kept(
    migration: Migration, run_id: uuid.UUID, owner: str | None
) -> None:
    async with migration.harness.session_factory() as session:
        fresh = await session.get_one(AgentRun, run_id)
        assert fresh.status == AgentRunStatus.SUCCEEDED
        assert fresh.result["commitOwner"] == owner
        assert fresh.result["commitState"] == "IN_PROGRESS"  # 늦은 old 의 release 0행
        assert not fresh.result.get("repoCommitted")
        assert "redeployRequestId" not in fresh.result
        assert "redeployRejectedCode" not in fresh.result


async def test_late_runner_result_after_takeover_returns_200_without_redeploy(
    migration: Migration, caplog: pytest.LogCaptureFixture
) -> None:
    """러너 결과 제출(POST .../result) 경로. 기준선 200, 수정 전 후보 500."""
    harness = migration.harness
    harness.use_settings(
        ai_diagnosis_runner_enabled=True, patch_apply_enabled=True, ai_apply_commit_enabled=True
    )
    release = asyncio.Event()
    # 두 요청이 같은 외부 저장소를 본다.
    writer = BlockingWriter({"Dockerfile": "FROM a\n"}, release)
    app.dependency_overrides[get_repo_commit_writer] = lambda: writer
    runner: RunnerFixture = await add_runner(harness.session_factory)
    failed = await _failed_deploy(migration)
    diagnosis_id = _data(await _request_diagnosis(migration, failed["id"]))["id"]
    claimed = await _claim(runner, DIAGNOSIS_CAPABILITIES)
    await claimed.post("events", {"events": [event(1, "INPUT_LOAD")]})
    submitted = await claimed.post("result", _diagnosis_result())
    assert submitted.status_code == 200, submitted.text
    approved = await _approve(migration, diagnosis_id, PATCH)
    assert approved.status_code == 200, approved.text
    apply_claim = await _claim(runner, DIAGNOSIS_CAPABILITIES)
    run_id = uuid.UUID(apply_claim.run_id)
    before = await _ids(migration)

    async def late_submit() -> Any:
        # 공통 미들웨어의 500 을 그대로 보도록 앱 예외를 테스트로 올리지 않는다.
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
            headers={"Authorization": f"Bearer {apply_claim.run_token}"},
        ) as client:
            return await client.post(
                apply_claim.path("result"),
                json={
                    "status": "SUCCEEDED",
                    "failureCode": None,
                    "simulated": False,
                    "appliedCommitSha": SANDBOX_SHA,
                    "branch": f"applied/{apply_claim.run_id}",
                },
            )

    caplog.set_level(logging.WARNING, logger="app.services.diagnosis_service")
    task = asyncio.create_task(late_submit())
    await asyncio.wait_for(writer.entered.wait(), timeout=10)
    harness.clock.advance(COMMIT_CLAIM_TTL.total_seconds())
    async with harness.session_factory() as second:
        taken = await ApplyCommitService(
            second, harness.clock, writer, enabled=True
        ).prepare_redeploy_commit(run_id)
    # 재진입은 POST 없이 GET-only reconcile 만 쓴다(원본 탐침 P1 과 같은 조건).
    assert writer.posts == 1
    assert writer.reconciles == 1
    assert taken.owner is not None and not taken.stopped
    release.set()
    response = await asyncio.wait_for(task, timeout=10)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "SUCCEEDED"  # 결과 저장은 유지
    await _assert_new_owner_kept(migration, run_id, taken.owner)
    assert await _ids(migration) == before  # 재배포 요청·할 일 0
    assert writer.posts == 1  # 늦은 old 도 다시 쓰지 않는다
    assert [r.message for r in caplog.records].count(SKIP_LOG) == 1


async def test_late_redeploy_after_apply_skips_link_instead_of_raising(
    migration: Migration, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """승인 경로가 부르는 같은 경계(DiagnosisService.redeploy_after_apply). 원본 탐침 P1 준비."""
    run = await pending_apply(migration, monkeypatch)
    harness = migration.harness
    async with harness.session_factory() as session, session.begin():
        row = await session.get_one(AgentRun, run.id)
        row.simulated = False
    release = asyncio.Event()
    writer = BlockingWriter(PROBE_FILES, release)
    before = await _ids(migration)
    caplog.set_level(logging.WARNING, logger="app.services.diagnosis_service")
    async with harness.session_factory() as first, harness.session_factory() as second:
        deploy = get_deploy_request_service(
            first,
            harness.settings,
            harness.clock,
            harness.source_client,
            harness.executors.as_set(),
        )
        old = DiagnosisService(
            first,
            harness.clock,
            deploy,
            simulate_enabled=True,
            apply_commit=ApplyCommitService(first, harness.clock, writer, enabled=True),
        )
        new = ApplyCommitService(second, harness.clock, writer, enabled=True)
        task = asyncio.create_task(old.redeploy_after_apply(run.id))
        await asyncio.wait_for(writer.entered.wait(), timeout=10)
        harness.clock.advance(COMMIT_CLAIM_TTL.total_seconds())
        taken = await new.prepare_redeploy_commit(run.id)
        assert writer.posts == 1
        assert writer.reconciles == 1
        assert taken.owner is not None and not taken.stopped
        release.set()
        returned = await asyncio.wait_for(task, timeout=10)

    assert returned is not None and returned.id == run.id
    await _assert_new_owner_kept(migration, run.id, taken.owner)
    assert await _ids(migration) == before
    assert [r.message for r in caplog.records].count(SKIP_LOG) == 1
