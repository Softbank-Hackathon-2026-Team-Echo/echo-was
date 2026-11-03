"""환경 API 통합 시험(실제 PostgreSQL, HTTP 호출). SPEC api-2 의 AC-1~AC-12."""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from time import perf_counter
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.base import (
    ClusterInfo,
    ConnectionCheckError,
    EnvironmentConfig,
    SimulatedEnvironmentAdapter,
)
from app.adapters.registry import discover
from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.logging import JsonFormatter
from app.core.secrets import SecretValue, organization_env_secret_prefix
from app.dependencies import get_adapter_resolver
from app.enums import ConnectionErrorCode, OrganizationRole
from app.main import app
from app.models import Environment
from app.repositories.environment_repository import EnvironmentRepository
from app.services.environment_connection_service import AdapterResolver
from tests.adapter_contract import assert_adapter_contract
from tests.integration.auth_support import (
    Member,
    auth_settings,
    create_member,
    create_organization,
    make_client,
    org_path,
)
from tests.integration.factories import (
    DEFAULT_ORG_SLUG,
    ShopApiExample,
    secret_env_name,
    seed_shop_api_example,
)

SessionFactory = async_sessionmaker[AsyncSession]

# client 픽스처의 base_url 이 조직 경로(/api/v1/orgs/{org_id})라 환경 경로는 그 아래 상대 경로다.
BASE = "/environments"
ENVIRONMENT_KINDS = "http://test/api/v1/environment-kinds"
ORG_SECRET_PREFIX = organization_env_secret_prefix(DEFAULT_ORG_SLUG)
SUMMARY_KEYS = {
    "id",
    "name",
    "kind",
    "location",
    "config",
    "secretRef",
    "connectionStatus",
    "connectionCheckedAt",
    "connectionError",
    "createdAt",
    "updatedAt",
}
DETAIL_KEYS = SUMMARY_KEYS | {"clusterInfo", "clusterInfoError"}

AWS_CONFIG = {"region": "ap-northeast-2", "clusterName": "echo-eks"}
GCP_CONFIG = {"projectId": "echo-hack-123", "region": "asia-northeast3", "clusterName": "echo-gke"}
ONPREM_CONFIG = {"location": "team-laptop", "kubeContext": "k3d-echo"}

AWS_SECRET_ENV = secret_env_name("aws-seoul")
GCP_SECRET_ENV = secret_env_name("gcp-seoul")
ONPREM_SECRET_ENV = secret_env_name("onprem-k3s")


def _payload(
    name: str = "aws-seoul",
    kind: str = "aws",
    config: dict[str, object] | None = None,
    secret_ref: str = f"env:{AWS_SECRET_ENV}",
) -> dict[str, object]:
    return {
        "name": name,
        "kind": kind,
        "config": AWS_CONFIG if config is None else config,
        "secretRef": secret_ref,
    }


