"""GitHub 로그인·세션(SPEC api-6 AC-1~AC-4). 가짜 GitHub 는 의존성 교체로만 넣는다."""

import asyncio
from datetime import timedelta
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select, text

from app.clients.github_user import GithubUser
from app.enums import OrganizationRole
from app.models import Organization, OrganizationMember, User, UserSession
from tests.integration.auth_support import (
    FRONTEND_BASE_URL,
    REDIRECT_URI,
    SESSION_COOKIE,
    create_organization,
    make_client,
)
from tests.integration.conftest import TABLES
from tests.integration.deploy_harness import DeployHarness

LOGIN = "/api/v1/auth/github/login"
CALLBACK = "/api/v1/auth/github/callback"
STATE_COOKIE = "echo_oauth_state"
ALICE = GithubUser(id=1001, login="Alice-Dev", name="Alice", avatar_url="https://avatars.test/a")
# 시험 도구(harness)가 미리 넣은 행: 사용자 2(team-echo 만든 사람·어드민), 세션 1(어드민).
BASELINE = (2, 1)


def _set_cookies(response: Response) -> dict[str, SimpleCookie]:
    cookies: dict[str, SimpleCookie] = {}
    for header in response.headers.get_list("set-cookie"):
        parsed = SimpleCookie()
        parsed.load(header)
        for name in parsed:
            cookies[name] = parsed
    return cookies


async def _start(client: AsyncClient, next_path: str | None = "/dashboard") -> str:
    params = {"next": next_path} if next_path is not None else {}
    response = await client.get(LOGIN, params=params)
    assert response.status_code == 302, response.text
    query = parse_qs(urlsplit(response.headers["location"]).query)
    return query["state"][0]


async def _login(
    client: AsyncClient, code: str = "code-alice", next_path: str | None = "/dashboard"
) -> Response:
    state = await _start(client, next_path)
    return await client.get(CALLBACK, params={"code": code, "state": state})


async def _count(harness: DeployHarness, model: Any) -> int:
    async with harness.session_factory() as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


async def _personal_org(harness: DeployHarness, github_id: int) -> list[Organization]:
    async with harness.session_factory() as session:
        user = await session.scalar(select(User).where(User.github_id == github_id))
        assert user is not None
        return list(
            (
                await session.scalars(
                    select(Organization).where(
                        Organization.created_by_user_id == user.id, Organization.personal
                    )
                )
            ).all()
        )


# --- AC-1: 로그인 ---


async def test_login_redirects_to_github_with_state_cookie(harness: DeployHarness) -> None:
    async with make_client() as client:
        response = await client.get(LOGIN, params={"next": "/services"})

    location = urlsplit(response.headers["location"])
    query = parse_qs(location.query)
    cookie = _set_cookies(response)[STATE_COOKIE][STATE_COOKIE]
    assert response.status_code == 302
    assert f"{location.scheme}://{location.netloc}{location.path}" == (
        "https://github.com/login/oauth/authorize"
    )
    assert query["client_id"] == ["Iv1.test-client-id"]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert len(query["state"][0]) >= 43
    assert cookie["httponly"] is True
    assert cookie["samesite"] == "lax"
    assert cookie["max-age"] == "600"
    assert cookie["path"] == "/api/v1/auth/github"


async def test_callback_creates_user_personal_org_and_session(harness: DeployHarness) -> None:
    harness.github_users.add_user("code-alice", ALICE)

    async with make_client() as client:
        response = await _login(client)
        me = await client.get("/api/v1/me")

    session_cookie = _set_cookies(response)[SESSION_COOKIE][SESSION_COOKIE]
    assert response.status_code == 302
    assert response.headers["location"] == f"{FRONTEND_BASE_URL}/dashboard"
    assert session_cookie["httponly"] is True
    assert session_cookie["samesite"] == "lax"
    assert session_cookie["max-age"] == str(7 * 24 * 60 * 60)
    assert session_cookie["path"] == "/"
    assert not session_cookie["secure"]  # 시험 설정 session_cookie_secure=False
    organizations = me.json()["data"]["organizations"]
    assert me.status_code == 200
    assert me.json()["data"]["user"]["githubLogin"] == "Alice-Dev"
    assert [(org["slug"], org["personal"], org["role"]) for org in organizations] == [
        ("alice-dev", True, "ADMIN")
    ]
    async with harness.session_factory() as session:
        stored = await session.scalar(select(UserSession))
    assert stored is not None
    assert stored.expires_at == harness.clock.now() + timedelta(days=7)


