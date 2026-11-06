"""배포 실패 진단의 가리기·원인 한 줄·문장·failure 칸 시험(API 서버 4-1)."""

import pytest

from app.core.log_masking import mask_secrets, mask_secrets_in_json
from app.core.secrets import SecretValue
from app.enums import DeployLogCode, DeployRequestErrorCode
from app.executors.base import CONTAINER_LOG_LINE_MAX_LENGTH, ContainerDiagnostics
from app.executors.fake import FAKE_CRASH_LOG_TAIL, fake_crash_diagnostics
from app.executors.kubernetes.executors import _read_log_tail
from app.models.deploy_request_log import DEPLOY_LOG_MESSAGE_MAX_LENGTH
from app.services.container_diagnostics import (
    build_failure_detail,
    diagnostics_message,
    failed_message,
    is_maskable_env_value,
    last_error_line,
    mask_container_log,
)
from app.services.deploy_log_messages import DEPLOY_LOG_ENTRIES, ERROR_MESSAGES

DB_PASSWORD = "pa55-w0rd-xyz"
DB_URL = f"postgresql://shop:{DB_PASSWORD}@db.internal:5432/shop"


def test_mask_container_log_hides_env_values_url_passwords_and_tokens() -> None:
    lines = [
        f"connecting to {DB_URL}",
        f"DATABASE_URL={DB_URL}",
        "redis://cache:other-secret-1@redis:6379/0",
        'config {"password": "hunter2-long", "token": "abc123def456"}',
        "Authorization: Bearer abcdefghijklmnop",
        "AKIAABCDEFGHIJKLMNOP leaked",
        "PORT=8080 DEBUG=true",
    ]

    masked = mask_container_log(
        lines,
        {
            "DATABASE_URL": SecretValue(DB_URL),
            "DEBUG": SecretValue("true"),  # 짧은 값은 가리지 않는다
        },
    )

    joined = "\n".join(masked)
    for secret in (DB_PASSWORD, DB_URL, "other-secret-1", "hunter2-long", "abc123def456"):
        assert secret not in joined
    assert "abcdefghijklmnop" not in joined
    assert "AKIAABCDEFGHIJKLMNOP" not in joined
    assert masked[0] == "connecting to ***"
    assert masked[2] == "redis://cache:***@redis:6379/0"
    assert masked[-1] == "PORT=8080 DEBUG=true"


def test_mask_container_log_without_env_values_still_masks_shapes() -> None:
    assert mask_container_log([f"url {DB_URL}"], None) == (
        "url postgresql://shop:***@db.internal:5432/shop",
    )


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (list(FAKE_CRASH_LOG_TAIL), "ValidationError: database_url"),
        (
            [
                "pydantic_core._pydantic_core.ValidationError: 2 validation errors for Settings",
                "database_url",
                "  Field required [type=missing]",
                "secret_key",
                "  Field required [type=missing]",
            ],
            "ValidationError: database_url, secret_key",
        ),
        (
            ["boot", "KeyError: 'DATABASE_URL'", "shutting down"],
            "KeyError: 'DATABASE_URL'",
        ),
        (
            ["start", "panic: runtime error: nil map", "goroutine 1"],
            "panic: runtime error: nil map",
        ),
        (["start", "listening", ""], "listening"),
        ([], None),
        (["", "  "], None),
    ],
)
def test_last_error_line(lines: list[str], expected: str | None) -> None:
    assert last_error_line(lines) == expected


def test_failed_message_appends_last_error_or_termination_reason() -> None:
    code = DeployRequestErrorCode.CONTAINER_START_FAILED
    base = ERROR_MESSAGES[code]

    assert failed_message(code, None) == base
    assert failed_message(code, fake_crash_diagnostics(FAKE_CRASH_LOG_TAIL)) == (
        f"{base} Last error: ValidationError: database_url"
    )
    oom = ContainerDiagnostics(termination_reason="OOMKilled", exit_code=137)
    assert failed_message(code, oom) == f"{base} Last error: OOMKilled (exit code 137)"
    assert failed_message(code, ContainerDiagnostics()) == base
    long = failed_message(code, ContainerDiagnostics(log_tail=("E" * 2000,)))
    assert len(long) <= DEPLOY_LOG_MESSAGE_MAX_LENGTH


def test_fake_crash_diagnostics_keeps_last_50_lines() -> None:
    diagnostics = fake_crash_diagnostics([f"line {index}" for index in range(70)])

    assert len(diagnostics.log_tail) == 50
    assert diagnostics.log_tail[0] == "line 20"
    assert diagnostics.log_truncated is True
    assert (diagnostics.reason, diagnostics.exit_code) == ("CrashLoopBackOff", 1)


def test_failure_detail_round_trips_summary_line() -> None:
    diagnostics = ContainerDiagnostics(
        container="app",
        reason="CrashLoopBackOff",
        termination_reason="OOMKilled",
        exit_code=137,
        restart_count=4,
        log_tail=("a", "b"),
        log_truncated=True,
    )
    rows = [
        (DeployLogCode.CONTAINER_DIAGNOSTICS.value, diagnostics_message(diagnostics)),
        (DeployLogCode.CONTAINER_LOG.value, "a"),
        (DeployLogCode.CONTAINER_LOG.value, "b"),
        (DeployLogCode.FAILED.value, "failed. Last error: OOMKilled (exit code 137)"),
        (DeployLogCode.REVERT_SUCCEEDED_NO_PREVIOUS.value, "withdrawn"),
    ]

    detail = build_failure_detail("FAILED", "CONTAINER_START_FAILED", rows)

    assert detail is not None
    assert (detail.container, detail.container_reason, detail.termination_reason) == (
        "app",
        "CrashLoopBackOff",
        "OOMKilled",
    )
    assert (detail.exit_code, detail.restart_count) == (137, 4)
    assert (detail.log_tail, detail.log_truncated) == (["a", "b"], True)
    assert detail.message == "failed. Last error: OOMKilled (exit code 137)"
    assert detail.previous_version_running is False


