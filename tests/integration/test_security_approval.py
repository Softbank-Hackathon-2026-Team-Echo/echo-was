"""승인 거절 요청의 게이트 부작용과 판정 중 변경의 잠금 안 재검사."""

import asyncio
import uuid

import pytest
from sqlalchemy import func, select

from app.enums import AuditResult, JobKind, MigrationPlanStatus
from app.models import AuditLog, Finding, Job
from app.services.security_gate import SecurityGate
from app.services.security_rules import SecurityDecision
from tests.integration.auth_support import create_member, create_organization, make_client, org_path
from tests.integration.migration_support import Migration
from tests.integration.security_gate_support import enable_gate, prepare_passing_gate


async def _finding_count(migration: Migration) -> int:
    async with migration.harness.session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(Finding)) or 0)


@pytest.mark.parametrize("state", ["self", "cancelled", "rejected", "approved"])
async def test_rejected_approval_does_not_probe_or_record_findings(
    migration: Migration, state: str
) -> None:
    fakes = await prepare_passing_gate(migration)
    plan = await migration.create_plan()
    if state == "cancelled":
        await migration.act(plan["id"], "cancel", client=migration.author_client)
    elif state == "rejected":
        await migration.act(plan["id"], "reject")
    elif state == "approved":
        await migration.approve(plan["id"])
    fakes.probe.calls.clear()
    before = await _finding_count(migration)
    codes = await migration.log_codes(plan["id"])

    # 끝난 계획의 자기 승인도 기존 순서대로 403보다 PLAN_NOT_PENDING 409가 먼저다.
    for _ in range(2):
        response = await migration.act(plan["id"], "approve", client=migration.author_client)
        expected = (
            (403, "SELF_APPROVAL_FORBIDDEN") if state == "self" else (409, "PLAN_NOT_PENDING")
        )
        assert (response.status_code, response.json()["code"]) == expected

    assert fakes.probe.calls == []
    assert await _finding_count(migration) == before
    assert await migration.log_codes(plan["id"]) == codes
    if state == "self":
        async with migration.harness.session_factory() as session:
            denied = await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.result == AuditResult.DENIED,
                    AuditLog.reason == "SELF_APPROVAL_FORBIDDEN",
                )
            )
        assert denied == 2
        assert await migration.plan_job(plan["id"]) is None


async def test_allowed_self_approval_still_evaluates_gate(migration: Migration) -> None:
    fakes = enable_gate(migration)
    settings = org_path(migration.harness.organization.id, "/settings")
    changed = await migration.harness.client.patch(settings, json={"allowSelfApproval": True})
    assert changed.status_code == 200
    plan = await migration.create_plan()

    response = await migration.act(plan["id"], "approve", client=migration.author_client)

    assert (response.status_code, response.json()["code"]) == (409, "SECURITY_GATE_BLOCKED")
    assert len(fakes.probe.calls) == 2
    assert await _finding_count(migration) > 0
    assert (await migration.plan_row(plan["id"])).status == MigrationPlanStatus.PENDING_APPROVAL
    assert await migration.plan_job(plan["id"]) is None


@pytest.mark.parametrize("change", ["cancel", "revoke_self_approval"])
async def test_approval_rechecks_state_and_permission_after_gate(
    migration: Migration, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    await prepare_passing_gate(migration)
    settings = org_path(migration.harness.organization.id, "/settings")
    if change == "revoke_self_approval":
        enabled = await migration.harness.client.patch(settings, json={"allowSelfApproval": True})
        assert enabled.status_code == 200
    plan = await migration.create_plan()
    evaluate = SecurityGate.evaluate_plan

    async def evaluate_then_change(gate: SecurityGate, plan_id: uuid.UUID) -> SecurityDecision:
        decision = await evaluate(gate, plan_id)
        if change == "cancel":
            changed = await migration.act(str(plan_id), "cancel", client=migration.author_client)
        else:
            changed = await migration.harness.client.patch(
                settings, json={"allowSelfApproval": False}
            )
        assert changed.status_code == 200
        return decision

    monkeypatch.setattr(SecurityGate, "evaluate_plan", evaluate_then_change)
    client = (
        migration.author_client if change == "revoke_self_approval" else migration.approver_client
    )

    response = await migration.act(plan["id"], "approve", client=client)

    expected = (
        (403, "SELF_APPROVAL_FORBIDDEN")
        if change == "revoke_self_approval"
        else (409, "PLAN_NOT_PENDING")
    )
    assert (response.status_code, response.json()["code"]) == expected
    assert await migration.plan_job(plan["id"]) is None
    codes = await migration.log_codes(plan["id"])
    assert "SECURITY_GATE_PASSED" not in codes
    assert "APPROVED" not in codes
    assert "SELF_APPROVED" not in codes


async def test_cross_organization_approval_does_not_evaluate_gate(migration: Migration) -> None:
    fakes = enable_gate(migration)
    plan = await migration.create_plan()
    harness = migration.harness
    other = await create_organization(harness.session_factory, "gate-other")
    outsider = await create_member(
        harness.session_factory,
        other,
        migration.approver.role,
        harness.clock.now(),
        login="gate-outsider",
    )
    async with make_client(session_token=outsider.session_token) as client:
        response = await client.post(org_path(other.id, f"/migration-plans/{plan['id']}/approve"))

    assert (response.status_code, response.json()["code"]) == (404, "MIGRATION_PLAN_NOT_FOUND")
    assert fakes.probe.calls == []
    assert await _finding_count(migration) == 0
    assert await migration.plan_job(plan["id"]) is None


async def test_concurrent_approvals_enqueue_one_job(migration: Migration) -> None:
    await prepare_passing_gate(migration)
    plan = await migration.create_plan()

    responses = await asyncio.gather(
        migration.act(plan["id"], "approve"),
        migration.act(plan["id"], "approve"),
    )

    assert sorted(response.status_code for response in responses) == [202, 409]
    rejected = next(response for response in responses if response.status_code == 409)
    assert rejected.json()["code"] == "PLAN_NOT_PENDING"
    async with migration.harness.session_factory() as session:
        jobs = await session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.kind == JobKind.MIGRATION_PLAN, Job.subject_id == uuid.UUID(plan["id"]))
        )
    assert jobs == 1
    assert (await migration.log_codes(plan["id"])).count("APPROVED") == 1