@pytest.fixture
def secrets_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """가짜 비밀값을 프로세스 환경변수로 넣는다(개발자 .env 와 섞이지 않게 monkeypatch)."""
    values = {
        AWS_SECRET_ENV: f"fake-aws-kubeconfig-{uuid.uuid4().hex}",
        GCP_SECRET_ENV: f"fake-gcp-kubeconfig-{uuid.uuid4().hex}",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(ONPREM_SECRET_ENV, raising=False)
    return values


@pytest.fixture
async def team(session_factory: SessionFactory) -> Member:
    """team-echo 조직과 그 ADMIN 멤버(세션 쿠키)."""
    organization = await create_organization(session_factory, DEFAULT_ORG_SLUG)
    return await create_member(
        session_factory, organization, OrganizationRole.ADMIN, SystemClock().now()
    )


@pytest.fixture
async def client(session_factory: SessionFactory, team: Member) -> AsyncIterator[AsyncClient]:
    """team-echo 어드민의 세션 쿠키·허용 Origin 을 싣고 base_url 이 그 조직 경로인 클라이언트."""
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    settings = auth_settings(get_settings())
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with make_client(
            session_token=team.session_token, base_path=org_path(team.organization.id)
        ) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def _use_adapter(adapter: SimulatedEnvironmentAdapter) -> None:
    resolver: AdapterResolver = lambda kind: adapter  # noqa: E731
    app.dependency_overrides[get_adapter_resolver] = lambda: resolver


def _use_timeout(seconds: float) -> None:
    settings = auth_settings(get_settings(), environment_check_timeout_seconds=seconds)
    app.dependency_overrides[get_settings] = lambda: settings


async def _create(client: AsyncClient, **kwargs: Any) -> dict[str, Any]:
    response = await client.post(BASE, json=_payload(**kwargs))
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def _stored(session_factory: SessionFactory, environment_id: str) -> Environment:
    async with session_factory() as session:
        return await session.get_one(Environment, uuid.UUID(environment_id))


class _Adapter(SimulatedEnvironmentAdapter):
    simulated_server_version = "v1.31.0"
    simulated_node_count = 3


# --- AC-1 / AC-2: 같은 열, 새 종류는 파일 하나 ---


async def test_search_environments_three_kinds_share_same_keys_with_nulls(
    client: AsyncClient, secrets_env: dict[str, str]
) -> None:
    await _create(client, name="aws-seoul", kind="aws", config=AWS_CONFIG)
    await _create(
        client,
        name="gcp-seoul",
        kind="gcp",
        config=GCP_CONFIG,
        secret_ref=f"env:{GCP_SECRET_ENV}",
    )
    # 온프레는 비밀값을 넣지 않아 FAILED 가 된다.
    await _create(
        client,
        name="onprem-k3s",
        kind="onprem",
        config=ONPREM_CONFIG,
        secret_ref=f"env:{ONPREM_SECRET_ENV}",
    )

    response = await client.get(BASE)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"success", "data"}
    page = body["data"]
    assert (page["total"], page["page"], page["size"]) == (3, 0, 20)
    items = page["items"]
    assert [set(item) for item in items] == [SUMMARY_KEYS] * 3
    assert [(i["name"], i["kind"], i["location"]) for i in items] == [
        ("aws-seoul", "aws", "ap-northeast-2"),
        ("gcp-seoul", "gcp", "asia-northeast3"),
        ("onprem-k3s", "onprem", "team-laptop"),
    ]
    assert [i["config"] for i in items] == [AWS_CONFIG, GCP_CONFIG, ONPREM_CONFIG]
    assert [(i["connectionStatus"], i["connectionError"]) for i in items] == [
        ("CONNECTED", None),
        ("CONNECTED", None),
        ("FAILED", "SECRET_NOT_FOUND"),
    ]
    # 값이 없는 칸도 null 로 들어 있다.
    assert items[0]["connectionError"] is None


async def test_new_kind_file_registers_lists_and_passes_contract(
    client: AsyncClient, azure_adapter_package: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    discover(azure_adapter_package)
    monkeypatch.setenv(f"{ORG_SECRET_PREFIX}AZURE_KOREA", "fake-azure-kubeconfig")
    await _create(
        client, name="aws-seoul", kind="aws", secret_ref=f"env:{ORG_SECRET_PREFIX}MISSING_AWS"
    )

    created = await client.post(
        BASE,
        json=_payload(
            name="azure-korea",
            kind="azure",
            config={"subscriptionId": "sub-1", "location": "koreacentral", "clusterName": "aks"},
            secret_ref=f"env:{ORG_SECRET_PREFIX}AZURE_KOREA",
        ),
    )
    listed = await client.get(BASE)
    kinds = await client.get(ENVIRONMENT_KINDS)

    assert created.status_code == 201
    assert created.json()["data"]["connectionStatus"] == "CONNECTED"
    assert created.json()["data"]["clusterInfo"] == {
        "serverVersion": "v1.30.5",
        "nodeCount": 2,
        "simulated": True,
    }
    items = listed.json()["data"]["items"]
    assert [set(item) for item in items] == [SUMMARY_KEYS, SUMMARY_KEYS]
    assert items[1]["location"] == "koreacentral"
    assert [k["kind"] for k in kinds.json()["data"]] == ["aws", "azure", "gcp", "onprem"]
    await assert_adapter_contract(
        "azure", {"subscription_id": "sub-1", "location": "koreacentral", "cluster_name": "aks"}
    )


async def test_search_environment_kinds_returns_camel_case_config_schema(
    client: AsyncClient,
) -> None:
    response = await client.get(ENVIRONMENT_KINDS)

    assert response.status_code == 200
    kinds = response.json()["data"]
    assert [k["kind"] for k in kinds] == ["aws", "gcp", "onprem"]
    assert all(set(k) == {"kind", "configSchema"} for k in kinds)
    assert set(kinds[0]["configSchema"]["properties"]) == {
        "region",
        "clusterName",
        "roleArn",
        "ingressDomain",
        "ingressGroup",
        "certificateArn",
        "ingressClass",
    }
    assert set(kinds[0]["configSchema"]["required"]) == {"region", "clusterName"}
    assert set(kinds[1]["configSchema"]["required"]) == {"projectId", "region", "clusterName"}


# --- AC-3: 설정 오류 details ---


async def test_create_environment_invalid_config_returns_field_paths_without_values(
    client: AsyncClient,
) -> None:
    response = await client.post(BASE, json=_payload(config={"region": "seoul"}))

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "ENVIRONMENT_CONFIG_INVALID"
    assert sorted(body["details"], key=lambda d: d["field"]) == [
        {"field": "config.clusterName", "reason": "MISSING"},
        {"field": "config.region", "reason": "INVALID_FORMAT"},
    ]
    assert "seoul" not in response.text


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (
            {**AWS_CONFIG, "accessKey": "AKIA-not-real"},
            [{"field": "config.accessKey", "reason": "UNKNOWN_FIELD"}],
        ),
        (
            {"region": "ap-northeast-2", "cluster_name": "echo-eks"},
            [
                {"field": "config.clusterName", "reason": "MISSING"},
                {"field": "config.cluster_name", "reason": "UNKNOWN_FIELD"},
            ],
        ),
    ],
)
async def test_create_environment_unknown_or_snake_case_key_rejected(
    client: AsyncClient, config: dict[str, object], expected: list[dict[str, str]]
) -> None:
    response = await client.post(BASE, json=_payload(config=config))

    assert response.status_code == 422
    assert response.json()["code"] == "ENVIRONMENT_CONFIG_INVALID"
    assert sorted(response.json()["details"], key=lambda d: d["field"]) == expected
    assert "AKIA-not-real" not in response.text


