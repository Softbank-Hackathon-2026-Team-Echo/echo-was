"""권한 검사(SPEC api-6 AC-5, AC-9~AC-14): Origin, 역할 × 경로 표, 조직 격리, 교차 조직,
에이전트 토큰, 거부 기록, 환경 열쇠 이름 묶기."""

import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from httpx import AsyncClient, Response
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.auth.guards import AUTH_GUARDS, ORGANIZATION_GUARDS
from app.enums import ActorType, AuditResult, OrganizationRole
from app.main import app
from app.models import AuditLog, Organization
from tests.integration.auth_support import (
    Member,
    add_member,
    create_agent_token,
    create_member,
    create_organization,
    create_session,
    create_user,
    make_client,
    org_path,
)
from tests.integration.deploy_harness import DeployHarness
from tests.integration.factories import secret_ref_for

PUBLIC_PATHS = {
    ("GET", "/healthz"),
    ("GET", "/livez"),
    ("GET", "/readyz"),
    ("GET", "/api/v1/auth/github/login"),
    ("GET", "/api/v1/auth/github/callback"),
    ("POST", "/api/v1/auth/logout"),
}
ACTORS = ("anonymous", "outsider", "viewer", "developer", "admin", "agent")
ORG_PREFIX = "/api/v1/orgs/{org_id}"


def iter_api_routes(routes: list[Any] | None = None) -> Iterator[APIRoute]:
    """앱에 등록된 모든 API 라우트(포함된 라우터 안쪽까지)."""
    for route in app.routes if routes is None else routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            yield from iter_api_routes(route.original_router.routes)


def _dependency_calls(dependant: Dependant) -> Iterator[object]:
    for dependency in dependant.dependencies:
        yield dependency.call
        yield from _dependency_calls(dependency)


def _endpoints() -> set[tuple[str, str]]:
    return {(method, route.path) for route in iter_api_routes() for method in route.methods}


# --- 역할 × 경로 표(AC-9) ---
# 기대값: 401·403·404(조직 없음으로 숨김) 또는 ok(검사를 통과함. 리소스 404·검증 422·2xx·409 등).
_OK = "ok"


def _org_rule(min_role: OrganizationRole, *, human_only: bool, method: str) -> dict[str, str]:
    ranks = {
        "viewer": OrganizationRole.VIEWER,
        "developer": OrganizationRole.DEVELOPER,
        "admin": OrganizationRole.ADMIN,
    }
    expected = {"anonymous": "401", "outsider": "404"}
    for actor, role in ranks.items():
        expected[actor] = _OK if role.includes(min_role) else "403"
    agent_ok = method == "GET" and not human_only and min_role is OrganizationRole.VIEWER
    expected["agent"] = _OK if agent_ok else "403"
    return expected


HUMAN_ANYWHERE = {actor: _OK for actor in ACTORS} | {"anonymous": "401", "agent": "403"}
LOGGED_IN_ANYWHERE = {actor: _OK for actor in ACTORS} | {"anonymous": "401"}
RUNNER_ONLY = {actor: "403" for actor in ACTORS} | {"anonymous": "401"}
V, D, A = OrganizationRole.VIEWER, OrganizationRole.DEVELOPER, OrganizationRole.ADMIN


def _org(method: str, suffix: str, role: OrganizationRole, human_only: bool = False) -> Any:
    return (method, f"{ORG_PREFIX}{suffix}", _org_rule(role, human_only=human_only, method=method))


