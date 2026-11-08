"""Real PostgreSQL/API approval boundaries; provider/cloud effects remain fixtures."""

import copy
import uuid

import pytest
from sqlalchemy import select

from app.clients.repo_commit_writer import FakeRepoCommitWriter, RepoCommitError
from app.core.review_evidence import REVIEW_POLICY, approval_digest
from app.models import AgentPatchApproval, AgentRun, DeployRequest
from app.services.apply_commit_service import ApplyCommitService
from tests.integration.migration_support import Migration
from tests.integration.runner_support import add_runner, prechecks_path
from tests.integration.test_apply_commit_to_repo import REPO_SHA, _use_writer
from tests.integration.test_failed_deploy_diagnosis import (
    DIAGNOSIS_CAPABILITIES,
    _claim,
    _detail,
    _failed_deploy,
    _request_diagnosis,
)
from tests.test_diagnosis_result import _body, _diagnosis
from tests.test_patch_review import reviewed_patch


async def setup(migration: Migration):
    migration.harness.use_settings(
        ai_diagnosis_runner_enabled=True, ai_patch_review_required=True, patch_apply_enabled=True
    )
    failed = await _failed_deploy(migration)
    runner = await add_runner(migration.harness.session_factory)
    response = await _request_diagnosis(migration, failed["id"])
    assert response.status_code == 201, response.text
    claimed = await _claim(runner, DIAGNOSIS_CAPABILITIES)
    assert claimed.run["inputSnapshot"]["reviewPolicy"] == REVIEW_POLICY
    patch = reviewed_patch(failed["commitSha"])
    return runner, claimed, patch


async def test_store_approve_apply_and_redeploy(migration: Migration) -> None:
    runner, claimed, patch = await setup(migration)
    original = (
        "# fixture\n" * 211
        + "def index():\n"
        + '    return FileResponse(INDEX_HTML, media_type="text/html; charset=utf-8"\n'
    )
    writer = FakeRepoCommitWriter(files={"app/main.py": original}, commit_sha=REPO_SHA)
    _use_writer(writer)
    migration.harness.use_settings(ai_apply_commit_enabled=True)
    response = await claimed.post("result", _body(patches=[patch]))
    assert response.status_code == 200, response.text
    visible = (await _detail(migration, claimed.run_id))["patches"][0]
    assert visible["reviewEvidence"] == patch["reviewEvidence"]
    path = prechecks_path(migration, f"/{claimed.run_id}/patches/{patch['id']}/approve")
    body = {"patchHash": patch["patchHash"]}
    missing = await migration.approver_client.post(path, json=body)
    assert missing.status_code == 409 and missing.json()["code"] == "PATCH_STALE"
    body["reviewEvidenceHash"] = patch["reviewEvidence"]["evidenceHash"]
    approved = await migration.approver_client.post(path, json=body)
    assert approved.status_code == 200, approved.text
    repeated = await migration.approver_client.post(path, json=body)
    assert repeated.status_code == 200
    stale = await migration.approver_client.post(
        path, json={**body, "reviewEvidenceHash": "sha256:" + "0" * 64}
    )
    assert stale.status_code == 409
    applied = await _claim(runner, DIAGNOSIS_CAPABILITIES)
    snapshot = applied.run["inputSnapshot"]
    async with migration.harness.session_factory() as session:
        approval = await session.scalar(
            select(AgentPatchApproval).where(AgentPatchApproval.run_id == uuid.UUID(claimed.run_id))
        )
        assert approval is not None and approval.policy_version == REVIEW_POLICY
        assert approval.checks_digest == approval_digest(snapshot["patch"])
    done = await applied.post(
        "result",
        {
            "status": "SUCCEEDED",
            "failureCode": None,
            "simulated": False,
            "appliedCommitSha": "2" * 40,
            "branch": snapshot["branch"],
        },
    )
    assert done.status_code == 200, done.text
    after = (await _detail(migration, claimed.run_id))["patches"][0]["apply"]
    assert after["redeployRequestId"] is not None
    assert after["appliedCommitSha"] == REPO_SHA
    redeploy = await migration.harness.get_request(after["redeployRequestId"])
    assert redeploy["commitSha"] == REPO_SHA
    assert len(writer.requests) == 1
    assert writer.requests[0].files["app/main.py"] == original.replace(
        'charset=utf-8"', 'charset=utf-8")'
    )


