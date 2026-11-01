"""비밀값 조회기: secret_ref 가 가리키는 비밀 저장소에서 값을 읽는다.

지금은 `env` 저장소(서버 프로세스 환경변수)만 실제로 읽는다. `k8s`·`aws-sm`·`gcp-sm` 은 형식만
받고 조회는 하지 않는다(SECRET_STORE_UNSUPPORTED).

환경변수 이름이 secret_ref 에 따라 달라 Settings 필드로 선언할 수 없다. 그래서 이 파일에서만
os.environ 을 직접 읽는다(backend-conventions 의 예외). `.env` 파일은 보지 않는다.
로컬에서는 `uv run --env-file .env ...` 로 프로세스 환경변수에 넣는다.
"""

import os
from typing import Protocol

from app.core.exceptions import ExternalError
from app.core.secrets import SecretRef, SecretStore, SecretValue
from app.enums import ConnectionErrorCode


class SecretLookupError(ExternalError):
    """비밀값을 읽지 못했다. reason 은 접속 실패 이유 코드다. 메시지에 값·위치를 넣지 않는다."""

    code = "SECRET_LOOKUP_FAILED"

    def __init__(self, reason: ConnectionErrorCode) -> None:
        super().__init__("secret lookup failed", reason=reason)
        self.reason = reason


class SecretClient(Protocol):
    async def get_secret(self, ref: SecretRef) -> SecretValue:
        """ref 의 비밀값을 읽는다. 없거나 읽지 못하면 SecretLookupError."""
        ...


class EnvVarSecretClient:
    """`env` 저장소만 읽는 조회기. 다른 저장소는 SECRET_STORE_UNSUPPORTED 로 실패한다."""

    async def get_secret(self, ref: SecretRef) -> SecretValue:
        if ref.store is not SecretStore.ENV:
            raise SecretLookupError(ConnectionErrorCode.SECRET_STORE_UNSUPPORTED)
        value = os.environ.get(ref.location)
        if not value:
            raise SecretLookupError(ConnectionErrorCode.SECRET_NOT_FOUND)
        return SecretValue(value)