ROLE_TABLE: list[tuple[str, str, dict[str, str]]] = [
    ("GET", "/api/v1/me", HUMAN_ANYWHERE),
    ("POST", "/api/v1/orgs", HUMAN_ANYWHERE),
    ("GET", "/api/v1/orgs", HUMAN_ANYWHERE),
    ("GET", "/api/v1/environment-kinds", LOGGED_IN_ANYWHERE),
    _org("GET", "", V),
    _org("GET", "/audit-logs", V),
    _org("GET", "/members", V, human_only=True),
    _org("PATCH", "/members/{user_id}", A, human_only=True),
    # 본인 탈퇴는 누구나. 다른 사람(여기서는 없는 id)은 어드민만.
    # 검사기는 뷰어로 걸고 서비스가 잠금 안에서 다시 본다.
    ("DELETE", f"{ORG_PREFIX}/members/{{user_id}}", _org_rule(A, human_only=True, method="DELETE")),
    _org("POST", "/invitations", A, human_only=True),
    _org("GET", "/invitations", A, human_only=True),
    _org("DELETE", "/invitations/{invitation_id}", A, human_only=True),
    _org("POST", "/agent-tokens", D, human_only=True),
    _org("GET", "/agent-tokens", D, human_only=True),
    _org("DELETE", "/agent-tokens/{token_id}", D, human_only=True),
    _org("GET", "/environments", V),
    _org("GET", "/environments/{environment_id}", V),
    _org("POST", "/environments", D),
    _org("PATCH", "/environments/{environment_id}", D),
    _org("POST", "/environments/{environment_id}/connection-check", D),
    _org("DELETE", "/environments/{environment_id}", A),
    _org("GET", "/services", V),
    _org("GET", "/services/{service_id}", V),
    _org("GET", "/services/{service_id}/metrics", V),
    _org("GET", "/services/{service_id}/deploy-requests", V),
    _org("GET", "/deploy-requests/{deploy_request_id}", V),
    _org("GET", "/deploy-requests/{deploy_request_id}/logs", V),
    # 되돌리기는 배포 요청 접수와 같은 검사기(개발자, 에이전트는 쓰기라 403).
    _org("POST", "/deploy-requests/{deploy_request_id}/rollback", D),
    # 배포 실패 AI 진단 만들기(이슈 #98)는 사전 점검 만들기와 같은 검사기(개발자 이상 사람).
    _org("POST", "/deploy-requests/{deploy_request_id}/diagnosis", D, human_only=True),
    _org("POST", "/services", D),
    _org("PATCH", "/services/{service_id}", D),
    _org("POST", "/services/{service_id}/deploy-requests", D),
    _org("DELETE", "/services/{service_id}", A),
    # --- API 서버 8: 이전 계획(조회는 뷰어·에이전트, 바꾸는 일은 개발자 이상 사람) ---
    _org("GET", "/services/{service_id}/migration-plans", V),
    _org("GET", "/migration-plans/{plan_id}", V),
    _org("GET", "/migration-plans/{plan_id}/logs", V),
    _org("GET", "/security/matrix", V),
    _org("GET", "/security/findings", V),
    _org("GET", "/security/compare", V),
    _org("GET", "/costs", V),
    _org("POST", "/services/{service_id}/migration-plans", D, human_only=True),
    _org("POST", "/migration-plans/{plan_id}/approve", D, human_only=True),
    _org("POST", "/migration-plans/{plan_id}/reject", D, human_only=True),
    _org("POST", "/migration-plans/{plan_id}/cancel", D, human_only=True),
    _org("POST", "/migration-plans/{plan_id}/abort", D, human_only=True),
    # API 서버 10: 앱 환경 변수(목록은 뷰어·에이전트, 넣기는 개발자 사람, 삭제는 어드민 사람)
    _org("GET", "/services/{service_id}/environments/{environment_id}/variables", V),
    _org(
        "PUT",
        "/services/{service_id}/environments/{environment_id}/variables/{key}",
        D,
        human_only=True,
    ),
    _org(
        "DELETE",
        "/services/{service_id}/environments/{environment_id}/variables/{key}",
        A,
        human_only=True,
    ),
    # 이슈 #99: 변수 복사는 넣기와 같은 검사기(개발자 사람).
    _org("POST", "/services/{service_id}/variables:copy", D, human_only=True),
    # --- 보안 3: 기한부 예외(조회는 뷰어·에이전트, 요청·승인·철회는 개발자 이상 사람) ---
    _org("GET", "/security/exceptions", V),
    _org("GET", "/security/exceptions/{exception_id}", V),
    _org("POST", "/security/exceptions", D, human_only=True),
    _org("POST", "/security/exceptions/{exception_id}/approve", D, human_only=True),
    _org("POST", "/security/exceptions/{exception_id}/revoke", D, human_only=True),
    # CVE 감시(SPEC sec-6-cve-watch): 조회·주입·진단 요청은 개발자 이상 사람.
    _org("POST", "/security/cve-watch:check", D, human_only=True),
    _org("POST", "/security/cve-watch:inject", D, human_only=True),
    _org("POST", "/security/findings/{finding_id}/diagnosis", D, human_only=True),
    # --- AI 러너 레인 A: 사전 점검(조회는 뷰어·에이전트, 만들기·취소는 개발자 이상 사람) ---
    _org("GET", "/prechecks/{precheck_id}", V),
    _org("GET", "/prechecks/{precheck_id}/logs", V),
    _org("POST", "/prechecks", D, human_only=True),
    _org("POST", "/prechecks/{precheck_id}/cancel", D, human_only=True),
    _org("POST", "/prechecks/{precheck_id}/patches/{patch_id}/approve", D, human_only=True),
    _org("POST", "/prechecks/{precheck_id}/patches/{patch_id}/reject", D, human_only=True),
    # --- AI 3: 데이터 레지던시 요약(읽기만이라 뷰어·에이전트) ---
    _org("GET", "/ai/residency", V),
    # --- API 서버 9: 재난(조회·미리보기는 뷰어·에이전트, 만들기·종료는 개발자 이상 사람) ---
    _org("GET", "/disasters/preview", V),
    _org("GET", "/disasters", V),
    _org("GET", "/disasters/{disaster_id}", V),
    _org("POST", "/disasters", D, human_only=True),
    _org("POST", "/disasters/{disaster_id}/end", D, human_only=True),
    _org("GET", "/disasters/feed", V),
    _org("POST", "/disasters/feed:inject", D, human_only=True),
    _org("POST", "/disasters/{disaster_id}/evacuate", D, human_only=True),
    # 자기 승인 스위치는 어드민(사람)만 바꾼다.
    _org("PATCH", "/settings", A, human_only=True),
    # 시연 조직 스냅샷·복귀는 어드민만(만들기·복귀는 사람만).
    _org("POST", "/demo/snapshot", A, human_only=True),
    _org("GET", "/demo/snapshot", A),
    _org("POST", "/demo/restore", A, human_only=True),
    # 러너 경로는 러너·실행 토큰만 받는다(사람 쿠키·에이전트 토큰은 403).
    ("POST", "/api/v1/runner/runs:claim", RUNNER_ONLY),
    ("POST", "/api/v1/runner/runs/{run_id}/heartbeat", RUNNER_ONLY),
    ("POST", "/api/v1/runner/runs/{run_id}/events", RUNNER_ONLY),
    ("POST", "/api/v1/runner/runs/{run_id}/result", RUNNER_ONLY),
]


