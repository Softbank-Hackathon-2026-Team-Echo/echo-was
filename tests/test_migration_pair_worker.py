"""실제 processor 경로를 DB 없이 실행한다. 저장소·외부 창구만 fixture다."""

from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.core.config import Settings
from app.enums import MigrationPlanStatus, ServiceStatus
from app.services.migration_pair_plan_guard import MigrationPairPlanGuard
from app.services.migration_pair_registry import MigrationPairRegistry
from app.services.migration_plan_processor import MigrationPlanProcessor, _Locked
from tests.test_migration_pair_plan_gate import resources
from tests.test_migration_pairs import NOW, registry_data


class Session:
    @asynccontextmanager
    async def begin(self):
        yield


def worker_fixture():
    service, source, target, plan = resources()
    service.status, service.is_deleted = ServiceStatus.ACTIVE, False
    plan.id, plan.service_id = UUID(int=101), service.id
    plan.status = MigrationPlanStatus.APPROVED
    plan.abort_requested_at = None
    plan.current_step, plan.step_phase = 0, "JUDGE"
    plan.step_results = [{"status": "PENDING"}]
    plan.traffic_requested_at = None
    plan.fingerprint["migrationPair"] = (
        MigrationPairPlanGuard(MigrationPairRegistry.from_config(registry_data()))
        .snapshot(service, source, target, plan, NOW)
        .model_dump(mode="json")
    )
    settings = Settings(migration_pair_guard_enabled=True, migration_pair_registry=registry_data())
    lease = SimpleNamespace(
        job=SimpleNamespace(subject_id=plan.id), is_exhausted=False, extend=AsyncMock()
    )
    external = AsyncMock(side_effect=AssertionError("external side effect before pair guard"))
    executors = SimpleNamespace(
        db_cutover=SimpleNamespace(start=external), traffic=SimpleNamespace(set_weights=external)
    )
    worker = MigrationPlanProcessor(
        Session(),
        lease,
        settings,
        SimpleNamespace(now=lambda: NOW),
        executors,
        SimpleNamespace(evaluate_plan=external),
    )
    worker._lock = AsyncMock(return_value=_Locked(service, [source, target], plan, source, target))
    worker._plans.transition = AsyncMock(return_value=True)
    worker._log.append = AsyncMock()
    worker._record_finished = AsyncMock()
    return worker, plan, source, target, external


async def test_worker_start_checks_valid_pair_before_security_or_traffic():
    worker, plan, _, _, external = worker_fixture()
    await worker._start()
    external.assert_not_awaited()
    change = worker._plans.transition.await_args.args[2]
    assert change["status"] == MigrationPlanStatus.FAILED
    assert worker._log.append.await_args.kwargs["data"] == {
        "migrationPairReason": "DB_PAIR_WORKER_NOT_READY"
    }


@pytest.mark.parametrize("phase", ["start", "run", "job"])
@pytest.mark.parametrize(
    "case,reason",
    [
        ("valid", "DB_PAIR_WORKER_NOT_READY"),
        ("changed", "DB_PAIR_SNAPSHOT_CHANGED"),
        ("tenant", "DB_BINDING_SCOPE_MISMATCH"),
        ("writer", "DB_WRITER_MISMATCH"),
        ("stale", "ROUTE_STALE"),
        ("disabled", "DB_PAIR_GUARD_DISABLED"),
        ("missing", "DB_PAIR_SNAPSHOT_CHANGED"),
    ],
)
async def test_actual_worker_boundaries_refuse_before_any_external_call(phase, case, reason):
    worker, plan, source, target, external = worker_fixture()
    if case == "changed":
        plan.fingerprint["migrationPair"]["target_db_id"] = "other-db"
    elif case == "tenant":
        target.environment.organization_id = UUID(int=999)
    elif case == "writer":
        worker._settings.migration_pair_registry["pairs"][0]["writer_db_id"] = "gcp-b-app"
    elif case == "stale":
        worker._clock.now = lambda: NOW + timedelta(seconds=301)
    elif case == "disabled":
        worker._settings.migration_pair_guard_enabled = False
    elif case == "missing":
        plan.fingerprint = {}
    if phase == "start":
        await worker._start()
    else:
        plan.status, plan.step_phase = MigrationPlanStatus.RUNNING, "APPLY"
        plan.steps = [{"kind": "DB_CUTOVER", "maxWaitSeconds": 120}]
        plan.simulate = None
        worker._cutover_request = AsyncMock(return_value=SimpleNamespace())
        if phase == "job":
            # 직접 호출해도 요청 시각 저장·Job 송신보다 먼저 차단한다.
            await worker._cutover_apply(plan)
        else:
            await worker._run(plan)
    external.assert_not_awaited()
    worker._lease.extend.assert_not_awaited()
    changes = worker._plans.transition.await_args.args[2]
    assert changes["status"] == MigrationPlanStatus.FAILED
    assert changes["rollback_status"] == ("NOT_NEEDED" if phase == "start" else "SKIPPED")
    assert worker._log.append.await_args.kwargs["data"] == {"migrationPairReason": reason}
    assert plan.traffic_requested_at is None


async def test_bound_result_rejects_already_writer_without_receipt():
    from app.executors.base import CutoverRunNotFound
    from app.services.migration_db_cutover import judge_db_cutover

    worker, plan, _, _, _ = worker_fixture()
    from app.schemas.migration_capability import MigrationPairSnapshot

    snapshot = MigrationPairSnapshot.model_validate(plan.fingerprint["migrationPair"])
    verdict = judge_db_cutover(
        exit_code=0,
        reason="ALREADY_WRITER",
        finished=True,
        lost=False,
        timed_out=False,
        run_result=CutoverRunNotFound(),
        plan_id=plan.id,
        pair_snapshot=snapshot,
    )
    assert not verdict.passed
    assert verdict.recovery == "MANUAL"
    assert verdict.reason == "DB_PAIR_RESULT_MISMATCH"


async def test_actual_result_path_does_not_accept_unbound_already_writer():
    from app.executors.base import CutoverRunNotFound
    from app.services.migration_db_cutover import db_cutover_request
    from tests.test_migration_pairs import GCP

    worker, plan, _, _, external = worker_fixture()
    plan.status, plan.step_phase = MigrationPlanStatus.RUNNING, "JUDGE"
    plan.simulate = None
    plan.step_results = [
        {"cutover": {"jobFinished": True, "exitCode": 0, "reason": "ALREADY_WRITER"}}
    ]
    worker._cutover_request = AsyncMock(
        return_value=db_cutover_request(plan.id, plan.mode, GCP, "gcp", promote_consented=False)
    )
    worker._find_cutover_run = AsyncMock(return_value=CutoverRunNotFound())
    worker._finish_cutover = AsyncMock()
    await worker._cutover_judge(plan)
    verdict = worker._finish_cutover.await_args.args[4]
    assert not verdict.passed
    assert verdict.reason == "DB_PAIR_RESULT_MISMATCH"
    external.assert_not_awaited()