async def test_callback_secure_cookie_follows_setting(harness: DeployHarness) -> None:
    harness.github_users.add_user("code-alice", ALICE)
    harness.use_settings(session_cookie_secure=True)

    async with make_client() as client:
        state_response = await client.get(LOGIN)
        state = parse_qs(urlsplit(state_response.headers["location"]).query)["state"][0]
        cookie_value = state_response.cookies[STATE_COOKIE]
        # Secure 쿠키는 http://test 로 돌려보내지 않으므로 시험이 직접 싣는다.
        response = await client.get(
            CALLBACK,
            params={"code": "code-alice", "state": state},
            headers={"Cookie": f"{STATE_COOKIE}={cookie_value}"},
        )

    assert _set_cookies(state_response)[STATE_COOKIE][STATE_COOKIE]["secure"] is True
    assert _set_cookies(response)[SESSION_COOKIE][SESSION_COOKIE]["secure"] is True


async def test_second_login_updates_profile_without_new_personal_org(
    harness: DeployHarness,
) -> None:
    harness.github_users.add_user("code-alice", ALICE)
    async with make_client() as client:
        await _login(client)
    renamed = GithubUser(id=ALICE.id, login="alice-new", name="Alice Kim", avatar_url=None)
    harness.github_users.add_user("code-alice-2", renamed)

    async with make_client() as client:
        response = await _login(client, code="code-alice-2")

    async with harness.session_factory() as session:
        user = await session.scalar(select(User).where(User.github_id == ALICE.id))
    assert response.status_code == 302
    assert user is not None
    assert (user.github_login, user.name, user.avatar_url) == ("alice-new", "Alice Kim", None)
    assert len(await _personal_org(harness, ALICE.id)) == 1
    assert await _count(harness, UserSession) == 3  # 시험 도구의 어드민 세션 + 2


async def test_concurrent_first_callbacks_create_one_user_and_one_personal_org(
    harness: DeployHarness,
) -> None:
    harness.github_users.add_user("code-alice", ALICE)
    async with make_client() as first, make_client() as second:
        states = [await _start(first), await _start(second)]
        responses = await asyncio.gather(
            first.get(CALLBACK, params={"code": "code-alice", "state": states[0]}),
            second.get(CALLBACK, params={"code": "code-alice", "state": states[1]}),
        )

    assert [response.status_code for response in responses] == [302, 302]
    assert all(
        response.headers["location"] == f"{FRONTEND_BASE_URL}/dashboard" for response in responses
    )
    async with harness.session_factory() as session:
        users = await session.scalar(
            select(func.count()).select_from(User).where(User.github_id == ALICE.id)
        )
    assert users == 1
    assert len(await _personal_org(harness, ALICE.id)) == 1
    assert await _count(harness, UserSession) == 3  # 시험 도구의 어드민 세션 + 2


async def test_login_not_configured_returns_503_and_other_apis_work(
    harness: DeployHarness,
) -> None:
    harness.use_settings(github_app_client_id=None)

    async with make_client() as client:
        login = await client.get(LOGIN)
        callback = await client.get(CALLBACK, params={"code": "x", "state": "y"})
    services = await harness.client.get(harness.services)
    harness.use_settings(frontend_base_url=None)
    async with make_client() as client:
        callback_without_frontend = await client.get(CALLBACK, params={"code": "x", "state": "y"})

    assert (login.status_code, login.json()["code"]) == (503, "LOGIN_NOT_CONFIGURED")
    assert callback.status_code == 302
    assert callback.headers["location"] == f"{FRONTEND_BASE_URL}/login?error=LOGIN_NOT_CONFIGURED"
    assert callback_without_frontend.status_code == 503
    assert services.status_code == 200


