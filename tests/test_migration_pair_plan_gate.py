from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.core.config import DbCutoverExecutorMode, ExecutorMode
from app.enums import MigrationPlanMode, ServiceStatus
from app.services.migration_pair_plan_guard import MigrationPairPlanGuard
from app.services.migration_pair_registry import MigrationPairBlockedError, MigrationPairRegistry
from app.services.migration_plan_service import CreateMigrationPlanCommand, MigrationPlanService
from tests.test_migration_pairs import AWS, DEP_A, DEP_B, GCP, NOW, ORG, SERVICE, registry_data


@pytest.mark.parametrize("registered", [False, True])
async def test_real_create_plan_rejects_unregistered_pair_before_insert_or_audit(
    monkeypatch,
    registered,
) -> None:
    from app.enums import EnvironmentConnectionStatus
    from app.services import migration_plan_service as module

    class Session:
        @asynccontextmanager
        async def begin(self):
            yield

    service, source, target, _ = resources()
    service.status, service.is_deleted = ServiceStatus.ACTIVE, False
    source.id, target.id = UUID(int=21), UUID(int=22)
    source.traffic_weight, target.traffic_weight = 100, 0
    environment = SimpleNamespace(
        id=GCP, is_deleted=False, connection_status=EnvironmentConnectionStatus.CONNECTED
    )
    source.environment.id, target.environment.id = AWS, GCP
    clock = SimpleNamespace(now=lambda: NOW)
    api = MigrationPlanService(
        Session(),
        clock,
        ExecutorMode.FAKE,
        [20, 50, 50],
        30,
        10,
        1,
        300,
        db_cutover_mode=DbCutoverExecutorMode.FAKE,
        pair_registry=MigrationPairRegistry.from_config(registry_data() if registered else {}),
    )
    api._services.get_live_by_id = AsyncMock(return_value=service)
    api._environments.get_live_by_id = AsyncMock(return_value=environment)
    api._connections.lock_live_by_service = AsyncMock(return_value=[source, target])
    api._expire_pending_for_service = AsyncMock()
    api._plans.find_open_for_service = AsyncMock(return_value=None)
    api._plans.find_last_succeeded_for_service = AsyncMock(return_value=None)
    api._check_target_not_in_active_disaster = AsyncMock()
    api._check_no_open_deploy_requests = AsyncMock()
    api._check_target_ready = AsyncMock(return_value=target)
    api._insert = AsyncMock(side_effect=AssertionError("must not insert"))
    api._record_audit = AsyncMock(side_effect=AssertionError("must not audit success"))
    monkeypatch.setattr(module, "read_fingerprint", AsyncMock(return_value={}))
    command = CreateMigrationPlanCommand(GCP, None, None, 1.0, 800, 1.0, include_db_cutover=True)
    if registered:
        api._insert = AsyncMock()
        api._record_audit = AsyncMock()
        api._log.append = AsyncMock()
        api._views = AsyncMock(return_value=[None])
        await api.create_plan(ORG, SERVICE, UUID(int=10), command)
        created = api._insert.await_args.args[0]
        pinned = created.fingerprint["migrationPair"]
        assert pinned["source_db_id"] == "aws-a-app"
        assert pinned["target_db_id"] == "gcp-b-app"
        MigrationPairPlanGuard(MigrationPairRegistry.from_config(registry_data())).check(
            service, source, target, created, NOW
        )
        api._get_in_organization = AsyncMock(return_value=created)
        api._organizations.find_by_id = AsyncMock(
            return_value=SimpleNamespace(allow_self_approval=True)
        )
        api._connections.find_by_id = AsyncMock(side_effect=[source, target])
        with pytest.raises(MigrationPairBlockedError) as exc:
            await api.approve_plan(ORG, created.id, UUID(int=10))
        assert exc.value.details[0].reason == "DB_PAIR_WORKER_NOT_READY"
        return
    with pytest.raises(MigrationPairBlockedError):
        await api.create_plan(ORG, SERVICE, UUID(int=10), command)
    api._insert.assert_not_awaited()
    api._record_audit.assert_not_awaited()


