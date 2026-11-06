"""플랫폼 자기 시험(보안 5-3, C-IAM-2·C-AUD-1 준비 증거) 통합 시험.

일꾼이 일회용 에이전트 토큰으로 앱의 쓰기 경로를 모두 불러 보고(C-IAM-2), 최근 24시간 감사 기록을
대조해(C-AUD-1) 조직 + WAS 버전 범위의 증거를 남기는지, 그 증거가 매트릭스의 모든 환경 행에서
기한부 예외보다 먼저 쓰이는지 본다.
"""

import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import ASGIApp, Receive, Scope, Send

from app.audit.actions import AuditAction, AuditComponent
from app.audit.route_coverage import agent_probe_routes
from app.enums import (
    ActorType,
    AgentRunKind,
    AgentRunStatus,
    AuditResult,
    SecurityControlId,
    SecurityFindingMode,
    SecurityFindingReason,
    SecurityFindingSource,
    SecurityGateStatus,
    SecurityReasonCode,
    SecurityResult,
)
from app.main import app
from app.models import AgentRun, AgentToken, AuditLog, Finding, SecurityException
from app.services.platform_self_test import (
    EVIDENCE_TTL,
    FAIL_RENEW_MARGIN,
    IN_FLIGHT_GRACE,
    SELF_TEST_SOURCE_REF,
    SELF_TEST_TOKEN_NAME,
    PlatformSelfTest,
)
from app.services.security_rules import POLICY_VERSION
from tests.integration.auth_support import create_user, org_path
from tests.integration.deploy_harness import DeployHarness
from tests.integration.migration_support import Migration
from tests.integration.rollback_support import deploy_and_finish
from tests.integration.runner_support import add_runner, finish_precheck

WAS_VERSION = "2026.11.07"
APPROVE_ROUTES = {
    "POST /api/v1/orgs/{org_id}/migration-plans/{plan_id}/approve",
    "POST /api/v1/orgs/{org_id}/security/exceptions/{exception_id}/approve",
    "POST /api/v1/orgs/{org_id}/prechecks/{precheck_id}/patches/{patch_id}/approve",
}
LEAKY_ROUTE = "POST /api/v1/orgs/{org_id}/migration-plans/{plan_id}/approve"
# 에이전트 토큰으로 부르는 쓰기 경로 수(라우터에서 계산 — 경로가 늘어도 숫자를 고치지 않는다).
PROBE_COUNT = len(agent_probe_routes(app.routes))
# 시험 시작 직전 유예를 지나 도메인 줄이 대조 범위에 들어오게 하는 시간(초).
PAST_GRACE = IN_FLIGHT_GRACE.total_seconds() + 1


def _self_test(
    harness: DeployHarness,
    *,
    target: ASGIApp = app,
    routes: list[Any] | None = None,
    gate_enabled: bool = True,
) -> PlatformSelfTest:
    harness.use_settings(was_version=WAS_VERSION)
    settings = harness.settings.model_copy(update={"security_gate_enabled": gate_enabled})
    return PlatformSelfTest(
        harness.session_factory,
        harness.clock,
        settings,
        target,
        routes if routes is not None else app.routes,
    )


async def _findings(harness: DeployHarness) -> dict[SecurityControlId, Finding]:
    async with harness.session_factory() as session:
        rows = (
            await session.scalars(select(Finding).where(Finding.source_ref == SELF_TEST_SOURCE_REF))
        ).all()
    assert len(rows) == 2
    return {SecurityControlId(row.control_id): row for row in rows}


async def _matrix(harness: DeployHarness) -> dict[str, Any]:
    response = await harness.client.get(org_path(harness.organization.id, "/security/matrix"))
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


def _control(cell: dict[str, Any], control_id: SecurityControlId) -> dict[str, Any]:
    return next(c for c in cell["controls"] if c["controlId"] == control_id.value)


