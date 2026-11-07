"""실제 앱/router를 통과한다. 저장소와 인증 자료만 fixture로 교체한다.

운영 접속·PostgreSQL 검증이 아니다. 실제 DB/세션 권한은 integration 시험 및 CI로 구분한다.
"""

import socket
import subprocess
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth.actors import HumanActor, OrganizationAccess
from app.auth.guards import ViewerAccess
from app.dependencies import get_clock, get_session
from app.enums import OrganizationRole, ServiceEnvironmentRole, ServiceStatus
from app.main import app
from app.models import Environment, Organization, Service, ServiceEnvironment, User
from app.repositories.deployment_repository import DeploymentRepository
from app.repositories.environment_repository import EnvironmentRepository
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.repositories.service_repository import ServiceRepository
from app.services.cost_catalog import get_cost_catalog, load_cost_catalog

NOW = datetime(2026, 11, 7, 9, tzinfo=UTC)


@pytest.fixture
def cost_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Service, Environment, Environment]]:
    organization = Organization(id=uuid4(), slug="fixture", name="fixture")
    service = Service(
        id=uuid4(), organization_id=organization.id, name="fixture-app", status=ServiceStatus.ACTIVE
    )
    aws = Environment(
        id=uuid4(),
        organization_id=organization.id,
        name="fixture-aws",
        kind="aws",
        config={"region": "ap-northeast-1"},
        is_deleted=False,
    )
    onprem = Environment(
        id=uuid4(),
        organization_id=organization.id,
        name="fixture-lab",
        kind="onprem",
        config={},
        is_deleted=False,
    )
    connection = ServiceEnvironment(
        id=uuid4(),
        service_id=service.id,
        environment_id=aws.id,
        current_deployment_id=None,
        traffic_weight=100,
        traffic_applied_at=NOW,
        is_deleted=False,
    )
    connection.environment = aws
    access = OrganizationAccess(
        organization=organization,
        actor=HumanActor(user=User(id=uuid4()), session_id=uuid4()),
        role=OrganizationRole.VIEWER,
    )
    clock = MagicMock()
    clock.now.return_value = NOW
    session = MagicMock()
    session.begin.return_value.__aenter__ = AsyncMock()
    session.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    session.flush = AsyncMock(side_effect=AssertionError("product mutation"))
    session.commit = AsyncMock(side_effect=AssertionError("explicit commit"))
    session.execute = AsyncMock(side_effect=AssertionError("unexpected SQL write"))
    session.add.side_effect = AssertionError("product mutation")

    async def scoped_get(
        self: object, organization_id: object, service_id: object, **kwargs: object
    ) -> Service:
        from app.core.exceptions import ServiceNotFoundError

        if organization_id != organization.id or service_id != service.id:
            raise ServiceNotFoundError("service not found")
        return service

    async def provide_session() -> AsyncIterator[object]:
        yield session

    monkeypatch.setattr(ServiceRepository, "get_live_by_id", scoped_get)
    monkeypatch.setattr(
        EnvironmentRepository, "search_all_live", AsyncMock(return_value=[aws, onprem])
    )
    monkeypatch.setattr(
        ServiceEnvironmentRepository, "search_live_by_service", AsyncMock(return_value=[connection])
    )
    monkeypatch.setattr(DeploymentRepository, "search_by_ids", AsyncMock(return_value=[]))

    def deny_external(*args: object, **kwargs: object) -> None:
        raise AssertionError("cost GET attempted socket/subprocess")

    monkeypatch.setattr(socket.socket, "connect", deny_external)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_external)
    monkeypatch.setattr(subprocess, "Popen", deny_external)
    monkeypatch.setattr(subprocess, "run", deny_external)
    overrides = dict(app.dependency_overrides)
    app.dependency_overrides[ViewerAccess.__metadata__[0].dependency] = lambda: access
    app.dependency_overrides[get_session] = provide_session
    app.dependency_overrides[get_clock] = lambda: clock
    app.dependency_overrides[get_cost_catalog] = load_cost_catalog
    try:
        yield service, aws, onprem
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)