@dataclass
class Actors:
    organization: Organization
    other_organization: Organization
    clients: dict[str, AsyncClient]
    members: dict[str, Member]
    agent_token: str


@pytest.fixture
async def actors(harness: DeployHarness) -> Any:
    team = harness.organization
    now = harness.clock.now()
    other = await create_organization(harness.session_factory, "other-org")
    outsider = await create_member(harness.session_factory, other, OrganizationRole.ADMIN, now)
    viewer = await create_member(harness.session_factory, team, OrganizationRole.VIEWER, now)
    developer = await create_member(harness.session_factory, team, OrganizationRole.DEVELOPER, now)
    _, agent_token = await create_agent_token(
        harness.session_factory, team, harness.admin.user, now
    )
    members = {
        "outsider": outsider,
        "viewer": viewer,
        "developer": developer,
        "admin": harness.admin,
    }
    clients = {
        "anonymous": make_client(),
        **{name: make_client(session_token=m.session_token) for name, m in members.items()},
        "agent": make_client(agent_token=agent_token),
    }
    try:
        yield Actors(team, other, clients, members, agent_token)
    finally:
        for client in clients.values():
            await client.aclose()


def _fill(path: str, organization_id: uuid.UUID) -> str:
    path = path.replace("{org_id}", str(organization_id))
    return re.sub(r"\{[a-z_]+\}", lambda _: str(uuid.uuid4()), path)


def _outcome(response: Response) -> str:
    code = response.json().get("code") if response.content else None
    if response.status_code == 401:
        return "401"
    if response.status_code == 403:
        return "403"
    if response.status_code == 404 and code == "ORGANIZATION_NOT_FOUND":
        return "404"
    return _OK


def test_role_table_covers_every_api_route() -> None:
    table = {(method, path) for method, path, _ in ROLE_TABLE}

    assert table == _endpoints() - PUBLIC_PATHS


