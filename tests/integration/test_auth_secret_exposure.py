"""로그인·에이전트 토큰 비밀값 0회 노출(SPEC api-6 AC-15).

세션 쿠키 값, 에이전트 토큰 원문(발급 응답 제외), GitHub 사용자 토큰, client secret 이 로그·응답
본문·DB 모든 표 텍스트에 나오지 않는다. 실제 GitHub 사용자 클라이언트를 MockTransport 로 돌린다.
"""

import logging
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import text

from app.clients.github_user import GithubUser, HttpGithubUserClient
from app.clients.secret_client import EnvVarSecretClient
from app.core.logging import JsonFormatter
from app.core.secrets import parse_secret_ref
from app.dependencies import get_github_user_client
from app.main import app
from tests.github_user_fake import (
    API_BASE,
    CLIENT_SECRET_ENV,
    CLIENT_SECRET_VALUE,
    OAUTH_BASE,
    FakeGithubOauth,
    user_token_for,
)
from tests.integration.auth_support import SESSION_COOKIE, make_client, org_path
from tests.integration.conftest import TABLES
from tests.integration.deploy_harness import DeployHarness

ALICE = GithubUser(id=4242, login="alice", name="Alice", avatar_url=None)


async def test_login_and_agent_token_secrets_never_exposed(
    harness: DeployHarness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    for noisy in ("httpx", "httpcore"):
        caplog.set_level(logging.DEBUG, logger=noisy)
    monkeypatch.setenv(CLIENT_SECRET_ENV, CLIENT_SECRET_VALUE)
    github = FakeGithubOauth(users_by_code={"code-alice": ALICE}, memberships={ALICE.id: "active"})
    client = HttpGithubUserClient(
        oauth_base_url=OAUTH_BASE,
        api_base_url=API_BASE,
        client_id="Iv1.test",
        client_secret_ref=parse_secret_ref(f"env:{CLIENT_SECRET_ENV}"),
        secret_client=EnvVarSecretClient(),
        timeout_seconds=10,
        transport=github.transport(),
    )
    app.dependency_overrides[get_github_user_client] = lambda: client
    harness.use_settings(
        github_oauth_base_url=OAUTH_BASE, login_allowed_github_org="softbank-team-echo"
    )
    bodies: list[str] = []

    async with make_client() as browser:
        start = await browser.get("/api/v1/auth/github/login", params={"next": "/home"})
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        callback = await browser.get(
            "/api/v1/auth/github/callback", params={"code": "code-alice", "state": state}
        )
        session_cookie = browser.cookies[SESSION_COOKIE]
        me = await browser.get("/api/v1/me")
        bodies += [start.text, start.headers["location"], callback.headers["location"], me.text]
        personal_id = me.json()["data"]["organizations"][0]["id"]
        tokens_path = org_path(personal_id, "/agent-tokens")
        issued = await browser.post(tokens_path, json={"name": "bot"})
        listed = await browser.get(tokens_path)
        bodies.append(listed.text)
        agent_token = issued.json()["data"]["token"]
        async with make_client(agent_token=agent_token) as agent:
            for response in (
                await agent.get(org_path(personal_id, "/services")),
                await agent.post(org_path(personal_id, "/services"), json={}),
                await agent.get(org_path(harness.organization.id, "/services")),
            ):
                bodies.append(response.text)
        logout = await browser.post("/api/v1/auth/logout")
        bodies.append(logout.text)

    async with harness.session_factory() as session:
        dumps = [
            await session.scalar(
                text(f"SELECT coalesce(string_agg(t::text, '\n'), '') FROM {table} t")
            )
            for table in TABLES
        ]
    formatter = JsonFormatter()
    logs = [formatter.format(record) for record in caplog.records] + [caplog.text]
    haystacks = {
        "logs": "\n".join(logs),
        "responses": "\n".join(bodies),
        "db": "\n".join(dump or "" for dump in dumps),
    }
    secrets = [session_cookie, agent_token, user_token_for("code-alice"), CLIENT_SECRET_VALUE]

    assert issued.status_code == 201 and agent_token.startswith("echo_agt_")
    assert callback.headers["location"] == "https://app.echo.test/home"
    # 대조군: 비밀이 아닌 사용자 아이디·조직 id 는 세 곳에서 찾혀야 검색 대상이 모인 것이다.
    assert "alice" in haystacks["db"] and personal_id in haystacks["responses"]
    assert personal_id in haystacks["logs"]
    leaks = [
        (where, index)
        for where, haystack in haystacks.items()
        for index, secret in enumerate(secrets)
        if secret in haystack
    ]
    assert leaks == []
