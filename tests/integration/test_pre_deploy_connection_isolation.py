"""배포 없는 연결이 이전·재난 대피·지표에 새지 않는다(PR #96 독립 검증 P1·P2 회귀).

- P1: 승인된 이전(재난 자동 대피 포함)이 도는 중 제3 환경에 첫 변수 PUT 을 해도 대피가 끝까지
  간다. 연결을 새로 만드는 PUT 은 409 MIGRATION_IN_PROGRESS 이고, 다른 길(첫 배포 실패 등)로 배포
  없는 연결이 생겨도 지문 connections 가 그 연결을 보지 않아 STALE 이 되지 않는다.
- P2: 같은 공급자의 배포 없는 연결이 생겨도 서비스 중 환경의 지표 시리즈가 비지 않는다.
"""

import uuid

from sqlalchemy import func, select

from app.dependencies import get_prometheus_client
from app.main import app
from app.models import Environment, Service, ServiceEnvironment
from app.services.deploy_support import create_service_connection
from app.services.service_metrics import ServiceMetricsCache
from tests.integration.deploy_harness import DeployHarness
from tests.integration.disaster_support import (
    allow_self_approval,
    declare_disaster,
    tokyo_serving_seoul_standby,
)
from tests.integration.migration_support import Migration
from tests.integration.test_service_metrics_api import FakePrometheus
from tests.integration.test_service_variables_before_deploy import _put


async def _connection_count(harness: DeployHarness, service_id: str) -> int:
    async with harness.session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ServiceEnvironment)
            .where(
                ServiceEnvironment.service_id == uuid.UUID(service_id),
                ServiceEnvironment.is_deleted.is_(False),
            )
        )
        return int(count or 0)


async def _insert_undeployed_connection(
    harness: DeployHarness, service_id: str, environment_id: uuid.UUID
) -> None:
    """첫 배포 실패와 같은 모양(비율 0, 현재 배포 없음)의 연결을 API 를 거치지 않고 만든다."""
    async with harness.session_factory() as session, session.begin():
        service = await session.get_one(Service, uuid.UUID(service_id))
        environment = await session.get_one(Environment, environment_id)
        await create_service_connection(
            session, service, environment, harness.settings.public_base_domain
        )


async def _approved_evacuation(m: Migration) -> str:
    await allow_self_approval(m.harness)
    disaster = await declare_disaster(m)
    (item,) = disaster["plans"]
    assert item["targetEnvironmentId"] == str(m.gcp.id), item
    plan = await m.get_plan(item["planId"])
    assert plan["mode"] == "EMERGENCY" and plan["status"] == "APPROVED", plan
    return str(item["planId"])


async def test_put_on_third_environment_during_approved_evacuation_keeps_evacuation(
    harness: DeployHarness,
) -> None:
    # 검증자 재현: 도쿄 100% + 서울(gcp) 예비, 도쿄 재난 → EMERGENCY APPROVED → 제3 환경 첫 PUT.
    m = await tokyo_serving_seoul_standby(harness)
    try:
        third = await harness.add_environment("aws-seoul")
        plan_id = await _approved_evacuation(m)
        before = await _connection_count(harness, m.service_id)

        response = await _put(harness, m.service_id, third.id)
        assert response.status_code == 409, response.text
        body = response.json()
        assert body["code"] == "MIGRATION_IN_PROGRESS", body
        assert body["details"] == [
            {"field": "planId", "reason": "MIGRATION_IN_PROGRESS", "value": plan_id}
        ], body
        assert await _connection_count(harness, m.service_id) == before

        await harness.run_until_idle()
        result = await m.get_plan(plan_id)
        assert result["status"] == "SUCCEEDED", result
        assert await m.weights() == (0, 100)
        assert harness.executors.traffic.calls != []
        # 대피가 끝나면 같은 PUT 이 다시 된다(배포 없는 연결 생성).
        response = await _put(harness, m.service_id, third.id)
        assert response.status_code == 201, response.text
    finally:
        await m.author_client.aclose()
        await m.approver_client.aclose()


async def test_undeployed_connection_created_during_approved_evacuation_is_not_stale(
    harness: DeployHarness,
) -> None:
    # PUT 가드를 거치지 않는 길(첫 배포 실패 모양)로 생겨도 지문이 그 연결을 보지 않는다.
    m = await tokyo_serving_seoul_standby(harness)
    try:
        third = await harness.add_environment("aws-seoul")
        plan_id = await _approved_evacuation(m)
        await _insert_undeployed_connection(harness, m.service_id, third.id)

        await harness.run_until_idle()
        result = await m.get_plan(plan_id)
        assert result["status"] == "SUCCEEDED", result
        assert await m.weights() == (0, 100)
        assert "STALE" not in await m.log_codes(plan_id)
    finally:
        await m.author_client.aclose()
        await m.approver_client.aclose()


async def test_put_on_third_environment_while_pending_approval_does_not_stale_plan(
    migration: Migration,
) -> None:
    # 승인 대기는 막지 않는다. 그때 만든 배포 없는 연결은 승인·시작 지문 모두에 잡히지 않는다.
    harness = migration.harness
    third = await harness.add_environment("aws-tokyo")
    plan = await migration.create_plan()
    response = await _put(harness, migration.service_id, third.id)
    assert response.status_code == 201, response.text

    approved = await migration.approve(plan["id"])
    assert approved["status"] == "APPROVED", approved
    await harness.run_until_idle()
    result = await migration.get_plan(plan["id"])
    assert result["status"] == "SUCCEEDED", result
    assert await migration.weights() == (0, 100)


async def test_put_on_existing_connection_during_approved_plan_is_allowed(
    migration: Migration,
) -> None:
    # 가드는 연결을 새로 만들 때만이다. 서비스 중 환경의 변수 넣기는 이전 중에도 된다.
    harness = migration.harness
    plan = await migration.create_plan()
    await migration.approve(plan["id"])
    response = await _put(harness, migration.service_id, migration.aws.id)
    assert response.status_code == 201, response.text

    await harness.run_until_idle()
    result = await migration.get_plan(plan["id"])
    assert result["status"] == "SUCCEEDED", result


async def test_metrics_keep_serving_series_when_same_provider_has_variables_only(
    migration: Migration,
) -> None:
    # 검증자 재현: aws-seoul 서비스 중 + gcp-seoul 예비, aws-tokyo 에 변수만 → aws-seoul 2점 유지.
    harness = migration.harness
    app.dependency_overrides[get_prometheus_client] = lambda: FakePrometheus().client()
    url = f"{harness.services}/{migration.service_id}/metrics"

    async def points() -> dict[str, int]:
        app.state.service_metrics_cache = ServiceMetricsCache()
        response = await harness.client.get(url)
        assert response.status_code == 200, response.text
        return {
            item["environmentName"]: len(item["points"])
            for item in response.json()["data"]["series"]
        }

    assert await points() == {"aws-seoul": 2, "gcp-seoul": 2}
    tokyo = await harness.add_environment("aws-tokyo")
    response = await _put(harness, migration.service_id, tokyo.id)
    assert response.status_code == 201, response.text
    assert await points() == {"aws-seoul": 2, "gcp-seoul": 2}
