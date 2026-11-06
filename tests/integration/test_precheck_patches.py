"""AI 사전 점검 패치 승인·거절 API(SPEC api-10-patch-approval AC-1~5).

migration 픽스처: 개발자 A(author_client)가 사전 점검을 만들고 개발자 B(approver_client)가 결정한다.
스위치는 harness.use_settings(patch_apply_enabled=True)로 켠다(API 의 설정 의존성이 이 값을 읽는다).
"""

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select, update

from app.audit.actions import AuditAction
from app.enums import ActorType, AgentRunKind, AgentRunStatus, AuditResult, OrganizationRole
from app.models import AgentPatchApproval, AgentRun, AuditLog
from app.repositories.agent_patch_approval_repository import AgentPatchApprovalRepository
from app.services.precheck_patch_service import PATCH_POLICY_VERSION, PrecheckPatchService
from app.services.precheck_service import compute_checks_digest, compute_input_hash
from tests.integration.auth_support import (
    create_agent_token,
    create_member,
    create_organization,
    make_client,
    org_path,
)
from tests.integration.disaster_support import allow_self_approval
from tests.integration.migration_support import Migration
from tests.integration.runner_support import (
    PASSED_PATCH_CHECK,
    RunnerFixture,
    add_runner,
    claim,
    claim_run,
    create_precheck,
    finish_precheck,
    prechecks_path,
    request_precheck,
    run_audit_rows,
    run_patch,
    run_result,
    run_row,
)

UNKNOWN_ID = "00000000-0000-0000-0000-000000000001"
DAY = 24 * 60 * 60


@pytest.fixture
async def runner(migration: Migration) -> RunnerFixture:
    return await add_runner(migration.harness.session_factory)


