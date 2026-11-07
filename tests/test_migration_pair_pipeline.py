"""이 시험은 정적 인계 계약이다. 운영 worker/엔진 활성화 증거가 아니다."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.services.migration_pair_plan_guard import MigrationPairPlanGuard
from app.services.migration_pair_registry import MigrationPairRegistry
from tests.test_migration_pair_plan_gate import resources
from tests.test_migration_pairs import NOW


@pytest.mark.parametrize(
    "mode,reverse,kind",
    [
        ("PLANNED", False, "cutover"),
        ("PLANNED", True, "rollback"),
        ("FAILBACK", True, "rollback"),
        ("EMERGENCY", False, "promote"),
    ],
)
def test_prepared_contract_preserves_pair_from_snapshot_to_result(mode, reverse, kind):
    from app.executors.base import CutoverRunNotFound
    from app.executors.kubernetes.cutover_job import cutover_job_manifest
    from app.schemas.migration_capability import MigrationPairSnapshot
    from app.services.migration_db_cutover import db_cutover_request, judge_db_cutover
    from tests.test_migration_pairs import registry_data

    config = registry_data()
    service, source, target, plan = resources(mode)
    plan.id = UUID(int=101)
    if reverse:
        source, target = target, source
        config["pairs"][0].update(
            source_db_id="gcp-b-app", target_db_id="aws-a-app", writer_db_id="gcp-b-app"
        )
    guard = MigrationPairPlanGuard(MigrationPairRegistry.from_config(config))
    snapshot = guard.snapshot(service, source, target, plan, NOW)
    plan.fingerprint["migrationPair"] = snapshot.model_dump(mode="json")
    guard.check(service, source, target, plan, NOW)  # 승인용 쌍 검사만. 승인 성공은 아님.
    restored = MigrationPairSnapshot.model_validate_json(snapshot.model_dump_json())
    request = db_cutover_request(
        plan.id,
        plan.mode,
        target.environment_id,
        "aws" if reverse else "gcp",
        promote_consented=mode == "EMERGENCY",
        pair_snapshot=restored,
    )
    assert request.pair_snapshot == snapshot
    assert request.job_kind == kind
    manifest = cutover_job_manifest(
        request, namespace="echo-ops", image="fixture@sha256:" + "1" * 64
    )
    env = {
        item["name"]: item["value"]
        for item in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["SOURCE_DB"] == snapshot.source_db_id
    assert env["TARGET_DB"] == snapshot.target_db_id
    assert env["DB_PAIR_REGISTRY_SHA256"] == snapshot.registry_sha256
    receipt = dict(
        planId=str(plan.id),
        sourceDbId=env["SOURCE_DB"],
        targetDbId=env["TARGET_DB"],
        registrySha256=env["DB_PAIR_REGISTRY_SHA256"],
        contractVersion=env["DB_PAIR_CONTRACT_VERSION"],
        writerDbId=env["TARGET_DB"],
    )
    verdict = judge_db_cutover(
        exit_code=0,
        reason="ALREADY_WRITER",
        finished=True,
        lost=False,
        timed_out=False,
        run_result=CutoverRunNotFound(),
        plan_id=plan.id,
        pair_snapshot=restored,
        pair_receipt=receipt,
    )
    assert verdict.passed
    assert not judge_db_cutover(
        exit_code=0,
        reason="ALREADY_WRITER",
        finished=True,
        lost=False,
        timed_out=False,
        run_result=CutoverRunNotFound(),
        plan_id=plan.id,
        pair_snapshot=restored,
        pair_receipt={**receipt, "targetDbId": "other-db"},
    ).passed


async def test_real_kubernetes_executor_refuses_pair_request_without_image_contract():
    from app.executors.base import DbCutoverStartFailed
    from app.executors.kubernetes.cutover_job import KubernetesDbCutoverExecutor
    from app.services.migration_db_cutover import db_cutover_request
    from tests.test_migration_pairs import GCP, context, registry_data

    registry = MigrationPairRegistry.from_config(registry_data())
    request = db_cutover_request(
        UUID(int=101),
        "PLANNED",
        GCP,
        "gcp",
        promote_consented=False,
        pair_snapshot=registry.snapshot(context(), "dbCutover", NOW),
    )
    http = SimpleNamespace(request=AsyncMock(side_effect=AssertionError("must not send")))
    executor = KubernetesDbCutoverExecutor(http, namespace="echo-ops", image="fixture", reader=None)
    result = await executor.start(request)
    assert isinstance(result, DbCutoverStartFailed)
    assert result.code == "DB_PAIR_WORKER_NOT_READY"
    assert not result.maybe_created
    http.request.assert_not_awaited()


def test_pair_reattachment_checks_entire_identity_not_just_plan_and_kind():
    from app.executors.kubernetes.cutover_job import _same_job, cutover_job_manifest
    from app.services.migration_db_cutover import db_cutover_request
    from tests.test_migration_pairs import GCP, context, registry_data

    registry = MigrationPairRegistry.from_config(registry_data())
    request = db_cutover_request(
        UUID(int=101),
        "PLANNED",
        GCP,
        "gcp",
        promote_consented=False,
        pair_snapshot=registry.snapshot(context(), "dbCutover", NOW),
    )
    manifest = cutover_job_manifest(request, namespace="echo-ops", image="fixture")
    assert _same_job(manifest, request)
    entries = manifest["spec"]["template"]["spec"]["containers"][0]["env"]
    for entry in entries:
        if entry["name"] == "TARGET_DB":
            entry["value"] = "onprem-demo"
    assert not _same_job(manifest, request)


async def test_worker_lock_loads_environment_scope_before_pair_guard():
    from app.services.migration_plan_processor import MigrationPlanProcessor
    from tests.test_migration_pair_worker import worker_fixture

    worker, plan, source, target, _ = worker_fixture()
    source.id, target.id = UUID(int=21), UUID(int=22)
    plan.source_service_environment_id, plan.target_service_environment_id = source.id, target.id
    worker._plans.find_by_id = AsyncMock(return_value=plan)
    service = worker._lock.return_value.service
    worker._services.find_by_id = AsyncMock(return_value=service)
    worker._connections.lock_live_by_service = AsyncMock(
        return_value=[SimpleNamespace(id=source.id), SimpleNamespace(id=target.id)]
    )
    worker._connections.find_by_id = AsyncMock(side_effect=[source, target])
    worker._lease.hold = AsyncMock()
    locked = await MigrationPlanProcessor._lock(worker)
    assert locked.source is source and locked.target is target
    assert worker._connections.find_by_id.await_count == 2