@pytest.mark.parametrize(
    ("method", "path", "expected"), ROLE_TABLE, ids=[f"{m} {p}" for m, p, _ in ROLE_TABLE]
)
async def test_role_table(
    harness: DeployHarness, actors: Actors, method: str, path: str, expected: dict[str, str]
) -> None:
    url = _fill(path, actors.organization.id)
    outcomes = {}
    for actor in ACTORS:
        # 본문은 일부러 비운다. 검사는 본문 검증보다 먼저라 권한 없는 쪽은 401·403·404 다.
        response = await actors.clients[actor].request(method, url, json={})
        outcomes[actor] = _outcome(response)

    assert outcomes == expected


# --- 조직 격리·검사기 빠짐 방지(AC-10) ---


def test_every_route_has_guards() -> None:
    missing_org_guard = []
    missing_auth_guard = []
    for route in iter_api_routes():
        calls = set(_dependency_calls(route.dependant))
        if route.path.startswith(f"{ORG_PREFIX}") and not calls & ORGANIZATION_GUARDS:
            missing_org_guard.append(route.path)
        public = all((method, route.path) in PUBLIC_PATHS for method in route.methods)
        if not public and not calls & AUTH_GUARDS:
            missing_auth_guard.append(route.path)

    assert missing_org_guard == []
    assert missing_auth_guard == []


async def test_every_org_route_hides_other_organizations_and_needs_login(
    harness: DeployHarness, actors: Actors
) -> None:
    hidden = {}
    anonymous = {}
    for method, path in sorted(_endpoints() - PUBLIC_PATHS):
        if path.startswith(ORG_PREFIX):
            # 남의 조직(team-echo)을 그 조직 멤버가 아닌 사람이 부른다.
            response = await actors.clients["outsider"].request(
                method, _fill(path, actors.organization.id), json={}
            )
            hidden[f"{method} {path}"] = (response.status_code, response.json()["code"])
        response = await actors.clients["anonymous"].request(
            method, _fill(path, actors.organization.id), json={}
        )
        anonymous[f"{method} {path}"] = response.status_code

    assert hidden
    assert set(hidden.values()) == {(404, "ORGANIZATION_NOT_FOUND")}
    assert set(anonymous.values()) == {401}


@pytest.mark.parametrize(
    "org_id", ["not-a-uuid", str(uuid.uuid4())], ids=["not-a-uuid", "unknown-uuid"]
)
async def test_malformed_or_unknown_org_id_is_not_found(
    harness: DeployHarness, org_id: str
) -> None:
    response = await harness.client.get(f"/api/v1/orgs/{org_id}/services")

    assert (response.status_code, response.json()["code"]) == (404, "ORGANIZATION_NOT_FOUND")


# --- Origin(AC-5) ---


async def _audit_rows(harness: DeployHarness) -> list[AuditLog]:
    """거부 줄만(성공 줄·에이전트 읽기 줄은 test_audit_logs.py 가 본다)."""
    async with harness.session_factory() as session:
        return list(
            (
                await session.scalars(
                    select(AuditLog)
                    .where(AuditLog.result == AuditResult.DENIED)
                    .order_by(AuditLog.created_at, AuditLog.seq)
                )
            ).all()
        )


async def test_cookie_write_requires_allowed_origin(harness: DeployHarness, actors: Actors) -> None:
    token = harness.admin.session_token
    body = {"name": "shop-api", "repoUrl": "https://github.com/team-echo/shop-api"}
    async with make_client(session_token=token, origin="https://evil.test") as evil:
        evil_post = await evil.post(harness.services, json=body)
        evil_org = await evil.post("/api/v1/orgs", json={"slug": "evil", "name": "e"})
        evil_get = await evil.get(harness.services)
    async with make_client(session_token=token, origin=None) as no_origin:
        missing = await no_origin.post(harness.services, json=body)
    allowed = await harness.client.post(harness.services, json=body)
    async with make_client(agent_token=actors.agent_token, origin="https://evil.test") as agent:
        agent_get = await agent.get(harness.services)

    assert (evil_post.status_code, evil_post.json()["code"]) == (403, "ORIGIN_NOT_ALLOWED")
    assert (evil_org.status_code, evil_org.json()["code"]) == (403, "ORIGIN_NOT_ALLOWED")
    assert (missing.status_code, missing.json()["code"]) == (403, "ORIGIN_NOT_ALLOWED")
    assert evil_get.status_code == 200
    assert allowed.status_code == 201
    assert agent_get.status_code == 200
    rows = await _audit_rows(harness)
    assert [(row.reason, row.organization_id) for row in rows] == [
        ("ORIGIN_NOT_ALLOWED", harness.organization.id),
        ("ORIGIN_NOT_ALLOWED", None),
        ("ORIGIN_NOT_ALLOWED", harness.organization.id),
    ]


