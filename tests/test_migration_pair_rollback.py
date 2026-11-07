"""저장된 bound 롤백 재개는 복구 요청 전에 사람 확인으로 끝낸다."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.enums import MigrationErrorCode, MigrationPlanStatus, MigrationRollbackStatus
from tests.test_migration_pair_worker import worker_fixture
from tests.test_migration_pairs import NOW


@pytest.mark.parametrize("requested_at", [None, NOW])
@pytest.mark.parametrize(
    "case,reason",
    [
        ("valid", "DB_PAIR_WORKER_NOT_READY"),
        ("snapshot", "DB_PAIR_SNAPSHOT_CHANGED"),
        ("off", "DB_PAIR_GUARD_DISABLED"),
        ("writer", "DB_WRITER_MISMATCH"),
        ("stale", "ROUTE_STALE"),
        ("null_snapshot_off", "DB_PAIR_GUARD_DISABLED"),
        ("missing_snapshot_on", "DB_PAIR_SNAPSHOT_CHANGED"),
        ("missing_target", "DB_BINDING_UNREGISTERED"),
    ],
)
async def test_bound_rollback_resume_stops_before_recovery_side_effects(
    requested_at, case, reason
) -> None:
    worker, plan, _, _, external = worker_fixture()
    plan.status = MigrationPlanStatus.ROLLING_BACK
    plan.rollback_attempts, plan.rollback_requested_at = 0, requested_at
    if case == "snapshot":
        plan.fingerprint["migrationPair"]["target_db_id"] = "other-db"
    elif case == "off":
        worker._settings.migration_pair_guard_enabled = False
    elif case == "writer":
        worker._settings.migration_pair_registry["pairs"][0]["writer_db_id"] = "gcp-b-app"
    elif case == "stale":
        worker._clock.now = lambda: NOW + timedelta(seconds=301)
    elif case == "null_snapshot_off":
        worker._settings.migration_pair_guard_enabled = False
        plan.fingerprint["migrationPair"] = None
    elif case == "missing_snapshot_on":
        plan.fingerprint = {}
    elif case == "missing_target":
        worker._lock.return_value = replace(worker._lock.return_value, target=None)
    worker._plans.find_by_id = AsyncMock(return_value=plan)
    worker._targets = AsyncMock(side_effect=AssertionError("recovery target lookup forbidden"))

    outcome = await worker.advance()

    assert outcome is not None
    external.assert_not_awaited()  # traffic, DB Job, security executor의 공동 거절 창구
    worker._targets.assert_not_awaited()
    worker._lease.extend.assert_not_awaited()
    worker._plans.transition.assert_awaited_once()
    transition = worker._plans.transition.await_args
    assert transition.args[:2] == (plan.id, MigrationPlanStatus.ROLLING_BACK)
    changes = transition.args[2]
    assert changes["status"] == MigrationPlanStatus.FAILED
    assert changes["error_code"] == MigrationErrorCode.DB_CUTOVER_UNKNOWN
    assert changes["rollback_status"] == MigrationRollbackStatus.SKIPPED
    assert "rollback_requested_at" not in changes
    assert "rollback_attempts" not in changes
    assert plan.rollback_requested_at == requested_at
    assert plan.rollback_attempts == 0
    worker._log.append.assert_awaited_once()
    assert worker._log.append.await_args.kwargs["data"] == {"migrationPairReason": reason}
    worker._record_finished.assert_awaited_once_with(
        plan, MigrationPlanStatus.FAILED, MigrationErrorCode.DB_CUTOVER_UNKNOWN
    )


@pytest.mark.parametrize("case", ["valid", "off", "writer", "snapshot"])
async def test_bound_rollback_exhausted_attempts_still_records_pair_manual_stop(case) -> None:
    worker, plan, _, _, external = worker_fixture()
    plan.status = MigrationPlanStatus.ROLLING_BACK
    plan.rollback_attempts = worker._settings.migration_rollback_max_attempts
    plan.rollback_requested_at = NOW
    if case == "off":
        worker._settings.migration_pair_guard_enabled = False
    elif case == "writer":
        worker._settings.migration_pair_registry["pairs"][0]["writer_db_id"] = "gcp-b-app"
    elif case == "snapshot":
        plan.fingerprint["migrationPair"]["target_db_id"] = "other-db"
    worker._plans.find_by_id = AsyncMock(return_value=plan)
    worker._targets = AsyncMock()

    await worker.advance()

    external.assert_not_awaited()
    worker._targets.assert_not_awaited()
    worker._lease.extend.assert_not_awaited()
    worker._plans.transition.assert_awaited_once()
    changes = worker._plans.transition.await_args.args[2]
    assert changes["status"] == MigrationPlanStatus.FAILED
    assert changes["rollback_status"] == MigrationRollbackStatus.SKIPPED
    assert "rollback_attempts" not in changes
    assert "migrationPairReason" in worker._log.append.await_args.kwargs["data"]
