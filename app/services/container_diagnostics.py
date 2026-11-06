"""배포 실패 진단(API 서버 4-1): 컨테이너 로그 꼬리 가리기·원인 한 줄·배포 작업 로그 문장·실패 상세.

흐름
- 실행기 poll 이 실패를 판정하면 되돌리기 전에 읽은 ContainerDiagnostics 를 DeployFailed 에 담는다.
- 처리기가 그 배포의 앱 환경 변수 값·접속 문자열 비밀번호·`password=` 꼴 값을 가린 뒤
  (mask_container_log) 배포 작업 로그에 CONTAINER_DIAGNOSTICS 1줄 + CONTAINER_LOG 줄(최대 50)을
  쓰고, FAILED 줄 문장 뒤에 원인 한 줄(last_error_line)을 붙인다.
- 배포 요청 상세는 그 줄들로 failure 칸을 만든다(build_failure_detail). 저장 칸을 새로 만들지 않는다
  (마이그레이션 없음).
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.core.log_masking import MASK, mask_secrets
from app.core.secrets import SecretValue
from app.enums import DeployLogCode, DeployRequestErrorCode, DeployRequestStatus
from app.executors.base import CONTAINER_LOG_LINE_MAX_LENGTH, ContainerDiagnostics
from app.models.deploy_request_log import DEPLOY_LOG_MESSAGE_MAX_LENGTH
from app.services.deploy_log_messages import ERROR_MESSAGES

# 이 길이 이상인 환경 변수 값은 키와 상관없이 가린다. 더 짧은 값은 키 이름이 SENSITIVE_ENV_KEY
# 모양일 때만 가린다("true"·"8080"·"1" 같은 짧은 설정값이 로그 낱말을 모두 지우지 않게).
ENV_VALUE_MIN_MASK_LENGTH = 6
# 짧은 값도 가리는 민감 키 이름(대소문자 무시, 이름 어디에 있어도).
SENSITIVE_ENV_KEY = re.compile(
    r"(?i)password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key"
    r"|credential|auth|session|cookie|salt|signing|dsn|database[_-]?url|conn(?:ection)?[_-]?str"
)
LAST_ERROR_MAX_LENGTH = 200
LAST_ERROR_PREFIX = " Last error: "

# 배포 상세 failure 칸을 만들 때 읽는 배포 작업 로그 코드.
FAILURE_LOG_CODES = (
    DeployLogCode.CONTAINER_DIAGNOSTICS,
    DeployLogCode.CONTAINER_LOG,
    DeployLogCode.FAILED,
    DeployLogCode.REVERT_SUCCEEDED,
    DeployLogCode.REVERT_SUCCEEDED_NO_PREVIOUS,
    DeployLogCode.REVERT_FAILED,
)

# scheme://user:password@host → scheme://user:***@host
_URL_PASSWORD = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+@")
# password=..., "token": "...", api_key: ... 의 값
_KEY_VALUE_SECRET = re.compile(
    r"(?i)\b((?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)[\"']?\s*[=:]\s*[\"']?)"
    r"[^\s\"',}]+"
)
_EXCEPTION = re.compile(r"(?:^|[\s.])([A-Z][A-Za-z0-9_]*(?:Error|Exception))\b(.*)$")
_FATAL = re.compile(r"(?i)^(?:panic|fatal(?: error)?|error)\s*:")
_PYDANTIC_HEADER = re.compile(r"^:\s*\d+ validation errors? for ")
_PYDANTIC_FIELD = re.compile(r"^[A-Za-z_][\w.\[\]-]*$")
_SUMMARY = re.compile(
    r"^Container (?P<container>[^:\s]+): reason=(?P<reason>[^,\s]+), "
    r"terminationReason=(?P<termination>[^,\s]+), exitCode=(?P<exit>-?\d+|-), "
    r"restartCount=(?P<restarts>\d+|-), logLines=\d+, truncated=(?P<truncated>true|false)\.$"
)
_NONE = "-"


def is_maskable_env_value(key: str, value: str) -> bool:
    """로그에서 가릴 환경 변수 값인가. 빈 값은 가리지 않는다. ENV_VALUE_MIN_MASK_LENGTH 자 이상이면
    키와 상관없이, 그보다 짧으면 키 이름이 민감 키 모양(SENSITIVE_ENV_KEY)일 때만 가린다
    ("true"·"8080"·"1" 같은 짧은 설정값이 로그 낱말을 모두 지우지 않게)."""
    if not value:
        return False
    return len(value) >= ENV_VALUE_MIN_MASK_LENGTH or SENSITIVE_ENV_KEY.search(key) is not None


def mask_container_log(
    lines: Sequence[str], env_values: Mapping[str, SecretValue] | None
) -> tuple[str, ...]:
    """그 배포의 환경 변수 값(is_maskable_env_value), 접속 문자열 비밀번호, `password=` 꼴 값,
    알려진 토큰 모양을 MASK 로 바꾼 뒤 한 줄을 CONTAINER_LOG_LINE_MAX_LENGTH 자로 자른 사본.

    순서가 중요하다: 가리기 → 자르기. 먼저 자르면 잘린 줄 끝의 값 앞부분이 전체 값 비교에 안 맞아
    그대로 남는다. 실행기는 줄을 자르지 않고 넘긴다. 실행기가 받은 글 자체가 바이트 상한
    (limitBytes)으로 잘려 마지막 줄 끝에 값 앞부분만 남은 경우를 위해, 가린 뒤에도 마지막 줄 끝이
    가릴 값의 앞 ENV_VALUE_MIN_MASK_LENGTH 자 이상과 같으면 그 부분을 가린다.

    여러 줄에 걸친 값(PEM 등)도 맞도록 줄을 이어 붙인 글에서 값을 바꾼다. 그 값이 실행기의
    mask_secrets 로 이미 일부 바뀐 꼴도 함께 찾는다.
    """
    values = _mask_targets(env_values)
    text = "\n".join(lines)
    for value in values:
        text = text.replace(value, MASK)
    split = text.split("\n") if lines else []
    if split:
        # 바이트 상한 절단은 받은 글의 끝, 곧 마지막 줄에서만 일어난다.
        split[-1] = _mask_trailing_prefix(split[-1], values)
    masked: list[str] = []
    for line in split:
        line = _URL_PASSWORD.sub(rf"\1{MASK}@", line)
        line = _KEY_VALUE_SECRET.sub(rf"\1{MASK}", line)
        masked.append(_cap_line(mask_secrets(line)))
    return tuple(masked)


def _mask_targets(env_values: Mapping[str, SecretValue] | None) -> list[str]:
    targets: set[str] = set()
    for key, secret in (env_values or {}).items():
        value = secret.get_secret_value()
        if not is_maskable_env_value(key, value):
            continue
        targets.add(value)
        pre_masked = mask_secrets(value)
        if pre_masked != value and pre_masked.strip(MASK + " "):
            targets.add(pre_masked)
    # 긴 값부터 바꿔 한 값이 다른 값의 일부일 때 앞부분이 남지 않게 한다.
    return sorted(targets, key=len, reverse=True)


def _mask_trailing_prefix(line: str, values: Sequence[str]) -> str:
    for value in values:
        for size in range(len(value) - 1, ENV_VALUE_MIN_MASK_LENGTH - 1, -1):
            if line.endswith(value[:size]):
                return line[:-size] + MASK
    return line


def _cap_line(line: str) -> str:
    if len(line) <= CONTAINER_LOG_LINE_MAX_LENGTH:
        return line
    return line[: CONTAINER_LOG_LINE_MAX_LENGTH - 1] + "…"


def last_error_line(lines: Sequence[str]) -> str | None:
    """사람이 읽을 원인 한 줄. 마지막 예외 줄(`…Error: …`·`…Exception: …`), 없으면 마지막
    `panic:`·`fatal:`·`error:` 줄, 없으면 마지막 빈 줄 아닌 줄.

    pydantic 설정 오류(`ValidationError: 1 validation error for Settings` 뒤 필드 이름 줄)는
    `ValidationError: database_url` 처럼 필드 이름으로 줄인다.
    """
    for index in range(len(lines) - 1, -1, -1):
        match = _EXCEPTION.search(lines[index].strip())
        if match is None:
            continue
        name, rest = match[1], match[2]
        if name == "ValidationError" and _PYDANTIC_HEADER.match(rest):
            fields = [
                line.strip()
                for line in lines[index + 1 :]
                if line and not line[0].isspace() and _PYDANTIC_FIELD.fullmatch(line.strip())
            ]
            if fields:
                return _cap(f"ValidationError: {', '.join(fields)}", LAST_ERROR_MAX_LENGTH)
        return _cap(f"{name}{rest}", LAST_ERROR_MAX_LENGTH)
    for line in reversed(lines):
        if _FATAL.match(line.strip()):
            return _cap(line.strip(), LAST_ERROR_MAX_LENGTH)
    for line in reversed(lines):
        if line.strip():
            return _cap(line.strip(), LAST_ERROR_MAX_LENGTH)
    return None


def diagnostics_message(diagnostics: ContainerDiagnostics) -> str:
    """CONTAINER_DIAGNOSTICS 줄 문장. build_failure_detail 이 같은 모양으로 다시 읽는다."""
    return (
        f"Container {_text(diagnostics.container)}: reason={_text(diagnostics.reason)}, "
        f"terminationReason={_text(diagnostics.termination_reason)}, "
        f"exitCode={_number(diagnostics.exit_code)}, "
        f"restartCount={_number(diagnostics.restart_count)}, "
        f"logLines={len(diagnostics.log_tail)}, "
        f"truncated={'true' if diagnostics.log_truncated else 'false'}."
    )


def failed_message(code: DeployRequestErrorCode, diagnostics: ContainerDiagnostics | None) -> str:
    """FAILED 줄·failure.message 문장. 원인 한 줄을 읽었으면 고정 문장 뒤에 붙인다."""
    base = ERROR_MESSAGES[code]
    reason = last_error_line(diagnostics.log_tail) if diagnostics is not None else None
    if reason is None and diagnostics is not None and diagnostics.termination_reason:
        # 로그가 없어도 OOMKilled 같은 종료 이유는 원인이 된다.
        reason = diagnostics.termination_reason
        if diagnostics.exit_code is not None:
            reason = f"{reason} (exit code {diagnostics.exit_code})"
    if reason is None:
        return base
    return _cap(f"{base}{LAST_ERROR_PREFIX}{reason}", DEPLOY_LOG_MESSAGE_MAX_LENGTH)


@dataclass(frozen=True)
class FailureDetail:
    """배포 요청 상세의 failure 칸(실패한 요청만). 컨테이너 칸은 진단이 없으면 None·빈 값."""

    code: DeployRequestErrorCode
    message: str
    container: str | None
    container_reason: str | None
    termination_reason: str | None
    exit_code: int | None
    restart_count: int | None
    log_tail: list[str]
    log_truncated: bool
    # 되돌리기 뒤 이전 버전이 실행 중인가. 이전 배포가 없었으면 False, 되돌리기 전·실패면 None.
    previous_version_running: bool | None


def build_failure_detail(
    status: str, error_code: str | None, rows: Sequence[tuple[str, str]]
) -> FailureDetail | None:
    """(code, message) 배포 작업 로그 줄(seq 순, FAILURE_LOG_CODES 만)로 failure 칸을 만든다."""
    if status != DeployRequestStatus.FAILED or error_code is None:
        return None
    code = DeployRequestErrorCode(error_code)
    message = ERROR_MESSAGES[code]
    summary: re.Match[str] | None = None
    tail: list[str] = []
    previous_running: bool | None = None
    for row_code, row_message in rows:
        if row_code == DeployLogCode.CONTAINER_DIAGNOSTICS:
            summary = _SUMMARY.fullmatch(row_message)
        elif row_code == DeployLogCode.CONTAINER_LOG:
            tail.append(mask_secrets(row_message))
        elif row_code == DeployLogCode.FAILED:
            message = mask_secrets(row_message)
        elif row_code == DeployLogCode.REVERT_SUCCEEDED:
            previous_running = True
        elif row_code == DeployLogCode.REVERT_SUCCEEDED_NO_PREVIOUS:
            previous_running = False
    return FailureDetail(
        code=code,
        message=message,
        container=_parsed(summary, "container"),
        container_reason=_parsed(summary, "reason"),
        termination_reason=_parsed(summary, "termination"),
        exit_code=_parsed_int(summary, "exit"),
        restart_count=_parsed_int(summary, "restarts"),
        log_tail=tail,
        log_truncated=summary is not None and summary["truncated"] == "true",
        previous_version_running=previous_running,
    )


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _text(value: str | None) -> str:
    return value if value else _NONE


def _number(value: int | None) -> str:
    return _NONE if value is None else str(value)


def _parsed(summary: re.Match[str] | None, name: str) -> str | None:
    if summary is None or summary[name] == _NONE:
        return None
    return summary[name]


def _parsed_int(summary: re.Match[str] | None, name: str) -> int | None:
    value = _parsed(summary, name)
    return int(value) if value is not None else None