# --- 교차 조직(AC-11) ---


async def test_cross_organization_resources_are_not_found(harness: DeployHarness) -> None:
    other = await create_organization(harness.session_factory, "acme")
    other_admin = await create_member(
        harness.session_factory, other, OrganizationRole.ADMIN, harness.clock.now()
    )
    team_env = await harness.add_environment("aws-seoul")
    other_env = await harness.add_environment("aws-seoul", organization=other)  # 같은 이름 가능
    service = await harness.create_service("shop-api")
    accepted = await harness.deploy(service["id"], team_env.id)

    cross_deploy = await harness.request_deploy(service["id"], other_env.id)
    async with make_client(session_token=other_admin.session_token) as client:
        other_paths = org_path(other.id)
        cross_request = await client.get(f"{other_paths}/deploy-requests/{accepted['id']}")
        cross_service = await client.get(f"{other_paths}/services/{service['id']}")
        cross_env = await client.get(f"{other_paths}/environments/{team_env.id}")
        same_service_name = await client.post(
            f"{other_paths}/services",
            json={"name": "shop-api", "repoUrl": "https://github.com/acme/shop-api"},
        )
        listed = await client.get(f"{other_paths}/services")

    assert (cross_deploy.status_code, cross_deploy.json()["code"]) == (
        404,
        "ENVIRONMENT_NOT_FOUND",
    )
    assert cross_request.json()["code"] == "DEPLOY_REQUEST_NOT_FOUND"
    assert cross_service.json()["code"] == "SERVICE_NOT_FOUND"
    assert cross_env.json()["code"] == "ENVIRONMENT_NOT_FOUND"
    # 서비스 이름은 조직 안에서만 유일하다(API 서버 3-1). 주소는 suffix 로 갈린다.
    assert same_service_name.status_code == 201, same_service_name.text
    assert same_service_name.json()["data"]["url"] != service["url"]
    # 다른 조직의 목록에는 그 조직이 만든 서비스만 보인다.
    assert [item["id"] for item in listed.json()["data"]["items"]] == [
        same_service_name.json()["data"]["id"]
    ]


