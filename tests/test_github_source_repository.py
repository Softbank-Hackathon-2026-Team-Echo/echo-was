"""실제 GitHub App 소스 저장소 클라이언트 시험(SPEC api-3 AC-14). httpx.MockTransport 만 쓴다."""

from datetime import UTC, datetime

import httpx
import jwt
import pytest

from app.clients.github_source_repository import GithubAppSourceRepositoryClient
from app.clients.secret_client import EnvVarSecretClient
from app.core.exceptions import (
    RefNotFoundError,
    RepoNotAccessibleError,
    SourceProviderUnavailableError,
)
from app.core.repo_url import RepoLocator
from app.core.secrets import parse_secret_ref
from tests.github_fake import (
    API_BASE,
    APP_ID,
    COMMIT_SHA,
    INSTALLATION_TOKEN,
    TEST_APP_PRIVATE_KEY_PEM,
    TEST_APP_PUBLIC_KEY_PEM,
    FakeGithub,
)

KEY_ENV = "ECHO_SECRET_GITHUB_APP_PRIVATE_KEY"
INSTALL_URL = "https://github.com/apps/echo-deploy/installations/new"
NOW = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
PRIVATE_REPO = RepoLocator(owner="team-echo", name="billing")
PUBLIC_REPO = RepoLocator(owner="team-echo", name="shop-api")


class _Clock:
    def now(self) -> datetime:
        return NOW


@pytest.fixture(autouse=True)
def private_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY_ENV, TEST_APP_PRIVATE_KEY_PEM)


def _client(github: FakeGithub, *, app_configured: bool = True) -> GithubAppSourceRepositoryClient:
    return GithubAppSourceRepositoryClient(
        api_base_url=API_BASE,
        app_id=APP_ID if app_configured else None,
        private_key_ref=parse_secret_ref(f"env:{KEY_ENV}") if app_configured else None,
        secret_client=EnvVarSecretClient(),
        install_url=INSTALL_URL,
        timeout_seconds=10,
        clock=_Clock(),
        transport=github.transport(),
    )


async def test_installed_private_repo_uses_installation_token() -> None:
    github = FakeGithub(installed={"team-echo/billing": True})
    client = _client(github)

    access = await client.check_access(PRIVATE_REPO)
    sha = await client.resolve_ref(PRIVATE_REPO, "main")
    token = await client.issue_read_token(PRIVATE_REPO)

    assert (access.private, access.default_branch) == (True, "main")
    assert sha == COMMIT_SHA
    assert token is not None and token.get_secret_value() == INSTALLATION_TOKEN
    assert INSTALLATION_TOKEN not in repr(token)
    token_request = next(r for r in github.requests if r.url.path.endswith("/access_tokens"))
    assert token_request.method == "POST"
    assert b'"contents":"read"' in token_request.content.replace(b" ", b"")


async def test_public_repo_without_installation_reads_anonymously() -> None:
    github = FakeGithub(public={"team-echo/shop-api"})
    client = _client(github)

    access = await client.check_access(PUBLIC_REPO)
    token = await client.issue_read_token(PUBLIC_REPO)

    assert access.private is False
    assert token is None
    repo_requests = [r for r in github.requests if r.url.path == "/repos/team-echo/shop-api"]
    assert repo_requests and "authorization" not in repo_requests[0].headers


async def test_unconfigured_app_skips_installation_lookup() -> None:
    github = FakeGithub(public={"team-echo/shop-api"})

    await _client(github, app_configured=False).check_access(PUBLIC_REPO)

    assert [r.url.path for r in github.requests] == ["/repos/team-echo/shop-api"]


async def test_missing_or_uninstalled_private_repo_returns_not_accessible_with_install_url() -> (
    None
):
    github = FakeGithub()

    with pytest.raises(RepoNotAccessibleError) as raised:
        await _client(github).check_access(PRIVATE_REPO)

    assert raised.value.details is not None
    assert [(item.field, item.value) for item in raised.value.details] == [
        ("installUrl", INSTALL_URL)
    ]


@pytest.mark.parametrize("ref", ["missing-branch", "feature/x"])
async def test_unknown_ref_returns_ref_not_found(ref: str) -> None:
    github = FakeGithub(public={"team-echo/shop-api"})

    with pytest.raises(RefNotFoundError):
        await _client(github).resolve_ref(PUBLIC_REPO, ref)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, headers={"x-ratelimit-remaining": "0"}),
        httpx.Response(429, headers={"retry-after": "60"}),
        httpx.Response(500),
        httpx.Response(503),
    ],
)
async def test_rate_limit_and_server_errors_return_provider_unavailable(
    response: httpx.Response,
) -> None:
    github = FakeGithub(public={"team-echo/shop-api"}, override=lambda request: response)

    with pytest.raises(SourceProviderUnavailableError):
        await _client(github).check_access(PUBLIC_REPO)


async def test_repo_forbidden_without_rate_limit_headers_is_not_accessible() -> None:
    def forbid_repo(request: httpx.Request) -> httpx.Response | None:
        return httpx.Response(403) if request.url.path == "/repos/team-echo/shop-api" else None

    github = FakeGithub(public={"team-echo/shop-api"}, override=forbid_repo)

    with pytest.raises(RepoNotAccessibleError):
        await _client(github).check_access(PUBLIC_REPO)


async def test_timeout_returns_provider_unavailable() -> None:
    def time_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    github = FakeGithub(override=time_out)

    with pytest.raises(SourceProviderUnavailableError):
        await _client(github).check_access(PUBLIC_REPO)


async def test_missing_private_key_returns_provider_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(KEY_ENV)
    github = FakeGithub(public={"team-echo/shop-api"})

    with pytest.raises(SourceProviderUnavailableError):
        await _client(github).check_access(PUBLIC_REPO)

    assert github.requests == []


async def test_app_jwt_is_rs256_with_app_issuer_and_at_most_ten_minutes() -> None:
    github = FakeGithub(installed={"team-echo/billing": True})

    await _client(github).check_access(PRIVATE_REPO)

    installation = next(r for r in github.requests if r.url.path.endswith("/installation"))
    token = installation.headers["authorization"].removeprefix("Bearer ")
    header = jwt.get_unverified_header(token)
    claims = jwt.decode(
        # 시험 시계(NOW)로 만든 토큰이라 실제 시각 기준 검사는 끈다. 값은 아래에서 NOW 로 비교한다.
        token,
        TEST_APP_PUBLIC_KEY_PEM,
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False, "verify_nbf": False},
    )
    assert header["alg"] == "RS256"
    assert claims["iss"] == APP_ID
    assert claims["exp"] - claims["iat"] <= 600
    assert claims["iat"] <= NOW.timestamp() < claims["exp"]


async def test_all_calls_go_through_mock_transport_only() -> None:
    github = FakeGithub(installed={"team-echo/billing": True})

    sha = await _client(github).resolve_ref(PRIVATE_REPO, "main")

    assert sha == COMMIT_SHA
    assert github.requests
    assert all(r.url.host == "api.github.test" for r in github.requests)