# --- AC-2: 콜백 실패 ---


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"code": "code-alice"}, "OAUTH_STATE_MISMATCH"),
        ({"code": "code-alice", "state": "forged"}, "OAUTH_STATE_MISMATCH"),
        ({"error": "access_denied", "state": "ignored"}, "OAUTH_DENIED"),
    ],
)
async def test_callback_failures_redirect_with_error_and_create_nothing(
    harness: DeployHarness, params: dict[str, str], expected: str
) -> None:
    harness.github_users.add_user("code-alice", ALICE)

    async with make_client() as client:
        await _start(client)
        response = await client.get(CALLBACK, params=params)

    assert response.status_code == 302
    assert response.headers["location"] == f"{FRONTEND_BASE_URL}/login?error={expected}"
    # 상태 쿠키는 어느 경우든 지운다.
    assert _set_cookies(response)[STATE_COOKIE][STATE_COOKIE]["max-age"] == "0"
    assert SESSION_COOKIE not in _set_cookies(response)
    assert (await _count(harness, User), await _count(harness, UserSession)) == BASELINE


async def test_callback_without_state_cookie_is_state_mismatch(harness: DeployHarness) -> None:
    harness.github_users.add_user("code-alice", ALICE)
    async with make_client() as starter:
        state = await _start(starter)

    async with make_client() as other_browser:
        response = await other_browser.get(CALLBACK, params={"code": "code-alice", "state": state})

    assert response.headers["location"] == f"{FRONTEND_BASE_URL}/login?error=OAUTH_STATE_MISMATCH"


async def test_callback_expired_state_is_state_mismatch(harness: DeployHarness) -> None:
    harness.github_users.add_user("code-alice", ALICE)
    async with make_client() as client:
        state = await _start(client)
        harness.clock.advance(601)
        response = await client.get(CALLBACK, params={"code": "code-alice", "state": state})

    assert response.headers["location"] == f"{FRONTEND_BASE_URL}/login?error=OAUTH_STATE_MISMATCH"


@pytest.mark.parametrize(
    ("setup", "expected"),
    [("unavailable", "GITHUB_UNAVAILABLE"), ("rejected", "OAUTH_DENIED")],
)
async def test_callback_github_failure_redirects_without_user(
    harness: DeployHarness, setup: str, expected: str
) -> None:
    harness.github_users.add_user("code-alice", ALICE)
    if setup == "unavailable":
        harness.github_users.unavailable = True
    else:
        harness.github_users.rejected_codes.add("code-alice")

    async with make_client() as client:
        response = await _login(client)

    assert response.headers["location"] == f"{FRONTEND_BASE_URL}/login?error={expected}"
    assert (await _count(harness, User), await _count(harness, UserSession)) == BASELINE


@pytest.mark.parametrize(
    "next_path",
    ["https://evil.test/x", "//evil.test/x", "/\\evil.test", "javascript:alert(1)", ""],
)
async def test_external_next_redirects_only_to_our_frontend(
    harness: DeployHarness, next_path: str
) -> None:
    harness.github_users.add_user("code-alice", ALICE)

    async with make_client() as client:
        response = await _login(client, next_path=next_path)

    assert response.status_code == 302
    assert response.headers["location"] == f"{FRONTEND_BASE_URL}/"


# --- AC-3: 로그인 허용 스위치 ---


