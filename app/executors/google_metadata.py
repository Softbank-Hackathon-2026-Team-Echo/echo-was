"""GCE·GKE 메타데이터 서버에서 관제탑 GSA(워크로드 신원)의 토큰을 받는다.

- 액세스 토큰(`…/service-accounts/default/token`, 약 1시간): GKE API·클러스터 호출에 쓰고
  만료 5분 전까지 재사용한다. 온프레 이미지 받기 자격은 남은 수명을 정확히 적어야 해서 캐시
  없이 받는다.
- ID 토큰(`…/identity?audience=…&format=full`): AWS STS 웹 신원 교환에 쓴다.

토큰은 SecretValue 로만 다루고 로그·예외에 넣지 않는다. 시험은 base_url·transport 를 바꾼다.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.core.clock import Clock
from app.core.secrets import SecretValue
from app.executors.retry import TransientError

_TOKEN_PATH = "/computeMetadata/v1/instance/service-accounts/default/token"
_IDENTITY_PATH = "/computeMetadata/v1/instance/service-accounts/default/identity"
_HEADERS = {"Metadata-Flavor": "Google"}
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)
METADATA_UNREACHABLE = "METADATA_UNREACHABLE"


class MetadataAuthError(Exception):
    """메타데이터 서버가 토큰을 주지 않았다(서비스 계정 없음 등 4xx)."""


@dataclass(frozen=True)
class AccessToken:
    token: SecretValue
    expires_at: datetime


class GoogleMetadataClient:
    def __init__(
        self,
        base_url: str,
        clock: Clock,
        *,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._clock = clock
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._cached: AccessToken | None = None

    async def access_token(self, *, force: bool = False) -> AccessToken:
        """만료 5분 전까지 재사용하는 액세스 토큰. force 면 새로 받는다(401 뒤)."""
        cached = self._cached
        if (
            not force
            and cached is not None
            and self._clock.now() < cached.expires_at - TOKEN_REFRESH_MARGIN
        ):
            return cached
        self._cached = await self.fresh_access_token()
        return self._cached

    async def fresh_access_token(self) -> AccessToken:
        """캐시 없이 받은 액세스 토큰. expires_at = 지금 + 응답의 expires_in."""
        body = await self._get_json(_TOKEN_PATH, {})
        token, expires_in = body.get("access_token"), body.get("expires_in")
        if not isinstance(token, str) or not token or not isinstance(expires_in, int):
            raise MetadataAuthError
        return AccessToken(
            token=SecretValue(token),
            expires_at=self._clock.now() + timedelta(seconds=expires_in),
        )

    async def identity_token(self, audience: str) -> SecretValue:
        response = await self._get(_IDENTITY_PATH, {"audience": audience, "format": "full"})
        token = response.text.strip()
        if not token:
            raise MetadataAuthError
        return SecretValue(token)

    async def service_account_email(self) -> str:
        """현재 workload 신원 확인. 자격 값은 반환하지 않는다."""
        response = await self._get(
            "/computeMetadata/v1/instance/service-accounts/default/email", {}
        )
        email = response.text.strip()
        if not email.endswith(".iam.gserviceaccount.com") or "@" not in email:
            raise MetadataAuthError
        return email

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _get_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        response = await self._get(path, params)
        try:
            body = response.json()
        except ValueError:
            raise MetadataAuthError from None
        if not isinstance(body, dict):
            raise MetadataAuthError
        return body

    async def _get(self, path: str, params: dict[str, str]) -> httpx.Response:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                transport=self._transport,
                timeout=self._timeout_seconds,
                follow_redirects=False,
            )
        try:
            response = await self._http.get(path, params=params, headers=_HEADERS)
        except httpx.TransportError:
            raise TransientError(METADATA_UNREACHABLE) from None
        if response.status_code >= 500 or response.status_code == 429:
            raise TransientError(METADATA_UNREACHABLE)
        if not response.is_success:
            raise MetadataAuthError
        return response
