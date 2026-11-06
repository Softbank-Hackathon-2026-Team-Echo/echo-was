"""고객 앱 환경 변수 API·배포 고정·값 0건(SPEC api-10 AC-1~AC-4, AC-6, AC-7, AC-10, AC-11)."""

import asyncio
import base64
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select, text

from app.clients.app_secret_store import (
    AppSecretRejectedError,
    AppSecretUnavailableError,
    FakeAppSecretStore,
    GcpAppSecretStore,
)
from app.core.config import (
    AppSecretStoreMode,
    ExecutorMode,
    HealthCheckerMode,
    Settings,
    SourceClientMode,
)
from app.core.logging import JsonFormatter
from app.core.secrets import SecretValue
from app.enums import (
    DeployRequestStatus,
    DeployRequestTrigger,
    OrganizationRole,
)
from app.executors import ExecutorSet, RealExecutorTransports, build_executor_set
from app.executors.base import (
    DeploymentSpec,
    DeployTarget,
    ExecutorUnavailableError,
    RevertResult,
)
from app.executors.fake import FakeDeployExecutor
from app.executors.google_metadata import GoogleMetadataClient
from app.models import Deployment, DeployRequest, Environment, ServiceEnvironmentVariable
from app.services import service_variable_service
from tests.integration.auth_support import (
    create_agent_token,
    create_member,
    create_organization,
    make_client,
    org_path,
)
from tests.integration.conftest import TABLES
from tests.integration.deploy_harness import DeployHarness
from tests.integration.factories import secret_ref_for
from tests.integration.rollback_support import deploy_and_finish, read_all_logs, rollback
from tests.real_executor_fakes import (
    METADATA_BASE,
    DictSecretClient,
    FakeCloud,
    FakeCluster,
    FakeSecretManager,
    RecordingSleep,
)

KEY = "DATABASE_URL"
LEASE_SECONDS = 31


def _value() -> str:
    return f"sv-{uuid.uuid4()}"


def _store(harness: DeployHarness) -> FakeAppSecretStore:
    assert isinstance(harness.app_secret_store, FakeAppSecretStore)
    return harness.app_secret_store


def _path(
    harness: DeployHarness, service_id: str, environment_id: object, key: str | None = None
) -> str:
    base = f"{harness.services}/{service_id}/environments/{environment_id}/variables"
    return base if key is None else f"{base}/{key}"


async def _put(
    harness: DeployHarness,
    service_id: str,
    environment_id: object,
    key: str = KEY,
    value: str | None = None,
    client: AsyncClient | None = None,
) -> Response:
    response = await (client or harness.client).put(
        _path(harness, service_id, environment_id, key),
        json={"value": value if value is not None else _value()},
    )
    harness.responses.append(response.text)
    return response


async def _list(harness: DeployHarness, service_id: str, environment_id: object) -> dict[str, Any]:
    response = await harness.client.get(_path(harness, service_id, environment_id))
    assert response.status_code == 200, response.text
    harness.responses.append(response.text)
    data: dict[str, Any] = response.json()["data"]
    return data


async def _deployment(harness: DeployHarness, deployment_id: str) -> Deployment:
    async with harness.session_factory() as session:
        return await session.get_one(Deployment, uuid.UUID(deployment_id))


async def _rows(harness: DeployHarness) -> list[ServiceEnvironmentVariable]:
    async with harness.session_factory() as session:
        return list(
            (
                await session.scalars(
                    select(ServiceEnvironmentVariable).order_by(
                        ServiceEnvironmentVariable.created_at
                    )
                )
            ).all()
        )


def _name_of(secret_ref: str) -> str:
    return secret_ref.removeprefix("fake-sm:").split("/versions/")[0]


@dataclass
class Connected:
    service_id: str
    environment: Environment
    first_request: dict[str, Any]


async def _connected(harness: DeployHarness, name: str = "shop-api") -> Connected:
    """첫 배포를 마친 연결(배포 전 연결은 test_service_variables_before_deploy.py)."""
    service = await harness.create_service(name)
    environment = await harness.add_environment("aws-seoul")
    finished = await deploy_and_finish(harness, service["id"], environment.id)
    return Connected(service["id"], environment, finished)


class _CapturingDeployExecutor(FakeDeployExecutor):
    """시험 안에서만 값을 보려는 가짜 실행기(운영 가짜는 키 이름만 남긴다)."""

    def __init__(self, harness: DeployHarness) -> None:
        super().__init__(harness.clock, 0)
        self.applied_values: list[dict[str, str] | None] = []
        self.reverted_values: list[dict[str, str] | None] = []

    @staticmethod
    def _plain(deployment: DeploymentSpec | None) -> dict[str, str] | None:
        if deployment is None or deployment.env_values is None:
            return None
        return {key: value.get_secret_value() for key, value in deployment.env_values.items()}

    async def apply(
        self, deployment: DeploymentSpec, target: DeployTarget, idempotency_key: str
    ) -> str:
        self.applied_values.append(self._plain(deployment))
        return await super().apply(deployment, target, idempotency_key)

    async def revert(
        self, target: DeployTarget, previous: DeploymentSpec | None, idempotency_key: str
    ) -> RevertResult:
        self.reverted_values.append(self._plain(previous))
        return await super().revert(target, previous, idempotency_key)


def _capture(harness: DeployHarness) -> _CapturingDeployExecutor:
    deployer = _CapturingDeployExecutor(harness)
    harness.replace_executors(deploy=deployer)
    return deployer


class _VersionFailingStore(FakeAppSecretStore):
    def __init__(self) -> None:
        super().__init__()
        self.rejected_versions: set[tuple[str, int]] = set()
        self.unavailable_versions: set[tuple[str, int]] = set()

    async def access(self, name: str, version: int) -> SecretValue:
        if (name, version) in self.rejected_versions:
            raise AppSecretRejectedError
        if (name, version) in self.unavailable_versions:
            raise AppSecretUnavailableError
        return await super().access(name, version)


