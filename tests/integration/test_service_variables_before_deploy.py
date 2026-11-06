"""첫 배포 전 앱 환경 변수(API 서버 10-4, 이슈 #93).

- 넣기는 연결이 없으면 배포 없는 연결(비율 0, 현재 배포 없음 = 역할 NONE)을 만들고, 첫 배포 요청은
  그 연결을 그대로 쓴다. 목록·지우기는 연결을 만들지 않는다.
- 배포 없는 연결은 서비스 중·예비가 아니다: 상세 역할 NONE, 보안 매트릭스·C-SEC-1 기준·예비
  따라가기·이전 도착(게이트 앞에서 409 TARGET_NOT_READY) 어디에도 잡히지 않는다.
"""

import uuid
from typing import Any

from httpx import Response
from sqlalchemy import select

from app.clients.app_secret_store import FakeAppSecretStore
from app.enums import DeployRequestTrigger
from app.models import Deployment, DeployRequest, ServiceEnvironment
from tests.integration.auth_support import create_organization
from tests.integration.deploy_harness import DeployHarness, connection_of
from tests.integration.migration_support import Migration
from tests.integration.rollback_support import deploy_and_finish, read_all_logs
from tests.integration.security_gate_support import put_variable

KEY = "DATABASE_URL"


def _path(
    harness: DeployHarness, service_id: str, environment_id: object, key: str | None = None
) -> str:
    base = f"{harness.services}/{service_id}/environments/{environment_id}/variables"
    return base if key is None else f"{base}/{key}"


async def _put(
    harness: DeployHarness, service_id: str, environment_id: object, key: str = KEY
) -> Response:
    response = await harness.client.put(
        _path(harness, service_id, environment_id, key), json={"value": f"sv-{uuid.uuid4()}"}
    )
    harness.responses.append(response.text)
    return response


async def _list(harness: DeployHarness, service_id: str, environment_id: object) -> dict[str, Any]:
    response = await harness.client.get(_path(harness, service_id, environment_id))
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def _connections(harness: DeployHarness, service_id: str) -> list[ServiceEnvironment]:
    async with harness.session_factory() as session:
        rows = await session.scalars(
            select(ServiceEnvironment)
            .where(ServiceEnvironment.service_id == uuid.UUID(service_id))
            .order_by(ServiceEnvironment.created_at, ServiceEnvironment.id)
        )
        return list(rows.all())


async def _request_row(harness: DeployHarness, request_id: str) -> DeployRequest:
    async with harness.session_factory() as session:
        return await session.get_one(DeployRequest, uuid.UUID(request_id))


def _store(harness: DeployHarness) -> FakeAppSecretStore:
    assert isinstance(harness.app_secret_store, FakeAppSecretStore)
    return harness.app_secret_store


async def test_put_before_first_deploy_then_first_deploy_uses_same_connection(
    harness: DeployHarness,
) -> None:
    service = await harness.create_service("shap-api")
    aws = await harness.add_environment("aws-tokyo")
    gcp = await harness.add_environment("gcp-seoul")

    empty = await _list(harness, service["id"], aws.id)
    assert empty == {"items": [], "simulated": True, "deployed": False}
    assert await _connections(harness, service["id"]) == []  # 목록은 연결을 만들지 않는다

    put_aws = await _put(harness, service["id"], aws.id)
    put_gcp = await _put(harness, service["id"], gcp.id)

    assert (put_aws.status_code, put_gcp.status_code) == (201, 201)
    saved = put_aws.json()["data"]
    assert (saved["version"], saved["appliedVersion"], saved["pendingRedeploy"]) == (1, None, True)
    assert saved["deployed"] is False
    before = await _connections(harness, service["id"])
    assert [(c.environment_id, c.traffic_weight, c.current_deployment_id) for c in before] == [
        (aws.id, 0, None),
        (gcp.id, 0, None),
    ]
    detail = await harness.get_service(service["id"])
    for name in ("aws-tokyo", "gcp-seoul"):
        item = connection_of(detail, name)
        assert (item["role"], item["trafficWeight"], item["currentDeployment"]) == ("NONE", 0, None)
    listed = await _list(harness, service["id"], aws.id)
    assert listed["deployed"] is False
    assert [(i["key"], i["appliedVersion"]) for i in listed["items"]] == [(KEY, None)]

    first = await deploy_and_finish(harness, service["id"], aws.id)

    assert first["status"] == "SUCCEEDED"
    request = await _request_row(harness, first["id"])
    assert request.service_environment_id == before[0].id  # 같은 연결을 쓴다
    after = await _connections(harness, service["id"])
    assert [c.id for c in after] == [c.id for c in before]  # 새 연결 없음
    assert request.deployment_id is not None
    async with harness.session_factory() as session:
        deployment = await session.get_one(Deployment, request.deployment_id)
    assert set(deployment.config_snapshot["envVars"]) == {KEY}
    messages = [line["message"] for line in await read_all_logs(harness, first["id"], "DEPLOY_JOB")]
    assert "App environment variables applied: 1 (keys: DATABASE_URL)." in messages
    applied = await _list(harness, service["id"], aws.id)
    assert applied["deployed"] is True
    assert [(i["appliedVersion"], i["pendingRedeploy"]) for i in applied["items"]] == [(1, False)]
    detail = await harness.get_service(service["id"])
    assert connection_of(detail, "aws-tokyo")["role"] == "SERVING"
    assert connection_of(detail, "gcp-seoul")["role"] == "NONE"

    second = await deploy_and_finish(harness, service["id"], gcp.id)

    assert second["status"] == "SUCCEEDED"
    assert (await _request_row(harness, second["id"])).service_environment_id == before[1].id
    assert connection_of(await harness.get_service(service["id"]), "gcp-seoul")["role"] == (
        "STANDBY"
    )
    assert (await _list(harness, service["id"], gcp.id))["items"][0]["appliedVersion"] == 1