@pytest.mark.parametrize("problem", ["missing", "wrong_commit", "simulated", "reject"])
async def test_bad_result_not_saved_and_no_apply(migration: Migration, problem: str) -> None:
    _, claimed, patch = await setup(migration)
    body = _body(patches=[patch])
    if problem == "missing":
        patch.pop("reviewEvidence")
    elif problem == "wrong_commit":
        body["patches"] = [reviewed_patch("0" * 40)]
    elif problem == "simulated":
        body["simulated"] = True
    else:
        patch["reviewEvidence"]["review"]["verdict"] = "REJECT"
    response = await claimed.post("result", body)
    assert response.status_code == 422, response.text
    detail = await _detail(migration, claimed.run_id)
    assert detail["patches"] == []
    # A genuine no-patch rejection can still finish without blocking delivery.
    accepted = await claimed.post(
        "result",
        _body(
            patches=[],
            diagnosis=_diagnosis(fixable=False, unfixableReason="Independent review did not pass"),
        ),
    )
    assert accepted.status_code == 200


async def test_post_approval_test_change_blocks_apply_success(migration: Migration) -> None:
    runner, claimed, patch = await setup(migration)
    assert (await claimed.post("result", _body(patches=[patch]))).status_code == 200
    approved = await migration.approver_client.post(
        prechecks_path(migration, f"/{claimed.run_id}/patches/{patch['id']}/approve"),
        json={
            "patchHash": patch["patchHash"],
            "reviewEvidenceHash": patch["reviewEvidence"]["evidenceHash"],
        },
    )
    assert approved.status_code == 200, approved.text
    applied = await _claim(runner, DIAGNOSIS_CAPABILITIES)
    async with migration.harness.session_factory() as session, session.begin():
        row = await session.get(AgentRun, uuid.UUID(applied.run_id))
        assert row is not None
        data = copy.deepcopy(row.input_snapshot)
        data["patch"].pop("reviewEvidence")
        data.pop("reviewPolicy")
        row.input_snapshot = data
    response = await applied.post(
        "result",
        {
            "status": "SUCCEEDED",
            "failureCode": None,
            "simulated": False,
            "appliedCommitSha": "2" * 40,
            "branch": applied.run["inputSnapshot"]["branch"],
        },
    )
    assert response.status_code == 422, response.text
    assert (await _detail(migration, claimed.run_id))["patches"][0]["apply"][
        "redeployRequestId"
    ] is None


async def test_remote_commit_rechecks_stripped_review_before_writer(migration: Migration) -> None:
    _, claimed, patch = await setup(migration)
    assert (await claimed.post("result", _body(patches=[patch]))).status_code == 200
    approved = await migration.approver_client.post(
        prechecks_path(migration, f"/{claimed.run_id}/patches/{patch['id']}/approve"),
        json={
            "patchHash": patch["patchHash"],
            "reviewEvidenceHash": patch["reviewEvidence"]["evidenceHash"],
        },
    )
    assert approved.status_code == 200
    writer = FakeRepoCommitWriter(files={})
    async with migration.harness.session_factory() as session, session.begin():
        parent = await session.get(AgentRun, uuid.UUID(claimed.run_id))
        apply = await session.get(AgentRun, uuid.UUID(approved.json()["data"]["apply"]["runId"]))
        assert parent is not None and apply is not None
        failed = await session.get(DeployRequest, parent.deploy_request_id)
        assert failed is not None
    data = copy.deepcopy(apply.input_snapshot)
    data.pop("reviewPolicy")
    data["patch"].pop("reviewEvidence")
    apply.input_snapshot = data
    async with migration.harness.session_factory() as session:
        service = ApplyCommitService(session, migration.harness.clock, writer, enabled=True)
        with pytest.raises(RepoCommitError):
            await service._build_request(apply, parent, failed)
    assert writer.reads == [] and writer.requests == []
