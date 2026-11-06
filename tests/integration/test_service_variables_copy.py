"""변수를 다른 환경으로 복사(이슈 #99 1번).

- 원본 연결의 최신 버전 값을 보관함에서 읽어 대상마다 새 버전으로 쓴다(넣기와 같은 저장 경로).
- overwrite = false(기본)면 이미 있는 키는 failed ENV_VAR_ALREADY_EXISTS,
  원본에 없는 키는 ENV_VAR_NOT_FOUND.
- 첫 배포 전 환경도 대상이 된다(배포 없는 연결). 새 연결이 필요한데 이전 실행 중이면 409.
- 값은 응답·감사 로그·DB·로그 어디에도 없다(키만).
"""

import logging
import uuid
from typing import Any

import pytest
from httpx import Response
from sqlalchemy import select

from app.audit.actions import AuditAction
from app.clients.app_secret_store import AppSecretUnavailableError, FakeAppSecretStore
from app.core.secrets import SecretValue
from app.enums import OrganizationRole
from app.models import AuditLog, ServiceEnvironment
from tests.integration.auth_support import create_agent_token, create_member, make_client
from tests.integration.deploy_harness import DeployHarness, connection_of
from tests.integration.migration_support import Migration
from tests.integration.rollback_support import deploy_and_finish
from tests.integration.test_service_variables import _dump_tables, _logs_text, _variants

KEY = "DATABASE_URL"


def _value() -> str:
    return f"sv-{uuid.uuid4()}"


def _store(harness: DeployHarness) -> FakeAppSecretStore:
    assert isinstance(harness.app_secret_store, FakeAppSecretStore)
    return harness.app_secret_store


def _var_path(harness: DeployHarness, service_id: str, environment_id: object, key: str) -> str:
    return f"{harness.services}/{service_id}/environments/{environment_id}/variables/{key}"


async def _put(
    harness: DeployHarness, service_id: str, environment_id: object, key: str, value: str
) -> None:
    response = await harness.client.put(
        _var_path(harness, service_id, environment_id, key), json={"value": value}
    )
    harness.responses.append(response.text)
    assert response.status_code in (200, 201), response.text


async def _copy(harness: DeployHarness, service_id: str, **body: Any) -> Response:
    response = await harness.client.post(
        f"{harness.services}/{service_id}/variables:copy", json=body
    )
    harness.responses.append(response.text)
    return response


def _body(source: object, targets: list[object], keys: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "sourceEnvironmentId": str(source),
        "targetEnvironmentIds": [str(item) for item in targets],
        "keys": keys,
        **extra,
    }


async def _list(harness: DeployHarness, service_id: str, environment_id: object) -> dict[str, Any]:
    response = await harness.client.get(
        f"{harness.services}/{service_id}/environments/{environment_id}/variables"
    )
    assert response.status_code == 200, response.text
    harness.responses.append(response.text)
    data: dict[str, Any] = response.json()["data"]
    return data


async def _value_of(
    harness: DeployHarness, service_id: str, environment_id: object, key: str
) -> str:
    """시험 안에서만 보관함 값을 읽는다(대조용)."""
    listed = await _list(harness, service_id, environment_id)
    item = next(item for item in listed["items"] if item["key"] == key)
    name = item["secretRef"].removeprefix("fake-sm:").split("/versions/")[0]
    return (await _store(harness).access(name, item["version"])).get_secret_value()


async def _copy_audit_rows(harness: DeployHarness) -> list[AuditLog]:
    async with harness.session_factory() as session:
        rows = await session.scalars(
            select(AuditLog)
            .where(AuditLog.action == AuditAction.SERVICE_VARIABLE_COPIED)
            .order_by(AuditLog.created_at, AuditLog.id)
        )
        return list(rows.all())