async def test_list_and_delete_before_deploy_create_nothing_and_foreign_environment_is_404(
    harness: DeployHarness,
) -> None:
    service = await harness.create_service("shap-api")
    environment = await harness.add_environment("aws-seoul")
    other = await create_organization(harness.session_factory, "acme")
    foreign = await harness.add_environment("aws-seoul", organization=other)
    deleted = await harness.add_environment("gcp-seoul", deleted=True)

    for environment_id in (foreign.id, deleted.id, uuid.uuid4()):
        url = _path(harness, service["id"], environment_id)
        listed = await harness.client.get(url)
        put = await harness.client.put(f"{url}/{KEY}", json={"value": "x"})
        removed = await harness.client.delete(f"{url}/{KEY}")
        for response in (listed, put, removed):
            assert (response.status_code, response.json()["code"]) == (
                404,
                "SERVICE_ENVIRONMENT_NOT_FOUND",
            ), response.text
    no_key = await harness.client.delete(_path(harness, service["id"], environment.id, KEY))
    no_service = await harness.client.put(
        _path(harness, str(uuid.uuid4()), environment.id, KEY), json={"value": "x"}
    )
    listed = await _list(harness, service["id"], environment.id)

    assert (no_key.status_code, no_key.json()["code"]) == (404, "ENV_VAR_NOT_FOUND")
    assert (no_service.status_code, no_service.json()["code"]) == (404, "SERVICE_NOT_FOUND")
    assert listed == {"items": [], "simulated": True, "deployed": False}
    assert await _connections(harness, service["id"]) == []
    assert _store(harness).names == []


async def test_pre_deploy_connection_is_not_serving_standby_or_migration_target(
    migration: Migration,
) -> None:
    """aws-seoul 서비스 중·gcp-seoul 예비인 서비스에 aws-tokyo 변수만 먼저 넣은 상태."""
    harness = migration.harness
    tokyo = await harness.add_environment("aws-tokyo")
    # 서비스 중 연결에만 있는 키. 배포 없는 연결이 C-SEC-1 판정에 들어가면 tokyo 가 FAIL 로 잡힌다.
    await put_variable(migration, migration.aws, "API_KEY")
    await put_variable(migration, tokyo, KEY)

    detail = await harness.get_service(migration.service_id)
    item = connection_of(detail, "aws-tokyo")
    assert (item["role"], item["trafficWeight"], item["versionStatus"]) == ("NONE", 0, None)
    assert connection_of(detail, "aws-seoul")["role"] == "SERVING"

    response = await harness.client.get(f"/api/v1/orgs/{harness.organization.id}/security/matrix")
    assert response.status_code == 200, response.text
    row = next(r for r in response.json()["data"]["rows"] if r["environmentName"] == "aws-tokyo")
    assert row["services"] == []
    assert not [
        control
        for cell in row["cells"].values()
        for control in cell["controls"]
        if control.get("serviceId") == migration.service_id
    ]

    plan = await migration.request_plan(targetEnvironmentId=str(tokyo.id))
    assert (plan.status_code, plan.json()["code"]) == (409, "TARGET_NOT_READY")
    assert plan.json()["details"] == [{"field": "targetEnvironmentId", "reason": "NO_DEPLOYMENT"}]

    redeploy = await deploy_and_finish(harness, migration.service_id, migration.aws.id)
    assert redeploy["status"] == "SUCCEEDED"
    async with harness.session_factory() as session:
        sync_environments = set(
            (
                await session.scalars(
                    select(DeployRequest.environment_id).where(
                        DeployRequest.service_id == uuid.UUID(migration.service_id),
                        DeployRequest.trigger == DeployRequestTrigger.STANDBY_SYNC,
                    )
                )
            ).all()
        )
    assert sync_environments == {migration.gcp.id}  # 배포 없는 tokyo 는 따라가지 않는다
    tokyo_connection = next(
        c for c in await _connections(harness, migration.service_id) if c.environment_id == tokyo.id
    )
    assert (tokyo_connection.traffic_weight, tokyo_connection.current_deployment_id) == (0, None)