async def test_create_environment_unknown_kind_returns_422(client: AsyncClient) -> None:
    response = await client.post(BASE, json=_payload(kind="azure"))

    assert response.status_code == 422
    assert response.json()["code"] == "UNKNOWN_ENVIRONMENT_KIND"


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "aws", "config": AWS_CONFIG, "secretRef": f"env:{AWS_SECRET_ENV}"},
        _payload(name="AWS_Seoul"),
        _payload() | {"config": []},
        _payload() | {"extra": 1},
        {
            "name": "aws-seoul",
            "kind": "aws",
            "config": AWS_CONFIG,
            "secret_ref": "env:ECHO_SECRET_X",
        },
    ],
)
async def test_create_environment_schema_violations_return_validation_error(
    client: AsyncClient, body: dict[str, object]
) -> None:
    response = await client.post(BASE, json=body)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


# --- AC-4: 등록 + 접속 확인 + secretRef ---


async def test_create_environment_connects_with_simulated_cluster_info(
    client: AsyncClient, secrets_env: dict[str, str]
) -> None:
    response = await client.post(BASE, json=_payload())

    assert response.status_code == 201
    data = response.json()["data"]
    assert set(data) == DETAIL_KEYS
    assert data["connectionStatus"] == "CONNECTED"
    assert data["connectionCheckedAt"] is not None
    assert data["connectionCheckedAt"].endswith("Z")
    assert data["connectionError"] is None
    assert data["clusterInfo"] == {"serverVersion": "v1.31.0", "nodeCount": 3, "simulated": True}
    assert data["clusterInfoError"] is None
    assert data["secretRef"] == f"env:{AWS_SECRET_ENV}"


@pytest.mark.parametrize(
    ("secret_ref", "reason"),
    [
        (f"env:{ORG_SECRET_PREFIX}NOT_PROVIDED", "SECRET_NOT_FOUND"),
        ("k8s:echo-system/aws-seoul/kubeconfig", "SECRET_STORE_UNSUPPORTED"),
        ("aws-sm:echo/aws-seoul", "SECRET_STORE_UNSUPPORTED"),
        ("gcp-sm:projects/echo-hack-123/secrets/gke", "SECRET_STORE_UNSUPPORTED"),
    ],
)
async def test_create_environment_secret_unavailable_still_registers_as_failed(
    client: AsyncClient, session_factory: SessionFactory, secret_ref: str, reason: str
) -> None:
    response = await client.post(BASE, json=_payload(secret_ref=secret_ref))

    assert response.status_code == 201
    data = response.json()["data"]
    assert (data["connectionStatus"], data["connectionError"]) == ("FAILED", reason)
    assert (data["clusterInfo"], data["clusterInfoError"]) == (None, reason)
    stored = await _stored(session_factory, data["id"])
    assert (stored.connection_status, stored.connection_error) == ("FAILED", reason)


@pytest.mark.parametrize(
    "secret_ref",
    ["env:DATABASE_URL", "echo/environments/aws-seoul", "vault:x", "k8s:ns/only", "env:"],
)
async def test_create_environment_invalid_secret_ref_rejected_without_pattern(
    client: AsyncClient, secret_ref: str
) -> None:
    response = await client.post(BASE, json=_payload(secret_ref=secret_ref))

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["details"] == [{"field": "secretRef", "reason": "INVALID_SECRET_REF"}]
    assert "ECHO_SECRET_[" not in response.text
    assert "^" not in response.text


async def test_create_environment_duplicate_live_name_conflicts(
    client: AsyncClient, secrets_env: dict[str, str]
) -> None:
    await _create(client)

    response = await client.post(BASE, json=_payload())

    assert response.status_code == 409
    assert response.json()["code"] == "ENVIRONMENT_NAME_CONFLICT"


