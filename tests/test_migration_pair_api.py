from types import SimpleNamespace

import httpx

from app.auth.guards import ViewerAccess
from app.dependencies import get_migration_capability_service
from app.main import app
from app.services.migration_pair_registry import MigrationPairRegistry
from tests.test_migration_pairs import AWS, GCP, NOW, ORG, SERVICE, context


async def test_capability_endpoint_returns_fail_closed_schema_without_side_effects() -> None:
    class FakeCapabilities:
        async def get_capabilities(self, organization_id, service_id, source_id, target_id):
            assert (organization_id, service_id, source_id, target_id) == (ORG, SERVICE, AWS, GCP)
            return MigrationPairRegistry.from_config({}).capabilities(context(), NOW)

    async def access():
        return SimpleNamespace(organization_id=ORG)

    guard = ViewerAccess.__metadata__[0].dependency
    app.dependency_overrides[guard] = access
    app.dependency_overrides[get_migration_capability_service] = lambda: FakeCapabilities()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            response = await client.get(
                f"/api/v1/orgs/{ORG}/services/{SERVICE}/migration-capabilities",
                params={"sourceEnvironmentId": str(AWS), "targetEnvironmentId": str(GCP)},
            )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["contractVersion"] == "db-pair/v1"
        assert data["dbCutover"]["supported"] is False
        assert data["dbCutover"]["status"] == "UNKNOWN"
        assert "host" not in str(data).lower()
        assert data["creationGateEnforced"] is False
    finally:
        app.dependency_overrides.clear()


def test_openapi_has_query_fields_and_no_caller_db_or_host_input() -> None:
    path = "/api/v1/orgs/{org_id}/services/{service_id}/migration-capabilities"
    operation = app.openapi()["paths"][path]["get"]
    assert {p["name"] for p in operation["parameters"]} >= {
        "sourceEnvironmentId",
        "targetEnvironmentId",
    }
    assert "requestBody" not in operation


async def test_real_capability_service_reports_connected_gates_but_not_execution_ready(monkeypatch):
    from unittest.mock import AsyncMock

    from app.repositories.environment_repository import EnvironmentRepository
    from app.repositories.service_environment_repository import ServiceEnvironmentRepository
    from app.repositories.service_repository import ServiceRepository
    from app.services.migration_capability_service import MigrationCapabilityService
    from tests.test_migration_pair_plan_gate import resources
    from tests.test_migration_pair_worker import Session
    from tests.test_migration_pairs import registry_data

    service, source, target, _ = resources()
    monkeypatch.setattr(ServiceRepository, "get_live_by_id", AsyncMock(return_value=service))
    monkeypatch.setattr(EnvironmentRepository, "get_live_by_id", AsyncMock())
    monkeypatch.setattr(
        ServiceEnvironmentRepository,
        "search_live_by_service",
        AsyncMock(return_value=[source, target]),
    )
    api = MigrationCapabilityService(
        Session(),
        MigrationPairRegistry.from_config(registry_data()),
        SimpleNamespace(now=lambda: NOW),
        guard_enabled=True,
    )
    result = await api.get_capabilities(ORG, SERVICE, AWS, GCP)
    assert (
        result.creation_gate_enforced
        and result.approval_gate_enforced
        and result.worker_gate_enforced
    )
    assert not result.execution_ready
    assert not result.db_cutover.supported
    assert result.db_cutover.reasons == ["DB_PAIR_WORKER_NOT_READY"]