# --- AC-1 ---


async def test_put_new_key_201_same_key_200_and_list_never_shows_value(
    harness: DeployHarness,
) -> None:
    setup = await _connected(harness)
    secret = _value()

    created = await _put(harness, setup.service_id, setup.environment.id, value=secret)
    updated = await _put(harness, setup.service_id, setup.environment.id, value=_value())
    await _put(harness, setup.service_id, setup.environment.id, key="API_KEY")
    listed = await _list(harness, setup.service_id, setup.environment.id)

    assert (created.status_code, updated.status_code) == (201, 200)
    created_data, updated_data = created.json()["data"], updated.json()["data"]
    assert created_data["version"] == 1
    assert updated_data["version"] == 2
    assert created_data["simulated"] is True
    assert updated_data["secretRef"].startswith("fake-sm:echo-app-team-echo-")
    assert updated_data["secretRef"].endswith("-database-url/versions/2")
    assert updated_data["updatedBy"] == {
        "id": str(harness.admin.user.id),
        "githubLogin": harness.admin.user.github_login,
    }
    assert listed["simulated"] is True
    assert [item["key"] for item in listed["items"]] == ["API_KEY", KEY]
    item = listed["items"][1]
    assert set(item) == {
        "key",
        "version",
        "secretRef",
        "updatedAt",
        "updatedBy",
        "appliedVersion",
        "pendingRedeploy",
    }
    assert (item["version"], item["appliedVersion"], item["pendingRedeploy"]) == (2, None, True)
    assert secret not in created.text + updated.text + str(listed)


async def test_pending_redeploy_follows_current_deployment(harness: DeployHarness) -> None:
    setup = await _connected(harness)
    await _put(harness, setup.service_id, setup.environment.id)

    await deploy_and_finish(harness, setup.service_id, setup.environment.id)
    deployed = (await _list(harness, setup.service_id, setup.environment.id))["items"][0]
    changed = (await _put(harness, setup.service_id, setup.environment.id)).json()["data"]

    assert (deployed["version"], deployed["appliedVersion"]) == (1, 1)
    assert deployed["pendingRedeploy"] is False
    assert (changed["version"], changed["appliedVersion"]) == (2, 1)
    assert changed["pendingRedeploy"] is True


async def test_missing_service_and_key_are_404(harness: DeployHarness) -> None:
    """연결이 없는 환경(첫 배포 전)은 test_service_variables_before_deploy.py 가 본다."""
    setup = await _connected(harness)

    no_service = await harness.client.get(_path(harness, str(uuid.uuid4()), setup.environment.id))
    no_key = await harness.client.delete(
        _path(harness, setup.service_id, setup.environment.id, "NOPE")
    )

    assert no_service.json()["code"] == "SERVICE_NOT_FOUND"
    assert (no_key.status_code, no_key.json()["code"]) == (404, "ENV_VAR_NOT_FOUND")
    assert _store(harness).names == []


async def test_delete_marks_row_keeps_store_and_hides_from_list(harness: DeployHarness) -> None:
    setup = await _connected(harness)
    await _put(harness, setup.service_id, setup.environment.id)

    response = await harness.client.delete(
        _path(harness, setup.service_id, setup.environment.id, KEY)
    )
    listed = await _list(harness, setup.service_id, setup.environment.id)
    again = await harness.client.delete(_path(harness, setup.service_id, setup.environment.id, KEY))
    [row] = await _rows(harness)

    assert response.status_code == 204
    assert response.content == b""
    assert listed["items"] == []
    assert again.status_code == 404
    assert row.is_deleted and row.deleted_at is not None
    assert _store(harness).names == [row.secret_name]


async def test_concurrent_puts_of_same_key_keep_versions_in_order(harness: DeployHarness) -> None:
    setup = await _connected(harness)

    responses = await asyncio.gather(
        *(_put(harness, setup.service_id, setup.environment.id) for _ in range(4))
    )

    assert sorted(response.status_code for response in responses) == [200, 200, 200, 201]
    assert sorted(response.json()["data"]["version"] for response in responses) == [1, 2, 3, 4]
    [row] = await _rows(harness)
    assert row.latest_version == 4


async def test_put_during_service_deletion_is_conflict_and_teardown_marks_rows(
    harness: DeployHarness,
) -> None:
    setup = await _connected(harness)
    await _put(harness, setup.service_id, setup.environment.id)

    await harness.client.delete(f"{harness.services}/{setup.service_id}")
    during_put = await _put(harness, setup.service_id, setup.environment.id, key="OTHER")
    during_delete = await harness.client.delete(
        _path(harness, setup.service_id, setup.environment.id, KEY)
    )
    await harness.run_until_idle()
    [row] = await _rows(harness)

    assert (during_put.status_code, during_put.json()["code"]) == (409, "SERVICE_DELETING")
    assert during_delete.json()["code"] == "SERVICE_DELETING"
    assert row.is_deleted
    assert _store(harness).names == [row.secret_name]


# --- AC-2 ---