async def test_same_environment_name_in_two_organizations_via_api(
    harness: DeployHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = await create_organization(harness.session_factory, "acme")
    other_admin = await create_member(
        harness.session_factory, other, OrganizationRole.ADMIN, harness.clock.now()
    )
    body = {"name": "aws-seoul", "kind": "aws", "config": {"region": "ap-northeast-2"}}
    config = {"region": "ap-northeast-2", "clusterName": "eks"}

    team = await harness.client.post(
        harness.environments, json=body | {"config": config, "secretRef": secret_ref_for("x")}
    )
    async with make_client(session_token=other_admin.session_token) as client:
        acme = await client.post(
            org_path(other.id, "/environments"),
            json=body | {"config": config, "secretRef": secret_ref_for("x", "acme")},
        )
    duplicate = await harness.client.post(
        harness.environments, json=body | {"config": config, "secretRef": secret_ref_for("x")}
    )

    assert (team.status_code, acme.status_code) == (201, 201)
    assert duplicate.json()["code"] == "ENVIRONMENT_NAME_CONFLICT"


# --- 에이전트 토큰(AC-12) ---


async def test_agent_token_lifecycle_and_read_only(harness: DeployHarness) -> None:
    developer = await create_member(
        harness.session_factory,
        harness.organization,
        OrganizationRole.DEVELOPER,
        harness.clock.now(),
    )
    other = await create_organization(harness.session_factory, "acme")
    tokens_path = org_path(harness.organization.id, "/agent-tokens")
    environment = await harness.add_environment("aws-seoul")
    service = await harness.create_service("shop-api")

    async with make_client(session_token=developer.session_token) as dev:
        issued = await dev.post(tokens_path, json={"name": "triage-bot", "expiresInDays": 7})
        listed = await dev.get(tokens_path)
        too_long = await dev.post(tokens_path, json={"name": "x", "expiresInDays": 91})
    token = issued.json()["data"]["token"]

    async with make_client(agent_token=token) as agent:
        read = await agent.get(harness.services)
        read_org = await agent.get(org_path(harness.organization.id))
        other_org = await agent.get(org_path(other.id, "/services"))
        deploy = await agent.post(
            f"{harness.services}/{service['id']}/deploy-requests",
            json={"environmentId": str(environment.id), "branch": "main"},
        )
        members = await agent.get(org_path(harness.organization.id, "/members"))
        invitations = await agent.get(org_path(harness.organization.id, "/invitations"))
        agent_tokens = await agent.get(tokens_path)
        me = await agent.get("/api/v1/me")
        kinds = await agent.get("/api/v1/environment-kinds")

    data = issued.json()["data"]
    assert issued.status_code == 201
    assert token.startswith("echo_agt_") and data["last4"] == token[-4:]
    assert listed.status_code == 200
    assert "token" not in listed.json()["data"]["items"][0]
    assert token not in listed.text
    assert listed.json()["data"]["items"][0]["last4"] == token[-4:]
    assert too_long.status_code == 422
    assert read.status_code == 200
    assert read_org.json()["data"]["role"] is None
    assert (other_org.status_code, other_org.json()["code"]) == (404, "ORGANIZATION_NOT_FOUND")
    assert (deploy.status_code, deploy.json()["code"]) == (403, "AGENT_READ_ONLY")
    for denied in (members, invitations, agent_tokens, me):
        assert (denied.status_code, denied.json()["code"]) == (403, "AGENT_READ_ONLY")
    assert kinds.status_code == 200

    rows = await _audit_rows(harness)
    assert rows[0].actor_type == ActorType.AGENT
    assert rows[0].actor_agent_token_id == uuid.UUID(data["id"])
    assert rows[0].action == "DEPLOY_REQUESTED"
    assert rows[0].route == "POST /api/v1/orgs/{org_id}/services/{service_id}/deploy-requests"
    assert [row.reason for row in rows] == ["AGENT_READ_ONLY"] * 5


async def test_agent_token_revoked_or_expired_is_unauthenticated(harness: DeployHarness) -> None:
    token_row, token = await create_agent_token(
        harness.session_factory, harness.organization, harness.admin.user, harness.clock.now()
    )
    _, short_token = await create_agent_token(
        harness.session_factory,
        harness.organization,
        harness.admin.user,
        harness.clock.now(),
        days=1,
    )
    tokens_path = org_path(harness.organization.id, "/agent-tokens")

    revoked = await harness.client.delete(f"{tokens_path}/{token_row.id}")
    revoked_again = await harness.client.delete(f"{tokens_path}/{token_row.id}")
    async with make_client(agent_token=token) as agent:
        after_revoke = await agent.get(harness.services)
    harness.clock.advance(24 * 60 * 60)
    async with make_client(agent_token=short_token) as agent:
        after_expiry = await agent.get(harness.services)
    async with make_client(agent_token="echo_agt_not-a-real-token") as agent:
        unknown = await agent.get(harness.services)
    async with make_client(agent_token="something-else") as agent:
        wrong_prefix = await agent.get(harness.services)

    assert (revoked.status_code, revoked_again.status_code) == (204, 204)
    for response in (after_revoke, after_expiry, unknown, wrong_prefix):
        assert (response.status_code, response.json()["code"]) == (401, "UNAUTHENTICATED")


async def test_bearer_takes_precedence_over_cookie(harness: DeployHarness) -> None:
    _, token = await create_agent_token(
        harness.session_factory, harness.organization, harness.admin.user, harness.clock.now()
    )
    async with make_client(session_token=harness.admin.session_token, agent_token=token) as both:
        response = await both.post(harness.services, json={})

    assert (response.status_code, response.json()["code"]) == (403, "AGENT_READ_ONLY")


async def test_agent_token_survives_creator_leaving(harness: DeployHarness) -> None:
    developer = await create_member(
        harness.session_factory,
        harness.organization,
        OrganizationRole.DEVELOPER,
        harness.clock.now(),
    )
    _, token = await create_agent_token(
        harness.session_factory, harness.organization, developer.user, harness.clock.now()
    )
    async with make_client(session_token=developer.session_token) as dev:
        await dev.delete(org_path(harness.organization.id, f"/members/{developer.user.id}"))
        gone = await dev.get(harness.services)
    async with make_client(agent_token=token) as agent:
        still = await agent.get(harness.services)

    assert gone.json()["code"] == "ORGANIZATION_NOT_FOUND"
    assert still.status_code == 200


# --- 거부 기록(AC-13) ---


async def test_denials_are_recorded_once_with_context_and_not_401_404(
    harness: DeployHarness, actors: Actors
) -> None:
    viewer = actors.clients["viewer"]
    service_path = f"{harness.services}/{uuid.uuid4()}"

    denied = await viewer.delete(service_path, headers={"X-Request-ID": "req-denied-1"})
    await actors.clients["anonymous"].get(harness.services)
    await actors.clients["outsider"].get(harness.services)
    own_role = await harness.client.patch(
        org_path(harness.organization.id, f"/members/{harness.admin.user.id}"),
        json={"role": "VIEWER"},
    )

    rows = await _audit_rows(harness)
    assert denied.status_code == 403 and own_role.status_code == 403
    assert len(rows) == 2
    first, second = rows
    assert (first.organization_id, first.actor_type, first.actor_user_id) == (
        harness.organization.id,
        ActorType.USER,
        actors.members["viewer"].user.id,
    )
    assert first.action == "SERVICE_DELETE_REQUESTED"
    assert first.route == "DELETE /api/v1/orgs/{org_id}/services/{service_id}"
    assert first.path == service_path
    assert (first.reason, first.result, first.request_id) == (
        "ROLE_FORBIDDEN",
        "DENIED",
        "req-denied-1",
    )
    assert first.created_at == harness.clock.now()
    assert (second.reason, second.actor_user_id) == (
        "CANNOT_CHANGE_OWN_ROLE",
        harness.admin.user.id,
    )
    assert second.request_id == own_role.headers["x-request-id"]


async def test_audit_logs_cannot_be_deleted(harness: DeployHarness, actors: Actors) -> None:
    await actors.clients["viewer"].post(harness.services, json={})

    async with harness.session_factory() as session:
        with pytest.raises(DBAPIError, match="audit_logs rows are append-only"):
            await session.execute(text("DELETE FROM audit_logs"))
    async with harness.session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(AuditLog))
    assert count == 1


