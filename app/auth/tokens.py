"""세션 쿠키·에이전트 토큰·러너 토큰·실행 토큰·로그인 상태 값 만들기와 해시.

원문은 SecretValue 로만 들고 다닌다. DB 에는 SHA-256(16진수 64자)만 둔다.
"""

import base64
import binascii
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from app.core.secrets import SecretValue

TOKEN_BYTES = 32
AGENT_TOKEN_PREFIX = "echo_agt_"
RUNNER_TOKEN_PREFIX = "echo_rnr_"
RUN_TOKEN_PREFIX = "echo_run_"
AGENT_TOKEN_LAST_CHARS = 4
_STATE_SEPARATOR = "."


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_session_token() -> SecretValue:
    return SecretValue(secrets.token_urlsafe(TOKEN_BYTES))


def new_agent_token() -> SecretValue:
    return SecretValue(AGENT_TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES))


def new_runner_token() -> SecretValue:
    return SecretValue(RUNNER_TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES))


def new_run_token() -> SecretValue:
    return SecretValue(RUN_TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES))


def new_oauth_state() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def is_same_secret(left: str, right: str) -> bool:
    """상수 시간 비교."""
    return hmac.compare_digest(left.encode(), right.encode())


@dataclass(frozen=True)
class OauthState:
    """로그인 상태 쿠키에 담는 값: 위조 방지 state, 만든 시각(초), 로그인 뒤 갈 경로."""

    state: str
    issued_at: int
    next_path: str

    def encode(self) -> str:
        encoded_next = base64.urlsafe_b64encode(self.next_path.encode()).decode().rstrip("=")
        return _STATE_SEPARATOR.join((self.state, str(self.issued_at), encoded_next))

    @classmethod
    def decode(cls, value: str) -> "OauthState | None":
        parts = value.split(_STATE_SEPARATOR)
        if len(parts) != 3 or not parts[0] or not parts[1].isdigit():
            return None
        try:
            padded = parts[2] + "=" * (-len(parts[2]) % 4)
            next_path = base64.b64decode(padded.encode(), altchars=b"-_", validate=True).decode()
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return None
        return cls(state=parts[0], issued_at=int(parts[1]), next_path=next_path)


def safe_next_path(value: str | None) -> str:
    """로그인 뒤 갈 경로는 `/` 로 시작하는 상대 경로만 받는다. 그 밖은 `/`.

    `//host`·`/\\host`(브라우저가 다른 호스트로 읽음)·스킴·제어 문자가 섞이면 `/` 로 바꾼다.
    """
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    if "\\" in value or ":" in value.split("?", 1)[0]:
        return "/"
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return "/"
    return value
