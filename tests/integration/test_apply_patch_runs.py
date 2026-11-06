"""AI 패치 반영 실행(APPLY_PATCH)을 러너가 가져가고 결과를 낸다(SPEC api-10-2 AC-1~6).

migration 픽스처: 개발자 A(author_client)가 사전 점검을 만들고 개발자 B(approver_client)가 승인한다.
스위치는 harness.use_settings(patch_apply_enabled=True)로 켠다.
"""

import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import Response
from sqlalchemy import select, update

from app.audit.actions import AuditAction
from app.enums import ActorType, AgentRunKind, AgentRunStatus, AuditResult
from app.main import app
from app.models import AgentRun
from app.services.run_result import compute_patch_hash
from tests.integration.auth_support import create_agent_token, make_client
from tests.integration.migration_support import Migration
from tests.integration.runner_support import (
    ClaimedRunFixture,
    RunnerFixture,
    add_runner,
    claim,
    finish_precheck,
    prechecks_path,
    request_precheck,
    run_audit_rows,
    run_patch,
    run_result,
    run_row,
)

COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"
APPLY_CAPABILITIES = ["PRECHECK", "APPLY_PATCH"]
# 응답 예를 PR 본문에 싣고 싶을 때만 경로를 준다(기본은 쓰지 않음).
_EXAMPLE_DIR = os.environ.get("ECHO_APPLY_EXAMPLE_DIR")


@pytest.fixture
async def runner(migration: Migration) -> RunnerFixture:
    return await add_runner(migration.harness.session_factory)


