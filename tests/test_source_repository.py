"""저장소 주소 형식, 가짜 소스 저장소 클라이언트, 원터치 배포 설정 검사 시험."""

import hashlib

import pytest
from pydantic import ValidationError

from app.clients.source_repository import FakeSourceRepositoryClient
from app.core.config import ExecutorMode, Settings
from app.core.exceptions import (
    RefNotFoundError,
    RepoNotAccessibleError,
    SourceProviderUnavailableError,
)
from app.core.repo_url import InvalidRepoUrlError, RepoLocator, parse_repo_url

DATABASE_URL = "postgresql+asyncpg://x:y@127.0.0.1:1/z"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://github.com/team-echo/shop-api", "https://github.com/team-echo/shop-api"),
        ("https://github.com/team-echo/shop-api/", "https://github.com/team-echo/shop-api"),
        ("https://github.com/team-echo/shop-api.git", "https://github.com/team-echo/shop-api"),
        ("https://github.com/Team_Echo/shop.api.git/", "https://github.com/Team_Echo/shop.api"),
    ],
)
def test_parse_repo_url_normalizes(value: str, expected: str) -> None:
    assert parse_repo_url(value).url == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://github.com/a/b",
        "https://gitlab.com/a/b",
        "https://www.github.com/a/b",
        "https://user:pw@github.com/a/b",
        "https://github.com/a",
        "https://github.com/a/b/c",
        "https://github.com/a/.git",
        "https://github.com/../b",
        "https://github.com/a/b?x=1",
        "https://github.com/" + "a" * 101 + "/b",
        " https://github.com/a/b",
    ],
)
def test_parse_repo_url_rejects_other_forms(value: str) -> None:
    with pytest.raises(InvalidRepoUrlError):
        parse_repo_url(value)


async def test_fake_source_client_follows_owner_and_ref_rules() -> None:
    client = FakeSourceRepositoryClient(install_url="https://example.test/install")
    public = RepoLocator("team-echo", "shop-api")
    private = RepoLocator("private-acme", "billing")

    public_access = await client.check_access(public)
    private_access = await client.check_access(private)
    sha = await client.resolve_ref(public, "main")
    full_sha = await client.resolve_ref(public, "f" * 40)

    assert (public_access.private, private_access.private) == (False, True)
    expected = hashlib.sha1(b"https://github.com/team-echo/shop-api@main").hexdigest()
    assert sha == expected
    assert full_sha == "f" * 40
    assert await client.issue_read_token(public) is None
    assert await client.issue_read_token(private) is not None
    assert client.simulated is True


@pytest.mark.parametrize(
    ("owner", "error"),
    [
        ("denied-acme", RepoNotAccessibleError),
        ("missing-acme", RepoNotAccessibleError),
        ("flaky-acme", SourceProviderUnavailableError),
    ],
)
async def test_fake_source_client_owner_prefix_errors(owner: str, error: type[Exception]) -> None:
    client = FakeSourceRepositoryClient(install_url=None)

    with pytest.raises(error):
        await client.check_access(RepoLocator(owner, "shop"))


async def test_fake_source_client_missing_ref_raises_ref_not_found() -> None:
    client = FakeSourceRepositoryClient(install_url=None)

    with pytest.raises(RefNotFoundError):
        await client.resolve_ref(RepoLocator("team-echo", "shop"), "missing-feature")


def test_settings_defaults_are_fake_modes_and_spec_timeouts() -> None:
    settings = Settings(database_url=DATABASE_URL)

    assert settings.public_base_domain == "apps.echo.test"
    assert settings.executor_mode is ExecutorMode.FAKE
    assert (
        settings.build_timeout_seconds,
        settings.deploy_timeout_seconds,
        settings.teardown_timeout_seconds,
        settings.health_check_interval_seconds,
        settings.worker_lease_seconds,
    ) == (900, 300, 300, 5, 30)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("public_base_domain", "Apps.Echo.Test"),
        ("public_base_domain", "localhost"),
        ("public_base_domain", ("a" * 60 + ".") * 2 + "b" * 10),
        ("executor_mode", "real"),
        ("github_app_private_key_ref", "/etc/key.pem"),
    ],
)
def test_settings_rejects_invalid_values(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(database_url=DATABASE_URL, **{field: value})