class _LeakyApp:
    """한 쓰기 경로(이전 계획 승인)만 에이전트 쓰기를 통과시키는 앱(검사기가 뚫린 상황)."""

    def __init__(self, inner: ASGIApp) -> None:
        self._inner = inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and "/migration-plans/" in path and path.endswith("/approve"):
            body = json.dumps({"success": True, "code": "OK"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 202,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self._inner(scope, receive, send)


# --- C-IAM-2 ---


async def test_self_test_records_passing_evidence_with_was_version_scope(
    harness: DeployHarness,
) -> None:
    await harness.add_environment("aws-seoul")

    recorded = await _self_test(harness).run_once()

    assert recorded == 2
    findings = await _findings(harness)
    iam = findings[SecurityControlId.IAM_2]
    audit = findings[SecurityControlId.AUD_1]
    for finding, source in (
        (iam, SecurityFindingSource.WAS_TEST),
        (audit, SecurityFindingSource.AUDIT_LOG),
    ):
        assert finding.result == SecurityResult.PASS
        assert finding.mode == SecurityFindingMode.REAL
        assert finding.source == source
        assert finding.organization_id == harness.organization.id
        assert finding.was_version == WAS_VERSION
        assert (finding.environment_id, finding.service_id) == (None, None)
        assert finding.policy_version == POLICY_VERSION == "sec-rules-2026.11.06"
        assert finding.expires_at - finding.collected_at == EVIDENCE_TTL
    assert iam.reason == SecurityFindingReason.AGENT_WRITES_DENIED
    labels = {item["item_id"]: item for item in iam.items}
    assert iam.total_items == len(labels) == PROBE_COUNT
    assert APPROVE_ROUTES <= set(labels)
    assert all(item["target"] == "403 AGENT_READ_ONLY" for item in labels.values())
    assert audit.reason == SecurityFindingReason.AUDIT_TRAIL_COMPLETE
    summary = {item["item_id"]: item["target"] for item in audit.items}
    assert summary["denial:AGENT_READ_ONLY"] == f"{PROBE_COUNT}/{PROBE_COUNT}"


async def test_self_test_token_is_revoked_and_every_step_is_audited(
    harness: DeployHarness,
) -> None:
    await harness.add_environment("aws-seoul")

    await _self_test(harness).run_once()

    async with harness.session_factory() as session:
        tokens = (
            await session.scalars(select(AgentToken).where(AgentToken.name == SELF_TEST_TOKEN_NAME))
        ).all()
        rows = (await session.scalars(select(AuditLog).order_by(AuditLog.seq))).all()
    assert len(tokens) == 1
    token = tokens[0]
    assert token.revoked_at is not None
    assert token.organization_id == harness.organization.id
    system_rows = [row for row in rows if row.actor_type == ActorType.SYSTEM]
    assert [(row.action, row.component, row.target_id) for row in system_rows] == [
        (AuditAction.AGENT_TOKEN_ISSUED, AuditComponent.SECURITY_SELF_TEST, token.id),
        (AuditAction.AGENT_TOKEN_REVOKED, AuditComponent.SECURITY_SELF_TEST, token.id),
    ]
    denials = [row for row in rows if row.actor_type == ActorType.AGENT]
    assert len(denials) == PROBE_COUNT
    assert {(row.result, row.reason, row.actor_agent_token_id) for row in denials} == {
        (AuditResult.DENIED, "AGENT_READ_ONLY", token.id)
    }
    assert {row.organization_id for row in denials} == {harness.organization.id}


async def test_agent_write_that_passes_fails_iam_evidence(harness: DeployHarness) -> None:
    await harness.add_environment("aws-seoul")

    await _self_test(harness, target=_LeakyApp(app)).run_once()

    iam = (await _findings(harness))[SecurityControlId.IAM_2]
    assert iam.result == SecurityResult.FAIL
    assert iam.reason == SecurityFindingReason.AGENT_WRITE_NOT_DENIED
    # 통과한 경로가 근거 첫 줄이다.
    assert iam.items[0] == {
        "item_id": LEAKY_ROUTE,
        "target": "202 OK",
        "reason": SecurityFindingReason.AGENT_WRITE_NOT_DENIED.value,
    }
    assert sum(item["reason"] == "AGENT_WRITES_DENIED" for item in iam.items) == PROBE_COUNT - 1


async def test_failed_evidence_blocks_matrix_even_after_older_pass(harness: DeployHarness) -> None:
    await harness.add_environment("aws-seoul")
    await _self_test(harness).run_once()
    harness.clock.advance(60)

    await _self_test(harness, target=_LeakyApp(app)).run_once()

    matrix = await _matrix(harness)
    control = _control(matrix["rows"][0]["cells"]["iam"], SecurityControlId.IAM_2)
    assert control["result"] == SecurityResult.FAIL.value
    assert control["gateStatus"] == SecurityGateStatus.BLOCKED.value


# --- C-AUD-1 ---


async def test_unnamed_write_route_fails_audit_evidence(harness: DeployHarness) -> None:
    await harness.add_environment("aws-seoul")

    async def endpoint() -> None:
        return None

    extra = APIRoute("/api/v1/orgs/{org_id}/unnamed-thing", endpoint, methods=["POST"])

    await _self_test(harness, routes=[*app.routes, extra]).run_once()

    audit = (await _findings(harness))[SecurityControlId.AUD_1]
    assert audit.result == SecurityResult.FAIL
    assert audit.reason == SecurityFindingReason.AUDIT_ROUTE_UNNAMED
    assert audit.items[0]["item_id"] == "POST /api/v1/orgs/{org_id}/unnamed-thing"


async def test_recent_activity_with_audit_rows_passes(migration: Migration) -> None:
    harness = migration.harness
    runner = await add_runner(harness.session_factory)
    precheck = await finish_precheck(migration, runner)
    exception = await _create_and_approve_exception(migration)
    plan = await migration.create_plan()
    await migration.approve(plan["id"])

    harness.clock.advance(PAST_GRACE)
    # 이 하네스는 보안 게이트가 꺼져 있어 승인 때 게이트 실행 로그가 없다. 게이트 대조는 끈다.
    await _self_test(harness, gate_enabled=False).run_once()

    audit = (await _findings(harness))[SecurityControlId.AUD_1]
    assert audit.result == SecurityResult.PASS, audit.items
    summary = {item["item_id"]: item["target"] for item in audit.items}
    assert summary[f"approval:{AuditAction.MIGRATION_PLAN_APPROVED}"] == "1/1"
    assert summary[f"approval:{AuditAction.SECURITY_EXCEPTION_APPROVED}"] == "1/1"
    assert summary[f"exception:{AuditAction.SECURITY_EXCEPTION_REQUESTED}"] == "1/1"
    assert summary[f"agent:{AuditAction.AGENT_RUN_CLAIMED}"] == "1/1"
    assert summary[f"agent:{AuditAction.AGENT_RUN_FINISHED}"] == "1/1"
    assert "gate:SECURITY_GATE_PASSED" not in summary
    assert precheck["id"] and exception["id"]


async def test_simulated_diagnosis_and_apply_runs_pass_audit_evidence(
    migration: Migration,
) -> None:
    """운영 C-AUD-1 FAIL 재현(가짜 진단 410a410b…): 가짜 진단·가짜 반영도 종료 줄이 있어야 PASS."""
    harness = migration.harness
    failed = await deploy_and_finish(
        harness, migration.service_id, migration.aws.id, simulate={"failAt": "DEPLOYING"}
    )
    path = org_path(harness.organization.id, f"/deploy-requests/{failed['id']}/diagnosis")
    created = await migration.author_client.post(path)
    assert created.status_code == 201, created.text
    run = created.json()["data"]
    assert (run["status"], run["simulated"]) == ("SUCCEEDED", True)
    patch = run["patches"][0]
    approved = await migration.approver_client.post(
        org_path(harness.organization.id, f"/prechecks/{run['id']}/patches/{patch['id']}/approve"),
        json={"patchHash": patch["patchHash"]},
    )
    assert approved.status_code == 200, approved.text
    apply = approved.json()["data"]["apply"]
    assert (apply["status"], apply["simulated"]) == ("SUCCEEDED", True)

    harness.clock.advance(PAST_GRACE)
    await _self_test(harness, gate_enabled=False).run_once()

    audit = (await _findings(harness))[SecurityControlId.AUD_1]
    assert audit.result == SecurityResult.PASS, audit.items
    summary = {item["item_id"]: item["target"] for item in audit.items}
    assert summary[f"agent:{AuditAction.AGENT_RUN_FINISHED}"] == "2/2"
    assert summary[f"approval:{AuditAction.AGENT_PATCH_APPROVED}"] == "1/1"


async def test_finished_run_without_audit_row_still_fails(migration: Migration) -> None:
    """대조 규칙은 그대로다: 종료 줄 없이 끝난 실행(옛 가짜 진단 모양)은 여전히 FAIL 이다."""
    harness = migration.harness
    failed = await deploy_and_finish(
        harness, migration.service_id, migration.aws.id, simulate={"failAt": "DEPLOYING"}
    )
    harness.clock.advance(PAST_GRACE)
    now = harness.clock.now()
    run = AgentRun(
        organization_id=harness.organization.id,
        service_id=uuid.UUID(migration.service_id),
        kind=AgentRunKind.CHANGE_PROPOSAL,
        target_environment_id=migration.aws.id,
        deploy_request_id=uuid.UUID(failed["id"]),
        status=AgentRunStatus.SUCCEEDED,
        simulated=True,
        input_snapshot={"service": {"name": "legacy-simulated"}},
        input_hash="sha256:" + "0" * 64,
        requested_by_user_id=harness.admin.user.id,
        deadline_at=now + timedelta(minutes=15),
        finished_at=now,
        created_at=now,
        updated_at=now,
    )
    async with harness.session_factory() as session, session.begin():
        session.add(run)
    harness.clock.advance(PAST_GRACE)

    await _self_test(harness).run_once()

    audit = (await _findings(harness))[SecurityControlId.AUD_1]
    assert audit.result == SecurityResult.FAIL
    assert audit.reason == SecurityFindingReason.AUDIT_RECORD_MISSING
    assert (audit.items[0]["item_id"], audit.items[0]["target"]) == (
        f"agent:{AuditAction.AGENT_RUN_FINISHED}",
        str(run.id),
    )


async def test_approval_without_gate_log_fails_when_gate_is_on(migration: Migration) -> None:
    harness = migration.harness
    plan = await migration.create_plan()
    await migration.approve(plan["id"])
    harness.clock.advance(PAST_GRACE)

    await _self_test(harness, gate_enabled=True).run_once()

    audit = (await _findings(harness))[SecurityControlId.AUD_1]
    assert audit.result == SecurityResult.FAIL
    assert audit.reason == SecurityFindingReason.AUDIT_RECORD_MISSING
    first = audit.items[0]
    assert (first["item_id"], first["target"]) == ("gate:SECURITY_GATE_PASSED", plan["id"])


async def test_exception_without_audit_row_fails_audit_evidence(harness: DeployHarness) -> None:
    await harness.add_environment("aws-seoul")
    approver = await create_user(harness.session_factory, "exception-approver")
    now = harness.clock.now()
    # API 를 거치지 않고 넣은 예외: 감사 줄이 없다.
    exception = SecurityException(
        organization_id=harness.organization.id,
        control_id=SecurityControlId.AUD_1,
        was_version=WAS_VERSION,
        reason="감사 줄 없이 넣은 예외",
        requested_by_user_id=harness.admin.user.id,
        approved_by_user_id=approver.id,
        approved_at=now,
        expires_at=now + timedelta(days=1),
        created_at=now,
        updated_at=now,
    )
    async with harness.session_factory() as session, session.begin():
        session.add(exception)
    harness.clock.advance(PAST_GRACE)

    await _self_test(harness).run_once()

    audit = (await _findings(harness))[SecurityControlId.AUD_1]
    assert audit.result == SecurityResult.FAIL
    missing = {
        (item["item_id"], item["target"])
        for item in audit.items
        if item["reason"] == "AUDIT_RECORD_MISSING" and item["target"] == str(exception.id)
    }
    assert missing == {
        (f"exception:{AuditAction.SECURITY_EXCEPTION_REQUESTED}", str(exception.id)),
        (f"approval:{AuditAction.SECURITY_EXCEPTION_APPROVED}", str(exception.id)),
    }


# --- C-AUD-1: 정상 동시 요청과의 경합(검증 P1) ---


async def _audit_findings(harness: DeployHarness) -> list[Finding]:
    async with harness.session_factory() as session:
        return list(
            (
                await session.scalars(
                    select(Finding)
                    .where(
                        Finding.source_ref == SELF_TEST_SOURCE_REF,
                        Finding.control_id == SecurityControlId.AUD_1,
                    )
                    .order_by(Finding.collected_at)
                )
            ).all()
        )


@pytest.mark.parametrize(
    "after_sql",
    [
        # 예전 순서의 재현: 감사 줄을 읽은 직후 정상 요청(예외 생성·승인)이
        # 도메인 줄과 감사 줄을 커밋한다.
        "FROM audit_logs",
        # 도메인 줄(예외)을 읽은 직후, 감사 줄을 읽기 전에 커밋된다.
        "FROM security_exceptions",
    ],
)
async def test_concurrent_valid_request_during_audit_check_passes(
    migration: Migration, monkeypatch: pytest.MonkeyPatch, after_sql: str
) -> None:
    harness = migration.harness
    original_execute = AsyncSession.execute
    injected: dict[str, Any] = {}

    async def interleaved(self: AsyncSession, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = await original_execute(self, statement, *args, **kwargs)
        sql = str(statement)
        if not injected and after_sql in sql and "WHERE" in sql and "organization_id" in sql:
            injected["sql"] = sql
            injected["exception"] = await _create_and_approve_exception(migration)
        return result

    monkeypatch.setattr(AsyncSession, "execute", interleaved)
    runner = _self_test(harness, gate_enabled=False)
    await runner.run_once()
    monkeypatch.setattr(AsyncSession, "execute", original_execute)

    assert injected, "경합 지점에 요청을 끼워 넣지 못했다"
    exception_id = injected["exception"]["id"]
    async with harness.session_factory() as session:
        actions = set(
            (
                await session.scalars(
                    select(AuditLog.action).where(AuditLog.target_id == exception_id)
                )
            ).all()
        )
    assert {
        AuditAction.SECURITY_EXCEPTION_REQUESTED,
        AuditAction.SECURITY_EXCEPTION_APPROVED,
    } <= actions
    (first,) = await _audit_findings(harness)
    assert first.result == SecurityResult.PASS, first.items

    # 다음 주기에는 그 요청도 대조 범위에 들어오고, 감사 줄이 있으므로 그대로 PASS 다.
    harness.clock.advance(PAST_GRACE)
    await runner.run_once()
    latest = (await _audit_findings(harness))[-1]
    assert latest.result == SecurityResult.PASS, latest.items
    summary = {item["item_id"]: item["target"] for item in latest.items}
    assert summary[f"exception:{AuditAction.SECURITY_EXCEPTION_REQUESTED}"] == "1/1"
    assert summary[f"approval:{AuditAction.SECURITY_EXCEPTION_APPROVED}"] == "1/1"


async def test_in_flight_row_is_deferred_and_real_missing_audit_still_fails(
    harness: DeployHarness,
) -> None:
    await harness.add_environment("aws-seoul")
    approver = await create_user(harness.session_factory, "exception-approver")
    now = harness.clock.now()
    # 시험 시작 직전(유예 안)에 감사 줄 없이 생긴 예외: 진행 중 요청과 구분할 수 없어 이번엔 뺀다.
    exception = SecurityException(
        organization_id=harness.organization.id,
        control_id=SecurityControlId.AUD_1,
        was_version=WAS_VERSION,
        reason="감사 줄 없이 넣은 예외",
        requested_by_user_id=harness.admin.user.id,
        approved_by_user_id=approver.id,
        approved_at=now,
        expires_at=now + timedelta(days=1),
        created_at=now,
        updated_at=now,
    )
    async with harness.session_factory() as session, session.begin():
        session.add(exception)
    runner = _self_test(harness)

    await runner.run_once()
    (deferred,) = await _audit_findings(harness)
    assert deferred.result == SecurityResult.PASS, deferred.items

    # 유예가 지난 다음 시험에서는 진짜 누락으로 FAIL 이다.
    harness.clock.advance(PAST_GRACE)
    await runner.run_once()
    latest = (await _audit_findings(harness))[-1]
    assert latest.result == SecurityResult.FAIL
    assert latest.reason == SecurityFindingReason.AUDIT_RECORD_MISSING
    missing = {
        item["item_id"]
        for item in latest.items
        if item["reason"] == "AUDIT_RECORD_MISSING" and item["target"] == str(exception.id)
    }
    assert missing == {
        f"exception:{AuditAction.SECURITY_EXCEPTION_REQUESTED}",
        f"approval:{AuditAction.SECURITY_EXCEPTION_APPROVED}",
    }


async def test_fail_evidence_expires_after_next_cycle_so_later_pass_unblocks(
    harness: DeployHarness,
) -> None:
    await harness.add_environment("aws-seoul")

    async def endpoint() -> None:
        return None

    extra = APIRoute("/api/v1/orgs/{org_id}/unnamed-thing", endpoint, methods=["POST"])
    await _self_test(harness, routes=[*app.routes, extra]).run_once()
    (failed,) = await _audit_findings(harness)
    interval = timedelta(seconds=harness.settings.security_platform_self_test_interval_seconds)
    assert failed.result == SecurityResult.FAIL
    # FAIL 은 24시간이 아니라 다음 주기 + 여유까지만 쓴다.
    assert failed.expires_at - failed.collected_at == interval + FAIL_RENEW_MARGIN < EVIDENCE_TTL
    blocked = _control(
        (await _matrix(harness))["rows"][0]["cells"]["auditLog"], SecurityControlId.AUD_1
    )
    assert blocked["gateStatus"] == SecurityGateStatus.BLOCKED.value

    # 다음 주기: 원인이 사라져 PASS. FAIL 이 아직 유효한 동안은 보수적으로 막힌 채다.
    harness.clock.advance(interval.total_seconds())
    await _self_test(harness).run_once()
    passed = (await _audit_findings(harness))[-1]
    assert passed.result == SecurityResult.PASS
    assert passed.expires_at - passed.collected_at == EVIDENCE_TTL
    still = _control(
        (await _matrix(harness))["rows"][0]["cells"]["auditLog"], SecurityControlId.AUD_1
    )
    assert still["result"] == SecurityResult.FAIL.value

    # 여유가 지나면 FAIL 이 만료되고 최신 PASS 만 남아 게이트가 열린다(24시간 기다리지 않는다).
    harness.clock.advance(FAIL_RENEW_MARGIN.total_seconds())
    control = _control(
        (await _matrix(harness))["rows"][0]["cells"]["auditLog"], SecurityControlId.AUD_1
    )
    assert control["result"] == SecurityResult.PASS.value
    assert control["gateStatus"] == SecurityGateStatus.PASS.value
    assert control["findingIds"] == [str(passed.id)]


# --- 범위·매트릭스 ---


async def test_self_test_skips_when_was_version_is_empty_or_no_environment(
    harness: DeployHarness,
) -> None:
    # 환경이 없는 조직은 시험하지 않는다.
    assert await _self_test(harness).run_once() == 0
    await harness.add_environment("aws-seoul")
    harness.use_settings(was_version=None)
    settings = harness.settings.model_copy(update={"security_gate_enabled": True})
    self_test = PlatformSelfTest(harness.session_factory, harness.clock, settings, app, app.routes)

    assert await self_test.run_once() == 0
    async with harness.session_factory() as session:
        assert (await session.scalars(select(AgentToken))).all() == []


async def test_matrix_uses_pass_evidence_in_every_environment_before_exception(
    harness: DeployHarness,
) -> None:
    for name in ("aws-seoul", "gcp-seoul", "onprem-k3s"):
        await harness.add_environment(name)
    approver = await create_user(harness.session_factory, "exception-approver")
    exceptions = [
        await _add_was_exception(harness, control, approver)
        for control in (SecurityControlId.IAM_2, SecurityControlId.AUD_1)
    ]
    self_test = _self_test(harness)
    before = await _matrix(harness)
    for row in before["rows"]:
        iam = _control(row["cells"]["iam"], SecurityControlId.IAM_2)
        assert iam["result"] == SecurityResult.UNKNOWN.value
        assert iam["gateStatus"] == SecurityGateStatus.WAIVED.value

    await self_test.run_once()

    matrix = await _matrix(harness)
    assert len(matrix["rows"]) == 3
    assert matrix["policyVersion"] == POLICY_VERSION
    for row in matrix["rows"]:
        for cell_name, control_id in (
            ("iam", SecurityControlId.IAM_2),
            ("auditLog", SecurityControlId.AUD_1),
        ):
            control = _control(row["cells"][cell_name], control_id)
            assert control["result"] == SecurityResult.PASS.value
            assert control["gateStatus"] == SecurityGateStatus.PASS.value
            assert control["exception"] is None
            assert SecurityReasonCode.EXCEPTION_APPLIED.value not in control["reasons"]
            assert len(control["findingIds"]) == 1
    # 예외는 그대로 남아 만료를 기다린다.
    async with harness.session_factory() as session:
        stored = (await session.scalars(select(SecurityException))).all()
    assert {row.id for row in stored} == {exception.id for exception in exceptions}
    assert all(row.revoked_at is None for row in stored)


async def _add_was_exception(
    harness: DeployHarness, control: SecurityControlId, approver: Any
) -> SecurityException:
    created_at = harness.clock.now() - timedelta(days=2)
    exception = SecurityException(
        organization_id=harness.organization.id,
        control_id=control,
        was_version=WAS_VERSION,
        reason="수집기 전(#32) 기한부 예외",
        requested_by_user_id=harness.admin.user.id,
        approved_by_user_id=approver.id,
        approved_at=created_at,
        expires_at=created_at + timedelta(days=5),
        created_at=created_at,
        updated_at=created_at,
    )
    async with harness.session_factory() as session, session.begin():
        session.add(exception)
    return exception


async def _create_and_approve_exception(migration: Migration) -> dict[str, Any]:
    path = org_path(migration.harness.organization.id, "/security/exceptions")
    body = {
        "controlId": SecurityControlId.DB_1.value,
        "serviceId": migration.service_id,
        "environmentId": str(migration.gcp.id),
        "reason": "D04~D07 결정 전, 시연은 쓰기 정지 구간에서 수동 확인",
        "expiresAt": (migration.harness.clock.now() + timedelta(days=1)).isoformat(),
    }
    response = await migration.author_client.post(path, json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()["data"]
    approved = await migration.approver_client.post(f"{path}/{created['id']}/approve")
    assert approved.status_code == 200, approved.text
    return created
