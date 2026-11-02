"""서버 쪽 로그 마스킹(SPEC api-4 AC-13 단위)과 배포 작업 로그 문장표."""

import pytest

from app.core.log_masking import MASK, mask_secrets
from app.enums import DeployLogCode, DeployRequestErrorCode, LogLevel
from app.services.deploy_log_messages import (
    DEPLOY_LOG_ENTRIES,
    ERROR_MESSAGES,
    deploy_log_message,
)
from tests.github_fake import TEST_APP_PRIVATE_KEY_PEM

GITHUB_TOKENS = [prefix + "_" + "Ab1" * 8 for prefix in ("ghp", "gho", "ghu", "ghs", "ghr")]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        *[(f"token={token} end", f"token={MASK} end") for token in GITHUB_TOKENS],
        ("key AKIAABCDEFGHIJ234567 used", f"key {MASK} used"),
        ("Authorization: Bearer abc.def-ghi_jkl==", f"Authorization: Bearer {MASK}"),
        ("authorization: bearer abc123xyz", f"authorization: bearer {MASK}"),
        ("OPENAI_API_KEY=sk-proj_ABCDEFGHIJKLMNOPQRSTUV", f"OPENAI_API_KEY={MASK}"),
        (
            f"before\n{TEST_APP_PRIVATE_KEY_PEM.strip()}\nafter",
            f"before\n{MASK}\nafter",
        ),
        (
            "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg (truncated log)",
            MASK,
        ),
        (
            "two ghs_" + "x" * 20 + " and AKIA" + "Z" * 16,
            f"two {MASK} and {MASK}",
        ),
    ],
)
def test_mask_secrets_replaces_known_token_shapes(raw: str, expected: str) -> None:
    assert mask_secrets(raw) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Building image registry.echo.test/shop-api",
        "Image digest sha256:" + "a" * 64,
        "ghs_short is not a token",
        "AKIA123 too short",
        "sk-short",
        "the bearer of bad news",
        "GET /healthz 200",
        "",
        "multi\nline\nlog",
    ],
)
def test_mask_secrets_keeps_ordinary_lines(text: str) -> None:
    assert mask_secrets(text) == text


def test_every_deploy_log_code_has_a_fixed_entry() -> None:
    assert set(DEPLOY_LOG_ENTRIES) == set(DeployLogCode)
    assert set(ERROR_MESSAGES) == set(DeployRequestErrorCode)
    for entry in DEPLOY_LOG_ENTRIES.values():
        assert entry.message and len(entry.message) <= 500
        assert mask_secrets(entry.message) == entry.message


@pytest.mark.parametrize(
    ("code", "level"),
    [
        (DeployLogCode.FAILED, LogLevel.ERROR),
        (DeployLogCode.REVERT_FAILED, LogLevel.ERROR),
        (DeployLogCode.HEALTH_PROBE_HTTP_ERROR, LogLevel.WARN),
        (DeployLogCode.HEALTH_PROBE_UNREACHABLE, LogLevel.WARN),
        (DeployLogCode.HEALTH_PROBE_OK, LogLevel.INFO),
        (DeployLogCode.SUCCEEDED, LogLevel.INFO),
    ],
)
def test_deploy_log_levels(code: DeployLogCode, level: LogLevel) -> None:
    assert DEPLOY_LOG_ENTRIES[code].level is level


def test_failed_line_uses_error_code_sentence() -> None:
    assert (
        deploy_log_message(DeployLogCode.FAILED, DeployRequestErrorCode.DEPLOY_TIMEOUT)
        == ERROR_MESSAGES[DeployRequestErrorCode.DEPLOY_TIMEOUT]
    )
