"""고객 앱 주소 형식(SPEC api-3-1 AC-1~AC-5). 주소 = 이름 + 무작위 8자, 도메인 바로 아래 한 단계."""

import re
import uuid
from typing import Any

from sqlalchemy import select

from app.enums import OrganizationRole
from app.models import Service
from tests.integration.auth_support import create_member, create_organization, make_client, org_path
from tests.integration.deploy_harness import DeployHarness, connection_of

DOMAIN = re.escape("apps.echo.test")
TAKEN = "taken000"
FRESH = "fresh111"


async def _stored_service(harness: DeployHarness, service_id: str) -> Service:
    async with harness.session_factory() as session:
        return await session.get_one(Service, uuid.UUID(service_id))


def _label(url: str) -> str:
    host = url.removeprefix("https://")
    assert host.endswith(".apps.echo.test"), url
    return host.removesuffix(".apps.echo.test")


# --- AC-1 대표 주소 ---


async def test_create_service_main_url_has_random_suffix_matching_db(
    harness: DeployHarness,
) -> None:
    service = await harness.create_service("shop")

    stored = await _stored_service(harness, service["id"])
    match = re.fullmatch(rf"https://shop-([a-z0-9]{{8}})\.{DOMAIN}", service["url"])
    assert match is not None, service["url"]
    assert match.group(1) == stored.host_suffix
    assert stored.public_host == f"shop-{stored.host_suffix}.apps.echo.test"


async def test_update_service_keeps_host_suffix_and_url(harness: DeployHarness) -> None:
    service = await harness.create_service("shop")
    before = await _stored_service(harness, service["id"])

    response = await harness.client.patch(
        f"{harness.services}/{service['id']}", json={"containerPort": 3000}
    )

    after = await _stored_service(harness, service["id"])
    assert response.status_code == 200, response.text
    assert response.json()["data"]["url"] == service["url"]
    assert after.host_suffix == before.host_suffix


# --- AC-2 환경별 주소 ---


async def test_deploy_environment_url_uses_service_suffix(harness: DeployHarness) -> None:
    service = await harness.create_service("shop")
    aws = await harness.add_environment("aws-seoul")

    accepted = await harness.deploy(service["id"], aws.id)
    await harness.run_until_idle()
    finished = await harness.get_request(accepted["id"])
    detail = await harness.get_service(service["id"])

    suffix = (await _stored_service(harness, service["id"])).host_suffix
    expected = f"https://shop-aws-seoul-{suffix}.apps.echo.test"
    assert finished["urls"] == {"main": service["url"], "environment": expected}
    assert connection_of(detail, "aws-seoul")["url"] == expected


# --- AC-3 63자 자르기 ---


async def test_deploy_63_char_service_and_environment_names_keep_label_within_63(
    harness: DeployHarness,
) -> None:
    service_name = "s" * 62 + "1"
    environment_name = "e" * 62 + "2"
    service = await harness.create_service(service_name)
    environment = await harness.add_environment(environment_name)

    await harness.deploy(service["id"], environment.id)
    detail = await harness.get_service(service["id"])

    suffix = (await _stored_service(harness, service["id"])).host_suffix
    main_label = _label(service["url"])
    environment_label = _label(connection_of(detail, environment_name)["url"])
    for label in (main_label, environment_label):
        assert len(label) <= 63
        assert label.endswith(f"-{suffix}")
        assert label == f"{service_name[:54]}-{suffix}"


# --- AC-4 suffix 충돌 재시도 ---


async def test_create_service_retries_taken_suffix_then_succeeds(harness: DeployHarness) -> None:
    harness.host_suffixes.queue(TAKEN)
    await harness.create_service("shop")
    other = await create_organization(harness.session_factory, "acme")
    other_admin = await create_member(
        harness.session_factory, other, OrganizationRole.ADMIN, harness.clock.now()
    )
    harness.host_suffixes.queue(TAKEN, TAKEN, FRESH)
    calls_before = harness.host_suffixes.calls

    async with make_client(session_token=other_admin.session_token) as client:
        response = await client.post(org_path(other.id, "/services"), json=_body("shop"))

    assert response.status_code == 201, response.text
    assert response.json()["data"]["url"] == f"https://shop-{FRESH}.apps.echo.test"
    assert harness.host_suffixes.calls - calls_before == 3