def _error(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["code"]


def _details(response: Response) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = response.json()["details"]
    return details


async def _finish(migration: Migration, runner: RunnerFixture, *patches: dict[str, Any]) -> str:
    detail = await finish_precheck(migration, runner, patches=list(patches))
    run_id: str = detail["id"]
    return run_id


async def _decide(
    migration: Migration,
    run_id: str,
    action: str,
    patch_id: str = "p1",
    *,
    client: AsyncClient | None = None,
    **body: Any,
) -> Response:
    """결정 호출. patchHash 를 주지 않으면 그 패치의 저장 값이다(run_patch 와 같은 규칙)."""
    body.setdefault("patchHash", run_patch(patch_id)["patchHash"])
    response = await (client or migration.approver_client).post(
        prechecks_path(migration, f"/{run_id}/patches/{patch_id}/{action}"), json=body
    )
    migration.harness.responses.append(response.text)
    return response


async def _patch_in_detail(migration: Migration, run_id: str, patch_id: str = "p1") -> Any:
    response = await migration.author_client.get(prechecks_path(migration, f"/{run_id}"))
    assert response.status_code == 200, response.text
    return next(item for item in response.json()["data"]["patches"] if item["id"] == patch_id)


async def _approvals(migration: Migration, run_id: str) -> list[AgentPatchApproval]:
    async with migration.harness.session_factory() as session:
        rows = await session.scalars(
            select(AgentPatchApproval)
            .where(AgentPatchApproval.run_id == uuid.UUID(run_id))
            .order_by(AgentPatchApproval.patch_id)
        )
        return list(rows.all())


async def _apply_runs(migration: Migration) -> list[AgentRun]:
    async with migration.harness.session_factory() as session:
        rows = await session.scalars(
            select(AgentRun).where(AgentRun.kind == AgentRunKind.APPLY_PATCH)
        )
        return list(rows.all())


async def _open_run_count(migration: Migration) -> int:
    async with migration.harness.session_factory() as session:
        rows = await session.scalars(
            select(AgentRun.id).where(
                AgentRun.status.in_(
                    (AgentRunStatus.QUEUED, AgentRunStatus.CLAIMED, AgentRunStatus.RUNNING)
                )
            )
        )
        return len(rows.all())


async def _decision_audit_rows(migration: Migration, run_id: str) -> list[AuditLog]:
    rows = await run_audit_rows(migration, run_id)
    return [
        row
        for row in rows
        if row.action in (AuditAction.AGENT_PATCH_APPROVED, AuditAction.AGENT_PATCH_REJECTED)
    ]


# --- AC-1: 승인 ---


async def test_patch_approve_round_trip(migration: Migration, runner: RunnerFixture) -> None:
    patch = run_patch("p1", origin="RULE")
    run_id = await _finish(migration, runner, patch)
    harness = migration.harness
    before = await _patch_in_detail(migration, run_id)
    assert (before["status"], before["origin"]) == ("PROPOSED", "RULE")
    assert (before["approval"], before["apply"]) == (None, None)
    harness.clock.advance(60)
    now = harness.clock.now()

    response = await _decide(migration, run_id, "approve", reason="규칙 패치 확인함")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "APPROVED"
    assert data["apply"] is None
    assert data["approval"]["decision"] == "APPROVED"
    assert data["approval"]["decidedByUserId"] == str(migration.approver.user.id)
    assert data["approval"]["reason"] == "규칙 패치 확인함"
    assert data["approval"]["decidedAt"] == now.isoformat().replace("+00:00", "Z")
    assert data == await _patch_in_detail(migration, run_id)
    assert {key: data[key] for key in before if key not in ("status", "approval")} == {
        key: before[key] for key in before if key not in ("status", "approval")
    }
    rows = await _approvals(migration, run_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.patch_id == "p1"
    assert row.patch_hash == patch["patchHash"]
    parent = await run_row(migration, run_id)
    assert parent.result is not None
    assert row.checks_digest == compute_checks_digest(parent.result["patches"][0]["checks"])
    assert row.policy_version == PATCH_POLICY_VERSION
    assert row.apply_run_id is None
    assert await _open_run_count(migration) == 0
    audit = await _decision_audit_rows(migration, run_id)
    assert [item.action for item in audit] == [AuditAction.AGENT_PATCH_APPROVED]
    assert audit[0].actor_type == ActorType.USER
    assert audit[0].actor_user_id == migration.approver.user.id
    assert audit[0].result == AuditResult.SUCCEEDED
    assert audit[0].target_type == "agent_run"
    assert str(audit[0].target_id) == run_id
    assert audit[0].target_label == migration.service["name"]
    assert audit[0].details == {
        "patchId": "p1",
        "patchHash": patch["patchHash"],
        "applyRunId": None,
    }

    harness.clock.advance(60)
    again = await _decide(migration, run_id, "approve", reason="다른 사유")

    assert again.status_code == 200, again.text
    assert again.json()["data"] == data
    assert len(await _approvals(migration, run_id)) == 1
    assert len(await _decision_audit_rows(migration, run_id)) == 1


async def test_patch_approve_with_apply_enabled(
    migration: Migration, runner: RunnerFixture
) -> None:
    patch = run_patch("p1", origin="RULE")
    run_id = await _finish(migration, runner, patch)
    harness = migration.harness
    harness.use_settings(patch_apply_enabled=True)
    now = harness.clock.now()

    response = await _decide(migration, run_id, "approve")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "APPROVED"
    apply_run_id = data["apply"]["runId"]
    assert data["apply"] == {
        "runId": apply_run_id,
        "status": "QUEUED",
        "failureCode": None,
        "finishedAt": None,
        "appliedCommitSha": None,
        "branch": None,
        "simulated": False,
    }
    assert data == await _patch_in_detail(migration, run_id)
    apply_runs = await _apply_runs(migration)
    assert [str(item.id) for item in apply_runs] == [apply_run_id]
    apply_run = apply_runs[0]
    parent = await run_row(migration, run_id)
    assert apply_run.status == AgentRunStatus.QUEUED
    assert str(apply_run.parent_run_id) == run_id
    assert apply_run.organization_id == parent.organization_id
    assert apply_run.service_id == parent.service_id
    assert apply_run.source_environment_id == parent.source_environment_id
    assert apply_run.target_environment_id == parent.target_environment_id
    assert apply_run.requested_by_user_id == migration.approver.user.id
    assert apply_run.deadline_at == now + timedelta(minutes=15)
    assert apply_run.input_snapshot == {
        "schemaVersion": 1,
        "parentRunId": run_id,
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
    assert apply_run.input_hash == compute_input_hash(apply_run.input_snapshot)
    rows = await _approvals(migration, run_id)
    assert [str(row.apply_run_id) for row in rows] == [apply_run_id]
    audit = await _decision_audit_rows(migration, run_id)
    assert len(audit) == 1
    assert audit[0].details["applyRunId"] == apply_run_id

    # 사전 점검만 하는 러너는 반영 실행을 받지 않는다(가져가기·결과는 test_apply_patch_runs.py).
    claimed = await claim(runner, capabilities=["PRECHECK"])
    assert claimed.json()["data"]["run"] is None

    # 반영 실행은 사람 쪽 상세로 읽히지만(kind 로 구분, SPEC api-10-3) 패치 결정은 받지 않는다.
    shown = await migration.author_client.get(prechecks_path(migration, f"/{apply_run_id}"))
    assert shown.status_code == 200, shown.text
    assert shown.json()["data"]["kind"] == "APPLY_PATCH"
    decide_hidden = await _decide(migration, apply_run_id, "approve")
    assert _error(decide_hidden) == (404, "PRECHECK_NOT_FOUND")

    # 열려 있는 동안 그 서비스의 새 사전 점검은 409, 같은 승인은 그대로 200 이다.
    busy = await request_precheck(migration)
    assert _error(busy) == (409, "PRECHECK_IN_PROGRESS")
    assert _details(busy)[0]["value"] == apply_run_id
    again = await _decide(migration, run_id, "approve")
    assert again.status_code == 200, again.text
    assert again.json()["data"] == data
    assert len(await _apply_runs(migration)) == 1
    assert len(await _decision_audit_rows(migration, run_id)) == 1


async def test_patch_approve_with_apply_disabled_by_default_creates_no_run(
    migration: Migration, runner: RunnerFixture
) -> None:
    run_id = await _finish(migration, runner, run_patch("p1"))
    assert migration.harness.settings.patch_apply_enabled is False

    response = await _decide(migration, run_id, "approve")

    assert response.status_code == 200, response.text
    assert await _apply_runs(migration) == []
    created = await request_precheck(migration)
    assert created.status_code == 201, created.text


async def test_patch_status_follows_apply_run_status(
    migration: Migration, runner: RunnerFixture
) -> None:
    run_id = await _finish(migration, runner, run_patch("p1"), run_patch("p2"))
    harness = migration.harness
    harness.use_settings(patch_apply_enabled=True)
    first = (await _decide(migration, run_id, "approve", "p1")).json()["data"]

    # 다른 패치 승인은 열린 반영 실행이 있으면 409 다.
    busy = await _decide(migration, run_id, "approve", "p2")
    assert _error(busy) == (409, "PRECHECK_IN_PROGRESS")
    assert _details(busy)[0]["value"] == first["apply"]["runId"]
    assert len(await _approvals(migration, run_id)) == 1

    # 마감이 지나면 다음 승인이 먼저 끝내고(TIMED_OUT) 앞 패치는 반영 실패로 보인다.
    harness.clock.advance(15 * 60 + 1)
    second = await _decide(migration, run_id, "approve", "p2")

    assert second.status_code == 200, second.text
    assert second.json()["data"]["apply"]["status"] == "QUEUED"
    failed = await _patch_in_detail(migration, run_id, "p1")
    assert failed["status"] == "APPLY_FAILED"
    assert failed["apply"]["status"] == "TIMED_OUT"
    assert failed["apply"]["failureCode"] == "DEADLINE_EXCEEDED"
    assert failed["apply"]["finishedAt"] is not None

    # 반영 결과를 받는 길이 생기면 SUCCEEDED 가 APPLIED 로 보인다.
    now = harness.clock.now()
    async with harness.session_factory() as session, session.begin():
        await session.execute(
            update(AgentRun)
            .where(AgentRun.id == uuid.UUID(second.json()["data"]["apply"]["runId"]))
            .values(status=AgentRunStatus.SUCCEEDED, finished_at=now, updated_at=now)
        )
    applied = await _patch_in_detail(migration, run_id, "p2")
    assert applied["status"] == "APPLIED"
    assert applied["apply"]["status"] == "SUCCEEDED"


async def test_patch_approve_with_apply_enabled_expires_overdue_precheck_first(
    migration: Migration, runner: RunnerFixture
) -> None:
    run_id = await _finish(migration, runner, run_patch("p1"))
    open_run = await create_precheck(migration)
    harness = migration.harness
    harness.use_settings(patch_apply_enabled=True)

    busy = await _decide(migration, run_id, "approve")
    assert _error(busy) == (409, "PRECHECK_IN_PROGRESS")
    assert _details(busy) == [
        {"field": "runId", "reason": "PRECHECK_IN_PROGRESS", "value": open_run["id"]}
    ]
    assert await _approvals(migration, run_id) == []

    harness.clock.advance(15 * 60)
    response = await _decide(migration, run_id, "approve")

    assert response.status_code == 200, response.text
    assert response.json()["data"]["apply"]["status"] == "QUEUED"
    expired = await run_row(migration, open_run["id"])
    assert expired.status == AgentRunStatus.TIMED_OUT
    timed_out = [
        row
        for row in await run_audit_rows(migration, open_run["id"])
        if row.action == AuditAction.AGENT_RUN_TIMED_OUT
    ]
    assert len(timed_out) == 1


# --- AC-2: 거절 ---


async def test_patch_reject_round_trip(migration: Migration, runner: RunnerFixture) -> None:
    patch = run_patch("p1")
    run_id = await _finish(migration, runner, patch)
    migration.harness.use_settings(patch_apply_enabled=True)

    response = await _decide(migration, run_id, "reject", reason="수동으로 고친다")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "REJECTED"
    assert data["apply"] is None
    assert data["approval"]["decision"] == "REJECTED"
    assert data["approval"]["reason"] == "수동으로 고친다"
    assert data == await _patch_in_detail(migration, run_id)
    assert await _apply_runs(migration) == []
    rows = await _approvals(migration, run_id)
    assert [(row.decision, row.apply_run_id) for row in rows] == [("REJECTED", None)]
    audit = await _decision_audit_rows(migration, run_id)
    assert [item.action for item in audit] == [AuditAction.AGENT_PATCH_REJECTED]
    assert audit[0].details == {"patchId": "p1", "patchHash": patch["patchHash"]}

    again = await _decide(migration, run_id, "reject")
    assert again.status_code == 200, again.text
    assert again.json()["data"] == data
    assert len(await _decision_audit_rows(migration, run_id)) == 1

    conflict = await _decide(migration, run_id, "approve")
    assert _error(conflict) == (409, "PATCH_ALREADY_DECIDED")
    assert _details(conflict) == [
        {"field": "decision", "reason": "PATCH_ALREADY_DECIDED", "value": "REJECTED"}
    ]
    assert len(await _approvals(migration, run_id)) == 1


async def test_patch_reject_after_approve_is_conflict(
    migration: Migration, runner: RunnerFixture
) -> None:
    run_id = await _finish(migration, runner, run_patch("p1"))
    assert (await _decide(migration, run_id, "approve")).status_code == 200

    conflict = await _decide(migration, run_id, "reject")

    assert _error(conflict) == (409, "PATCH_ALREADY_DECIDED")
    assert _details(conflict)[0]["value"] == "APPROVED"
    assert (await _patch_in_detail(migration, run_id))["status"] == "APPROVED"


async def test_patch_reject_blocked_patch_is_allowed(
    migration: Migration, runner: RunnerFixture
) -> None:
    run_id = await _finish(migration, runner, run_patch("p1", touchesProtectedFiles=True))

    response = await _decide(migration, run_id, "reject")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "REJECTED"
    assert data["blockReason"] == "PROTECTED_FILES_TOUCHED"


# --- AC-3: 거부 순서 ---


async def test_patch_decision_rejections_not_found(
    migration: Migration, runner: RunnerFixture
) -> None:
    harness = migration.harness
    run_id = await _finish(migration, runner, run_patch("p1"))
    empty_id = (await finish_precheck(migration, runner))["id"]
    other = await create_organization(harness.session_factory, "other-org")
    outsider = await create_member(
        harness.session_factory, other, OrganizationRole.DEVELOPER, harness.clock.now()
    )

    assert _error(await _decide(migration, UNKNOWN_ID, "approve")) == (404, "PRECHECK_NOT_FOUND")
    assert _error(await _decide(migration, run_id, "approve", "nope")) == (404, "PATCH_NOT_FOUND")
    assert _error(await _decide(migration, run_id, "reject", "nope")) == (404, "PATCH_NOT_FOUND")
    assert _error(await _decide(migration, empty_id, "approve")) == (404, "PATCH_NOT_FOUND")
    # 다른 조직 경로로 부르면 그 조직에 없는 실행이다.
    async with make_client(session_token=outsider.session_token) as client:
        foreign = await client.post(
            org_path(other.id, f"/prechecks/{run_id}/patches/p1/approve"),
            json={"patchHash": run_patch("p1")["patchHash"]},
        )
    assert _error(foreign) == (404, "PRECHECK_NOT_FOUND")
    assert await _approvals(migration, run_id) == []


async def test_patch_decision_rejections_parent_not_succeeded(
    migration: Migration, runner: RunnerFixture
) -> None:
    # 끝나지 않은 실행은 결과가 없어(결과는 SUCCEEDED·FAILED 만) 패치 없음이 먼저다.
    created = await create_precheck(migration)
    queued = await _decide(migration, created["id"], "approve")
    assert _error(queued) == (404, "PATCH_NOT_FOUND")
    claimed = await claim_run(runner)
    running = await _decide(migration, created["id"], "reject")
    assert _error(running) == (404, "PATCH_NOT_FOUND")

    # 패치를 낸 채 실패한 실행은 결정할 수 없다.
    result = run_result(
        migration, status="FAILED", failureCode="RUNNER_ERROR", patches=[run_patch("p1")]
    )
    assert (await claimed.post("result", result)).status_code == 200

    assert _error(await _decide(migration, created["id"], "approve")) == (409, "RUN_NOT_SUCCEEDED")
    assert _error(await _decide(migration, created["id"], "reject")) == (409, "RUN_NOT_SUCCEEDED")
    assert await _approvals(migration, created["id"]) == []


async def test_patch_decision_rejections_blocked_patch_cannot_be_approved(
    migration: Migration, runner: RunnerFixture
) -> None:
    run_id = await _finish(
        migration,
        runner,
        run_patch("p1", touchesProtectedFiles=True),
        run_patch("p2", checks=[]),
        run_patch("p3", checks=[PASSED_PATCH_CHECK | {"result": "FAIL"}]),
    )

    for patch_id, reason in (
        ("p1", "PROTECTED_FILES_TOUCHED"),
        ("p2", "CHECKS_MISSING"),
        ("p3", "CHECK_NOT_PASSED"),
    ):
        response = await _decide(migration, run_id, "approve", patch_id)
        assert _error(response) == (409, "PATCH_BLOCKED")
        assert _details(response) == [
            {"field": "blockReason", "reason": "PATCH_BLOCKED", "value": reason}
        ]
    assert await _approvals(migration, run_id) == []
    assert await _decision_audit_rows(migration, run_id) == []


async def test_patch_decision_rejections_hash_mismatch_is_stale(
    migration: Migration, runner: RunnerFixture
) -> None:
    run_id = await _finish(migration, runner, run_patch("p1"))
    wrong = "sha256:" + "0" * 64

    for action in ("approve", "reject"):
        response = await _decide(migration, run_id, action, patchHash=wrong)
        assert _error(response) == (409, "PATCH_STALE")
        assert _details(response) == [{"field": "patchHash", "reason": "PATCH_HASH_MISMATCH"}]
    assert await _approvals(migration, run_id) == []


async def test_patch_decision_rejections_expires_after_24_hours(
    migration: Migration, runner: RunnerFixture
) -> None:
    run_id = await _finish(migration, runner, run_patch("p1"), run_patch("p2"), run_patch("p3"))
    harness = migration.harness
    harness.clock.advance(DAY)

    on_boundary = await _decide(migration, run_id, "approve", "p1")
    assert on_boundary.status_code == 200, on_boundary.text

    harness.clock.advance(1)
    for action, patch_id in (("approve", "p2"), ("reject", "p3")):
        response = await _decide(migration, run_id, action, patch_id)
        assert _error(response) == (409, "PATCH_STALE")
        assert _details(response) == [{"field": "finishedAt", "reason": "PRECHECK_EXPIRED"}]

    # 이미 난 결정은 오래됐어도 먼저 본다(같은 결정 200, 다른 결정 409).
    assert (await _decide(migration, run_id, "approve", "p1")).status_code == 200
    assert _error(await _decide(migration, run_id, "reject", "p1")) == (
        409,
        "PATCH_ALREADY_DECIDED",
    )


async def test_patch_decision_rejection_order(migration: Migration, runner: RunnerFixture) -> None:
    wrong = "sha256:" + "0" * 64
    run_id = await _finish(
        migration,
        runner,
        run_patch("p1"),
        run_patch("blocked", touchesProtectedFiles=True),
        run_patch("decided"),
    )
    harness = migration.harness
    assert (await _decide(migration, run_id, "approve", "decided")).status_code == 200

    # 4 BLOCKED > 6 낡음
    blocked = await _decide(migration, run_id, "approve", "blocked", patchHash=wrong)
    assert _error(blocked) == (409, "PATCH_BLOCKED")
    # 5 이미 결정 > 6 낡음: 같은 결정이면 해시가 달라도 그대로 200 이다.
    same = await _decide(migration, run_id, "approve", "decided", patchHash=wrong)
    assert same.status_code == 200, same.text
    assert _error(await _decide(migration, run_id, "reject", "decided", patchHash=wrong)) == (
        409,
        "PATCH_ALREADY_DECIDED",
    )
    # 6 낡음 > 7 자기 승인: 만든 사람의 해시가 다르면 거부 줄 없이 409 다.
    stale = await _decide(
        migration, run_id, "approve", patchHash=wrong, client=migration.author_client
    )
    assert _error(stale) == (409, "PATCH_STALE")
    # 7 자기 승인 > 8 열린 실행: 스위치가 켜져 있고 실행이 열려 있어도 403 이 먼저다.
    harness.use_settings(patch_apply_enabled=True)
    await create_precheck(migration)
    forbidden = await _decide(migration, run_id, "approve", client=migration.author_client)
    assert _error(forbidden) == (403, "SELF_APPROVAL_FORBIDDEN")
    # 거절은 8 을 보지 않는다.
    rejected = await _decide(migration, run_id, "reject", client=migration.author_client)
    assert rejected.status_code == 200, rejected.text


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"patchHash": "sha256:abc"},
        {"patchHash": "a" * 64},
        {"patchHash": "sha256:" + "a" * 64, "reason": ""},
        {"patchHash": "sha256:" + "a" * 64, "reason": "x" * 2001},
        {"patchHash": "sha256:" + "a" * 64, "reason": "bad\x00reason"},
        {"patchHash": "sha256:" + "a" * 64, "note": "모르는 칸"},
        {"patch_hash": "sha256:" + "a" * 64},
    ],
    ids=[
        "missing-hash",
        "short-hash",
        "no-prefix",
        "empty-reason",
        "long-reason",
        "nul-reason",
        "unknown-field",
        "snake-case",
    ],
)
async def test_patch_decision_invalid_body_is_validation_error(
    migration: Migration, runner: RunnerFixture, body: dict[str, Any]
) -> None:
    run_id = await _finish(migration, runner, run_patch("p1"))

    for action in ("approve", "reject"):
        response = await migration.approver_client.post(
            prechecks_path(migration, f"/{run_id}/patches/p1/{action}"), json=body
        )
        assert _error(response) == (422, "VALIDATION_ERROR")
    assert await _approvals(migration, run_id) == []


