"""보안 게이트: 이전 승인과 일꾼 실행 시작에서 같은 판정(SPEC sec-5).

게이트는 harness.tls_checker 를 넣은 시험에서만 켜진다(enable_gate). DNS·TLS 는 가짜다.
"""

import uuid
from datetime import timedelta

from sqlalchemy import func, select

from app.enums import (
    MigrationErrorCode,
    MigrationLogCode,
    MigrationPlanStatus,
    MigrationRollbackStatus,
    SecurityControlId,
    SecurityFindingReason,
    SecurityReasonCode,
    SecurityResult,
)
from app.models import AgentRun, Finding
from app.services.security_finding_service import SecurityFindingService
from app.services.security_normalizers import normalize_secret_delivery
from app.services.security_rules import POLICY_VERSION, SecurityScope
from tests.integration.migration_support import Migration
from tests.integration.security_gate_support import (
    PRIVATE_IP,
    PUBLIC_IP,
    SECRET_VALUE,
    add_exception,
    add_precheck,
    connection_hosts,
    enable_gate,
    gate_findings,
    prepare_passing_gate,
    put_variable,
    record_passing_evidence,
    revoke_exception,
)

_C = SecurityControlId
_R = SecurityReasonCode
_L = MigrationLogCode

# 증거·예외·사전 점검이 하나도 없을 때 막는 필수 통제(C-IMG-3·C-TLS-1 은 게이트가 확인해 통과).
NO_EVIDENCE_BLOCKING = {
    _C.IMG_1.value,
    _C.IMG_2.value,
    _C.IAM_2.value,
    _C.AUD_1.value,
    _C.AI_1.value,
    _C.DB_1.value,
    _C.DST_1.value,
}


def _blocking(details: list[dict[str, str]]) -> dict[str, set[str]]:
    by_control: dict[str, set[str]] = {}
    for item in details:
        by_control.setdefault(item["field"], set()).add(item["reason"])
    return by_control


async def _finding_count(migration: Migration) -> int:
    async with migration.harness.session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(Finding)) or 0)


async def test_approve_with_gate_off_runs_no_checks_and_writes_no_gate_lines(
    migration: Migration,
) -> None:
    plan = await migration.create_plan()

    await migration.approve(plan["id"])
    await migration.harness.run_until_idle()

    codes = await migration.log_codes(plan["id"])
    assert _L.SECURITY_GATE_PASSED not in codes
    assert _L.SECURITY_GATE_BLOCKED not in codes
    assert (await migration.plan_row(plan["id"])).status == MigrationPlanStatus.SUCCEEDED
    assert await _finding_count(migration) == 0


async def test_approve_without_evidence_returns_409_and_keeps_plan_pending(
    migration: Migration,
) -> None:
    fakes = enable_gate(migration)
    plan = await migration.create_plan()
    main_host, target_host = await connection_hosts(migration)

    response = await migration.act(plan["id"], "approve")

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "SECURITY_GATE_BLOCKED"
    blocking = _blocking(body["details"])
    assert set(blocking) == NO_EVIDENCE_BLOCKING
    assert blocking[_C.AI_1.value] == {_R.PRECHECK_MISSING.value}
    assert blocking[_C.DB_1.value] == {_R.EVIDENCE_MISSING.value}
    row = await migration.plan_row(plan["id"])
    assert row.status == MigrationPlanStatus.PENDING_APPROVAL
    assert await migration.plan_job(plan["id"]) is None
    lines = await migration.log_rows(plan["id"])
    assert [line.code for line in lines] == [_L.PLAN_CREATED, _L.SECURITY_GATE_BLOCKED]
    data = lines[-1].data
    assert data is not None
    assert data["policyVersion"] == POLICY_VERSION
    assert data["phase"] == "EXECUTION"
    assert {item["controlId"] for item in data["blocking"]} == NO_EVIDENCE_BLOCKING
    # 실행 직전 확인: 두 주소 모두 확인한 IP 로만 접속했고, 증거를 남겼다.
    assert sorted(fakes.probe.calls) == sorted(
        [(main_host, 443, PUBLIC_IP), (target_host, 443, PUBLIC_IP)]
    )
    (image_match,) = await gate_findings(migration, _C.IMG_3)
    (tls,) = await gate_findings(migration, _C.TLS_1)
    assert (image_match.result, image_match.reason) == (
        SecurityResult.PASS,
        SecurityFindingReason.IMAGE_MATCH,
    )
    assert tls.result == SecurityResult.PASS
    assert tls.environment_id == migration.gcp.id
    assert {str(image_match.id), str(tls.id)} <= set(data["findingIds"])


