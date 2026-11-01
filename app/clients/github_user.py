"""GitHub 사용자 클라이언트: 로그인(코드 교환·내 정보·조직 멤버십)과 초대할 사람 찾기.

GitHub App 의 사용자 인증(웹 흐름)을 쓴다.
1. 콜백의 code 를 `POST {oauth}/login/oauth/access_token` 으로 사용자 토큰과 바꾼다.
2. 그 토큰으로 `GET /user`, 허용 스위치가 있으면 `GET /user/memberships/orgs/{org}` 를 부른다.
3. 초대는 `GET /users/{login}`(공개 API)으로 숫자 번호를 얻는다.

사용자 토큰·client secret 은 SecretValue 또는 지역 변수로만 다루고 저장·로그·예외 메시지에 넣지
않는다. 응답 본문도 로그에 남기지 않는다(상태 코드만).

가짜 구현은 시험 폴더에만 있다. 앱 설정으로 가짜를 고를 수 없다(개발용 로그인 없음).
시험은 httpx.MockTransport 를 transport 로 넣는다(실제 GitHub 호출 금지).
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from app.clients.secret_client import SecretClient, SecretLookupError
from app.core.exceptions import GithubCodeRejectedError, GithubUnavailableError
from app.core.secrets import SecretRef, SecretValue

logger = logging.getLogger(__name__)

_API_VERSION = "2022-11-28"
# GitHub 아이디: 영문·숫자·하이픈 1~39자. 경로에 넣기 전에 형식을 확인한다.
GITHUB_LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


@dataclass(frozen=True)
class GithubUser:
    id: int
    login: str
    name: str | None
    avatar_url: str | None


class GithubUserClient(Protocol):
    async def exchange_code(self, code: str, redirect_uri: str) -> SecretValue:
        """로그인 code 를 사용자 토큰으로 바꾼다. 거절되면 GithubCodeRejectedError."""
        ...

    async def get_user(self, token: SecretValue) -> GithubUser:
        """토큰 주인의 GitHub 정보."""
        ...

    async def is_active_org_member(self, token: SecretValue, org: str) -> bool:
        """토큰 주인이 GitHub 조직 org 의 active 멤버인가. 확인할 수 없으면(403·404) False."""
        ...

    async def find_user(self, login: str) -> GithubUser | None:
        """GitHub 아이디로 사람을 찾는다. 없으면 None."""
        ...


class HttpGithubUserClient:
    def __init__(
        self,
        *,
        oauth_base_url: str,
        api_base_url: str,
        client_id: str | None,
        client_secret_ref: SecretRef | None,
        secret_client: SecretClient,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._oauth_base_url = oauth_base_url.rstrip("/")
        self._api_base_url = api_base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret_ref = client_secret_ref
        self._secret_client = secret_client
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def exchange_code(self, code: str, redirect_uri: str) -> SecretValue:
        if self._client_id is None or self._client_secret_ref is None:
            raise GithubUnavailableError
        client_secret = await self._client_secret()
        async with self._http(self._oauth_base_url) as http:
            response = await self._send(
                http,
                "POST",
                "/login/oauth/access_token",
                headers={"Accept": "application/json"},
                data={
                    "client_id": self._client_id,
                    "client_secret": client_secret.get_secret_value(),
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
            )
        _raise_for_unexpected(response, "exchange_code")
        body = _json(response)
        token = body.get("access_token")
        if isinstance(token, str) and token:
            return SecretValue(token)
        # GitHub 는 거절도 200 + {"error": ...} 로 준다.
        # 코드가 만료·재사용됐거나 콜백 주소가 다를 때다.
        error = body.get("error")
        logger.warning(
            "github rejected the login code",
            extra={
                "action": "exchange_code",
                "github_error": error if isinstance(error, str) else None,
            },
        )
        if isinstance(error, str):
            raise GithubCodeRejectedError
        raise GithubUnavailableError

    async def get_user(self, token: SecretValue) -> GithubUser:
        async with self._http(self._api_base_url) as http:
            response = await self._send(http, "GET", "/user", headers=_bearer(token))
        _raise_for_unexpected(response, "get_user")
        user = _parse_user(_json(response))
        if user is None:
            raise GithubUnavailableError
        return user

    async def is_active_org_member(self, token: SecretValue, org: str) -> bool:
        async with self._http(self._api_base_url) as http:
            response = await self._send(
                http,
                "GET",
                f"/user/memberships/orgs/{quote(org, safe='')}",
                headers=_bearer(token),
            )
        if response.status_code in (403, 404) and not _is_rate_limited(response):
            # 403: App 이 그 조직에 설치되지 않았거나 Members 읽기 권한이 없다. 404: 멤버가 아니다.
            logger.info(
                "github org membership not confirmed",
                extra={"action": "check_org_membership", "status_code": response.status_code},
            )
            return False
        _raise_for_unexpected(response, "check_org_membership")
        state = _json(response).get("state")
        if state != "active":
            logger.info(
                "github org membership not active",
                extra={
                    "action": "check_org_membership",
                    "membership_state": state if isinstance(state, str) else None,
                },
            )
            return False
        return True

    async def find_user(self, login: str) -> GithubUser | None:
        if not GITHUB_LOGIN_PATTERN.fullmatch(login):
            return None
        async with self._http(self._api_base_url) as http:
            response = await self._send(http, "GET", f"/users/{quote(login, safe='')}", headers={})
        if response.status_code == 404:
            return None
        _raise_for_unexpected(response, "find_user")
        user = _parse_user(_json(response))
        if user is None:
            raise GithubUnavailableError
        return user

    async def _client_secret(self) -> SecretValue:
        assert self._client_secret_ref is not None
        try:
            return await self._secret_client.get_secret(self._client_secret_ref)
        except SecretLookupError as exc:
            logger.error(
                "github app client secret unavailable",
                extra={"action": "exchange_code", "reason": exc.reason},
            )
            raise GithubUnavailableError from None

    def _http(self, base_url: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=base_url,
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
        *,
        headers: dict[str, str],
        data: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            return await http.request(method, path, headers=headers, data=data)
        except httpx.TransportError as exc:
            logger.warning(
                "github unreachable",
                extra={"action": "call_github", "exc_type": type(exc).__name__},
            )
            raise GithubUnavailableError from None


def _bearer(token: SecretValue) -> dict[str, str]:
    return {"Authorization": f"Bearer {token.get_secret_value()}"}


def _is_rate_limited(response: httpx.Response) -> bool:
    return response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers


def _raise_for_unexpected(response: httpx.Response, action: str) -> None:
    """2xx 가 아니면 GITHUB_UNAVAILABLE(한도 초과 403·429, 5xx, 인증 설정 오류)."""
    if response.is_success:
        return
    logger.warning(
        "github returned an error",
        extra={"action": action, "status_code": response.status_code},
    )
    raise GithubUnavailableError


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise GithubUnavailableError from None
    if not isinstance(body, dict):
        raise GithubUnavailableError
    return body


def _parse_user(body: dict[str, Any]) -> GithubUser | None:
    user_id, login = body.get("id"), body.get("login")
    if not isinstance(user_id, int) or isinstance(user_id, bool) or not isinstance(login, str):
        return None
    name, avatar_url = body.get("name"), body.get("avatar_url")
    return GithubUser(
        id=user_id,
        login=login,
        name=name if isinstance(name, str) and name else None,
        avatar_url=avatar_url if isinstance(avatar_url, str) and avatar_url else None,
    )