async def test_allowed_org_switch_blocks_non_members_and_admits_members(
    harness: DeployHarness,
) -> None:
    bob = GithubUser(id=2002, login="bob", name=None, avatar_url=None)
    harness.github_users.add_user("code-alice", ALICE)
    harness.github_users.add_user("code-bob", bob)
    harness.github_users.org_members = {ALICE.id}
    harness.use_settings(login_allowed_github_org="softbank-team-echo")

    async with make_client() as client:
        rejected = await _login(client, code="code-bob")
    async with make_client() as client:
        allowed = await _login(client, code="code-alice")

    assert rejected.headers["location"] == f"{FRONTEND_BASE_URL}/login?error=LOGIN_NOT_ALLOWED"
    assert allowed.headers["location"] == f"{FRONTEND_BASE_URL}/dashboard"
    assert "is_active_org_member:softbank-team-echo" in harness.github_users.calls
    async with harness.session_factory() as session:
        logins = set((await session.scalars(select(User.github_login))).all())
    assert "bob" not in logins and "Alice-Dev" in logins


async def test_no_allowed_org_admits_anyone(harness: DeployHarness) -> None:
    harness.github_users.add_user("code-alice", ALICE)

    async with make_client() as client:
        response = await _login(client)

    assert response.headers["location"] == f"{FRONTEND_BASE_URL}/dashboard"
    assert not [call for call in harness.github_users.calls if call.startswith("is_active")]


# --- AC-4: 세션 ---


async def test_session_expires_after_seven_days_and_logout_revokes(
    harness: DeployHarness,
) -> None:
    harness.github_users.add_user("code-alice", ALICE)
    async with make_client() as client:
        await _login(client)
        before = await client.get("/api/v1/me")
        harness.clock.advance(7 * 24 * 60 * 60 + 1)
        expired = await client.get("/api/v1/me")

    async with make_client() as client:
        await _login(client)
        cookie = client.cookies[SESSION_COOKIE]
        logout = await client.post("/api/v1/auth/logout")
    async with make_client(session_token=cookie) as reused:
        after_logout = await reused.get("/api/v1/me")
    async with make_client() as anonymous:
        logout_without_session = await anonymous.post("/api/v1/auth/logout")

    assert before.status_code == 200
    assert (expired.status_code, expired.json()["code"]) == (401, "UNAUTHENTICATED")
    assert _set_cookies(expired)[SESSION_COOKIE][SESSION_COOKIE]["max-age"] == "0"
    assert logout.status_code == 204
    assert _set_cookies(logout)[SESSION_COOKIE][SESSION_COOKIE]["max-age"] == "0"
    assert after_logout.status_code == 401
    assert logout_without_session.status_code == 204


async def test_two_browsers_get_two_sessions_and_cookie_never_stored(
    harness: DeployHarness,
) -> None:
    harness.github_users.add_user("code-alice", ALICE)
    cookies = []
    for _ in range(2):
        async with make_client() as client:
            await _login(client)
            cookies.append(client.cookies[SESSION_COOKIE])
    async with harness.session_factory() as session:
        dumps = [
            await session.scalar(
                text(f"SELECT coalesce(string_agg(t::text, '\n'), '') FROM {table} t")
            )
            for table in TABLES
        ]
    db = "\n".join(dump or "" for dump in dumps)

    assert cookies[0] != cookies[1]
    assert await _count(harness, UserSession) == 3  # 시험 도구의 어드민 세션 + 2
    assert "Alice-Dev" in db  # 대조군
    assert all(cookie not in db for cookie in cookies)


async def test_me_lists_every_organization_with_role(harness: DeployHarness) -> None:
    harness.github_users.add_user("code-alice", ALICE)
    async with make_client() as client:
        await _login(client)
        async with harness.session_factory() as session:
            user = await session.scalar(select(User).where(User.github_id == ALICE.id))
        assert user is not None
        other = await create_organization(harness.session_factory, "acme")
        async with harness.session_factory() as session, session.begin():
            session.add(
                OrganizationMember(
                    organization_id=other.id, user_id=user.id, role=OrganizationRole.VIEWER
                )
            )
        me = await client.get("/api/v1/me")

    assert [(org["slug"], org["role"]) for org in me.json()["data"]["organizations"]] == [
        ("alice-dev", "ADMIN"),
        ("acme", "VIEWER"),
    ]