async def test_patch_decision_accepts_reason_of_2000_characters(
    migration: Migration, runner: RunnerFixture
) -> None:
    run_id = await _finish(migration, runner, run_patch("p1"))

    response = await _decide(migration, run_id, "reject", reason="x" * 2000)

    assert response.status_code == 200, response.text


async def test_patch_decision_concurrent_approve_and_reject_only_one_wins(
    migration: Migration, runner: RunnerFixture
) -> None:
    run_id = await _finish(migration, runner, run_patch("p1"))

    responses = await asyncio.gather(
        _decide(migration, run_id, "approve"),
        _decide(migration, run_id, "reject", client=migration.harness.client),
    )

    assert sorted(item.status_code for item in responses) == [200, 409]
    loser = next(item for item in responses if item.status_code == 409)
    assert loser.json()["code"] == "PATCH_ALREADY_DECIDED"
    assert len(await _approvals(migration, run_id)) == 1
    assert len(await _decision_audit_rows(migration, run_id)) == 1


async def test_patch_decision_concurrent_same_decision_both_succeed_with_one_row(
    migration: Migration, runner: RunnerFixture
) -> None:
    run_id = await _finish(migration, runner, run_patch("p1"))
    migration.harness.use_settings(patch_apply_enabled=True)

    responses = await asyncio.gather(
        _decide(migration, run_id, "approve"),
        _decide(migration, run_id, "approve", client=migration.harness.client),
    )

    assert [item.status_code for item in responses] == [200, 200]
    assert responses[0].json()["data"] == responses[1].json()["data"]
    assert len(await _approvals(migration, run_id)) == 1
    assert len(await _apply_runs(migration)) == 1
    assert len(await _decision_audit_rows(migration, run_id)) == 1


