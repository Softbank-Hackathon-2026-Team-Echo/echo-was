"""GitHub App 으로 저장소를 읽는 소스 저장소 클라이언트(`source_client_mode = github`).

1. App JWT(RS256, iss = App ID, 유효 10분 이하)로 `GET /repos/{o}/{r}/installation` 을 부른다.
2. 설치가 있으면 `POST /app/installations/{id}/access_tokens` 로 그 저장소 읽기 전용 토큰
   (1시간)을 받아 저장소·커밋을 읽는다. 설치가 없으면 인증 없이 공개 저장소로 읽는다.
3. 저장소 응답의 `private` 값을 그대로 쓴다.

서버에 두는 비밀값은 App 개인키 하나이고, 설정에는 비밀 저장소 위치(github_app_private_key_ref)만
둔다. 개인키·JWT·설치 토큰은 SecretValue 또는 지역 변수로만 다루고 로그·예외 메시지에 넣지
않는다. 응답 본문도 로그에 남기지 않는다(상태 코드만).

시험은 httpx.MockTransport 를 transport 로 넣는다(실제 GitHub 호출 금지).
"""

import logging
from datetime import timedelta
from typing import Any
from urllib.parse import quote

import httpx
import jwt

from app.clients.secret_client import SecretClient, SecretLookupError
from app.clients.source_repository import RepoAccess
from app.core.clock import Clock
from app.core.exceptions import (
    RefNotFoundError,
    RepoNotAccessibleError,
    SourceProviderUnavailableError,
)
from app.core.repo_url import RepoLocator
from app.core.secrets import SecretRef, SecretValue

logger = logging.getLogger(__name__)

# GitHub 는 exp 가 10분을 넘으면 거부한다. 서버 시계 차이를 감안해 iat 를 60초 앞당긴다.
APP_JWT_BACKDATE = timedelta(seconds=60)
APP_JWT_LIFETIME = timedelta(minutes=10)
_API_VERSION = "2022-11-28"
_RATE_LIMIT_STATUSES = frozenset({403, 429})


