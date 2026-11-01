"""실제 GitHub 사용자 클라이언트(httpx) — MockTransport 로만 시험한다(SPEC api-6 AC-16)."""

import logging

import httpx
import pytest

from app.clients.github_user import GithubUser, HttpGithubUserClient
from app.clients.secret_client import EnvVarSecretClient
from app.core.exceptions import GithubCodeRejectedError, GithubUnavailableError
from app.core.secrets import SecretValue, parse_secret_ref
from tests.github_user_fake import (
    API_BASE,
    CLIENT_SECRET_ENV,
    CLIENT_SECRET_VALUE,
    OAUTH_BASE,
    FakeGithubOauth,
    user_token_for,
)

ALICE = GithubUser(id=1001, login="alice", name="Alice", avatar_url="https://avatars.test/a")
REDIRECT_URI = "https://api.echo.test/api/v1/auth/github/callback"


@pytest.fixture(autouse=True)
def client_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CLIENT_SECRET_ENV, CLIENT_SECRET_VALUE)


def _client(
    github: FakeGithubOauth, transport: httpx.AsyncBaseTransport | None = None
) -> HttpGithubUserClient:
    return HttpGithubUserClient(
        oauth_base_url=OAUTH_BASE,
        api_base_url=API_BASE,
        client_id="Iv1.test",
        client_secret_ref=parse_secret_ref(f"env:{CLIENT_SECRET_ENV}"),
        secret_client=EnvVarSecretClient(),
        timeout_seconds=10,
        transport=transport or github.transport(),
    )


def _token() -> SecretValue:
    return SecretValue(user_token_for("code-alice"))


async def test_exchange_code_and_get_user() -> None:
    github = FakeGithubOauth(users_by_code={"code-alice": ALICE})
    client = _client(github)

    token = await client.exchange_code("code-alice", REDIRECT_URI)
    user = await client.get_user(token)

    exchange = github.requests[0]
    assert token.get_secret_value() == user_token_for("code-alice")
    assert user == ALICE
    assert exchange.url == f"{OAUTH_BASE}/login/oauth/access_token"
    assert exchange.headers["accept"] == "application/json"
    assert b"redirect_uri=https%3A%2F%2Fapi.echo.test" in exchange.content
    assert github.requests[1].headers["authorization"] == f"Bearer {user_token_for('code-alice')}"


async def test_exchange_code_rejected_and_missing_secret() -> None:
    github = FakeGithubOauth(users_by_code={"code-alice": ALICE}, client_secret="other")

    with pytest.raises(GithubCodeRejectedError):
        await _client(github).exchange_code("code-alice", REDIRECT_URI)
    with pytest.raises(GithubCodeRejectedError):
        await _client(FakeGithubOauth()).exchange_code("unknown", REDIRECT_URI)


async def test_exchange_code_secret_not_in_environment_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CLIENT_SECRET_ENV)

    with pytest.raises(GithubUnavailableError):
        await _client(FakeGithubOauth()).exchange_code("x", REDIRECT_URI)


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(200, json={"state": "active"}), True),
        (httpx.Response(200, json={"state": "pending"}), False),
        (httpx.Response(403, json={"message": "Resource not accessible by integration"}), False),
        (httpx.Response(404, json={"message": "Not Found"}), False),
    ],
)
async def test_org_membership_states(response: httpx.Response, expected: bool) -> None:
    github = FakeGithubOauth(override=lambda request: response)

    assert await _client(github).is_active_org_member(_token(), "softbank-team-echo") is expected
    assert github.requests[0].url.path == "/user/memberships/orgs/softbank-team-echo"


async def test_org_membership_rate_limited_is_unavailable() -> None:
    github = FakeGithubOauth(override=lambda request: httpx.Response(429))
    limited_403 = FakeGithubOauth(
        override=lambda request: httpx.Response(403, headers={"x-ratelimit-remaining": "0"})
    )

    with pytest.raises(GithubUnavailableError):
        await _client(github).is_active_org_member(_token(), "org")
    with pytest.raises(GithubUnavailableError):
        await _client(limited_403).is_active_org_member(_token(), "org")


async def test_find_user_found_and_not_found() -> None:
    github = FakeGithubOauth(users_by_code={"code-alice": ALICE})

    found = await _client(github).find_user("Alice")
    missing = await _client(github).find_user("nobody")
    invalid = await _client(github).find_user("../admin")

    assert found == ALICE
    assert missing is None
    assert invalid is None
    assert [request.url.path for request in github.requests] == ["/users/Alice", "/users/nobody"]
    assert "authorization" not in github.requests[0].headers


@pytest.mark.parametrize("status_code", [403, 429, 500, 502, 503])
async def test_find_user_errors_are_unavailable(status_code: int) -> None:
    github = FakeGithubOauth(override=lambda request: httpx.Response(status_code))

    with pytest.raises(GithubUnavailableError):
        await _client(github).find_user("alice")


@pytest.mark.parametrize("call", ["exchange", "user", "membership", "find"])
async def test_server_errors_and_timeouts_are_unavailable(call: str) -> None:
    server_error = FakeGithubOauth(override=lambda request: httpx.Response(500))

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    for client in (_client(server_error), _client(server_error, httpx.MockTransport(timeout))):
        with pytest.raises(GithubUnavailableError):
            if call == "exchange":
                await client.exchange_code("code", REDIRECT_URI)
            elif call == "user":
                await client.get_user(_token())
            elif call == "membership":
                await client.is_active_org_member(_token(), "org")
            else:
                await client.find_user("alice")


async def test_malformed_user_body_is_unavailable() -> None:
    github = FakeGithubOauth(override=lambda request: httpx.Response(200, json={"login": "x"}))

    with pytest.raises(GithubUnavailableError):
        await _client(github).get_user(_token())


async def test_tokens_and_secret_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    for noisy in ("httpx", "httpcore"):
        caplog.set_level(logging.DEBUG, logger=noisy)
    github = FakeGithubOauth(users_by_code={"code-alice": ALICE})
    client = _client(github)

    token = await client.exchange_code("code-alice", REDIRECT_URI)
    await client.get_user(token)
    await client.is_active_org_member(token, "org")
    with pytest.raises(GithubCodeRejectedError):
        await _client(FakeGithubOauth()).exchange_code("code-alice", REDIRECT_URI)

    assert caplog.records
    assert CLIENT_SECRET_VALUE not in caplog.text
    assert token.get_secret_value() not in caplog.text