async def test_permissions_by_role(harness: DeployHarness) -> None:
    setup = await _connected(harness)
    now = harness.clock.now()
    team = harness.organization
    viewer = await create_member(harness.session_factory, team, OrganizationRole.VIEWER, now)
    developer = await create_member(harness.session_factory, team, OrganizationRole.DEVELOPER, now)
    _, agent_token = await create_agent_token(
        harness.session_factory, team, harness.admin.user, now
    )
    other = await create_organization(harness.session_factory, "acme")
    outsider = await create_member(harness.session_factory, other, OrganizationRole.ADMIN, now)
    key_path = _path(harness, setup.service_id, setup.environment.id, KEY)
    list_path = _path(harness, setup.service_id, setup.environment.id)
    body = {"value": "x"}

    async with make_client(session_token=viewer.session_token) as client:
        viewer_get = await client.get(list_path)
        viewer_put = await client.put(key_path, json=body)
    async with make_client(session_token=developer.session_token) as client:
        developer_put = await client.put(key_path, json=body)
        developer_delete = await client.delete(key_path)
    async with make_client(agent_token=agent_token) as client:
        agent_get = await client.get(list_path)
        agent_put = await client.put(key_path, json=body)
        agent_delete = await client.delete(key_path)
    async with make_client(session_token=outsider.session_token) as client:
        outsider_get = await client.get(list_path)
        own_org_path = list_path.replace(str(team.id), str(other.id))
        cross_org = await client.get(own_org_path)
    admin_delete = await harness.client.delete(key_path)

    assert viewer_get.status_code == 200
    assert (viewer_put.status_code, viewer_put.json()["code"]) == (403, "ROLE_FORBIDDEN")
    assert developer_put.status_code == 201
    assert (developer_delete.status_code, developer_delete.json()["code"]) == (
        403,
        "ROLE_FORBIDDEN",
    )
    assert agent_get.status_code == 200
    assert agent_get.json()["data"]["items"][0]["key"] == KEY
    assert (agent_put.status_code, agent_put.json()["code"]) == (403, "AGENT_READ_ONLY")
    assert (agent_delete.status_code, agent_delete.json()["code"]) == (403, "AGENT_READ_ONLY")
    assert (outsider_get.status_code, outsider_get.json()["code"]) == (
        404,
        "ORGANIZATION_NOT_FOUND",
    )
    assert (cross_org.status_code, cross_org.json()["code"]) == (404, "SERVICE_NOT_FOUND")
    assert admin_delete.status_code == 204


async def test_cookie_put_needs_allowed_origin(harness: DeployHarness) -> None:
    setup = await _connected(harness)

    async with make_client(
        session_token=harness.admin.session_token, origin="https://evil.test"
    ) as client:
        response = await _put(harness, setup.service_id, setup.environment.id, client=client)

    assert (response.status_code, response.json()["code"]) == (403, "ORIGIN_NOT_ALLOWED")


# --- AC-3 ---


@pytest.mark.parametrize(
    ("key", "code"),
    [
        ("port", "ENV_VAR_KEY_INVALID"),
        ("1A", "ENV_VAR_KEY_INVALID"),
        ("A" * 65, "ENV_VAR_KEY_INVALID"),
        ("PORT", "ENV_VAR_KEY_RESERVED"),
        ("ECHO_X", "ENV_VAR_KEY_RESERVED"),
    ],
)
async def test_key_rules(harness: DeployHarness, key: str, code: str) -> None:
    setup = await _connected(harness)

    response = await _put(harness, setup.service_id, setup.environment.id, key=key)

    assert (response.status_code, response.json()["code"]) == (422, code)
    assert await _rows(harness) == []


@pytest.mark.parametrize("value", ["", "x" * 16385, "가" * 5462], ids=["empty", "ascii", "korean"])
async def test_value_rules(harness: DeployHarness, value: str) -> None:
    setup = await _connected(harness)

    response = await _put(harness, setup.service_id, setup.environment.id, value=value)

    assert (response.status_code, response.json()["code"]) == (422, "ENV_VAR_VALUE_INVALID")
    assert _store(harness).names == []


async def test_max_size_multiline_value_is_accepted(harness: DeployHarness) -> None:
    setup = await _connected(harness)

    largest = await _put(harness, setup.service_id, setup.environment.id, value="x" * 16384)
    multiline = await _put(harness, setup.service_id, setup.environment.id, "CERT", "a\nb\n")

    assert (largest.status_code, multiline.status_code) == (201, 201)


async def test_forty_first_key_is_rejected_but_existing_key_can_change(
    harness: DeployHarness,
) -> None:
    setup = await _connected(harness)
    for index in range(40):
        response = await _put(harness, setup.service_id, setup.environment.id, f"K{index}", "x")
        assert response.status_code == 201

    extra = await _put(harness, setup.service_id, setup.environment.id, "K40", "x")
    changed = await _put(harness, setup.service_id, setup.environment.id, "K0", "y")

    assert (extra.status_code, extra.json()["code"]) == (422, "ENV_VAR_LIMIT_EXCEEDED")
    assert changed.status_code == 200


# --- AC-4 ---


async def test_new_deployment_pins_latest_versions_and_empty_when_no_variables(
    harness: DeployHarness,
) -> None:
    setup = await _connected(harness)
    empty = await _deployment(harness, setup.first_request["deploymentId"])
    await _put(harness, setup.service_id, setup.environment.id)
    latest = await _put(harness, setup.service_id, setup.environment.id)

    finished = await deploy_and_finish(harness, setup.service_id, setup.environment.id)
    pinned = await _deployment(harness, finished["deploymentId"])

    assert empty.config_snapshot["envVars"] == {}
    assert pinned.config_snapshot["envVars"] == {KEY: latest.json()["data"]["secretRef"]}
    assert harness.executors.deploy.applied_env_keys == [(), (KEY,)]