@pytest.mark.parametrize("pinned,guard_enabled", [(False, True), (True, True), (True, False)])
async def test_real_approval_rejects_changed_pair_before_tls_or_job_enqueue(
    pinned, guard_enabled
) -> None:
    from app.enums import MigrationPlanStatus

    class Session:
        @asynccontextmanager
        async def begin(self):
            yield

    service, source, target, plan = resources()
    plan.status, plan.id, plan.service_id = (
        MigrationPlanStatus.PENDING_APPROVAL,
        UUID(int=100),
        SERVICE,
    )
    plan.created_by_user_id = UUID(int=10)
    plan.source_service_environment_id, plan.target_service_environment_id = (
        UUID(int=21),
        UUID(int=22),
    )
    if pinned:
        guard = MigrationPairPlanGuard(MigrationPairRegistry.from_config(registry_data()))
        plan.fingerprint["migrationPair"] = guard.snapshot(
            service, source, target, plan, NOW
        ).model_dump(mode="json")
    gate = SimpleNamespace(
        evaluate_plan=AsyncMock(side_effect=AssertionError("must not probe TLS"))
    )
    api = MigrationPlanService(
        Session(),
        SimpleNamespace(now=lambda: NOW),
        ExecutorMode.FAKE,
        [20, 50, 50],
        30,
        10,
        1,
        300,
        security_gate=gate,
        pair_registry=MigrationPairRegistry.from_config(registry_data()) if guard_enabled else None,
    )
    api._get_in_organization = AsyncMock(return_value=plan)
    api._organizations.find_by_id = AsyncMock(
        return_value=SimpleNamespace(allow_self_approval=True)
    )
    api._services.get_live_by_id = AsyncMock(return_value=service)
    api._connections.find_by_id = AsyncMock(side_effect=[source, target])
    api._lock = AsyncMock(side_effect=AssertionError("must not approve"))
    with pytest.raises(MigrationPairBlockedError):
        await api.approve_plan(ORG, plan.id, UUID(int=10))
    gate.evaluate_plan.assert_not_awaited()
    api._lock.assert_not_awaited()


def resources(mode=MigrationPlanMode.PLANNED):
    service = SimpleNamespace(id=SERVICE, organization_id=ORG)
    source = SimpleNamespace(
        service_id=SERVICE,
        environment_id=AWS,
        current_deployment_id=DEP_A,
        environment=SimpleNamespace(organization_id=ORG),
    )
    target = SimpleNamespace(
        service_id=SERVICE,
        environment_id=GCP,
        current_deployment_id=DEP_B,
        environment=SimpleNamespace(organization_id=ORG),
    )
    plan = SimpleNamespace(mode=mode, steps=[{"kind": "DB_CUTOVER"}], fingerprint={})
    return service, source, target, plan


@pytest.mark.parametrize("mode", list(MigrationPlanMode))
def test_plan_snapshot_creation_and_approval_check_bind_exact_pair(mode) -> None:
    config = registry_data()
    service, source, target, plan = resources(mode)
    if mode == MigrationPlanMode.FAILBACK:
        source, target = target, source
        config["pairs"][0].update(
            source_db_id="gcp-b-app", target_db_id="aws-a-app", writer_db_id="gcp-b-app"
        )
    guard = MigrationPairPlanGuard(MigrationPairRegistry.from_config(config))
    plan.fingerprint["migrationPair"] = guard.snapshot(
        service, source, target, plan, NOW
    ).model_dump(mode="json")
    guard.check(service, source, target, plan, NOW)
    target.current_deployment_id = UUID(int=999)
    with pytest.raises(MigrationPairBlockedError):
        guard.check(service, source, target, plan, NOW)


def test_legacy_plan_without_snapshot_is_not_grandfathered_when_guard_enabled() -> None:
    service, source, target, plan = resources()
    with pytest.raises(MigrationPairBlockedError):
        MigrationPairPlanGuard(MigrationPairRegistry.from_config(registry_data())).check(
            service, source, target, plan, NOW
        )