class GithubAppSourceRepositoryClient:
    def __init__(
        self,
        *,
        api_base_url: str,
        app_id: str | None,
        private_key_ref: SecretRef | None,
        secret_client: SecretClient,
        install_url: str | None,
        timeout_seconds: float,
        clock: Clock,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_base_url = api_base_url.rstrip("/")
        self._app_id = app_id
        self._private_key_ref = private_key_ref
        self._secret_client = secret_client
        self._install_url = install_url
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._transport = transport

    @property
    def simulated(self) -> bool:
        return False

    async def check_access(self, repo: RepoLocator) -> RepoAccess:
        async with self._http() as http:
            token = await self._read_token(http, repo)
            return await self._get_repo(http, repo, token)

    async def resolve_ref(self, repo: RepoLocator, ref: str) -> str:
        async with self._http() as http:
            token = await self._read_token(http, repo)
            # 저장소 접근부터 확인해야 '저장소 없음'과 'ref 없음'을 가를 수 있다.
            await self._get_repo(http, repo, token)
            response = await self._send(
                http,
                "GET",
                f"/repos/{repo.owner}/{repo.name}/commits/{quote(ref, safe='/')}",
                _bearer(token),
            )
            if response.status_code in (404, 409, 422):
                raise RefNotFoundError
            _raise_for_unexpected(response, "resolve_ref")
            sha = _json(response).get("sha")
            if not isinstance(sha, str) or len(sha) != 40:
                raise SourceProviderUnavailableError
            return sha.lower()

    async def issue_read_token(self, repo: RepoLocator) -> SecretValue | None:
        async with self._http() as http:
            return await self._read_token(http, repo)

    async def _read_token(self, http: httpx.AsyncClient, repo: RepoLocator) -> SecretValue | None:
        """App 이 이 저장소에 설치돼 있으면 읽기 전용 설치 토큰, 아니면 None(공개 저장소로 읽음)."""
        if self._app_id is None or self._private_key_ref is None:
            return None
        app_jwt = await self._app_jwt()
        response = await self._send(
            http, "GET", f"/repos/{repo.owner}/{repo.name}/installation", _bearer(app_jwt)
        )
        if response.status_code == 404:
            return None
        _raise_for_unexpected(response, "find_installation")
        installation_id = _json(response).get("id")
        if not isinstance(installation_id, int):
            raise SourceProviderUnavailableError
        response = await self._send(
            http,
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            _bearer(app_jwt),
            json={"repositories": [repo.name], "permissions": {"contents": "read"}},
        )
        _raise_for_unexpected(response, "issue_installation_token")
        token = _json(response).get("token")
        if not isinstance(token, str) or not token:
            raise SourceProviderUnavailableError
        return SecretValue(token)

    async def _get_repo(
        self, http: httpx.AsyncClient, repo: RepoLocator, token: SecretValue | None
    ) -> RepoAccess:
        response = await self._send(http, "GET", f"/repos/{repo.owner}/{repo.name}", _bearer(token))
        if response.status_code == 404 or (
            response.status_code == 403 and not _is_rate_limited(response)
        ):
            raise RepoNotAccessibleError(self._install_url)
        _raise_for_unexpected(response, "get_repository")
        body = _json(response)
        private, default_branch = body.get("private"), body.get("default_branch")
        if not isinstance(private, bool) or not isinstance(default_branch, str):
            raise SourceProviderUnavailableError
        return RepoAccess(private=private, default_branch=default_branch)

    async def _app_jwt(self) -> SecretValue:
        assert self._private_key_ref is not None
        try:
            private_key = await self._secret_client.get_secret(self._private_key_ref)
        except SecretLookupError as exc:
            logger.error(
                "github app private key unavailable",
                extra={"action": "sign_app_jwt", "reason": exc.reason},
            )
            raise SourceProviderUnavailableError from None
        now = self._clock.now()
        issued_at = now - APP_JWT_BACKDATE
        payload = {
            "iat": int(issued_at.timestamp()),
            "exp": int((issued_at + APP_JWT_LIFETIME).timestamp()),
            "iss": self._app_id,
        }
        try:
            encoded = jwt.encode(payload, private_key.get_secret_value(), algorithm="RS256")
        except (jwt.PyJWTError, ValueError, TypeError):
            # 예외 메시지에 키 내용이 섞일 수 있어 원인을 붙이지 않는다.
            logger.error("github app jwt signing failed", extra={"action": "sign_app_jwt"})
            raise SourceProviderUnavailableError from None
        return SecretValue(encoded)

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._api_base_url,
            transport=self._transport,
            timeout=self._timeout_seconds,
            follow_redirects=False,
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": _API_VERSION},
        )

    @staticmethod
    async def _send(
        http: httpx.AsyncClient,
        method: str,
        path: str,
        headers: dict[str, str],
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            return await http.request(method, path, headers=headers, json=json)
        except httpx.TransportError as exc:
            logger.warning(
                "source provider unreachable",
                extra={"action": "call_source_provider", "exc_type": type(exc).__name__},
            )
            raise SourceProviderUnavailableError from None


def _bearer(token: SecretValue | None) -> dict[str, str]:
    if token is None:
        return {}
    return {"Authorization": f"Bearer {token.get_secret_value()}"}


def _is_rate_limited(response: httpx.Response) -> bool:
    return response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers


def _raise_for_unexpected(response: httpx.Response, action: str) -> None:
    """2xx 가 아니면 SOURCE_PROVIDER_UNAVAILABLE 로 바꾼다(한도 초과·5xx·인증 설정 오류)."""
    if response.is_success:
        return
    logger.warning(
        "source provider returned an error",
        extra={
            "action": action,
            "status_code": response.status_code,
            "rate_limited": response.status_code in _RATE_LIMIT_STATUSES
            and _is_rate_limited(response),
        },
    )
    raise SourceProviderUnavailableError


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise SourceProviderUnavailableError from None
    if not isinstance(body, dict):
        raise SourceProviderUnavailableError
    return body