async def test_approve_with_evidence_exceptions_and_precheck_runs_migration(
    migration: Migration,
) -> None:
    await prepare_passing_gate(migration)
    plan = await migration.create_plan()

    approved = await migration.approve(plan["id"])
    await migration.harness.run_until_idle()

    assert approved["status"] == MigrationPlanStatus.APPROVED
    row = await migration.plan_row(plan["id"])
    assert row.status == MigrationPlanStatus.SUCCEEDED
    codes = await migration.log_codes(plan["id"])
    # 승인 때 한 번, 실행 시작 때 한 번 판정한다.
    assert codes[:5] == [
        _L.PLAN_CREATED,
        _L.SECURITY_GATE_PASSED,
        _L.APPROVED,
        _L.SECURITY_GATE_PASSED,
        _L.STARTED,
    ]
    lines = await migration.log_rows(plan["id"])
    passed = lines[1].data
    assert passed is not None
    assert passed["blocking"] == []
    # C-DB-1·C-DST-1 은 기한부 예외로 통과(값은 WARN)했고 예외 ID 가 판정에 남는다.
    assert len(passed["exceptionIds"]) == 2
    assert len(await gate_findings(migration, _C.TLS_1)) == 2


async def test_start_with_revoked_exception_fails_plan_without_traffic_change(
    migration: Migration,
) -> None:
    enable_gate(migration)
    await record_passing_evidence(migration)
    db_exception = await add_exception(migration, _C.DB_1)
    await add_exception(migration, _C.DST_1)
    await add_precheck(migration)
    plan = await migration.create_plan()
    await migration.approve(plan["id"])

    # 승인 뒤 예외를 철회하면 실행 시작 판정이 막는다(철회한 예외는 읽지 않는다).
    await revoke_exception(migration, db_exception)
    await migration.harness.run_until_idle()

    row = await migration.plan_row(plan["id"])
    assert row.status == MigrationPlanStatus.FAILED
    assert row.error_code == MigrationErrorCode.SECURITY_GATE_BLOCKED
    assert row.rollback_status == MigrationRollbackStatus.NOT_NEEDED
    assert migration.traffic_keys() == []
    lines = await migration.log_rows(plan["id"])
    assert [line.code for line in lines][-2:] == [_L.SECURITY_GATE_BLOCKED, _L.FAILED]
    blocked = lines[-2].data
    assert blocked is not None
    assert blocked["blocking"] == [
        {"controlId": _C.DB_1.value, "reasons": [_R.EVIDENCE_MISSING.value]}
    ]
    assert "security gate blocked" in lines[-1].message.lower()
    view = await migration.get_plan(plan["id"])
    assert view["errorCode"] == MigrationErrorCode.SECURITY_GATE_BLOCKED


async def test_approve_with_unapproved_or_expired_exception_is_blocked(
    migration: Migration,
) -> None:
    enable_gate(migration)
    await record_passing_evidence(migration)
    await add_exception(migration, _C.DB_1, approved=False)
    await add_exception(migration, _C.DST_1, expires_in=timedelta(minutes=1))
    await add_precheck(migration)
    plan = await migration.create_plan()

    response = await migration.act(plan["id"], "approve")

    assert response.status_code == 409, response.text
    blocking = _blocking(response.json()["details"])
    assert set(blocking) == {_C.DB_1.value, _C.DST_1.value}
    assert _R.EXCEPTION_EXPIRED.value in blocking[_C.DST_1.value]


async def test_approve_with_simulated_or_old_precheck_is_blocked_on_ai_1(
    migration: Migration,
) -> None:
    await prepare_passing_gate(migration)
    plan = await migration.create_plan()
    async with migration.harness.session_factory() as session, session.begin():
        for run in (await session.scalars(select(AgentRun))).all():
            run.simulated = True
    await add_precheck(migration, finished_ago=timedelta(hours=25))

    response = await migration.act(plan["id"], "approve")

    assert response.status_code == 409, response.text
    blocking = _blocking(response.json()["details"])
    assert set(blocking) == {_C.AI_1.value}
    assert _R.EVIDENCE_SIMULATED.value in blocking[_C.AI_1.value]


async def test_approve_with_private_target_host_uses_main_host_only(
    migration: Migration,
) -> None:
    fakes = await prepare_passing_gate(migration)
    main_host, target_host = await connection_hosts(migration)
    fakes.resolver.addresses_by_host[target_host] = [PRIVATE_IP]
    plan = await migration.create_plan()

    approved = await migration.approve(plan["id"])

    assert approved["status"] == MigrationPlanStatus.APPROVED
    assert fakes.probe.calls == [(main_host, 443, PUBLIC_IP)]
    (tls,) = await gate_findings(migration, _C.TLS_1)
    assert tls.result == SecurityResult.PASS
    reasons = {item["target"]: item["reason"] for item in tls.items}
    assert reasons == {target_host: SecurityFindingReason.TLS_HOST_NOT_PUBLIC}