def _error(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["code"]


def _details(response: Response) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = response.json()["details"]
    return details


def _save_example(name: str, value: Any) -> None:
    if _EXAMPLE_DIR:
        Path(_EXAMPLE_DIR, f"{name}.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=2)
        )


async def _approved_patch(migration: Migration, runner: RunnerFixture) -> tuple[str, str]:
    """스위치 켬 → 사전 점검 성공(규칙 패치 p1) → 개발자 B 승인. (부모 id, 반영 실행 id)."""
    detail = await finish_precheck(migration, runner, patches=[run_patch("p1", origin="RULE")])
    parent_id: str = detail["id"]
    migration.harness.use_settings(patch_apply_enabled=True)
    response = await migration.approver_client.post(
        prechecks_path(migration, f"/{parent_id}/patches/p1/approve"),
        json={"patchHash": run_patch("p1")["patchHash"]},
    )
    assert response.status_code == 200, response.text
    apply_run_id: str = response.json()["data"]["apply"]["runId"]
    return parent_id, apply_run_id


async def _claim_apply(runner: RunnerFixture) -> tuple[ClaimedRunFixture, dict[str, Any]]:
    response = await claim(runner, capabilities=APPLY_CAPABILITIES)
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    assert data["run"] is not None
    return ClaimedRunFixture(data["run"]["id"], data["runToken"], data["run"]), data


def _apply_result(apply_run_id: str, **changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "status": "SUCCEEDED",
        "failureCode": None,
        "simulated": False,
        "appliedCommitSha": COMMIT_SHA,
        "branch": f"applied/{apply_run_id}",
    }
    return body | changes


async def _patch_in_detail(migration: Migration, parent_id: str) -> dict[str, Any]:
    response = await migration.author_client.get(prechecks_path(migration, f"/{parent_id}"))
    assert response.status_code == 200, response.text
    patch: dict[str, Any] = response.json()["data"]["patches"][0]
    return patch


# --- AC-1: 끝까지 ---


async def test_apply_patch_round_trip(migration: Migration, runner: RunnerFixture) -> None:
    parent_id, apply_run_id = await _approved_patch(migration, runner)
    harness = migration.harness

    # 사전 점검만 하는 러너(지금 러너)는 반영 실행을 받지 않는다.
    precheck_only = await claim(runner, capabilities=["PRECHECK"])
    assert precheck_only.json()["data"] == {"run": None, "runToken": None}

    claimed, data = await _claim_apply(runner)

    _save_example("claim_apply_patch", {"data": data | {"runToken": "echo_run_…"}})
    run = data["run"]
    assert run["id"] == apply_run_id
    assert run["kind"] == "APPLY_PATCH"
    assert run["orgId"] == str(harness.organization.id)
    assert run["serviceId"] == migration.service_id
    patch = run_patch("p1", origin="RULE")
    assert run["inputSnapshot"] == {
        "schemaVersion": 1,
        "parentRunId": parent_id,
        "patch": {
            "id": "p1",
            "patchHash": patch["patchHash"],
            "files": patch["files"],
            "diff": patch["diff"],
            "origin": "RULE",
        },
        "repo": "Softbank-Hackathon-2026-Team-Echo/echo-ai-agent",
        "branch": f"applied/{apply_run_id}",
    }
    # 러너가 다시 계산하는 해시(PD-14)와 승인 지문이 같다.
    assert compute_patch_hash(run["inputSnapshot"]["patch"]["diff"]) == patch["patchHash"]
    claimed_view = await _patch_in_detail(migration, parent_id)
    assert (claimed_view["status"], claimed_view["apply"]["status"]) == ("APPROVED", "CLAIMED")

    heartbeat = await claimed.post("heartbeat")
    assert heartbeat.status_code == 200, heartbeat.text
    running = await _patch_in_detail(migration, parent_id)
    assert (running["status"], running["apply"]["status"]) == ("APPROVED", "RUNNING")

    harness.clock.advance(30)
    result = await claimed.post("result", _apply_result(apply_run_id))

    assert result.status_code == 200, result.text
    assert result.json()["data"] == {"status": "SUCCEEDED"}
    _save_example("result_apply_patch", result.json())
    applied = await _patch_in_detail(migration, parent_id)
    _save_example("detail_patch_applied", applied)
    assert applied["status"] == "APPLIED"
    assert applied["apply"] == {
        "runId": apply_run_id,
        "status": "SUCCEEDED",
        "failureCode": None,
        "finishedAt": harness.clock.now().isoformat().replace("+00:00", "Z"),
        "appliedCommitSha": COMMIT_SHA,
        "branch": f"applied/{apply_run_id}",
        "simulated": False,
        "pullRequestUrl": None,
        "redeployRequestId": None,
        "redeployRejectedCode": None,
    }
    row = await run_row(migration, apply_run_id)
    assert row.status == AgentRunStatus.SUCCEEDED
    assert row.result == {
        "appliedCommitSha": COMMIT_SHA,
        "branch": f"applied/{apply_run_id}",
    }
    assert (row.model_id, row.processing, row.simulated) == (None, None, False)

    lines = [
        line
        for line in await run_audit_rows(migration, apply_run_id)
        if line.action in (AuditAction.AGENT_RUN_CLAIMED, AuditAction.AGENT_RUN_FINISHED)
    ]
    assert [line.action for line in lines] == [
        AuditAction.AGENT_RUN_CLAIMED,
        AuditAction.AGENT_RUN_FINISHED,
    ]
    assert all(line.actor_type == ActorType.SYSTEM for line in lines)
    assert lines[0].details == {"runnerName": runner.runner.name, "kind": "APPLY_PATCH"}
    assert lines[1].result == AuditResult.SUCCEEDED
    assert lines[1].details == {
        "kind": "APPLY_PATCH",
        "status": "SUCCEEDED",
        "simulated": False,
        "aiReviewed": False,
        "appliedCommitSha": COMMIT_SHA,
    }

    # 끝났으니 서비스 잠금이 풀리고, 두 번째 결과는 409 다.
    again = await claimed.post("result", _apply_result(apply_run_id))
    assert _error(again) == (409, "RESULT_ALREADY_SUBMITTED")
    created = await request_precheck(migration)
    assert created.status_code == 201, created.text


async def test_apply_patch_failed_result_shows_apply_failed(
    migration: Migration, runner: RunnerFixture
) -> None:
    parent_id, apply_run_id = await _approved_patch(migration, runner)
    claimed, _ = await _claim_apply(runner)

    response = await claimed.post(
        "result",
        _apply_result(
            apply_run_id, status="FAILED", failureCode="BUILD_FAILED", appliedCommitSha=None
        ),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"status": "FAILED"}
    patch = await _patch_in_detail(migration, parent_id)
    assert patch["status"] == "APPLY_FAILED"
    assert patch["apply"]["status"] == "FAILED"
    assert patch["apply"]["failureCode"] == "BUILD_FAILED"
    assert patch["apply"]["appliedCommitSha"] is None
    finished = (await run_audit_rows(migration, apply_run_id))[-1]
    assert finished.result == AuditResult.FAILED
    assert finished.details["failureCode"] == "BUILD_FAILED"
    assert finished.details["appliedCommitSha"] is None


async def test_apply_patch_success_without_commit_is_applied(
    migration: Migration, runner: RunnerFixture
) -> None:
    """git 없는 사본(VM)에서 반영·검증만 한 성공: 커밋 SHA 는 null 이다."""
    parent_id, apply_run_id = await _approved_patch(migration, runner)
    claimed, _ = await _claim_apply(runner)

    response = await claimed.post("result", _apply_result(apply_run_id, appliedCommitSha=None))

    assert response.status_code == 200, response.text
    patch = await _patch_in_detail(migration, parent_id)
    assert patch["status"] == "APPLIED"
    assert (patch["apply"]["appliedCommitSha"], patch["apply"]["branch"]) == (
        None,
        f"applied/{apply_run_id}",
    )
    finished = (await run_audit_rows(migration, apply_run_id))[-1]
    assert finished.details["appliedCommitSha"] is None


async def test_apply_patch_simulated_result_is_shown(
    migration: Migration, runner: RunnerFixture
) -> None:
    parent_id, apply_run_id = await _approved_patch(migration, runner)
    claimed, _ = await _claim_apply(runner)

    response = await claimed.post("result", _apply_result(apply_run_id, simulated=True))

    assert response.status_code == 200, response.text
    patch = await _patch_in_detail(migration, parent_id)
    assert (patch["status"], patch["apply"]["simulated"]) == ("APPLIED", True)


async def test_apply_patch_lease_expiry_shows_timed_out(
    migration: Migration, runner: RunnerFixture
) -> None:
    parent_id, apply_run_id = await _approved_patch(migration, runner)
    claimed, _ = await _claim_apply(runner)
    migration.harness.clock.advance(61)

    late = await claimed.post("result", _apply_result(apply_run_id))

    assert _error(late) == (409, "RUN_NOT_ACTIVE")
    patch = await _patch_in_detail(migration, parent_id)
    assert patch["status"] == "APPLY_FAILED"
    assert (patch["apply"]["status"], patch["apply"]["failureCode"]) == (
        "TIMED_OUT",
        "LEASE_EXPIRED",
    )


# --- AC-2: 지문 ---


async def _change_snapshot(migration: Migration, apply_run_id: str, **patch_changes: Any) -> None:
    async with migration.harness.session_factory() as session, session.begin():
        row = await session.get(AgentRun, uuid.UUID(apply_run_id))
        assert row is not None
        snapshot = row.input_snapshot
        changed = snapshot | {"patch": snapshot["patch"] | patch_changes}
        await session.execute(
            update(AgentRun)
            .where(AgentRun.id == uuid.UUID(apply_run_id))
            .values(input_snapshot=changed)
        )


async def test_apply_patch_snapshot_hash_differs_from_approval_is_rejected(
    migration: Migration, runner: RunnerFixture
) -> None:
    """준 입력 사본의 patchHash 가 승인 지문과 다르면 성공 결과를 받지 않고 실행은 연 채로 둔다."""
    parent_id, apply_run_id = await _approved_patch(migration, runner)
    claimed, _ = await _claim_apply(runner)
    await _change_snapshot(migration, apply_run_id, patchHash=compute_patch_hash("other diff"))

    response = await claimed.post("result", _apply_result(apply_run_id))

    assert _error(response) == (422, "RESULT_INVALID")
    assert _details(response) == [
        {"field": "inputSnapshot.patch.patchHash", "reason": "PATCH_HASH_MISMATCH"}
    ]
    row = await run_row(migration, apply_run_id)
    assert row.status == AgentRunStatus.CLAIMED
    assert row.result is None
    assert (await _patch_in_detail(migration, parent_id))["status"] == "APPROVED"
    # 러너가 다시 계산해 불일치를 실패로 알리는 길은 열려 있다(계약 2.1).
    failure = await claimed.post(
        "result",
        _apply_result(
            apply_run_id, status="FAILED", failureCode="PATCH_HASH_MISMATCH", appliedCommitSha=None
        ),
    )
    assert failure.status_code == 200, failure.text
    assert (await _patch_in_detail(migration, parent_id))["status"] == "APPLY_FAILED"


async def test_apply_patch_server_recomputes_hash_from_input_diff(
    migration: Migration, runner: RunnerFixture
) -> None:
    """입력 사본의 diff 가 승인 지문과 달라졌으면(가린 diff 등) 성공 결과를 받지 않는다."""
    _, apply_run_id = await _approved_patch(migration, runner)
    claimed, data = await _claim_apply(runner)
    await _change_snapshot(
        migration, apply_run_id, diff=data["run"]["inputSnapshot"]["patch"]["diff"] + "x"
    )

    success = await claimed.post("result", _apply_result(apply_run_id))

    assert _error(success) == (422, "RESULT_INVALID")
    assert _details(success) == [
        {"field": "inputSnapshot.patch.diff", "reason": "PATCH_HASH_MISMATCH"}
    ]
    ok = await claimed.post(
        "result",
        _apply_result(
            apply_run_id, status="FAILED", failureCode="PATCH_HASH_MISMATCH", appliedCommitSha=None
        ),
    )
    assert ok.status_code == 200, ok.text


async def test_apply_patch_branch_mismatch_is_rejected(
    migration: Migration, runner: RunnerFixture
) -> None:
    _, apply_run_id = await _approved_patch(migration, runner)
    claimed, _ = await _claim_apply(runner)

    response = await claimed.post("result", _apply_result(apply_run_id, branch="main"))

    assert _error(response) == (422, "RESULT_INVALID")
    assert _details(response) == [{"field": "branch", "reason": "BRANCH_MISMATCH"}]
    # 실패 결과는 브랜치를 대조하지 않는다(러너가 입력 사본 검사에서 멈춘 경우).
    failure = await claimed.post(
        "result",
        _apply_result(
            apply_run_id,
            status="FAILED",
            failureCode="INPUT_INVALID",
            appliedCommitSha=None,
            branch="main",
        ),
    )
    assert failure.status_code == 200, failure.text


async def test_apply_patch_lease_expiry_is_checked_before_fingerprint(
    migration: Migration, runner: RunnerFixture
) -> None:
    """임대가 지난 뒤 브랜치가 틀린 결과: 422 가 아니라 409 이고 만료가 저장된다."""
    parent_id, apply_run_id = await _approved_patch(migration, runner)
    claimed, _ = await _claim_apply(runner)
    migration.harness.clock.advance(61)

    late = await claimed.post("result", _apply_result(apply_run_id, branch="main"))

    assert _error(late) == (409, "RUN_NOT_ACTIVE")
    row = await run_row(migration, apply_run_id)
    assert (row.status, row.failure_code) == (AgentRunStatus.TIMED_OUT, "LEASE_EXPIRED")
    assert row.result is None
    timed_out = (await run_audit_rows(migration, apply_run_id))[-1]
    assert timed_out.action == AuditAction.AGENT_RUN_TIMED_OUT
    patch = await _patch_in_detail(migration, parent_id)
    assert (patch["status"], patch["apply"]["status"]) == ("APPLY_FAILED", "TIMED_OUT")


async def test_apply_patch_failed_result_with_commit_sha_is_rejected(
    migration: Migration, runner: RunnerFixture
) -> None:
    """SPEC 3절: FAILED 결과의 appliedCommitSha 는 null 이어야 한다. 아니면 422, 저장 없음."""
    parent_id, apply_run_id = await _approved_patch(migration, runner)
    claimed, _ = await _claim_apply(runner)

    response = await claimed.post(
        "result",
        _apply_result(apply_run_id, status="FAILED", failureCode="BUILD_FAILED"),
    )

    assert _error(response) == (422, "RESULT_INVALID")
    row = await run_row(migration, apply_run_id)
    assert row.status == AgentRunStatus.CLAIMED
    assert row.result is None
    assert (await _patch_in_detail(migration, parent_id))["status"] == "APPROVED"


async def test_precheck_result_patch_hash_must_be_hash_of_diff(
    migration: Migration, runner: RunnerFixture
) -> None:
    """PD-14: 사전 점검 결과의 patchHash 를 diff 로 다시 계산해 다르면 422, 실행은 그대로다."""
    response = await request_precheck(migration)
    assert response.status_code == 201, response.text
    claimed_response = await claim(runner)
    data = claimed_response.json()["data"]
    claimed = ClaimedRunFixture(data["run"]["id"], data["runToken"], data["run"])
    wrong = run_patch("p1", patchHash=compute_patch_hash("other diff"))

    rejected = await claimed.post("result", run_result(migration, patches=[wrong]))

    assert _error(rejected) == (422, "RESULT_INVALID")
    assert _details(rejected)[0]["field"] == "patches.0"
    assert "patchHash must be sha256 of diff" in _details(rejected)[0]["reason"]
    assert (await run_row(migration, claimed.run_id)).status == AgentRunStatus.CLAIMED
    accepted = await claimed.post("result", run_result(migration, patches=[run_patch("p1")]))
    assert accepted.status_code == 200, accepted.text


# --- AC-3: 본문 모양은 실행 종류로 ---


async def test_result_body_is_chosen_by_run_kind(
    migration: Migration, runner: RunnerFixture
) -> None:
    _, apply_run_id = await _approved_patch(migration, runner)
    claimed, _ = await _claim_apply(runner)

    precheck_body = await claimed.post("result", run_result(migration))
    missing = await claimed.post(
        "result",
        {key: value for key, value in _apply_result(apply_run_id).items() if key != "branch"},
    )
    extra = await claimed.post("result", _apply_result(apply_run_id, checks=[]))
    with_hash = await claimed.post(
        "result", _apply_result(apply_run_id, patchHash=run_patch("p1")["patchHash"])
    )
    bad_sha = await claimed.post("result", _apply_result(apply_run_id, appliedCommitSha="abc"))
    failed_without_code = await claimed.post(
        "result", _apply_result(apply_run_id, status="FAILED", appliedCommitSha=None)
    )

    for response in (precheck_body, missing, extra, with_hash, bad_sha, failed_without_code):
        assert _error(response) == (422, "RESULT_INVALID"), response.text
    assert (await run_row(migration, apply_run_id)).status == AgentRunStatus.CLAIMED


async def test_precheck_run_does_not_accept_apply_result_body(
    migration: Migration, runner: RunnerFixture
) -> None:
    response = await request_precheck(migration)
    assert response.status_code == 201, response.text
    data = (await claim(runner, capabilities=APPLY_CAPABILITIES)).json()["data"]
    assert data["run"]["kind"] == "PRECHECK"
    claimed = ClaimedRunFixture(data["run"]["id"], data["runToken"], data["run"])

    rejected = await claimed.post("result", _apply_result(claimed.run_id))

    assert _error(rejected) == (422, "RESULT_INVALID")


# --- AC-4: 가져가기 종류 ---


@pytest.mark.parametrize("capabilities", [["PRECHECK"], [], ["CHANGE_PROPOSAL"], ["apply_patch"]])
async def test_claim_without_apply_capability_does_not_give_apply_run(
    migration: Migration, runner: RunnerFixture, capabilities: list[str]
) -> None:
    _, apply_run_id = await _approved_patch(migration, runner)

    response = await claim(runner, capabilities=capabilities)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["run"] is None
    assert (await run_row(migration, apply_run_id)).status == AgentRunStatus.QUEUED


async def test_claim_apply_only_capability_gets_apply_run(
    migration: Migration, runner: RunnerFixture
) -> None:
    _, apply_run_id = await _approved_patch(migration, runner)

    response = await claim(runner, capabilities=["APPLY_PATCH"])

    assert response.json()["data"]["run"]["id"] == apply_run_id
    # 같은 실행을 두 번 주지 않는다.
    again = await claim(runner, capabilities=APPLY_CAPABILITIES)
    assert again.json()["data"]["run"] is None


async def test_claim_apply_only_capability_does_not_get_precheck(
    migration: Migration, runner: RunnerFixture
) -> None:
    response = await request_precheck(migration)
    assert response.status_code == 201, response.text

    claimed = await claim(runner, capabilities=["APPLY_PATCH"])

    assert claimed.json()["data"]["run"] is None


# --- AC-5: 스위치 꺼짐은 그대로 ---


async def test_switch_off_creates_no_apply_run_and_claim_is_unchanged(
    migration: Migration, runner: RunnerFixture
) -> None:
    detail = await finish_precheck(migration, runner, patches=[run_patch("p1")])
    assert migration.harness.settings.patch_apply_enabled is False
    response = await migration.approver_client.post(
        prechecks_path(migration, f"/{detail['id']}/patches/p1/approve"),
        json={"patchHash": run_patch("p1")["patchHash"]},
    )
    assert response.status_code == 200, response.text
    assert (response.json()["data"]["status"], response.json()["data"]["apply"]) == (
        "APPROVED",
        None,
    )

    claimed = await claim(runner, capabilities=APPLY_CAPABILITIES)

    assert claimed.json()["data"]["run"] is None
    async with migration.harness.session_factory() as session:
        kinds = (await session.scalars(select(AgentRun.kind))).all()
    assert AgentRunKind.APPLY_PATCH not in kinds


# --- AC-6: 권한·OpenAPI ---


async def test_apply_run_token_is_scoped_and_other_credentials_are_refused(
    migration: Migration, runner: RunnerFixture
) -> None:
    parent_id, apply_run_id = await _approved_patch(migration, runner)
    claimed, _ = await _claim_apply(runner)
    harness = migration.harness
    _, agent_token = await create_agent_token(
        harness.session_factory, harness.organization, harness.admin.user, harness.clock.now()
    )
    body = _apply_result(apply_run_id)

    async with make_client(agent_token=agent_token, origin=None) as agent:
        by_agent = await agent.post(claimed.path("result"), json=body)
    by_human = await migration.approver_client.post(claimed.path("result"), json=body)
    async with make_client(agent_token=runner.token, origin=None) as runner_client:
        by_runner_token = await runner_client.post(claimed.path("result"), json=body)
    async with claimed.client() as client:
        on_parent = await client.post(claimed.path("result", parent_id), json=run_result(migration))

    assert _error(by_agent) == (403, "RUNNER_FORBIDDEN")
    assert _error(by_human) == (403, "RUNNER_FORBIDDEN")
    assert _error(by_runner_token) == (401, "RUNNER_TOKEN_INVALID")
    assert _error(on_parent) == (401, "RUNNER_TOKEN_INVALID")
    assert (await run_row(migration, apply_run_id)).status == AgentRunStatus.CLAIMED


def test_openapi_result_body_documents_all_shapes() -> None:
    """RunResult(사전 점검·배포 실패 진단, diagnosis 선택 칸)·ApplyPatchResult(반영) 두 모양.

    진단 결과를 따로 된 모양으로 두면 diagnosis 없는 RunResult 본문이 두 모양에 함께 맞아 oneOf
    가 겹친다(api-98 검증 반영). diagnosis 는 RunResult 의 선택 칸이다.
    """
    operation = app.openapi()["paths"]["/api/v1/runner/runs/{run_id}/result"]["post"]
    schema = operation["requestBody"]["content"]["application/json"]["schema"]

    shapes = schema["oneOf"]
    assert len(shapes) == 2
    assert "patches" in shapes[0]["properties"]
    assert "diagnosis" in shapes[0]["properties"]
    assert "diagnosis" not in shapes[0].get("required", [])
    assert set(shapes[1]["properties"]) == {
        "status",
        "failureCode",
        "simulated",
        "appliedCommitSha",
        "branch",
    }
    assert "$ref" not in json.dumps(schema)
