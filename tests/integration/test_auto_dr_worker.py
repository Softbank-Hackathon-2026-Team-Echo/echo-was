"""재시작·예외 만료·복귀 경계: HTTP/TLS·트래픽만 test double, 실제 worker 사용."""

import uuid

from sqlalchemy import select

from app.enums import MigrationErrorCode, MigrationPlanStatus
from app.models import MigrationPlan, SecurityException
from tests.integration.security_gate_support import prepare_passing_gate
from tests.integration.test_auto_dr_safety import automatic as automatic  # noqa: F401
from tests.integration.test_auto_dr_safety import payload, plans, poll


async def test_exception_expiry_between_weight_steps_blocks_next_dispatch(automatic):
    migration, _ = automatic
    harness = migration.harness
    await prepare_passing_gate(migration)
    await poll(migration, payload(migration))
    (plan,) = await plans(migration)
    await harness.worker().run_once()
    assert migration.traffic_keys(), "first 10% step must have actually dispatched"
    calls = len(migration.traffic_keys())
    async with harness.session_factory() as session, session.begin():
        exceptions = list(await session.scalars(select(SecurityException)))
        for exception in exceptions:
            exception.expires_at = harness.clock.now()
    harness.clock.advance(2)
    await harness.run_until_idle()
    final = await migration.plan_row(str(plan.id))
    assert final.status == "FAILED"
    assert final.error_code == "SECURITY_GATE_BLOCKED"
    assert final.rollback_status == "SKIPPED"
    assert len(migration.traffic_keys()) == calls


async def test_forward_consent_does_not_authorize_automatic_rollback(automatic):
    migration, _ = automatic
    await poll(migration, payload(migration))
    (plan,) = await plans(migration)
    async with migration.harness.session_factory() as session, session.begin():
        row = await session.get_one(MigrationPlan, uuid.UUID(str(plan.id)))
        row.status = MigrationPlanStatus.ROLLING_BACK
        row.error_code = MigrationErrorCode.ABORTED
    await migration.harness.run_until_idle()
    final = await migration.plan_row(str(plan.id))
    assert final.status == "FAILED" and final.rollback_status == "SKIPPED"
    assert migration.traffic_keys() == []