# --- 환경 열쇠 이름 묶기(AC-14) ---


async def test_env_secret_ref_must_use_organization_key(
    harness: DeployHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ECHO_SECRET_TEAM_ECHO__X", "kubeconfig-value")
    acme = await create_organization(harness.session_factory, "a-corp")
    acme_user = await create_user(harness.session_factory, "acme-admin")
    await add_member(harness.session_factory, acme, acme_user, OrganizationRole.ADMIN)
    acme_token = await create_session(harness.session_factory, acme_user, harness.clock.now())
    body = {"kind": "aws", "config": {"region": "ap-northeast-2", "clusterName": "eks"}}

    ok = await harness.client.post(
        harness.environments,
        json=body | {"name": "aws-a", "secretRef": "env:ECHO_SECRET_TEAM_ECHO__X"},
    )
    plain = await harness.client.post(
        harness.environments, json=body | {"name": "aws-b", "secretRef": "env:ECHO_SECRET_X"}
    )
    bare_prefix = await harness.client.post(
        harness.environments,
        json=body | {"name": "aws-c", "secretRef": "env:ECHO_SECRET_TEAM_ECHO__"},
    )
    k8s = await harness.client.post(
        harness.environments, json=body | {"name": "aws-d", "secretRef": "k8s:echo/kube/config"}
    )
    patched = await harness.client.patch(
        f"{harness.environments}/{ok.json()['data']['id']}",
        json={"secretRef": "env:ECHO_SECRET_A_CORP__X"},
    )
    async with make_client(session_token=acme_token) as client:
        prefix_collision = await client.post(
            org_path(acme.id, "/environments"),
            json=body | {"name": "aws-a", "secretRef": "env:ECHO_SECRET_A_CORP_X__Y"},
        )
        own = await client.post(
            org_path(acme.id, "/environments"),
            json=body | {"name": "aws-b", "secretRef": "env:ECHO_SECRET_A_CORP__Y"},
        )

    assert ok.status_code == 201
    assert ok.json()["data"]["connectionStatus"] == "CONNECTED"
    for response in (plain, bare_prefix, patched, prefix_collision):
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "SECRET_REF_NOT_IN_ORGANIZATION"
        assert response.json()["details"] == [
            {"field": "secretRef", "reason": "SECRET_REF_NOT_IN_ORGANIZATION"}
        ]
    assert k8s.status_code == 201
    assert own.status_code == 201
