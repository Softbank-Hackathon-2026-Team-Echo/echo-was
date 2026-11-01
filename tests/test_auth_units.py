"""로그인·조직 도우미 단위 시험(DB 없음)."""

import pytest
from pydantic import ValidationError

from app.auth.tokens import (
    AGENT_TOKEN_PREFIX,
    OauthState,
    hash_token,
    new_agent_token,
    new_session_token,
    safe_next_path,
)
from app.core.config import Settings
from app.core.secrets import (
    is_secret_ref_in_organization,
    organization_env_secret_prefix,
    parse_secret_ref,
)
from app.enums import OrganizationRole
from app.services.organization_service import is_valid_slug, personal_slug_candidates


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "/"),
        ("", "/"),
        ("/services?tab=1", "/services?tab=1"),
        ("https://evil.test", "/"),
        ("//evil.test/x", "/"),
        ("/\\evil.test", "/"),
        ("/javascript:alert(1)", "/"),
        ("/a\nb", "/"),
        ("relative", "/"),
    ],
)
def test_safe_next_path_only_allows_relative_paths(value: str | None, expected: str) -> None:
    assert safe_next_path(value) == expected


def test_oauth_state_round_trip_and_tampering() -> None:
    state = OauthState(state="abc", issued_at=1700000000, next_path="/a.b/c?x=1")

    assert OauthState.decode(state.encode()) == state
    assert OauthState.decode("abc") is None
    assert OauthState.decode("abc.notanumber.Lw") is None
    assert OauthState.decode("abc.1.%%%") is None


def test_tokens_are_random_and_hashed() -> None:
    first, second = new_session_token(), new_session_token()
    agent = new_agent_token().get_secret_value()

    assert first.get_secret_value() != second.get_secret_value()
    assert "*" in repr(first) and first.get_secret_value() not in repr(first)
    assert agent.startswith(AGENT_TOKEN_PREFIX) and len(agent) == len(AGENT_TOKEN_PREFIX) + 43
    assert len(hash_token(agent)) == 64 and hash_token(agent) != agent


def test_role_order() -> None:
    assert OrganizationRole.ADMIN.includes(OrganizationRole.DEVELOPER)
    assert OrganizationRole.DEVELOPER.includes(OrganizationRole.VIEWER)
    assert not OrganizationRole.VIEWER.includes(OrganizationRole.DEVELOPER)
    assert not OrganizationRole.DEVELOPER.includes(OrganizationRole.ADMIN)


@pytest.mark.parametrize(
    ("slug", "valid"),
    [
        ("a", True),
        ("team-echo", True),
        ("a" * 39, True),
        ("a" * 40, False),
        ("a--b", False),
        ("-a", False),
        ("a-", False),
        ("A", False),
    ],
)
def test_slug_rule(slug: str, valid: bool) -> None:
    assert is_valid_slug(slug) is valid


def test_personal_slug_candidates() -> None:
    candidates = list(personal_slug_candidates("Kim-Lee", 77))
    long = list(personal_slug_candidates("a" * 37 + "-b", 77))
    odd = list(personal_slug_candidates("bad--login", 77))

    assert candidates[:3] == ["kim-lee", "kim-lee-2", "kim-lee-3"]
    assert long[0] == "a" * 37 + "-b" and long[1] == "a" * 37 + "-2"
    assert all(is_valid_slug(slug) for slug in candidates + long + odd)
    assert odd[0] == "user-77"


def test_organization_secret_prefix_separates_similar_slugs() -> None:
    assert organization_env_secret_prefix("team-echo") == "ECHO_SECRET_TEAM_ECHO__"
    a_corp_x = parse_secret_ref("env:ECHO_SECRET_A_CORP_X__Y")
    a_corp = parse_secret_ref("env:ECHO_SECRET_A_CORP__Y")

    assert not is_secret_ref_in_organization(a_corp_x, "a-corp")
    assert is_secret_ref_in_organization(a_corp_x, "a-corp-x")
    assert is_secret_ref_in_organization(a_corp, "a-corp")
    assert not is_secret_ref_in_organization(a_corp, "a-corp-x")
    assert not is_secret_ref_in_organization(parse_secret_ref("env:ECHO_SECRET_X"), "a-corp")
    assert is_secret_ref_in_organization(parse_secret_ref("k8s:ns/s/key"), "a-corp")


def test_settings_auth_defaults_lists_and_login_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defaults = Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setenv("FRONTEND_ALLOWED_ORIGINS", "https://a.test, https://b.test,")
    monkeypatch.setenv("RESERVED_ORG_SLUGS", "team-echo,echo")
    configured = Settings(
        _env_file=None,  # type: ignore[call-arg]
        github_app_client_id="Iv1.x",
        github_app_client_secret_ref="env:ECHO_SECRET_GITHUB_APP_CLIENT_SECRET",
        github_oauth_redirect_uri="https://api.test/api/v1/auth/github/callback",
        frontend_base_url="https://app.test/",
    )

    assert defaults.is_login_configured is False
    assert defaults.reserved_org_slugs == ["team-echo"]
    assert defaults.session_ttl_seconds == 7 * 24 * 60 * 60
    assert defaults.session_cookie_secure is True
    assert configured.is_login_configured is True
    assert configured.frontend_base_url == "https://app.test"
    assert configured.frontend_allowed_origins == ["https://a.test", "https://b.test"]
    assert configured.reserved_org_slugs == ["team-echo", "echo"]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, github_app_client_secret_ref="plain-secret")  # type: ignore[call-arg]