async def test_standby_sync_uses_standby_connection_values(harness: DeployHarness) -> None:
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    gcp = await harness.add_environment("gcp-seoul")
    await deploy_and_finish(harness, service["id"], aws.id)
    await deploy_and_finish(harness, service["id"], gcp.id)
    await _put(harness, service["id"], aws.id)
    standby = await _put(harness, service["id"], gcp.id, key="ONLY_GCP")

    finished = await deploy_and_finish(harness, service["id"], aws.id)
    async with harness.session_factory() as session:
        sync = await session.scalar(
            select(DeployRequest)
            .where(DeployRequest.trigger == DeployRequestTrigger.STANDBY_SYNC)
            .order_by(DeployRequest.created_at.desc())
        )
    assert sync is not None and sync.deployment_id is not None
    pinned = await _deployment(harness, str(sync.deployment_id))
    serving = await _deployment(harness, finished["deploymentId"])

    assert sync.status == DeployRequestStatus.SUCCEEDED
    assert "envVars" not in sync.config_snapshot
    assert pinned.config_snapshot["envVars"] == {"ONLY_GCP": standby.json()["data"]["secretRef"]}
    assert list(serving.config_snapshot["envVars"]) == [KEY]


async def test_rollback_uses_old_record_versions_and_standby_keeps_its_own(
    harness: DeployHarness,
) -> None:
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    gcp = await harness.add_environment("gcp-seoul")
    await deploy_and_finish(harness, service["id"], aws.id)
    await deploy_and_finish(harness, service["id"], gcp.id)
    first = await _put(harness, service["id"], aws.id, value=_value())
    standby = await _put(harness, service["id"], gcp.id, key="ONLY_GCP")
    old_request = await deploy_and_finish(harness, service["id"], aws.id)
    await harness.run_until_idle()
    await _put(harness, service["id"], aws.id, value=_value())
    await deploy_and_finish(harness, service["id"], aws.id)
    await harness.run_until_idle()

    accepted = await rollback(harness, old_request["id"])
    await harness.run_until_idle()
    rolled_back = await harness.get_request(accepted["id"])
    pinned = await _deployment(harness, rolled_back["deploymentId"])
    async with harness.session_factory() as session:
        sync = await session.scalar(
            select(DeployRequest).where(
                DeployRequest.source_request_id == uuid.UUID(accepted["id"])
            )
        )
    assert sync is not None and sync.deployment_id is not None
    sync_pinned = await _deployment(harness, str(sync.deployment_id))
    listed = (await _list(harness, service["id"], aws.id))["items"][0]

    assert rolled_back["status"] == "SUCCEEDED"
    assert pinned.config_snapshot["envVars"] == {KEY: first.json()["data"]["secretRef"]}
    assert (listed["version"], listed["appliedVersion"]) == (2, 1)
    assert listed["pendingRedeploy"] is True
    assert sync_pinned.config_snapshot["envVars"] == {
        "ONLY_GCP": standby.json()["data"]["secretRef"]
    }


async def test_rerun_after_lost_lease_keeps_pinned_version(harness: DeployHarness) -> None:
    setup = await _connected(harness)
    first = await _put(harness, setup.service_id, setup.environment.id)
    store = _store(harness)
    name = _name_of(first.json()["data"]["secretRef"])
    store.unavailable_names.add(name)
    accepted = await harness.deploy(setup.service_id, setup.environment.id)
    worker = harness.worker()

    with pytest.raises(ExecutorUnavailableError):
        for _ in range(20):
            await worker.run_once()
    record = (await harness.get_request(accepted["id"]))["deploymentId"]
    store.unavailable_names.clear()
    # 일꾼이 멈춘 사이 값이 바뀌어도 이 배포 기록은 고정한 버전을 쓴다.
    changed = await _put(harness, setup.service_id, setup.environment.id)
    harness.clock.advance(LEASE_SECONDS)
    await harness.run_until_idle()
    finished = await harness.get_request(accepted["id"])
    pinned = await _deployment(harness, finished["deploymentId"])

    assert finished["status"] == "SUCCEEDED"
    assert finished["deploymentId"] == record
    assert pinned.config_snapshot["envVars"] == {KEY: first.json()["data"]["secretRef"]}
    assert changed.json()["data"]["version"] == 2
    assert (name, 2) not in store.access_calls
    assert store.access_calls.count((name, 1)) == 2


# --- AC-6 값 0건 ---


async def _dump_tables(harness: DeployHarness) -> str:
    async with harness.session_factory() as session:
        dumps = [
            await session.scalar(
                text(f"SELECT coalesce(string_agg(t::text, '\n'), '') FROM {table} t")
            )
            for table in TABLES
        ]
    return "\n".join(dump or "" for dump in dumps)


def _logs_text(caplog: pytest.LogCaptureFixture) -> str:
    formatter = JsonFormatter()
    return "\n".join([*(formatter.format(record) for record in caplog.records), caplog.text])


def _variants(value: str) -> list[str]:
    encoded = base64.b64encode(value.encode()).decode()
    return [value, value[3:], encoded]


async def _rejected_with_value(harness: DeployHarness, setup: Connected, value: str) -> None:
    """값을 실은 거절 요청들. 응답·로그에 그 값이 없어야 한다."""
    good_path = _path(harness, setup.service_id, setup.environment.id, KEY)
    responses = [
        await _put(harness, setup.service_id, setup.environment.id, key="port", value=value),
        await _put(harness, setup.service_id, setup.environment.id, key="PORT", value=value),
        await _put(harness, setup.service_id, setup.environment.id, value=(value * 1000)[:16385]),
        await harness.client.put(good_path, json={"value": [value]}),
        await harness.client.put(good_path, json={"value": {"nested": value}}),
        await harness.client.put(good_path, json={"value": value, "extra": value}),
        await harness.client.put(good_path, json={value: value}),
        await harness.client.put(
            good_path,
            content=f'{{"value": "{value}"'.encode(),
            headers={"content-type": "application/json"},
        ),
    ]
    for response in responses:
        harness.responses.append(response.text)
        assert response.status_code == 422, response.text