async def test_create_environment_concurrent_duplicate_integrity_error_conflicts(
    client: AsyncClient, secrets_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    await _create(client)

    async def find_nothing(
        self: EnvironmentRepository, organization_id: uuid.UUID, name: str
    ) -> None:
        return None

    # 사전 조회를 통과한 동시 등록을 흉내 낸다: DB 부분 unique index 가 막는다.
    monkeypatch.setattr(EnvironmentRepository, "find_live_by_name", find_nothing)
    response = await client.post(BASE, json=_payload())

    assert response.status_code == 409
    assert response.json()["code"] == "ENVIRONMENT_NAME_CONFLICT"


# --- 단건·목록 ---


async def test_get_environment_unknown_id_returns_not_found(client: AsyncClient) -> None:
    response = await client.get(f"{BASE}/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json() == {
        "success": False,
        "code": "ENVIRONMENT_NOT_FOUND",
        "message": "environment not found",
    }


async def test_search_environments_kind_filter_and_pagination(
    client: AsyncClient, secrets_env: dict[str, str]
) -> None:
    for index in range(3):
        await _create(client, name=f"aws-{index}")
    await _create(client, name="onprem-1", kind="onprem", config=ONPREM_CONFIG)

    first = await client.get(BASE, params={"kind": "aws", "page": 0, "size": 2})
    second = await client.get(BASE, params={"kind": "aws", "page": 1, "size": 2})
    too_big = await client.get(BASE, params={"size": 101})
    negative = await client.get(BASE, params={"page": -1})

    assert [i["name"] for i in first.json()["data"]["items"]] == ["aws-0", "aws-1"]
    assert [i["name"] for i in second.json()["data"]["items"]] == ["aws-2"]
    assert first.json()["data"]["total"] == 3
    assert (too_big.status_code, too_big.json()["code"]) == (422, "VALIDATION_ERROR")
    assert negative.status_code == 422


async def test_search_environments_does_not_call_adapter_or_secret_store(
    client: AsyncClient, secrets_env: dict[str, str]
) -> None:
    await _create(client)

    class _ExplodingAdapter(_Adapter):
        async def read_cluster_info(
            self, config: EnvironmentConfig, credentials: SecretValue
        ) -> ClusterInfo:
            raise AssertionError("list must not call adapters")

    _use_adapter(_ExplodingAdapter())
    response = await client.get(BASE)

    assert response.status_code == 200


# --- AC-5: 수정 ---


async def test_update_environment_kind_change_rejected_and_row_unchanged(
    client: AsyncClient, session_factory: SessionFactory, secrets_env: dict[str, str]
) -> None:
    created = await _create(client)

    response = await client.patch(f"{BASE}/{created['id']}", json={"kind": "gcp", "name": "x"})

    assert response.status_code == 422
    assert response.json()["code"] == "ENVIRONMENT_KIND_IMMUTABLE"
    stored = await _stored(session_factory, created["id"])
    assert (stored.kind, stored.name) == ("aws", "aws-seoul")


async def test_update_environment_same_kind_and_empty_body_return_current(
    client: AsyncClient, secrets_env: dict[str, str]
) -> None:
    created = await _create(client)

    same_kind = await client.patch(f"{BASE}/{created['id']}", json={"kind": "aws"})
    empty = await client.patch(f"{BASE}/{created['id']}", json={})

    for response in (same_kind, empty):
        assert response.status_code == 200
        data = response.json()["data"]
        assert set(data) == DETAIL_KEYS
        assert data["connectionCheckedAt"] == created["connectionCheckedAt"]
        assert data["clusterInfo"] is not None


async def test_update_environment_name_only_does_not_recheck(
    client: AsyncClient, secrets_env: dict[str, str]
) -> None:
    created = await _create(client)

    response = await client.patch(f"{BASE}/{created['id']}", json={"name": "aws-seoul-2"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["name"] == "aws-seoul-2"
    assert data["connectionCheckedAt"] == created["connectionCheckedAt"]
    assert data["clusterInfo"] == created["clusterInfo"]


@pytest.mark.parametrize(
    "change",
    [
        {"config": {"region": "us-east-1", "clusterName": "echo-eks"}},
        {"secretRef": f"env:{GCP_SECRET_ENV}"},
    ],
)
async def test_update_environment_config_or_secret_ref_rechecks(
    client: AsyncClient,
    session_factory: SessionFactory,
    secrets_env: dict[str, str],
    change: dict[str, object],
) -> None:
    created = await _create(client)
    await asyncio.sleep(0.01)

    response = await client.patch(f"{BASE}/{created['id']}", json=change)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["connectionStatus"] == "CONNECTED"
    assert data["connectionCheckedAt"] > created["connectionCheckedAt"]
    stored = await _stored(session_factory, created["id"])
    assert stored.connection_checked_at is not None
    if "config" in change:
        assert stored.config == {"region": "us-east-1", "cluster_name": "echo-eks"}
        assert data["location"] == "us-east-1"


async def test_update_environment_secret_ref_to_missing_secret_becomes_failed(
    client: AsyncClient, secrets_env: dict[str, str]
) -> None:
    created = await _create(client)

    response = await client.patch(
        f"{BASE}/{created['id']}", json={"secretRef": f"env:{ORG_SECRET_PREFIX}GONE"}
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert (data["connectionStatus"], data["connectionError"]) == ("FAILED", "SECRET_NOT_FOUND")


@pytest.mark.parametrize("field", ["name", "kind", "config", "secretRef"])
async def test_update_environment_null_value_rejected(
    client: AsyncClient, secrets_env: dict[str, str], field: str
) -> None:
    created = await _create(client)

    response = await client.patch(f"{BASE}/{created['id']}", json={field: None})

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert response.json()["details"] == [{"field": field, "reason": "NULL_NOT_ALLOWED"}]


async def test_update_environment_invalid_config_and_secret_ref_rejected(
    client: AsyncClient, secrets_env: dict[str, str]
) -> None:
    created = await _create(client)

    bad_config = await client.patch(
        f"{BASE}/{created['id']}", json={"config": {"region": "ap-northeast-2"}}
    )
    bad_ref = await client.patch(f"{BASE}/{created['id']}", json={"secretRef": "env:HOME"})

    assert bad_config.json()["code"] == "ENVIRONMENT_CONFIG_INVALID"
    assert bad_config.json()["details"] == [{"field": "config.clusterName", "reason": "MISSING"}]
    assert bad_ref.json()["details"] == [{"field": "secretRef", "reason": "INVALID_SECRET_REF"}]


async def test_update_environment_name_conflicts_with_other_live_environment(
    client: AsyncClient, secrets_env: dict[str, str]
) -> None:
    await _create(client, name="aws-seoul")
    other = await _create(client, name="aws-tokyo")

    response = await client.patch(f"{BASE}/{other['id']}", json={"name": "aws-seoul"})

    assert response.status_code == 409
    assert response.json()["code"] == "ENVIRONMENT_NAME_CONFLICT"


# --- AC-6: 삭제 ---


@pytest.fixture
async def shop_api(session_factory: SessionFactory, team: Member) -> ShopApiExample:
    async with session_factory() as session, session.begin():
        return await seed_shop_api_example(session, team.organization)


async def test_delete_environment_in_use_conflicts_and_row_unchanged(
    client: AsyncClient, session_factory: SessionFactory, shop_api: ShopApiExample
) -> None:
    environment_id = str(shop_api.environments["aws-seoul"].id)

    response = await client.delete(f"{BASE}/{environment_id}")

    assert response.status_code == 409
    assert response.json()["code"] == "ENVIRONMENT_IN_USE"
    stored = await _stored(session_factory, environment_id)
    assert stored.is_deleted is False


async def test_delete_environment_unused_soft_deletes_and_name_reusable(
    client: AsyncClient,
    session_factory: SessionFactory,
    shop_api: ShopApiExample,
    secrets_env: dict[str, str],
) -> None:
    environment_id = str(shop_api.environments["onprem-k3s"].id)

    deleted = await client.delete(f"{BASE}/{environment_id}")
    after = await client.get(f"{BASE}/{environment_id}")
    patch_after = await client.patch(f"{BASE}/{environment_id}", json={"name": "x"})
    check_after = await client.post(f"{BASE}/{environment_id}/connection-check")
    delete_again = await client.delete(f"{BASE}/{environment_id}")
    recreated = await client.post(
        BASE, json=_payload(name="onprem-k3s", kind="onprem", config=ONPREM_CONFIG)
    )

    assert (deleted.status_code, deleted.content) == (204, b"")
    assert [r.status_code for r in (after, patch_after, check_after, delete_again)] == [404] * 4
    assert recreated.status_code == 201
    stored = await _stored(session_factory, environment_id)
    assert stored.is_deleted is True
    assert stored.deleted_at is not None
    listed = await client.get(BASE)
    assert environment_id not in [item["id"] for item in listed.json()["data"]["items"]]


# --- AC-7: 다시 확인 / 단건은 읽기만 ---


async def test_check_environment_connection_updates_status_time_and_reason(
    client: AsyncClient,
    session_factory: SessionFactory,
    secrets_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = await _create(client)
    monkeypatch.delenv(AWS_SECRET_ENV)

    failed = await client.post(f"{BASE}/{created['id']}/connection-check")
    monkeypatch.setenv(AWS_SECRET_ENV, "restored")
    await asyncio.sleep(0.01)
    recovered = await client.post(f"{BASE}/{created['id']}/connection-check")

    assert failed.status_code == 200
    failed_data = failed.json()["data"]
    assert (failed_data["connectionStatus"], failed_data["connectionError"]) == (
        "FAILED",
        "SECRET_NOT_FOUND",
    )
    recovered_data = recovered.json()["data"]
    assert (recovered_data["connectionStatus"], recovered_data["connectionError"]) == (
        "CONNECTED",
        None,
    )
    assert recovered_data["connectionCheckedAt"] > failed_data["connectionCheckedAt"]
    stored = await _stored(session_factory, created["id"])
    assert stored.connection_status == "CONNECTED"


async def test_get_environment_reads_cluster_info_without_changing_stored_status(
    client: AsyncClient,
    session_factory: SessionFactory,
    secrets_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = await _create(client)
    before = await _stored(session_factory, created["id"])
    monkeypatch.delenv(AWS_SECRET_ENV)

    response = await client.get(f"{BASE}/{created['id']}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert set(data) == DETAIL_KEYS
    # 비밀값이 사라져 클러스터 정보는 못 읽지만, 저장된 상태는 그대로다.
    assert (data["clusterInfo"], data["clusterInfoError"]) == (None, "SECRET_NOT_FOUND")
    assert data["connectionStatus"] == "CONNECTED"
    after = await _stored(session_factory, created["id"])
    assert (after.connection_status, after.connection_checked_at, after.updated_at) == (
        before.connection_status,
        before.connection_checked_at,
        before.updated_at,
    )


async def test_get_environment_returns_cluster_info(
    client: AsyncClient, secrets_env: dict[str, str]
) -> None:
    created = await _create(client, name="gcp-seoul", kind="gcp", config=GCP_CONFIG)

    response = await client.get(f"{BASE}/{created['id']}")

    assert response.json()["data"]["clusterInfo"] == {
        "serverVersion": "v1.31.1-gke.1000",
        "nodeCount": 3,
        "simulated": True,
    }


# --- AC-8: 제한 시간 ---


async def test_create_environment_slow_connection_times_out_quickly(
    client: AsyncClient, secrets_env: dict[str, str]
) -> None:
    class _SlowConnect(_Adapter):
        async def check_connection(
            self, config: EnvironmentConfig, credentials: SecretValue
        ) -> None:
            await asyncio.sleep(5)

    _use_timeout(0.2)
    _use_adapter(_SlowConnect())
    started_at = perf_counter()

    response = await client.post(BASE, json=_payload())

    elapsed = perf_counter() - started_at
    assert response.status_code == 201
    data = response.json()["data"]
    assert (data["connectionStatus"], data["connectionError"]) == ("FAILED", "TIMEOUT")
    assert elapsed < 1.2


async def test_create_environment_slow_cluster_info_stays_connected(
    client: AsyncClient, secrets_env: dict[str, str]
) -> None:
    class _SlowClusterInfo(_Adapter):
        async def read_cluster_info(
            self, config: EnvironmentConfig, credentials: SecretValue
        ) -> ClusterInfo:
            await asyncio.sleep(5)
            raise AssertionError("unreachable")

    _use_timeout(0.2)
    _use_adapter(_SlowClusterInfo())

    response = await client.post(BASE, json=_payload())

    data = response.json()["data"]
    assert data["connectionStatus"] == "CONNECTED"
    assert (data["clusterInfo"], data["clusterInfoError"]) == (None, "TIMEOUT")


# --- AC-9: 비밀값이 새지 않는다 ---


async def test_secret_value_never_appears_in_logs_or_responses(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = f"leak-probe-{uuid.uuid4().hex}"
    monkeypatch.setenv(AWS_SECRET_ENV, secret)
    responses: list[Response] = []

    class _RejectingAdapter(_Adapter):
        async def check_connection(
            self, config: EnvironmentConfig, credentials: SecretValue
        ) -> None:
            raise ConnectionCheckError(ConnectionErrorCode.AUTH_FAILED)

    class _CrashingAdapter(_Adapter):
        async def check_connection(
            self, config: EnvironmentConfig, credentials: SecretValue
        ) -> None:
            raise RuntimeError(f"cannot use {credentials.get_secret_value()}")

    with caplog.at_level(logging.DEBUG):
        # 성공
        responses.append(await client.post(BASE, json=_payload(name="env-ok")))
        environment_id = responses[-1].json()["data"]["id"]
        responses.append(await client.get(f"{BASE}/{environment_id}"))
        # SECRET_NOT_FOUND
        responses.append(
            await client.post(
                BASE, json=_payload(name="env-missing", secret_ref=f"env:{ORG_SECRET_PREFIX}ABSENT")
            )
        )
        # 어댑터 ConnectionCheckError
        _use_adapter(_RejectingAdapter())
        responses.append(await client.post(f"{BASE}/{environment_id}/connection-check"))
        # 어댑터 일반 예외(메시지에 비밀값이 들어 있음)
        _use_adapter(_CrashingAdapter())
        responses.append(await client.post(f"{BASE}/{environment_id}/connection-check"))
        responses.append(await client.get(BASE))

    statuses = [
        (r.json()["data"]["connectionStatus"], r.json()["data"]["connectionError"])
        for r in responses[2:5]
    ]
    assert statuses == [
        ("FAILED", "SECRET_NOT_FOUND"),
        ("FAILED", "AUTH_FAILED"),
        ("FAILED", "UNKNOWN"),
    ]
    formatter = JsonFormatter()
    log_lines = [formatter.format(record) for record in caplog.records]
    log_lines += [repr(record.__dict__) for record in caplog.records]
    assert caplog.records
    assert sum(secret in line for line in log_lines) == 0
    assert sum(secret in r.text for r in responses) == 0
    assert secret not in caplog.text
    assert secret not in repr(SecretValue(secret))


# --- AC-11: 확인 도중 수정·삭제 ---


class _GatedAdapter(_Adapter):
    """첫 접속 확인만 gate 가 열릴 때까지 기다렸다가 UNREACHABLE 로 실패한다."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def check_connection(self, config: EnvironmentConfig, credentials: SecretValue) -> None:
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            await self.release.wait()
            raise ConnectionCheckError(ConnectionErrorCode.UNREACHABLE)


async def _start_gated_check(
    client: AsyncClient, environment_id: str
) -> tuple[_GatedAdapter, asyncio.Task[Response]]:
    adapter = _GatedAdapter()
    _use_adapter(adapter)
    task = asyncio.create_task(client.post(f"{BASE}/{environment_id}/connection-check"))
    await asyncio.wait_for(adapter.started.wait(), timeout=2)
    return adapter, task


async def test_check_connection_result_discarded_when_config_changed_during_check(
    client: AsyncClient, session_factory: SessionFactory, secrets_env: dict[str, str]
) -> None:
    created = await _create(client)
    adapter, first_check = await _start_gated_check(client, created["id"])

    # 첫 확인이 도는 사이 config 를 바꾸고 커밋한다(두 번째 확인은 바로 성공).
    patched = await client.patch(
        f"{BASE}/{created['id']}", json={"config": {"region": "us-east-1", "clusterName": "eks"}}
    )
    adapter.release.set()
    first = await first_check

    assert patched.status_code == 200
    assert patched.json()["data"]["connectionStatus"] == "CONNECTED"
    assert first.status_code == 200
    # 첫 확인의 결과(UNREACHABLE)는 저장되지 않았다. 응답은 그 시점 DB 값이다.
    assert first.json()["data"]["connectionStatus"] == "CONNECTED"
    assert first.json()["data"]["clusterInfo"] is None
    stored = await _stored(session_factory, created["id"])
    assert (stored.connection_status, stored.connection_error) == ("CONNECTED", None)
    assert stored.connection_checked_at is not None
    assert (
        stored.connection_checked_at.isoformat().replace("+00:00", "Z")
        == patched.json()["data"]["connectionCheckedAt"]
    )
    assert adapter.calls == 2


async def test_check_connection_environment_deleted_during_check_returns_not_found(
    client: AsyncClient, session_factory: SessionFactory, secrets_env: dict[str, str]
) -> None:
    created = await _create(client)
    adapter, first_check = await _start_gated_check(client, created["id"])

    deleted = await client.delete(f"{BASE}/{created['id']}")
    adapter.release.set()
    first = await first_check

    assert deleted.status_code == 204
    assert first.status_code == 404
    assert first.json()["code"] == "ENVIRONMENT_NOT_FOUND"
    stored = await _stored(session_factory, created["id"])
    assert stored.is_deleted is True
    assert stored.connection_status == "CONNECTED"


# --- AC-12: 저장 값을 읽지 못하는 행 ---


async def _insert_raw_environment(
    session_factory: SessionFactory,
    organization_id: uuid.UUID,
    name: str,
    config: dict[str, object],
    secret_ref: str,
) -> str:
    # 지금 양식으로 읽히지 않는 저장 행(종류 파일 변경 등)을 만들려고 시험에서만 SQL 로 직접 넣는다.
    async with session_factory() as session, session.begin():
        environment_id = await session.scalar(
            text(
                "INSERT INTO environments (organization_id, name, kind, config, secret_ref) "
                "VALUES (:organization_id, :name, 'aws', CAST(:config AS jsonb), :secret_ref) "
                "RETURNING id"
            ),
            {
                "organization_id": organization_id,
                "name": name,
                "config": json.dumps(config),
                "secret_ref": secret_ref,
            },
        )
    return str(environment_id)


async def test_unreadable_stored_config_does_not_break_list_and_check_fails(
    client: AsyncClient,
    session_factory: SessionFactory,
    secrets_env: dict[str, str],
    caplog: pytest.LogCaptureFixture,
    team: Member,
) -> None:
    await _create(client)
    broken_id = await _insert_raw_environment(
        session_factory,
        team.organization.id,
        "broken-aws",
        {"region": "seoul"},
        f"env:{AWS_SECRET_ENV}",
    )

    with caplog.at_level(logging.WARNING):
        listed = await client.get(BASE)
        detail = await client.get(f"{BASE}/{broken_id}")
    checked = await client.post(f"{BASE}/{broken_id}/connection-check")

    assert listed.status_code == 200
    items = {item["name"]: item for item in listed.json()["data"]["items"]}
    assert set(items["broken-aws"]) == SUMMARY_KEYS
    assert (items["broken-aws"]["location"], items["broken-aws"]["config"]) == (None, None)
    assert items["aws-seoul"]["location"] == "ap-northeast-2"
    assert detail.status_code == 200
    assert detail.json()["data"]["clusterInfoError"] == "CONFIG_INVALID"
    assert any(
        getattr(r, "environment_id", None) == broken_id
        and r.getMessage() == "stored environment config is unreadable"
        for r in caplog.records
    )
    data = checked.json()["data"]
    assert (data["connectionStatus"], data["connectionError"]) == ("FAILED", "CONFIG_INVALID")


async def test_invalid_stored_secret_ref_is_shown_as_is_and_check_fails(
    client: AsyncClient, session_factory: SessionFactory, team: Member
) -> None:
    broken_id = await _insert_raw_environment(
        session_factory,
        team.organization.id,
        "legacy-aws",
        {"region": "ap-northeast-2", "cluster_name": "eks"},
        "echo/environments/legacy-aws",
    )

    listed = await client.get(BASE)
    checked = await client.post(f"{BASE}/{broken_id}/connection-check")

    item = listed.json()["data"]["items"][0]
    assert item["secretRef"] == "echo/environments/legacy-aws"
    assert item["location"] == "ap-northeast-2"
    data = checked.json()["data"]
    assert (data["connectionStatus"], data["connectionError"]) == ("FAILED", "SECRET_REF_INVALID")


async def test_environment_without_secret_ref_check_fails_with_missing(
    client: AsyncClient, session_factory: SessionFactory, team: Member
) -> None:
    async with session_factory() as session, session.begin():
        environment = Environment(
            organization_id=team.organization.id,
            name="no-ref",
            kind="aws",
            config={"region": "ap-northeast-2", "cluster_name": "e"},
        )
        session.add(environment)

    response = await client.post(f"{BASE}/{environment.id}/connection-check")

    data = response.json()["data"]
    assert (data["secretRef"], data["connectionError"]) == (None, "SECRET_REF_MISSING")


# --- DB 제약: 접속 상태 칸 ---


@pytest.mark.parametrize(
    ("assignment", "constraint"),
    [
        (
            "connection_status = 'BROKEN', connection_checked_at = now()",
            "ck_environments_connection_status_valid",
        ),
        (
            "connection_status = 'CONNECTED'",
            "ck_environments_connection_checked_at_consistency",
        ),
        (
            "connection_status = 'FAILED', connection_checked_at = now()",
            "ck_environments_connection_error_consistency",
        ),
        (
            "connection_status = 'CONNECTED', connection_checked_at = now(), "
            "connection_error = 'TIMEOUT'",
            "ck_environments_connection_error_consistency",
        ),
    ],
)
async def test_database_rejects_inconsistent_connection_columns(
    session_factory: SessionFactory, assignment: str, constraint: str, team: Member
) -> None:
    environment_id = await _insert_raw_environment(
        session_factory,
        team.organization.id,
        "env-a",
        {"region": "ap-northeast-2", "cluster_name": "e"},
        "env:X",
    )
    async with session_factory() as session:
        with pytest.raises(IntegrityError, match=constraint):
            # CHECK 를 직접 확인하려고 시험에서만 SQL 로 고친다.
            await session.execute(
                text(f"UPDATE environments SET {assignment} WHERE id = :id"),
                {"id": environment_id},
            )


async def test_session_objects_written_by_service_keep_constraints(
    session_factory: SessionFactory, client: AsyncClient, secrets_env: dict[str, str]
) -> None:
    created = await _create(client)
    async with session_factory() as session:
        rows = (await session.scalars(select(Environment))).all()

    assert [(row.name, row.connection_status) for row in rows] == [("aws-seoul", "CONNECTED")]
    assert created["id"] == str(rows[0].id)