async def test_approve_with_suspicious_or_failing_host_is_blocked_on_tls_1(
    migration: Migration,
) -> None:
    fakes = await prepare_passing_gate(migration)
    main_host, target_host = await connection_hosts(migration)
    fakes.resolver.addresses_by_host[target_host] = [PUBLIC_IP, "127.0.0.1"]
    fakes.probe.reason_by_host[main_host] = SecurityFindingReason.TLS_EXPIRES_SOON
    plan = await migration.create_plan()

    response = await migration.act(plan["id"], "approve")

    assert response.status_code == 409, response.text
    assert _blocking(response.json()["details"]) == {_C.TLS_1.value: {_R.CONTROL_FAILED.value}}
    # 의심 주소에는 접속하지 않는다.
    assert fakes.probe.calls == [(main_host, 443, PUBLIC_IP)]
    (tls,) = await gate_findings(migration, _C.TLS_1)
    assert tls.result == SecurityResult.FAIL
    reasons = {item["target"]: item["reason"] for item in tls.items}
    assert reasons == {
        main_host: SecurityFindingReason.TLS_EXPIRES_SOON,
        target_host: SecurityFindingReason.TLS_HOST_SUSPICIOUS,
    }


async def test_approve_after_transient_tls_failure_uses_fresh_probe(
    migration: Migration,
) -> None:
    fakes = await prepare_passing_gate(migration)
    main_host, _ = await connection_hosts(migration)
    fakes.probe.reason_by_host[main_host] = SecurityFindingReason.TLS_TIMEOUT
    plan = await migration.create_plan()
    first = await migration.act(plan["id"], "approve")

    fakes.probe.reason_by_host.clear()
    second = await migration.act(plan["id"], "approve")

    assert first.status_code == 409
    assert _blocking(first.json()["details"]) == {_C.TLS_1.value: {_R.EVIDENCE_UNKNOWN.value}}
    assert second.status_code == 202, second.text


# --- C-SEC-1: 출발 연결의 앱 환경 변수 키가 도착 연결에 모두 있어야 한다 (sec-1-2) ---


async def test_approve_is_blocked_on_sec_1_until_target_has_every_source_secret_key(
    migration: Migration,
) -> None:
    await prepare_passing_gate(migration)
    for key in ("API_KEY", "DB_URL"):
        await put_variable(migration, migration.aws, key)
    await put_variable(migration, migration.gcp, "API_KEY")
    plan = await migration.create_plan()

    blocked = await migration.act(plan["id"], "approve")

    assert blocked.status_code == 409, blocked.text
    assert _blocking(blocked.json()["details"]) == {_C.SEC_1.value: {_R.CONTROL_FAILED.value}}
    (missing,) = await gate_findings(migration, _C.SEC_1)
    assert (missing.result, missing.reason) == (
        SecurityResult.FAIL,
        SecurityFindingReason.SECRETS_MISSING,
    )
    assert (missing.source, missing.environment_id) == ("WAS_STATE", migration.gcp.id)
    # 근거는 빠진 키 이름뿐이다. 값은 증거·응답 어디에도 없다.
    assert missing.items == [{"item_id": "DB_URL"}]
    assert SECRET_VALUE not in blocked.text
    assert SECRET_VALUE not in str(missing.items)
    lines = await migration.log_rows(plan["id"])
    assert str(missing.id) in (lines[-1].data or {})["findingIds"]

    await put_variable(migration, migration.gcp, "DB_URL")
    migration.harness.clock.advance(1)
    approved = await migration.act(plan["id"], "approve")

    assert approved.status_code == 202, approved.text
    ready = (await gate_findings(migration, _C.SEC_1))[-1]
    assert (ready.result, ready.reason, ready.items) == (
        SecurityResult.PASS,
        SecurityFindingReason.SECRETS_READY,
        [],
    )


async def test_stored_sec_1_state_evidence_is_not_reused_by_the_gate(
    migration: Migration,
) -> None:
    """저장된 WAS 상태 C-SEC-1 PASS(앞 판정의 기록)는 쓰지 않고 지금 키로 다시 계산한다."""
    await prepare_passing_gate(migration)
    await put_variable(migration, migration.aws, "API_KEY")
    now = migration.harness.clock.now()
    stale_pass = normalize_secret_delivery(
        set(),
        set(),
        scope=SecurityScope(
            migration.harness.organization.id,
            service_id=uuid.UUID(migration.service_id),
            environment_id=migration.gcp.id,
        ),
        collected_at=now - timedelta(minutes=1),
    )
    async with migration.harness.session_factory() as session:
        await SecurityFindingService(session).record_all([stale_pass], "migration-plan:earlier")
    plan = await migration.create_plan()

    blocked = await migration.act(plan["id"], "approve")

    assert blocked.status_code == 409, blocked.text
    assert _blocking(blocked.json()["details"]) == {_C.SEC_1.value: {_R.CONTROL_FAILED.value}}