def test_failure_detail_without_diagnostics_and_missing_values() -> None:
    empty = ContainerDiagnostics()
    detail = build_failure_detail(
        "FAILED",
        "HEALTH_CHECK_FAILED",
        [
            (DeployLogCode.CONTAINER_DIAGNOSTICS.value, diagnostics_message(empty)),
            (DeployLogCode.REVERT_SUCCEEDED.value, "kept"),
        ],
    )

    assert detail is not None
    assert detail.message == ERROR_MESSAGES[DeployRequestErrorCode.HEALTH_CHECK_FAILED]
    assert (detail.container, detail.exit_code, detail.restart_count) == (None, None, None)
    assert detail.previous_version_running is True
    assert build_failure_detail("SUCCEEDED", None, []) is None


def test_revert_messages_branch_on_previous_version() -> None:
    kept = DEPLOY_LOG_ENTRIES[DeployLogCode.REVERT_SUCCEEDED]
    none = DEPLOY_LOG_ENTRIES[DeployLogCode.REVERT_SUCCEEDED_NO_PREVIOUS]

    assert "previous version was kept" in kept.message
    assert "No version is running" in none.message
    assert (none.level, none.stage) == (kept.level, kept.stage)


# --- 4-1 검증 반영: 독립 검증 탐침 3건 회귀(긴 값 절단 누출·5자 비밀·단독 JWT) ---

# 시험용 가짜 JWT(헤더 {"alg":"HS256"} 꼴, 서명은 임의 글자).
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiJwcm9iZS11c2VyIn0" + ".c2lnbmF0dXJlLXByb2JlLXZhbHVl"


class _TextApi:
    """_read_log_tail 이 부르는 get_text 만 흉내 낸다(실행기가 받은 글 그대로)."""

    def __init__(self, text: str) -> None:
        self._text = text

    async def get_text(self, *args: object, **kwargs: object) -> str:
        return self._text


async def _executor_then_processor(text: str, env: dict[str, SecretValue]) -> str:
    """실제 경로 순서: 실행기 로그 꼬리 → 처리기 가리기 → 응답 직전 방어."""
    tail, _ = await _read_log_tail(_TextApi(text), "ns", "pod", "app", previous=False)  # type: ignore[arg-type]
    masked = mask_container_log(tail, env)
    assert all(len(line) <= CONTAINER_LOG_LINE_MAX_LENGTH for line in masked)
    return str(mask_secrets_in_json({"failure": {"logTail": list(masked)}}))


async def test_probe_long_env_value_is_masked_before_line_is_cut() -> None:
    value = "LongSecretUniqueXYZ123" * 30  # 660자 — 한 줄 상한 400자보다 길다
    rendered = await _executor_then_processor(f"prefix {value}", {"X": SecretValue(value)})

    assert "LongSecretUniqueXYZ123" not in rendered
    assert "prefix ***" in rendered


async def test_probe_short_secret_under_sensitive_key_is_masked() -> None:
    rendered = await _executor_then_processor(
        "prefix A1b2! suffix", {"DB_PASSWORD": SecretValue("A1b2!")}
    )

    assert "A1b2!" not in rendered
    assert "prefix *** suffix" in rendered


async def test_probe_standalone_jwt_is_masked() -> None:
    rendered = await _executor_then_processor(f"세션 {FAKE_JWT} 만료", {})

    assert FAKE_JWT not in rendered
    assert "eyJhbGciOiJIUzI1NiJ9" not in rendered
    assert mask_secrets(f"id_token={FAKE_JWT}") == "id_token=***"


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("X", "A1b2!", False),  # 5자 + 민감 키 아님 → 가리지 않는다(오탐 방지 규칙)
        ("DEBUG", "true", False),
        ("WORKERS", "4", False),
        ("API_TOKEN", "abc", True),
        ("db_password", "pw1", True),
        ("SESSION_KEY", "1", True),
        ("PORT", "123456", True),  # 6자 이상은 키와 상관없이
        ("API_TOKEN", "", False),  # 빈 값은 가리지 않는다
    ],
)
def test_maskable_env_value_rule(key: str, value: str, expected: bool) -> None:
    assert is_maskable_env_value(key, value) is expected


def test_mask_container_log_masks_value_prefix_cut_at_end_of_received_text() -> None:
    # 실행기가 받은 글이 바이트 상한으로 잘려 마지막 줄 끝에 값 앞부분만 남은 경우.
    value = "cut-secret-value-0123456789"
    masked = mask_container_log(
        ["boot", "PORT=123 server", f"connecting with {value[:12]}"], {"X": SecretValue(value)}
    )

    assert masked == ("boot", "PORT=123 server", "connecting with ***")


def test_mask_container_log_masks_multiline_value_and_caps_after_masking() -> None:
    value = "multi-line-secret-first\nmulti-line-secret-second"
    long_line = "a" * 500 + value.split("\n")[0]
    masked = mask_container_log(
        ["head", *value.split("\n"), long_line], {"PEMISH": SecretValue(value)}
    )

    joined = "\n".join(masked)
    assert "multi-line-secret" not in joined
    assert masked[0] == "head"
    assert masked[1] == "***"
    assert len(masked[-1]) == CONTAINER_LOG_LINE_MAX_LENGTH and masked[-1].endswith("…")