def _miss_first_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """앞선 읽기가 행을 못 본 경쟁을 만든다. 부모 잠금 때문에 보통은 이 분기에 닿지 않는다."""
    original = AgentPatchApprovalRepository.find_by_run_and_patch
    calls = {"count": 0}

    async def lookup(
        self: AgentPatchApprovalRepository, run_id: uuid.UUID, patch_id: str
    ) -> AgentPatchApproval | None:
        calls["count"] += 1
        return None if calls["count"] == 1 else await original(self, run_id, patch_id)

    monkeypatch.setattr(AgentPatchApprovalRepository, "find_by_run_and_patch", lookup)


async def test_patch_decision_unique_violation_same_decision_is_idempotent(
    migration: Migration, runner: RunnerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await _finish(migration, runner, run_patch("p1"))
    first = await _decide(migration, run_id, "approve", reason="먼저")
    assert first.status_code == 200, first.text
    _miss_first_lookup(monkeypatch)

    again = await _decide(migration, run_id, "approve", reason="나중")

    assert again.status_code == 200, again.text
    assert again.json()["data"] == first.json()["data"]
    assert len(await _approvals(migration, run_id)) == 1
    assert len(await _decision_audit_rows(migration, run_id)) == 1


async def test_patch_decision_unique_violation_other_decision_conflicts_without_orphan_run(
    migration: Migration, runner: RunnerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await _finish(migration, runner, run_patch("p1"))
    assert (await _decide(migration, run_id, "reject")).status_code == 200
    migration.harness.use_settings(patch_apply_enabled=True)
    _miss_first_lookup(monkeypatch)

    conflict = await _decide(migration, run_id, "approve")

    assert _error(conflict) == (409, "PATCH_ALREADY_DECIDED")
    assert await _apply_runs(migration) == []
    assert len(await _approvals(migration, run_id)) == 1
    assert len(await _decision_audit_rows(migration, run_id)) == 1


async def test_patch_approve_open_run_unique_violation_is_in_progress_without_decision(
    migration: Migration, runner: RunnerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await _finish(migration, runner, run_patch("p1"))
    open_run = await create_precheck(migration)
    migration.harness.use_settings(patch_apply_enabled=True)

    async def no_check(*_: Any) -> None:
        return None

    # 열린 실행 확인과 INSERT 사이에 다른 요청이 실행을 넣은 경쟁을 만든다.
    monkeypatch.setattr(PrecheckPatchService, "_check_no_open_run", no_check)

    response = await _decide(migration, run_id, "approve")

    assert _error(response) == (409, "PRECHECK_IN_PROGRESS")
    assert _details(response)[0]["value"] == open_run["id"]
    assert await _approvals(migration, run_id) == []
    assert await _apply_runs(migration) == []


# --- AC-4: 권한 ---


async def test_patch_self_approval_disabled_is_forbidden_with_denial_line(
    migration: Migration, runner: RunnerFixture
) -> None:
    run_id = await _finish(migration, runner, run_patch("p1"))
    harness = migration.harness

    response = await _decide(migration, run_id, "approve", client=migration.author_client)

    assert _error(response) == (403, "SELF_APPROVAL_FORBIDDEN")
    assert await _approvals(migration, run_id) == []
    async with harness.session_factory() as session:
        denied = list(
            (await session.scalars(select(AuditLog).where(AuditLog.result == AuditResult.DENIED)))
            .unique()
            .all()
        )
    assert [(row.reason, row.actor_user_id) for row in denied] == [
        ("SELF_APPROVAL_FORBIDDEN", migration.author.user.id)
    ]
    assert await _decision_audit_rows(migration, run_id) == []
    assert (await _patch_in_detail(migration, run_id))["status"] == "PROPOSED"


async def test_patch_self_approval_enabled_is_allowed(
    migration: Migration, runner: RunnerFixture
) -> None:
    run_id = await _finish(migration, runner, run_patch("p1"))
    await allow_self_approval(migration.harness)

    response = await _decide(migration, run_id, "approve", client=migration.author_client)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["approval"]["decidedByUserId"] == str(migration.author.user.id)


async def test_patch_self_reject_is_allowed_even_when_self_approval_disabled(
    migration: Migration, runner: RunnerFixture
) -> None:
    run_id = await _finish(migration, runner, run_patch("p1"))

    response = await _decide(migration, run_id, "reject", client=migration.author_client)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "REJECTED"


@pytest.mark.parametrize("allow_self", [False, True], ids=["switch-off", "switch-on"])
async def test_patch_other_developer_can_approve_regardless_of_switch(
    migration: Migration, runner: RunnerFixture, allow_self: bool
) -> None:
    run_id = await _finish(migration, runner, run_patch("p1"))
    if allow_self:
        await allow_self_approval(migration.harness)

    response = await _decide(migration, run_id, "approve")

    assert response.status_code == 200, response.text


async def test_patch_decision_viewer_and_agent_are_forbidden(
    migration: Migration, runner: RunnerFixture
) -> None:
    run_id = await _finish(migration, runner, run_patch("p1"))
    harness = migration.harness
    viewer = await create_member(
        harness.session_factory, harness.organization, OrganizationRole.VIEWER, harness.clock.now()
    )
    _, agent_token = await create_agent_token(
        harness.session_factory, harness.organization, harness.admin.user, harness.clock.now()
    )

    async with make_client(session_token=viewer.session_token) as viewer_client:
        for action in ("approve", "reject"):
            response = await _decide(migration, run_id, action, client=viewer_client)
            assert _error(response) == (403, "ROLE_FORBIDDEN")
    async with make_client(agent_token=agent_token) as agent_client:
        for action in ("approve", "reject"):
            response = await _decide(migration, run_id, action, client=agent_client)
            assert _error(response) == (403, "AGENT_READ_ONLY")
    assert await _approvals(migration, run_id) == []


# --- AC-5: 러너 계약 origin ---


async def test_runner_patch_origin_missing_is_stored_and_shown_as_ai(
    migration: Migration, runner: RunnerFixture
) -> None:
    run_id = await _finish(migration, runner, run_patch("p1"))

    row = await run_row(migration, run_id)

    assert row.result is not None
    assert row.result["patches"][0]["origin"] == "AI"
    assert (await _patch_in_detail(migration, run_id))["origin"] == "AI"


async def test_runner_patch_origin_rule_is_kept(
    migration: Migration, runner: RunnerFixture
) -> None:
    run_id = await _finish(migration, runner, run_patch("p1", origin="RULE"))

    row = await run_row(migration, run_id)

    assert row.result is not None
    assert row.result["patches"][0]["origin"] == "RULE"
    assert (await _patch_in_detail(migration, run_id))["origin"] == "RULE"


@pytest.mark.parametrize(
    "changes",
    [{"origin": "HUMAN"}, {"origin": None}, {"status": "PROPOSED"}, {"note": "모르는 칸"}],
    ids=["bad-origin", "null-origin", "status-field", "unknown-field"],
)
async def test_runner_patch_origin_invalid_is_rejected(
    migration: Migration, runner: RunnerFixture, changes: dict[str, Any]
) -> None:
    await create_precheck(migration)
    claimed = await claim_run(runner)

    response = await claimed.post(
        "result", run_result(migration, patches=[run_patch("p1", **changes)])
    )

    assert _error(response) == (422, "RESULT_INVALID")


async def test_runner_patch_origin_absent_in_old_result_is_shown_as_ai(
    migration: Migration, runner: RunnerFixture
) -> None:
    run_id = await _finish(migration, runner, run_patch("p1", origin="RULE"))
    row = await run_row(migration, run_id)
    assert row.result is not None
    stored = {key: value for key, value in row.result["patches"][0].items() if key != "origin"}
    async with migration.harness.session_factory() as session, session.begin():
        await session.execute(
            update(AgentRun)
            .where(AgentRun.id == row.id)
            .values(result={**row.result, "patches": [stored]})
        )

    assert (await _patch_in_detail(migration, run_id))["origin"] == "AI"
    harness = migration.harness
    harness.use_settings(patch_apply_enabled=True)
    approved = await _decide(migration, run_id, "approve")

    assert approved.status_code == 200, approved.text
    assert approved.json()["data"]["origin"] == "AI"
    apply_runs = await _apply_runs(migration)
    assert apply_runs[0].input_snapshot["patch"]["origin"] == "AI"