async def test_create_service_suffix_keeps_colliding_returns_409(harness: DeployHarness) -> None:
    harness.host_suffixes.queue(TAKEN)
    await harness.create_service("shop")
    other = await create_organization(harness.session_factory, "acme")
    other_admin = await create_member(
        harness.session_factory, other, OrganizationRole.ADMIN, harness.clock.now()
    )
    harness.host_suffixes.queue(*[TAKEN] * 10)
    calls_before = harness.host_suffixes.calls

    async with make_client(session_token=other_admin.session_token) as client:
        response = await client.post(org_path(other.id, "/services"), json=_body("shop"))

    assert (response.status_code, response.json()["code"]) == (409, "PUBLIC_HOST_CONFLICT")
    # 첫 시도 + 다시 시도 3번.
    assert harness.host_suffixes.calls - calls_before == 4
    assert await _count_services(harness, "shop") == 1


async def test_create_service_suffix_taken_by_environment_url_retries(
    harness: DeployHarness,
) -> None:
    # shop 의 aws 연결 주소 `shop-aws-{TAKEN}` 은 새 서비스 shop-aws 의 대표 주소와 같다.
    harness.host_suffixes.queue(TAKEN)
    shop = await harness.create_service("shop")
    aws = await harness.add_environment("aws")
    await harness.deploy(shop["id"], aws.id)
    harness.host_suffixes.queue(TAKEN, FRESH)

    created = await harness.create_service("shop-aws")

    assert created["url"] == f"https://shop-aws-{FRESH}.apps.echo.test"


# --- AC-5 조직 안에서만 유일한 이름 ---


async def test_same_service_name_in_two_organizations_both_201_with_different_urls(
    harness: DeployHarness,
) -> None:
    other = await create_organization(harness.session_factory, "acme")
    other_admin = await create_member(
        harness.session_factory, other, OrganizationRole.ADMIN, harness.clock.now()
    )

    team = await harness.client.post(harness.services, json=_body("shop"))
    async with make_client(session_token=other_admin.session_token) as client:
        acme = await client.post(org_path(other.id, "/services"), json=_body("shop"))

    assert (team.status_code, acme.status_code) == (201, 201)
    assert team.json()["data"]["url"] != acme.json()["data"]["url"]
    assert await _count_services(harness, "shop") == 2


async def test_same_service_name_in_same_organization_returns_409(harness: DeployHarness) -> None:
    await harness.create_service("shop")

    response = await harness.client.post(harness.services, json=_body("shop"))

    assert (response.status_code, response.json()["code"]) == (409, "SERVICE_NAME_CONFLICT")


async def test_recreate_deleted_service_name_gets_new_main_url(harness: DeployHarness) -> None:
    first = await harness.create_service("shop")
    deleted = await harness.client.delete(f"{harness.services}/{first['id']}")
    assert deleted.status_code == 202, deleted.text
    await harness.run_until_idle()

    second = await harness.create_service("shop")

    assert second["id"] != first["id"]
    assert second["url"] != first["url"]
    assert re.fullmatch(rf"https://shop-[a-z0-9]{{8}}\.{DOMAIN}", second["url"])
    assert (await _stored_service(harness, first["id"])).is_deleted is True


def _body(name: str) -> dict[str, Any]:
    return {"name": name, "repoUrl": f"https://github.com/team-echo/{name}"}


async def _count_services(harness: DeployHarness, name: str) -> int:
    async with harness.session_factory() as session:
        return len((await session.scalars(select(Service.id).where(Service.name == name))).all())