async def test_real_router_traffic_only_is_zero_not_invented_savings(
    cost_api: tuple[Service, Environment, Environment],
) -> None:
    service, aws, onprem = cost_api
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/orgs/{service.organization_id}/costs/recommendations",
            params={"serviceId": str(service.id)},
        )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["organizationId"] == str(service.organization_id)
    assert data["serviceId"] == str(service.id)
    assert data["currentMonthlyUsd"] is None
    assert data["inventoryObservedAt"] is None
    assert data["computedAt"] == "2026-11-07T09:00:00Z"
    assert data["currentPlacement"][0]["environmentId"] == str(aws.id)
    assert data["currentPlacement"][0]["role"] == ServiceEnvironmentRole.SERVING
    alternative = data["alternatives"][0]
    assert alternative["environmentId"] == str(onprem.id)
    assert alternative["fixedCostSavingsMonthlyUsd"] == 0
    assert alternative["netSavingsMonthlyUsd"] is None
    assert alternative["oneTimeMigrationUsd"] is None
    assert alternative["breakEvenMonths"] is None
    assert alternative["recoverySeconds"] is None
    assert alternative["isSafeRecommendation"] is False
    assert alternative["removedResourceIds"] == []
    assert {c["status"] for c in alternative["constraints"]} == {"UNKNOWN"}
    assert "SERVICE_RESOURCE_ATTRIBUTION_UNKNOWN" in data["missingReasons"]
    assert "ONPREM_COST_UNASSESSED" in alternative["missingReasons"]
    assert data["billing"][0]["scope"] == "ACCOUNT"
    assert data["billing"][0]["days"][0]["date"] == "2026-11-04"
    assert data["catalogEstimate"]["source"] == "ESTIMATE"


async def test_real_router_service_validation(
    cost_api: tuple[Service, Environment, Environment],
) -> None:
    service, _, _ = cost_api
    path = f"/api/v1/orgs/{service.organization_id}/costs/recommendations"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for params in ({}, {"serviceId": "invalid"}):
            assert (await client.get(path, params=params)).status_code == 422
        response = await client.get(path, params={"serviceId": str(uuid4())})
        assert response.status_code == 404
        assert response.json()["code"] == "SERVICE_NOT_FOUND"


async def test_real_router_empty_has_reasons_not_fake_zero(
    cost_api: tuple[Service, Environment, Environment],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _ = cost_api
    monkeypatch.setattr(EnvironmentRepository, "search_all_live", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        ServiceEnvironmentRepository, "search_live_by_service", AsyncMock(return_value=[])
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/orgs/{service.organization_id}/costs/recommendations",
            params={"serviceId": str(service.id)},
        )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["alternatives"] == []
    assert data["currentPlacement"] == []
    assert data["catalogEstimate"]["monthlyUsd"] is None
    assert data["currentMonthlyUsd"] is None
    assert "NO_ALTERNATIVE_ENVIRONMENT" in data["missingReasons"]
    assert data["confidence"] == "INSUFFICIENT_DATA"
    assert data["freshness"] == "UNVERIFIED"


async def test_real_router_catalog_failure_is_not_success_fixture(
    cost_api: tuple[Service, Environment, Environment],
) -> None:
    from app.services.cost_catalog import CostCatalogError

    def fail_catalog() -> None:
        raise CostCatalogError("invalid cost fixture")

    service, _, _ = cost_api
    app.dependency_overrides[get_cost_catalog] = fail_catalog
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/orgs/{service.organization_id}/costs/recommendations",
            params={"serviceId": str(service.id)},
        )
    assert response.status_code == 500
    assert response.json()["success"] is False
    assert response.json()["code"] == "COST_CATALOG_INVALID"


async def test_real_guard_rejects_anonymous_before_service_query() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/orgs/{uuid4()}/costs/recommendations", params={"serviceId": str(uuid4())}
        )
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"
