"""재난 통합 시험 도구.

tokyo_serving_seoul_standby(): shop-api 를 aws-tokyo(ap-northeast-1)에서 서비스(100%)하고 gcp-seoul
(asia-northeast3)에 같은 커밋을 올린 예비(0%)를 둔다. near Tokyo M7.2 를 내면 aws-tokyo 만 피해다.
반환하는 Migration 의 aws·gcp 는 이 두 환경이고, 만든 사람은 개발자 A, 승인은 개발자 B 다.
"""

from typing import Any

from httpx import AsyncClient, Response

from app.enums import ConnectionErrorCode, OrganizationRole
from app.models import Environment
from tests.integration.auth_support import create_member, make_client, org_path
from tests.integration.deploy_harness import DeployHarness
from tests.integration.factories import secret_ref_for
from tests.integration.migration_support import Migration
from tests.integration.rollback_support import deploy_and_finish

NEAR_TOKYO = {"lat": 35.2, "lng": 139.4}
NEAR_SEOUL = {"lat": 37.5, "lng": 127.0}
# 어느 환경과도 멀다(태평양 한가운데).
MID_PACIFIC = {"lat": 0.0, "lng": -150.0}


def aws_config(region: str) -> dict[str, object]:
    return {"region": region, "cluster_name": "echo-eks"}


def gcp_config(region: str) -> dict[str, object]:
    return {"project_id": "echo-hack-123", "region": region, "cluster_name": "echo-gke"}


def onprem_config(location: str) -> dict[str, object]:
    return {"location": location, "kube_context": "k3d-echo"}


async def add_located_environment(
    harness: DeployHarness, name: str, kind: str, config: dict[str, object], *, failed: bool = False
) -> Environment:
    """위치를 고른 환경 행. failed=True 면 마지막 접속 확인이 실패한 상태다."""
    environment = Environment(
        organization_id=harness.organization.id,
        name=name,
        kind=kind,
        config=dict(config),
        secret_ref=secret_ref_for(name, harness.organization.slug),
    )
    environment.record_connection_check(
        harness.clock.now(), ConnectionErrorCode.AUTH_FAILED if failed else None
    )
    async with harness.session_factory() as session, session.begin():
        session.add(environment)
    return environment


async def tokyo_serving_seoul_standby(harness: DeployHarness) -> Migration:
    service = await harness.create_service("shop-api")
    tokyo = await add_located_environment(harness, "aws-tokyo", "aws", aws_config("ap-northeast-1"))
    seoul = await add_located_environment(
        harness, "gcp-seoul", "gcp", gcp_config("asia-northeast3")
    )
    await deploy_and_finish(harness, service["id"], tokyo.id, branch="main")
    await deploy_and_finish(harness, service["id"], seoul.id, branch="main")
    now = harness.clock.now()
    author = await create_member(
        harness.session_factory, harness.organization, OrganizationRole.DEVELOPER, now, "dev-a"
    )
    approver = await create_member(
        harness.session_factory, harness.organization, OrganizationRole.DEVELOPER, now, "dev-b"
    )
    return Migration(
        harness=harness,
        author=author,
        approver=approver,
        author_client=make_client(session_token=author.session_token),
        approver_client=make_client(session_token=approver.session_token),
        service=service,
        aws=tokyo,
        gcp=seoul,
    )


def disasters_path(harness: DeployHarness, suffix: str = "") -> str:
    return org_path(harness.organization.id, f"/disasters{suffix}")


def disaster_body(
    magnitude: float = 7.2, epicenter: dict[str, float] | None = None, type_: str = "EARTHQUAKE"
) -> dict[str, Any]:
    return {"type": type_, "epicenter": epicenter or NEAR_TOKYO, "magnitude": magnitude}


async def request_disaster(
    migration: Migration, client: AsyncClient | None = None, **body: Any
) -> Response:
    """재난 버튼. 기본은 개발자 A 가 near Tokyo M7.2 를 낸다."""
    response = await (client or migration.author_client).post(
        disasters_path(migration.harness), json=disaster_body(**body)
    )
    migration.harness.responses.append(response.text)
    return response


async def declare_disaster(migration: Migration, **body: Any) -> dict[str, Any]:
    response = await request_disaster(migration, **body)
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def allow_self_approval(harness: DeployHarness) -> None:
    response = await harness.client.patch(
        org_path(harness.organization.id, "/settings"), json={"allowSelfApproval": True}
    )
    assert response.status_code == 200, response.text


async def add_standby_service(
    migration: Migration, name: str, *, standby_commit: str | None = None
) -> str:
    """aws-tokyo 서비스 중 + gcp-seoul 예비인 서비스를 더한다. standby_commit 이면 다른 이미지다."""
    harness = migration.harness
    service = await harness.create_service(name)
    await deploy_and_finish(harness, service["id"], migration.aws.id, branch="main")
    ref: dict[str, str] = (
        {"commitSha": standby_commit} if standby_commit is not None else {"branch": "main"}
    )
    await deploy_and_finish(harness, service["id"], migration.gcp.id, **ref)
    return str(service["id"])