async def test_copy_to_deployed_and_pre_deploy_environments_writes_new_versions(
    harness: DeployHarness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    for noisy in ("httpx", "httpcore", "sqlalchemy.engine"):
        caplog.set_level(logging.INFO, logger=noisy)
    service = await harness.create_service("shap-api")
    aws = await harness.add_environment("aws-seoul")
    gcp = await harness.add_environment("gcp-seoul")
    tokyo = await harness.add_environment("aws-tokyo")
    secret = _value()
    api_key = _value()
    await _put(harness, service["id"], aws.id, KEY, secret)
    await _put(harness, service["id"], aws.id, "API_KEY", api_key)
    deployed = await deploy_and_finish(harness, service["id"], gcp.id)  # gcp 는 배포한 연결
    assert deployed["status"] == "SUCCEEDED"

    response = await _copy(
        harness, service["id"], **_body(aws.id, [gcp.id, tokyo.id], [KEY, "API_KEY"])
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["failed"] == []
    assert data["simulated"] is True
    assert sorted(
        (c["environmentId"], c["key"], c["version"], c["pendingRedeploy"]) for c in data["copied"]
    ) == sorted(
        [
            (str(gcp.id), KEY, 1, True),
            (str(gcp.id), "API_KEY", 1, True),
            (str(tokyo.id), KEY, 1, True),
            (str(tokyo.id), "API_KEY", 1, True),
        ]
    )
    for environment in (gcp, tokyo):
        assert await _value_of(harness, service["id"], environment.id, KEY) == secret
        assert await _value_of(harness, service["id"], environment.id, "API_KEY") == api_key
    # 원본은 그대로(새 버전 없음), 첫 배포 전 tokyo 는 배포 없는 연결이 생겼다.
    assert [i["version"] for i in (await _list(harness, service["id"], aws.id))["items"]] == [1, 1]
    detail = await harness.get_service(service["id"])
    tokyo_item = connection_of(detail, "aws-tokyo")
    assert (tokyo_item["role"], tokyo_item["currentDeployment"]) == ("NONE", None)
    assert (await _list(harness, service["id"], gcp.id))["deployed"] is True

    rows = await _copy_audit_rows(harness)
    assert len(rows) == 4
    assert {(row.details["environmentId"], row.details["key"]) for row in rows} == {
        (str(gcp.id), KEY),
        (str(gcp.id), "API_KEY"),
        (str(tokyo.id), KEY),
        (str(tokyo.id), "API_KEY"),
    }
    assert all(set(row.details) == {"environmentId", "key", "sourceEnvironmentId"} for row in rows)
    assert all(row.details["sourceEnvironmentId"] == str(aws.id) for row in rows)

    # 값은 응답·DB(감사 줄 포함)·로그 어디에도 없다.
    haystacks = {
        "db": await _dump_tables(harness),
        "logs": _logs_text(caplog),
        "responses": "\n".join(harness.responses),
    }
    assert all(KEY in haystack for haystack in haystacks.values())
    leaks = [
        (where, variant)
        for where, haystack in haystacks.items()
        for value in (secret, api_key)
        for variant in _variants(value)
        if variant in haystack
    ]
    assert leaks == []


async def test_existing_key_fails_without_overwrite_and_gets_new_version_with_overwrite(
    harness: DeployHarness,
) -> None:
    service = await harness.create_service("shap-api")
    aws = await harness.add_environment("aws-seoul")
    gcp = await harness.add_environment("gcp-seoul")
    source_value = _value()
    target_value = _value()
    await _put(harness, service["id"], aws.id, KEY, source_value)
    await _put(harness, service["id"], aws.id, "API_KEY", _value())
    await _put(harness, service["id"], gcp.id, KEY, target_value)
    names_before = list(_store(harness).names)

    kept = await _copy(harness, service["id"], **_body(aws.id, [gcp.id], [KEY, "API_KEY"]))

    assert kept.status_code == 200, kept.text
    data = kept.json()["data"]
    assert data["failed"] == [
        {"environmentId": str(gcp.id), "key": KEY, "code": "ENV_VAR_ALREADY_EXISTS"}
    ]
    assert [(c["key"], c["version"]) for c in data["copied"]] == [("API_KEY", 1)]
    # 덮지 않았다: 대상 값·버전 그대로.
    assert await _value_of(harness, service["id"], gcp.id, KEY) == target_value
    gcp_items = {
        i["key"]: i["version"] for i in (await _list(harness, service["id"], gcp.id))["items"]
    }
    assert gcp_items == {KEY: 1, "API_KEY": 1}
    assert len(_store(harness).names) == len(names_before) + 1  # API_KEY 비밀만 새로

    overwritten = await _copy(
        harness, service["id"], **_body(aws.id, [gcp.id], [KEY], overwrite=True)
    )

    assert overwritten.status_code == 200, overwritten.text
    data = overwritten.json()["data"]
    assert data["failed"] == []
    assert data["copied"] == [
        {"environmentId": str(gcp.id), "key": KEY, "version": 2, "pendingRedeploy": True}
    ]
    assert await _value_of(harness, service["id"], gcp.id, KEY) == source_value


async def test_missing_key_and_request_rules(harness: DeployHarness) -> None:
    service = await harness.create_service("shap-api")
    aws = await harness.add_environment("aws-seoul")
    gcp = await harness.add_environment("gcp-seoul")
    tokyo = await harness.add_environment("aws-tokyo")
    deleted = await harness.add_environment("aws-osaka", deleted=True)
    await _put(harness, service["id"], aws.id, KEY, _value())

    missing = await _copy(harness, service["id"], **_body(aws.id, [gcp.id, tokyo.id], ["NOPE"]))
    assert missing.status_code == 200, missing.text
    assert missing.json()["data"] == {
        "copied": [],
        "failed": [
            {"environmentId": str(gcp.id), "key": "NOPE", "code": "ENV_VAR_NOT_FOUND"},
            {"environmentId": str(tokyo.id), "key": "NOPE", "code": "ENV_VAR_NOT_FOUND"},
        ],
        "simulated": True,
    }
    # 원본에 변수가 없으면(연결 없음) 모든 키가 ENV_VAR_NOT_FOUND, 연결도 만들지 않는다.
    empty = await _copy(harness, service["id"], **_body(tokyo.id, [gcp.id], [KEY]))
    assert [f["code"] for f in empty.json()["data"]["failed"]] == ["ENV_VAR_NOT_FOUND"]
    async with harness.session_factory() as session:
        connections = (
            await session.scalars(
                select(ServiceEnvironment.environment_id).where(
                    ServiceEnvironment.service_id == uuid.UUID(service["id"])
                )
            )
        ).all()
    assert set(connections) == {aws.id}

    same = await _copy(harness, service["id"], **_body(aws.id, [aws.id, gcp.id], [KEY]))
    duplicate_target = await _copy(harness, service["id"], **_body(aws.id, [gcp.id, gcp.id], [KEY]))
    duplicate_key = await _copy(harness, service["id"], **_body(aws.id, [gcp.id], [KEY, KEY]))
    no_target = await _copy(harness, service["id"], **_body(aws.id, [], [KEY]))
    bad_key = await _copy(harness, service["id"], **_body(aws.id, [gcp.id], ["bad-key"]))
    unknown = await _copy(harness, service["id"], **_body(aws.id, [gcp.id], [KEY], value="x"))
    assert (same.status_code, same.json()["code"]) == (422, "VALIDATION_ERROR")
    assert same.json()["details"] == [
        {"field": "targetEnvironmentIds", "reason": "SOURCE_IN_TARGETS"}
    ]
    assert duplicate_target.json()["details"] == [
        {"field": "targetEnvironmentIds", "reason": "DUPLICATE"}
    ]
    assert duplicate_key.json()["details"] == [{"field": "keys", "reason": "DUPLICATE"}]
    assert (no_target.status_code, no_target.json()["code"]) == (422, "VALIDATION_ERROR")
    assert (bad_key.status_code, bad_key.json()["code"]) == (422, "ENV_VAR_KEY_INVALID")
    assert (unknown.status_code, unknown.json()["code"]) == (422, "VALIDATION_ERROR")

    for environment_id in (deleted.id, uuid.uuid4()):
        as_target = await _copy(harness, service["id"], **_body(aws.id, [environment_id], [KEY]))
        as_source = await _copy(harness, service["id"], **_body(environment_id, [gcp.id], [KEY]))
        for response in (as_target, as_source):
            assert (response.status_code, response.json()["code"]) == (
                404,
                "SERVICE_ENVIRONMENT_NOT_FOUND",
            ), response.text
    no_service = await _copy(harness, str(uuid.uuid4()), **_body(aws.id, [gcp.id], [KEY]))
    assert (no_service.status_code, no_service.json()["code"]) == (404, "SERVICE_NOT_FOUND")
    assert await _copy_audit_rows(harness) == []


async def test_copy_needing_new_connection_during_migration_is_409(migration: Migration) -> None:
    harness = migration.harness
    tokyo = await harness.add_environment("aws-tokyo")
    await _put(harness, migration.service_id, migration.aws.id, KEY, _value())
    plan = await migration.create_plan()
    await migration.approve(plan["id"])

    blocked = await _copy(
        harness,
        migration.service_id,
        **_body(migration.aws.id, [migration.gcp.id, tokyo.id], [KEY]),
    )
    # 이미 연결이 있는 대상(gcp 예비)만이면 넣기처럼 이전 중에도 된다.
    allowed = await _copy(
        harness, migration.service_id, **_body(migration.aws.id, [migration.gcp.id], [KEY])
    )

    assert (blocked.status_code, blocked.json()["code"]) == (409, "MIGRATION_IN_PROGRESS")
    assert blocked.json()["details"] == [
        {"field": "planId", "reason": "MIGRATION_IN_PROGRESS", "value": plan["id"]}
    ]
    assert allowed.status_code == 200, allowed.text
    assert [c["environmentId"] for c in allowed.json()["data"]["copied"]] == [str(migration.gcp.id)]
    await harness.run_until_idle()
    assert (await migration.get_plan(plan["id"]))["status"] == "SUCCEEDED"


async def test_copy_is_human_developer_only(harness: DeployHarness) -> None:
    service = await harness.create_service("shap-api")
    aws = await harness.add_environment("aws-seoul")
    gcp = await harness.add_environment("gcp-seoul")
    await _put(harness, service["id"], aws.id, KEY, _value())
    body = _body(aws.id, [gcp.id], [KEY])
    url = f"{harness.services}/{service['id']}/variables:copy"
    now = harness.clock.now()
    team = harness.organization
    viewer = await create_member(harness.session_factory, team, OrganizationRole.VIEWER, now)
    developer = await create_member(harness.session_factory, team, OrganizationRole.DEVELOPER, now)
    _, agent_token = await create_agent_token(
        harness.session_factory, team, harness.admin.user, now
    )

    async with make_client(session_token=viewer.session_token) as client:
        as_viewer = await client.post(url, json=body)
    async with make_client(agent_token=agent_token) as client:
        as_agent = await client.post(url, json=body)
    async with make_client(session_token=developer.session_token) as client:
        as_developer = await client.post(url, json=body)

    assert (as_viewer.status_code, as_viewer.json()["code"]) == (403, "ROLE_FORBIDDEN")
    assert (as_agent.status_code, as_agent.json()["code"]) == (403, "AGENT_READ_ONLY")
    assert as_developer.status_code == 200, as_developer.text
    assert len(as_developer.json()["data"]["copied"]) == 1


async def test_store_outage_while_writing_fails_remaining_items_without_calling_again(
    harness: DeployHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = await harness.create_service("shap-api")
    aws = await harness.add_environment("aws-seoul")
    gcp = await harness.add_environment("gcp-seoul")
    tokyo = await harness.add_environment("aws-tokyo")
    await _put(harness, service["id"], aws.id, KEY, _value())
    await _put(harness, service["id"], aws.id, "API_KEY", _value())
    calls: list[str] = []

    async def unavailable(name: str, value: SecretValue, labels: object) -> int:
        calls.append(name)
        raise AppSecretUnavailableError

    monkeypatch.setattr(_store(harness), "add_version", unavailable)

    response = await _copy(
        harness, service["id"], **_body(aws.id, [gcp.id, tokyo.id], [KEY, "API_KEY"])
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["copied"] == []
    assert [f["code"] for f in data["failed"]] == ["SECRET_STORE_UNAVAILABLE"] * 4
    assert len(calls) == 1
    # 보관함 호출이 실패하면 트랜잭션이 되돌아가 대상 연결도 생기지 않는다.
    async with harness.session_factory() as session:
        environments = set(
            (
                await session.scalars(
                    select(ServiceEnvironment.environment_id).where(
                        ServiceEnvironment.service_id == uuid.UUID(service["id"])
                    )
                )
            ).all()
        )
    assert environments == {aws.id}
    assert await _copy_audit_rows(harness) == []
