"""통제별 시간 예산, 취소 뒤 DB 복구, API/일꾼의 차단을 확인한다."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text

from app.clients.tls_probe import PublicHostTlsChecker
from app.enums import MigrationPlanStatus, SecurityControlId, SecurityFindingSource, SecurityResult
from app.repositories.agent_run_repository import AgentRunRepository
from app.repositories.deployment_repository import DeploymentRepository
from app.repositories.finding_repository import FindingRepository
from app.repositories.service_environment_variable_repository import (
    ServiceEnvironmentVariableRepository,
)
from app.services import security_rules
from app.services.security_gate import SecurityGate, gate_log_data
from app.services.security_rules import SecurityScope
from tests.integration.migration_support import Migration
from tests.integration.security_gate_support import WAS_VERSION, add_exception, prepare_passing_gate
from tests.integration.test_security_finding_completeness import _finding

_C = SecurityControlId
_CHECKS = [_C.IMG_1, _C.IMG_3, _C.SEC_1, _C.AI_1, _C.TLS_1]
_METHODS: dict[SecurityControlId, tuple[type[Any], str]] = {
    _C.IMG_1: (FindingRepository, "search_unexpired_by_organization"),
    _C.IMG_3: (DeploymentRepository, "find_by_id"),
    _C.SEC_1: (ServiceEnvironmentVariableRepository, "search_live_keys_by_connections"),
    _C.AI_1: (AgentRunRepository, "find_latest_succeeded_precheck"),
    _C.TLS_1: (PublicHostTlsChecker, "check_hosts"),
}


def _short_budget(monkeypatch: pytest.MonkeyPatch, control: SecurityControlId) -> None:
    rule = replace(security_rules.get_rule(control), time_budget=timedelta(seconds=0.25))
    monkeypatch.setitem(security_rules._RULE_BY_CONTROL, control, rule)
    monkeypatch.setattr(
        security_rules,
        "RULES",
        tuple(rule if item.control_id == control else item for item in security_rules.RULES),
    )


def _slow_check(
    monkeypatch: pytest.MonkeyPatch,
    control: SecurityControlId,
    *,
    database_sleep: bool = False,
) -> list[str]:
    owner, name = _METHODS[control]
    original: Callable[..., Awaitable[Any]] = getattr(owner, name)
    events: list[str] = []

    async def slow(self: Any, *args: Any, **kwargs: Any) -> Any:
        # 옛 전역 조회와 새 통제별 조회 양쪽에서 C-IMG-1 지연을 재현한다.
        if control == _C.IMG_1 and kwargs.get("control_id") not in (None, _C.IMG_1):
            return await original(self, *args, **kwargs)
        events.append("started")
        try:
            if database_sleep:
                await self._session.execute(text("SELECT pg_sleep(0.6)"))
            else:
                await asyncio.sleep(0.6)
        except asyncio.CancelledError:
            events.append("cancelled")
            raise
        events.append("completed")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(owner, name, slow)
    _short_budget(monkeypatch, control)
    return events


async def _evaluate(migration: Migration, plan_id: str) -> security_rules.SecurityDecision:
    harness = migration.harness
    assert harness.tls_checker is not None
    async with harness.session_factory() as session:
        decision = await SecurityGate(
            session, harness.clock, harness.tls_checker, WAS_VERSION
        ).evaluate_plan(uuid.UUID(plan_id))
        # 실제 DB 쿼리 취소 뒤에도 같은 세션을 쓸 수 있어야 한다.
        async with session.begin():
            assert await session.scalar(text("SELECT 1")) == 1
        return decision


@pytest.mark.parametrize("control", _CHECKS)
async def test_slow_control_becomes_unknown_and_discards_late_pass(
    migration: Migration, monkeypatch: pytest.MonkeyPatch, control: SecurityControlId
) -> None:
    await prepare_passing_gate(migration)
    plan = await migration.create_plan()
    events = _slow_check(monkeypatch, control)

    decision = await _evaluate(migration, plan["id"])

    result = next(item for item in decision.controls if item.control_id == control)
    assert result.result == SecurityResult.UNKNOWN
    assert result.is_blocking
    assert [reason.value for reason in result.reasons] == ["CONTROL_TIMEOUT"]
    assert result.finding_ids == ()
    assert events == ["started", "cancelled"]
    assert control.value in gate_log_data(decision)["timedOutControlIds"]


async def test_db_timeout_rolls_back_and_can_record_blocking_decision(
    migration: Migration, monkeypatch: pytest.MonkeyPatch
) -> None:
    await prepare_passing_gate(migration)
    plan = await migration.create_plan()
    events = _slow_check(monkeypatch, _C.SEC_1, database_sleep=True)

    decision = await _evaluate(migration, plan["id"])

    assert decision.is_blocked
    assert events == ["started", "cancelled"]
    assert gate_log_data(decision)["blocking"] == [
        {"controlId": "C-SEC-1", "reasons": ["CONTROL_TIMEOUT"]}
    ]


async def test_timed_out_control_uses_existing_exception_without_claiming_pass(
    migration: Migration, monkeypatch: pytest.MonkeyPatch
) -> None:
    await prepare_passing_gate(migration)
    exception_id = await add_exception(migration, _C.SEC_1)
    plan = await migration.create_plan()
    _slow_check(monkeypatch, _C.SEC_1)

    decision = await _evaluate(migration, plan["id"])

    result = next(item for item in decision.controls if item.control_id == _C.SEC_1)
    assert (result.result, result.effective_result) == (SecurityResult.UNKNOWN, SecurityResult.WARN)
    assert result.exception_id == exception_id
    assert not decision.is_blocked
    assert gate_log_data(decision)["timedOutControlIds"] == ["C-SEC-1"]


async def test_approval_timeout_is_409_and_fresh_retry_succeeds(
    migration: Migration, monkeypatch: pytest.MonkeyPatch
) -> None:
    await prepare_passing_gate(migration)
    plan = await migration.create_plan()
    with monkeypatch.context() as patch:
        _slow_check(patch, _C.SEC_1)
        response = await migration.act(plan["id"], "approve")
    assert response.status_code == 409, response.text
    assert response.json()["details"] == [{"field": "C-SEC-1", "reason": "CONTROL_TIMEOUT"}]
    assert (await migration.plan_row(plan["id"])).status == MigrationPlanStatus.PENDING_APPROVAL
    assert await migration.plan_job(plan["id"]) is None
    lines = await migration.log_rows(plan["id"])
    assert (lines[-1].data or {})["timedOutControlIds"] == ["C-SEC-1"]

    approved = await migration.act(plan["id"], "approve")
    assert approved.status_code == 202, approved.text


async def test_worker_timeout_blocks_before_traffic_change(
    migration: Migration, monkeypatch: pytest.MonkeyPatch
) -> None:
    await prepare_passing_gate(migration)
    plan = await migration.create_plan()
    await migration.approve(plan["id"])
    _slow_check(monkeypatch, _C.SEC_1)

    await migration.harness.run_until_idle()

    row = await migration.plan_row(plan["id"])
    assert row.status == MigrationPlanStatus.FAILED
    assert row.error_code == "SECURITY_GATE_BLOCKED"
    assert migration.traffic_keys() == []
    lines = await migration.log_rows(plan["id"])
    blocked = next(line for line in lines if line.code == "SECURITY_GATE_BLOCKED")
    assert (blocked.data or {})["timedOutControlIds"] == ["C-SEC-1"]


async def test_image_reads_share_one_control_budget(
    migration: Migration, monkeypatch: pytest.MonkeyPatch
) -> None:
    await prepare_passing_gate(migration)
    plan = await migration.create_plan()
    original = DeploymentRepository.find_by_id
    events: list[str] = []

    async def delayed(self: DeploymentRepository, deployment_id: uuid.UUID) -> Any:
        events.append("started")
        try:
            await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            events.append("cancelled")
            raise
        events.append("completed")
        return await original(self, deployment_id)

    monkeypatch.setattr(DeploymentRepository, "find_by_id", delayed)
    _short_budget(monkeypatch, _C.IMG_3)

    decision = await _evaluate(migration, plan["id"])

    assert decision.get_control(_C.IMG_3).result == SecurityResult.UNKNOWN
    assert events == ["started", "completed", "started", "cancelled"]


async def test_evidence_and_key_reads_share_one_secret_control_budget(
    migration: Migration, monkeypatch: pytest.MonkeyPatch
) -> None:
    await prepare_passing_gate(migration)
    plan = await migration.create_plan()
    original_findings = FindingRepository.search_unexpired_by_organization
    original_keys = ServiceEnvironmentVariableRepository.search_live_keys_by_connections
    events: list[str] = []

    async def delayed_findings(self: FindingRepository, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("control_id") == _C.SEC_1:
            await asyncio.sleep(0.15)
            events.append("evidence")
        return await original_findings(self, *args, **kwargs)

    async def delayed_keys(
        self: ServiceEnvironmentVariableRepository, *args: Any, **kwargs: Any
    ) -> Any:
        try:
            await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            events.append("cancelled")
            raise
        events.append("keys")
        return await original_keys(self, *args, **kwargs)

    monkeypatch.setattr(FindingRepository, "search_unexpired_by_organization", delayed_findings)
    monkeypatch.setattr(
        ServiceEnvironmentVariableRepository, "search_live_keys_by_connections", delayed_keys
    )
    _short_budget(monkeypatch, _C.SEC_1)

    decision = await _evaluate(migration, plan["id"])

    assert decision.get_control(_C.SEC_1).result == SecurityResult.UNKNOWN
    assert events == ["evidence", "cancelled"]


async def test_unrelated_timeout_error_is_not_claimed_as_budget_expiry(
    migration: Migration, monkeypatch: pytest.MonkeyPatch
) -> None:
    await prepare_passing_gate(migration)
    plan = await migration.create_plan()

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("repository timeout before control deadline")

    monkeypatch.setattr(
        ServiceEnvironmentVariableRepository, "search_live_keys_by_connections", broken
    )
    with pytest.raises(TimeoutError, match="repository timeout"):
        await _evaluate(migration, plan["id"])


async def test_external_cancellation_propagates_and_stops_query(
    migration: Migration, monkeypatch: pytest.MonkeyPatch
) -> None:
    await prepare_passing_gate(migration)
    plan = await migration.create_plan()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def waiting(*args: Any, **kwargs: Any) -> Any:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(
        ServiceEnvironmentVariableRepository, "search_live_keys_by_connections", waiting
    )
    task = asyncio.create_task(_evaluate(migration, plan["id"]))
    async with asyncio.timeout(5):
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert cancelled.is_set()


async def test_gate_skips_old_live_check_evidence_without_hiding_manual_evidence(
    migration: Migration, monkeypatch: pytest.MonkeyPatch
) -> None:
    await prepare_passing_gate(migration)
    plan = await migration.create_plan()
    harness = migration.harness
    scope = SecurityScope(
        harness.organization.id,
        service_id=uuid.UUID(migration.service_id),
        environment_id=migration.gcp.id,
    )
    now = harness.clock.now()
    old_ids: set[uuid.UUID] = set()
    rows = []
    for control, source in [
        (_C.IMG_3, SecurityFindingSource.WAS_STATE),
        (_C.TLS_1, SecurityFindingSource.TLS_PROBE),
        (_C.SEC_1, SecurityFindingSource.WAS_STATE),
    ]:
        for _ in range(600):
            row = _finding(scope, control, source, SecurityResult.PASS, now - timedelta(seconds=30))
            rows.append(row)
            old_ids.add(row.id)
    manual = _finding(
        scope, _C.SEC_1, SecurityFindingSource.MANUAL_REVIEW, SecurityResult.PASS, now
    )
    manual.reviewer_user_id = migration.approver.user.id
    manual.basis_url = "https://example.com/security-review"
    async with harness.session_factory() as session, session.begin():
        session.add_all([*rows, manual])
    original = FindingRepository.search_unexpired_by_organization
    loaded: set[uuid.UUID] = set()

    async def tracked(self: FindingRepository, *args: Any, **kwargs: Any) -> Any:
        found = await original(self, *args, **kwargs)
        loaded.update(item.id for item in found)
        return found

    monkeypatch.setattr(FindingRepository, "search_unexpired_by_organization", tracked)

    decision = await _evaluate(migration, plan["id"])

    assert not decision.is_blocked
    assert loaded.isdisjoint(old_ids)
    assert manual.id in decision.get_control(_C.SEC_1).finding_ids