async def test_secret_value_appears_nowhere_after_full_deploy(
    harness: DeployHarness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    for noisy in ("httpx", "httpcore", "sqlalchemy.engine"):
        caplog.set_level(logging.INFO, logger=noisy)
    setup = await _connected(harness)
    secret = _value()
    deployer = _capture(harness)

    saved = await _put(harness, setup.service_id, setup.environment.id, value=secret)
    await _rejected_with_value(harness, setup, secret)
    finished = await deploy_and_finish(harness, setup.service_id, setup.environment.id)
    await _list(harness, setup.service_id, setup.environment.id)
    await harness.get_service(setup.service_id)
    await harness.list_requests(setup.service_id)
    for source in ("DEPLOY_JOB", "BUILD", "RUNTIME"):
        await read_all_logs(harness, finished["id"], source)
    database = await _dump_tables(harness)
    haystacks = {
        "db": database,
        "logs": _logs_text(caplog),
        "responses": "\n".join(harness.responses),
    }

    # 대조군: 값은 실제로 실행기까지 갔고, 검색 대상도 제대로 모였다.
    assert saved.status_code == 201
    assert deployer.applied_values == [{KEY: secret}]
    assert finished["status"] == "SUCCEEDED"
    assert all(finished["id"] in haystack for haystack in haystacks.values())
    assert all(KEY in haystack for haystack in haystacks.values())
    leaks = [
        (where, variant)
        for where, haystack in haystacks.items()
        for variant in _variants(secret)
        if variant in haystack
    ]
    assert leaks == []


async def test_deploy_job_log_lists_key_names_only(harness: DeployHarness) -> None:
    setup = await _connected(harness)
    await _put(harness, setup.service_id, setup.environment.id, value=_value())
    await _put(harness, setup.service_id, setup.environment.id, key="API_KEY", value=_value())

    finished = await deploy_and_finish(harness, setup.service_id, setup.environment.id)
    lines = await read_all_logs(harness, finished["id"], "DEPLOY_JOB")
    plain_lines = await read_all_logs(harness, setup.first_request["id"], "DEPLOY_JOB")

    messages = [line["message"] for line in lines]
    assert "App environment variables applied: 2 (keys: API_KEY, DATABASE_URL)." in messages
    assert not [line for line in plain_lines if "environment variables" in line["message"]]


async def test_store_errors_do_not_expose_secret_name_or_value(
    harness: DeployHarness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    setup = await _connected(harness)
    secret = _value()
    first = await _put(harness, setup.service_id, setup.environment.id)
    name = _name_of(first.json()["data"]["secretRef"])
    store = _store(harness)

    store.forbidden_names.add(name)
    forbidden = await _put(harness, setup.service_id, setup.environment.id, value=secret)
    store.forbidden_names.clear()
    store.unavailable_names.add(name)
    unavailable = await _put(harness, setup.service_id, setup.environment.id, value=secret)
    store.unavailable_names.clear()
    [row] = await _rows(harness)

    assert (forbidden.status_code, forbidden.json()["code"]) == (502, "SECRET_STORE_FORBIDDEN")
    assert (unavailable.status_code, unavailable.json()["code"]) == (
        502,
        "SECRET_STORE_UNAVAILABLE",
    )
    assert row.latest_version == 1
    combined = forbidden.text + unavailable.text
    assert name not in combined and secret not in combined
    assert secret not in caplog.text
    assert name not in "\n".join(
        record.getMessage() for record in caplog.records if record.name.startswith("app.")
    )


async def test_store_call_over_time_limit_is_unavailable_and_releases_locks(
    harness: DeployHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _HangingStore(FakeAppSecretStore):
        async def add_version(self, name: str, value: SecretValue, labels: Any) -> int:
            await asyncio.sleep(60)
            raise AssertionError

    setup = await _connected(harness)
    monkeypatch.setattr(service_variable_service, "STORE_CALL_TIMEOUT_SECONDS", 0.05)
    harness.app_secret_store = _HangingStore()

    response = await _put(harness, setup.service_id, setup.environment.id)
    accepted = await harness.deploy(setup.service_id, setup.environment.id)

    assert (response.status_code, response.json()["code"]) == (502, "SECRET_STORE_UNAVAILABLE")
    assert accepted["status"] == "QUEUED"
    assert await _rows(harness) == []


# --- AC-7 ---


async def test_apply_with_permanently_unreadable_version_fails_without_apply(
    harness: DeployHarness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    setup = await _connected(harness)
    secret = _value()
    saved = await _put(harness, setup.service_id, setup.environment.id, value=secret)
    _store(harness).fail_names.add(_name_of(saved.json()["data"]["secretRef"]))
    applies_before = len(
        [call for call in harness.executors.deploy.calls if call.method == "apply"]
    )

    finished = await deploy_and_finish(harness, setup.service_id, setup.environment.id)
    lines = await read_all_logs(harness, finished["id"], "DEPLOY_JOB")

    applies = [call for call in harness.executors.deploy.calls if call.method == "apply"]
    assert (finished["status"], finished["errorCode"]) == ("FAILED", "ENV_SECRET_UNAVAILABLE")
    assert len(applies) == applies_before
    assert finished["errorMessage"] == (
        "An app environment variable could not be read from the secret store."
    )
    assert any(line["level"] == "ERROR" for line in lines)
    warnings = [record for record in caplog.records if getattr(record, "env_keys", None)]
    assert [record.env_keys for record in warnings] == [[KEY]]  # type: ignore[attr-defined]
    assert warnings[0].error_code == "ENV_SECRET_UNAVAILABLE"  # type: ignore[attr-defined]
    assert secret not in caplog.text


async def test_apply_with_temporary_store_failure_raises_and_recovers(
    harness: DeployHarness,
) -> None:
    setup = await _connected(harness)
    saved = await _put(harness, setup.service_id, setup.environment.id)
    store = _store(harness)
    store.unavailable_names.add(_name_of(saved.json()["data"]["secretRef"]))
    accepted = await harness.deploy(setup.service_id, setup.environment.id)
    worker = harness.worker()

    with pytest.raises(ExecutorUnavailableError):
        for _ in range(20):
            await worker.run_once()
    during = await harness.get_request(accepted["id"])
    store.unavailable_names.clear()
    harness.clock.advance(LEASE_SECONDS)
    await harness.run_until_idle()
    finished = await harness.get_request(accepted["id"])

    assert during["status"] == "DEPLOYING"
    assert finished["status"] == "SUCCEEDED"


async def test_temporary_failures_three_times_end_with_worker_lost(harness: DeployHarness) -> None:
    setup = await _connected(harness)
    saved = await _put(harness, setup.service_id, setup.environment.id)
    _store(harness).unavailable_names.add(_name_of(saved.json()["data"]["secretRef"]))
    accepted = await harness.deploy(setup.service_id, setup.environment.id)
    worker = harness.worker()

    for _ in range(40):
        try:
            if await harness.request_status(accepted["id"]) == "FAILED":
                break
            await worker.run_once()
        except ExecutorUnavailableError:
            harness.clock.advance(LEASE_SECONDS)
    finished = await harness.get_request(accepted["id"])

    assert (finished["status"], finished["errorCode"]) == ("FAILED", "WORKER_LOST")


@pytest.mark.parametrize("failure", ["rejected", "unavailable"])
async def test_revert_with_unreadable_previous_keeps_secret_and_warns(
    harness: DeployHarness, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    caplog.set_level(logging.WARNING)
    store = _VersionFailingStore()
    harness.app_secret_store = store
    setup = await _connected(harness)
    secret = _value()
    old = await _put(harness, setup.service_id, setup.environment.id, value=secret)
    name = _name_of(old.json()["data"]["secretRef"])
    deployer = _capture(harness)
    await deploy_and_finish(harness, setup.service_id, setup.environment.id)
    await _put(harness, setup.service_id, setup.environment.id, value=_value())
    (store.rejected_versions if failure == "rejected" else store.unavailable_versions).add(
        (name, 1)
    )
    deployer.fail_service_environment(await _connection_id(harness, setup))

    finished = await deploy_and_finish(harness, setup.service_id, setup.environment.id)

    assert (finished["status"], finished["revertStatus"]) == ("FAILED", "SUCCEEDED")
    assert deployer.reverted_values == [None]
    assert len(deployer.reverts) == 1
    warnings = [
        record
        for record in caplog.records
        if record.getMessage() == "app environment variable unavailable"
    ]
    assert len(warnings) == 1
    assert warnings[0].env_keys == [KEY]  # type: ignore[attr-defined]
    assert secret not in caplog.text


async def _connection_id(harness: DeployHarness, setup: Connected) -> uuid.UUID:
    service = await harness.get_service(setup.service_id)
    return uuid.UUID(service["environments"][0]["serviceEnvironmentId"])


async def test_revert_restores_previous_values(harness: DeployHarness) -> None:
    setup = await _connected(harness)
    old_value = _value()
    await _put(harness, setup.service_id, setup.environment.id, value=old_value)
    deployer = _capture(harness)
    await deploy_and_finish(harness, setup.service_id, setup.environment.id)
    await _put(harness, setup.service_id, setup.environment.id, value=_value())
    deployer.fail_service_environment(await _connection_id(harness, setup))

    finished = await deploy_and_finish(harness, setup.service_id, setup.environment.id)

    assert finished["revertStatus"] == "SUCCEEDED"
    assert deployer.reverted_values == [{KEY: old_value}]


# --- 롤링 배포 호환(5-6): 이 기능 이전 배포 기록 ---


async def _strip_env_vars(harness: DeployHarness, deployment_id: str) -> None:
    async with harness.session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE deployments SET config_snapshot = config_snapshot - 'envVars' "
                "WHERE id = :id"
            ),
            {"id": deployment_id},
        )


async def test_rollback_to_record_before_this_feature_touches_no_secret(
    harness: DeployHarness,
) -> None:
    setup = await _connected(harness)
    old_deployment = setup.first_request["deploymentId"]
    await _strip_env_vars(harness, old_deployment)
    await _put(harness, setup.service_id, setup.environment.id)
    deployer = _capture(harness)
    await deploy_and_finish(harness, setup.service_id, setup.environment.id)

    accepted = await rollback(harness, setup.first_request["id"])
    await harness.run_until_idle()
    finished = await harness.get_request(accepted["id"])
    pinned = await _deployment(harness, finished["deploymentId"])
    listed = (await _list(harness, setup.service_id, setup.environment.id))["items"][0]

    assert finished["status"] == "SUCCEEDED"
    assert "envVars" not in pinned.config_snapshot
    assert deployer.applied_values[-1] is None
    assert listed["appliedVersion"] is None
    assert listed["pendingRedeploy"] is True


async def test_revert_to_record_before_this_feature_touches_no_secret(
    harness: DeployHarness,
) -> None:
    setup = await _connected(harness)
    await _strip_env_vars(harness, setup.first_request["deploymentId"])
    await _put(harness, setup.service_id, setup.environment.id)
    deployer = _capture(harness)
    deployer.fail_service_environment(await _connection_id(harness, setup))

    finished = await deploy_and_finish(harness, setup.service_id, setup.environment.id)

    assert (finished["status"], finished["revertStatus"]) == ("FAILED", "SUCCEEDED")
    assert deployer.reverted_values == [None]
    assert len(deployer.reverts) == 1


async def test_fake_refs_in_gcp_mode_touch_no_secret(harness: DeployHarness) -> None:
    setup = await _connected(harness)
    await _put(harness, setup.service_id, setup.environment.id)
    first = await deploy_and_finish(harness, setup.service_id, setup.environment.id)
    await deploy_and_finish(harness, setup.service_id, setup.environment.id)
    deployer = _capture(harness)
    harness.use_settings(
        app_secret_store_mode=AppSecretStoreMode.GCP, app_secret_project_id="team-echo-final"
    )

    accepted = await rollback(harness, first["id"])
    await harness.run_until_idle()
    finished = await harness.get_request(accepted["id"])

    assert finished["status"] == "SUCCEEDED"
    assert deployer.applied_values == [None]


# --- AC-10 병합만으로 운영 불변 ---


GCP_CONFIG: dict[str, object] = {
    "project_id": "team-echo-final",
    "region": "asia-northeast3",
    "cluster_name": "echo-dev-gke",
    "cluster_location": "asia-northeast3-a",
    "ingress_domain": "gcp-seoul.apps.echo.test",
}
SUFFIX = "k9x8y7z6"
NAMESPACE = f"shop-api-{SUFFIX}"
SECRET_PATH = f"/api/v1/namespaces/{NAMESPACE}/secrets/{NAMESPACE}-env"
HAND_SECRET: dict[str, Any] = {
    "apiVersion": "v1",
    "kind": "Secret",
    "type": "Opaque",
    "metadata": {
        "name": f"{NAMESPACE}-env",
        "namespace": NAMESPACE,
        "labels": {"app.kubernetes.io/managed-by": "jimin-manual"},
    },
    "data": {"DATABASE_URL": "aGFuZA==", "AUTO_MIGRATE": "ZmFsc2U="},
}


@dataclass
class RealWorld:
    cloud: FakeCloud
    cluster: FakeCluster
    executors: ExecutorSet
    environment: Environment
    service_id: str


@pytest.fixture
async def real_world(harness: DeployHarness) -> AsyncIterator[RealWorld]:
    """실제 쿠버네티스 실행기 + 가짜 창구. 빌드·헬스·대표 주소는 가짜 그대로다."""
    cloud = FakeCloud(harness.clock)  # type: ignore[arg-type]
    cluster = FakeCluster(harness.clock)  # type: ignore[arg-type]
    cluster.rolled_out = True
    settings = Settings(
        database_url=harness.settings.database_url,
        executor_mode=ExecutorMode.REAL,
        github_app_id="123",
        github_app_private_key_ref="env:ECHO_SECRET_GITHUB_APP_PRIVATE_KEY",
        source_client_mode=SourceClientMode.GITHUB,
        health_checker_mode=HealthCheckerMode.HTTP,
        google_metadata_base_url=METADATA_BASE,
    )
    executors = build_executor_set(
        settings,
        harness.clock,
        DictSecretClient({}),
        transports=RealExecutorTransports(
            cloud=cloud.transport(),
            metadata=cloud.transport(),
            kube_http_factory=cluster.factory(),
            sleep=RecordingSleep(),
        ),
    )
    harness.replace_executors(deploy=executors.deploy)
    harness.use_settings(executor_mode=ExecutorMode.REAL)
    harness.host_suffixes.queue(SUFFIX)
    service = await harness.create_service("shop-api")
    environment = Environment(
        organization_id=harness.organization.id,
        name="gcp-seoul",
        kind="gcp",
        config=dict(GCP_CONFIG),
        secret_ref=secret_ref_for("gcp-seoul"),
    )
    environment.record_connection_check(harness.clock.now(), None)
    async with harness.session_factory() as session, session.begin():
        session.add(environment)
    try:
        yield RealWorld(cloud, cluster, executors, environment, service["id"])
    finally:
        await executors.aclose()


def _secret_calls(cluster: FakeCluster) -> list[tuple[str, str]]:
    return [
        (request.method, request.url.path)
        for request in cluster.requests
        if request.url.path.endswith("/secrets") or request.url.path == SECRET_PATH
    ]


async def test_fake_store_with_real_executor_rejects_writes_and_leaves_hand_secret(
    harness: DeployHarness, real_world: RealWorld
) -> None:
    real_world.cluster.add_secret(SECRET_PATH, HAND_SECRET)
    before = dict(real_world.cluster.objects[SECRET_PATH])

    first = await deploy_and_finish(harness, real_world.service_id, real_world.environment.id)
    put = await _put(harness, real_world.service_id, real_world.environment.id)
    delete = await harness.client.delete(
        _path(harness, real_world.service_id, real_world.environment.id, KEY)
    )
    listed = await _list(harness, real_world.service_id, real_world.environment.id)
    second = await deploy_and_finish(harness, real_world.service_id, real_world.environment.id)

    assert (first["status"], second["status"]) == ("SUCCEEDED", "SUCCEEDED")
    assert (put.status_code, put.json()["code"]) == (409, "SECRET_STORE_NOT_CONFIGURED")
    assert (delete.status_code, delete.json()["code"]) == (409, "SECRET_STORE_NOT_CONFIGURED")
    assert listed == {"items": [], "simulated": True, "deployed": True}
    assert await _rows(harness) == []
    assert _store(harness).names == []
    assert _secret_calls(real_world.cluster) == []
    assert real_world.cluster.objects[SECRET_PATH] == before


async def test_records_made_before_store_is_on_keep_secret_after_rollback(
    harness: DeployHarness, real_world: RealWorld
) -> None:
    """가짜 보관함 동안의 기록에는 envVars 가 없어, 켠 뒤 되돌려도 Secret 을 비우지 않는다."""
    service_id, environment_id = real_world.service_id, real_world.environment.id
    before_store = await deploy_and_finish(harness, service_id, environment_id)
    record = await _deployment(harness, before_store["deploymentId"])
    store = GcpAppSecretStore(
        "team-echo-final",
        GoogleMetadataClient(METADATA_BASE, harness.clock, transport=real_world.cloud.transport()),  # type: ignore[arg-type]
        transport=FakeSecretManager().transport(),
        sleep=RecordingSleep(),
    )
    harness.app_secret_store = store
    harness.use_settings(
        app_secret_store_mode=AppSecretStoreMode.GCP, app_secret_project_id="team-echo-final"
    )
    secret = _value()
    await _put(harness, service_id, environment_id, value=secret)
    await deploy_and_finish(harness, service_id, environment_id)
    injected = dict(real_world.cluster.objects[SECRET_PATH])
    real_world.cluster.requests.clear()

    accepted = await rollback(harness, before_store["id"])
    await harness.run_until_idle()
    finished = await harness.get_request(accepted["id"])
    await store.aclose()

    assert "envVars" not in record.config_snapshot
    assert finished["status"] == "SUCCEEDED"
    assert _secret_calls(real_world.cluster) == []
    assert real_world.cluster.objects[SECRET_PATH] == injected
    assert base64.b64decode(injected["data"][KEY]).decode() == secret


async def test_default_settings_keep_fake_store() -> None:
    settings = Settings(database_url="postgresql+asyncpg://x:y@127.0.0.1:1/z")

    assert settings.app_secret_store_mode is AppSecretStoreMode.FAKE


# --- 끝에서 끝까지: gcp 보관함 + 실제 실행기(가짜 전송) ---


async def test_gcp_store_and_real_executor_inject_value_only_into_cluster(
    harness: DeployHarness, real_world: RealWorld, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    manager = FakeSecretManager()
    store = GcpAppSecretStore(
        "team-echo-final",
        GoogleMetadataClient(METADATA_BASE, harness.clock, transport=real_world.cloud.transport()),  # type: ignore[arg-type]
        transport=manager.transport(),
        sleep=RecordingSleep(),
    )
    harness.app_secret_store = store
    harness.use_settings(
        app_secret_store_mode=AppSecretStoreMode.GCP, app_secret_project_id="team-echo-final"
    )
    real_world.cluster.add_secret(SECRET_PATH, HAND_SECRET)
    service_id, environment_id = real_world.service_id, real_world.environment.id
    secret = _value()

    # 변수가 없는 연결: 손으로 만든 Secret 은 그대로다.
    plain = await deploy_and_finish(harness, service_id, environment_id)
    untouched = real_world.cluster.objects[SECRET_PATH]["data"]

    saved = await _put(harness, service_id, environment_id, value=secret)
    injected = await deploy_and_finish(harness, service_id, environment_id)
    cluster_secret = real_world.cluster.objects[SECRET_PATH]
    template = real_world.cluster.deployment(NAMESPACE)["spec"]["template"]
    pinned = await _deployment(harness, injected["deploymentId"])

    # 마지막 키를 지우고 다시 배포하면 echo-was 소유 Secret 이 비워진다.
    await harness.client.delete(_path(harness, service_id, environment_id, KEY))
    await deploy_and_finish(harness, service_id, environment_id)
    emptied = real_world.cluster.objects[SECRET_PATH]
    database = await _dump_tables(harness)
    logs = _logs_text(caplog)
    await store.aclose()

    ref = saved.json()["data"]["secretRef"]
    assert saved.json()["data"]["simulated"] is False
    assert ref.startswith("gcp-sm:projects/team-echo-final/secrets/echo-app-team-echo-")
    assert plain["status"] == injected["status"] == "SUCCEEDED"
    assert untouched == HAND_SECRET["data"]
    assert base64.b64decode(cluster_secret["data"][KEY]).decode() == secret
    assert set(cluster_secret["data"]) == {KEY}
    assert cluster_secret["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "echo-was"
    assert "echo.env-revision" in template["metadata"]["annotations"]
    assert pinned.config_snapshot["envVars"] == {KEY: ref}
    assert emptied["data"] == {}
    assert secret not in database and secret not in logs
    assert secret not in "\n".join(harness.responses)
    assert base64.b64encode(secret.encode()).decode() not in database + logs


# --- AC-11 ---


async def test_deleted_key_reput_continues_versions_and_old_record_keeps_old_value(
    harness: DeployHarness,
) -> None:
    setup = await _connected(harness)
    old_value, new_value = _value(), _value()
    deployer = _capture(harness)
    first = await _put(harness, setup.service_id, setup.environment.id, value=old_value)
    old_request = await deploy_and_finish(harness, setup.service_id, setup.environment.id)

    await harness.client.delete(_path(harness, setup.service_id, setup.environment.id, KEY))
    second = await _put(harness, setup.service_id, setup.environment.id, value=new_value)
    await deploy_and_finish(harness, setup.service_id, setup.environment.id)
    accepted = await rollback(harness, old_request["id"])
    await harness.run_until_idle()
    rows = await _rows(harness)

    assert second.status_code == 201
    assert _name_of(first.json()["data"]["secretRef"]) == _name_of(
        second.json()["data"]["secretRef"]
    )
    assert second.json()["data"]["version"] == 2
    assert [(row.is_deleted, row.latest_version) for row in rows] == [(True, 1), (False, 2)]
    assert rows[0].secret_name == rows[1].secret_name
    assert deployer.applied_values == [{KEY: old_value}, {KEY: new_value}, {KEY: old_value}]
    assert (await harness.get_request(accepted["id"]))["status"] == "SUCCEEDED"


async def test_other_organization_cannot_reach_variables(harness: DeployHarness) -> None:
    setup = await _connected(harness)
    other = await create_organization(harness.session_factory, "acme")
    admin = await create_member(
        harness.session_factory, other, OrganizationRole.ADMIN, harness.clock.now()
    )
    other_path = org_path(
        other.id, f"/services/{setup.service_id}/environments/{setup.environment.id}/variables"
    )

    async with make_client(session_token=admin.session_token) as client:
        response = await client.put(f"{other_path}/{KEY}", json={"value": "x"})

    assert (response.status_code, response.json()["code"]) == (404, "SERVICE_NOT_FOUND")
    assert await _rows(harness) == []
