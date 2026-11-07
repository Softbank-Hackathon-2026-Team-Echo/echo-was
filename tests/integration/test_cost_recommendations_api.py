"""PostgreSQL 실제 guard·Repository·GET 시험. 로컬 DB 없으면 skip, 기존 PR CI에서 실행.

fixture 배포는 가짜 실행기이며 운영·클라우드 절감 입증이 아니다.
"""

import uuid
from typing import Any

import pytest
from httpx import AsyncHTTPTransport
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.enums import OrganizationRole
from tests.integration.auth_support import (
    create_agent_token,
    create_member,
    create_organization,
    make_client,
    org_path,
)
from tests.integration.deploy_harness import DeployHarness
from tests.integration.factories import make_service
from tests.integration.migration_support import Migration


async def test_recommendations_reads_real_serving_standby_without_sql_or_external_writes(
    migration: Migration,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = migration.harness
    viewer = await create_member(
        harness.session_factory, harness.organization, OrganizationRole.VIEWER, harness.clock.now()
    )
    mutations: list[str] = []
    statements: list[str] = []

    def check_sql(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        operation = statement.lstrip().split(None, 1)[0].upper()
        statements.append(operation)
        if operation != "SELECT":
            mutations.append(operation)
            raise AssertionError(f"GET attempted SQL {operation}")

    async def no_external(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("cost GET attempted external HTTP")

    monkeypatch.setattr(AsyncHTTPTransport, "handle_async_request", no_external)
    event.listen(Engine, "before_cursor_execute", check_sql)
    try:
        async with make_client(session_token=viewer.session_token) as client:
            for _ in range(3):
                response = await client.get(
                    org_path(harness.organization.id, "/costs/recommendations"),
                    params={"serviceId": migration.service_id},
                )
                assert response.status_code == 200, response.text
                data = response.json()["data"]
                by_id = {item["environmentId"]: item for item in data["currentPlacement"]}
                assert by_id[str(migration.aws.id)]["role"] == "SERVING"
                assert by_id[str(migration.gcp.id)]["role"] == "STANDBY"
                assert all(item["deploymentStatus"] == "SUCCEEDED" for item in by_id.values())
                assert all(item["currentDeploymentId"] for item in by_id.values())
                assert data["currentMonthlyUsd"] is None
                assert data["alternatives"][0]["fixedCostSavingsMonthlyUsd"] == 0
                assert data["alternatives"][0]["netSavingsMonthlyUsd"] is None
                assert data["alternatives"][0]["isSafeRecommendation"] is False
                assert all(
                    item["observedAt"] is None for item in data["alternatives"][0]["constraints"]
                )
    finally:
        event.remove(Engine, "before_cursor_execute", check_sql)
    assert statements
    assert mutations == []
    assert await migration.weights() == (100, 0)


async def test_recommendations_real_auth_and_cross_tenant_service_are_isolated(
    harness: DeployHarness,
) -> None:
    now = harness.clock.now()
    service = await harness.create_service("cost-app")
    viewer = await create_member(
        harness.session_factory, harness.organization, OrganizationRole.VIEWER, now
    )
    other = await create_organization(harness.session_factory, "other-cost-org")
    outsider = await create_member(harness.session_factory, other, OrganizationRole.ADMIN, now)
    async with harness.session_factory() as session, session.begin():
        other_service = make_service("foreign-cost-app", other)
        deleted_service = make_service("deleted-cost-app", harness.organization)
        deleted_service.mark_as_deleted(now)
        session.add_all([other_service, deleted_service])
    _, agent_token = await create_agent_token(
        harness.session_factory, harness.organization, harness.admin.user, now
    )
    path = org_path(harness.organization.id, "/costs/recommendations")
    params = {"serviceId": service["id"]}
    async with make_client() as anonymous:
        assert (await anonymous.get(path, params=params)).status_code == 401
    async with make_client(session_token=outsider.session_token) as client:
        denied = await client.get(path, params=params)
        assert denied.status_code == 404
        assert denied.json()["code"] == "ORGANIZATION_NOT_FOUND"
    async with make_client(session_token=viewer.session_token) as client:
        result = await client.get(path, params=params)
        assert result.status_code == 200, result.text
        assert result.json()["data"]["alternatives"] == []
        assert "NO_ALTERNATIVE_ENVIRONMENT" in result.json()["data"]["missingReasons"]
        for service_id in (other_service.id, deleted_service.id, uuid.uuid4()):
            result = await client.get(path, params={"serviceId": str(service_id)})
            assert result.status_code == 404
            assert result.json()["code"] == "SERVICE_NOT_FOUND"
        for query in ({}, {"serviceId": "not-a-uuid"}):
            assert (await client.get(path, params=query)).status_code == 422
    async with make_client(agent_token=agent_token) as client:
        # 기존 auth의 token last_used·감사 INSERT는 제품 mutation과 분리한다.
        assert (await client.get(path, params=params)).status_code == 200
        assert (
            await client.get(
                org_path(other.id, "/costs/recommendations"),
                params={"serviceId": str(other_service.id)},
            )
        ).status_code == 404


async def test_recommendations_service_switch_has_no_stored_result(harness: DeployHarness) -> None:
    first = await harness.create_service("first-cost-app")
    second = await harness.create_service("second-cost-app")
    path = org_path(harness.organization.id, "/costs/recommendations")
    for service in (first, second, first):
        response = await harness.client.get(path, params={"serviceId": service["id"]})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["serviceId"] == service["id"]
        assert response.json()["data"]["currentPlacement"] == []
