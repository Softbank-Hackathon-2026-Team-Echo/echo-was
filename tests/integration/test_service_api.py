"""서비스 API 통합 시험(SPEC api-3 AC-1, §5.6 수정 규칙)."""

import re
from typing import Any

import pytest

from tests.integration.deploy_harness import DeployHarness

SERVICE_KEYS = {
    "id",
    "name",
    "description",
    "repoUrl",
    "private",
    "healthCheckPath",
    "healthCheckTimeoutSeconds",
    "containerPort",
    "url",
    "status",
    "deleteError",
    "simulated",
    "createdAt",
    "updatedAt",
}


async def test_create_service_public_repo_returns_201_with_main_url(
    harness: DeployHarness,
) -> None:
    service = await harness.create_service("shop-api", "https://github.com/team-echo/shop-api.git/")

    # 상세에는 환경 목록과 끝나지 않은 이전 계획 id(API 서버 8)가 더 있다.
    assert set(service) == SERVICE_KEYS | {"environments", "activeMigrationPlanId"}
    assert service["activeMigrationPlanId"] is None
    assert service["private"] is False
    assert re.fullmatch(r"https://shop-api-[a-z0-9]{8}\.apps\.echo\.test", service["url"])
    assert service["simulated"] is True
    assert service["repoUrl"] == "https://github.com/team-echo/shop-api"
    assert service["status"] == "ACTIVE"
    assert service["deleteError"] is None
    assert service["environments"] == []
    assert (service["healthCheckPath"], service["healthCheckTimeoutSeconds"]) == ("/healthz", 300)
    assert service["containerPort"] == 8080


async def test_create_service_private_owner_returns_private_true(harness: DeployHarness) -> None:
    service = await harness.create_service("billing", "https://github.com/private-acme/billing")

    assert service["private"] is True


@pytest.mark.parametrize("owner", ["denied-acme", "missing-acme"])
async def test_create_service_inaccessible_repo_returns_404_with_install_url(
    harness: DeployHarness, owner: str
) -> None:
    response = await harness.client.post(
        harness.services, json={"name": "shop-api", "repoUrl": f"https://github.com/{owner}/shop"}
    )

    body = response.json()
    assert response.status_code == 404
    assert body["code"] == "REPO_NOT_ACCESSIBLE"
    assert body["details"] == [
        {
            "field": "installUrl",
            "reason": "INSTALL_GITHUB_APP",
            "value": harness.settings.github_app_install_url,
        }
    ]


async def test_create_service_flaky_provider_returns_502(harness: DeployHarness) -> None:
    response = await harness.client.post(
        harness.services, json={"name": "shop-api", "repoUrl": "https://github.com/flaky-acme/shop"}
    )

    assert response.status_code == 502
    assert response.json()["code"] == "SOURCE_PROVIDER_UNAVAILABLE"


@pytest.mark.parametrize(
    "repo_url",
    [
        "https://gitlab.com/team-echo/shop-api",
        "http://github.com/team-echo/shop-api",
        "https://user:token@github.com/team-echo/shop-api",
        "https://github.com/team-echo",
        "https://github.com/team-echo/shop-api/tree/main",
        "git@github.com:team-echo/shop-api.git",
    ],
)
async def test_create_service_invalid_repo_url_returns_422(
    harness: DeployHarness, repo_url: str
) -> None:
    response = await harness.client.post(
        harness.services, json={"name": "shop-api", "repoUrl": repo_url}
    )

    assert response.status_code == 422
    assert response.json()["details"] == [{"field": "repoUrl", "reason": "INVALID_REPO_URL"}]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("healthCheckPath", "healthz"),
        ("healthCheckTimeoutSeconds", 9),
        ("healthCheckTimeoutSeconds", 1801),
        ("containerPort", 0),
        ("containerPort", 65536),
        ("name", "Shop_API"),
    ],
)
async def test_create_service_out_of_range_field_returns_422(
    harness: DeployHarness, field: str, value: Any
) -> None:
    body = {"name": "shop-api", "repoUrl": "https://github.com/team-echo/shop-api", field: value}

    response = await harness.client.post(harness.services, json=body)

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


async def test_create_service_duplicate_name_returns_409(harness: DeployHarness) -> None:
    await harness.create_service("shop-api")

    response = await harness.client.post(
        harness.services, json={"name": "shop-api", "repoUrl": "https://github.com/team-echo/other"}
    )

    assert response.status_code == 409
    assert response.json()["code"] == "SERVICE_NAME_CONFLICT"


async def test_search_services_returns_page_in_created_order(harness: DeployHarness) -> None:
    await harness.create_service("shop-api")
    await harness.create_service("billing")

    response = await harness.client.get(harness.services, params={"size": 1, "page": 1})

    page = response.json()["data"]
    assert response.status_code == 200
    assert page["total"] == 2
    assert [item["name"] for item in page["items"]] == ["billing"]
    assert set(page["items"][0]) == SERVICE_KEYS


async def test_get_service_unknown_id_returns_404(harness: DeployHarness) -> None:
    response = await harness.client.get(f"{harness.services}/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404
    assert response.json()["code"] == "SERVICE_NOT_FOUND"


async def test_update_service_changes_fields_and_rechecks_repo(harness: DeployHarness) -> None:
    service = await harness.create_service("shop-api", description="old")

    response = await harness.client.patch(
        f"{harness.services}/{service['id']}",
        json={
            "name": "shop-api",
            "description": None,
            "repoUrl": "https://github.com/private-acme/shop-api",
            "healthCheckPath": "/ready",
            "healthCheckTimeoutSeconds": 60,
            "containerPort": 3000,
        },
    )

    updated = response.json()["data"]
    assert response.status_code == 200, response.text
    assert updated["description"] is None
    assert updated["repoUrl"] == "https://github.com/private-acme/shop-api"
    assert updated["private"] is True
    assert (updated["healthCheckPath"], updated["healthCheckTimeoutSeconds"]) == ("/ready", 60)
    assert updated["containerPort"] == 3000
    assert updated["url"] == service["url"]


async def test_update_service_name_change_returns_422(harness: DeployHarness) -> None:
    service = await harness.create_service("shop-api")

    response = await harness.client.patch(
        f"{harness.services}/{service['id']}", json={"name": "other"}
    )

    assert response.status_code == 422
    assert response.json()["code"] == "SERVICE_NAME_IMMUTABLE"


@pytest.mark.parametrize(
    "field", ["repoUrl", "healthCheckPath", "healthCheckTimeoutSeconds", "containerPort", "name"]
)
async def test_update_service_null_other_than_description_returns_422(
    harness: DeployHarness, field: str
) -> None:
    service = await harness.create_service("shop-api")

    response = await harness.client.patch(f"{harness.services}/{service['id']}", json={field: None})

    assert response.status_code == 422
    assert response.json()["details"] == [{"field": field, "reason": "NULL_NOT_ALLOWED"}]


async def test_update_service_inaccessible_new_repo_returns_404_and_keeps_old(
    harness: DeployHarness,
) -> None:
    service = await harness.create_service("shop-api")

    response = await harness.client.patch(
        f"{harness.services}/{service['id']}", json={"repoUrl": "https://github.com/denied-x/shop"}
    )

    assert response.status_code == 404
    assert (await harness.get_service(service["id"]))["repoUrl"] == service["repoUrl"]
