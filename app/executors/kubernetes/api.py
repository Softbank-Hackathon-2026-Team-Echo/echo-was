"""쿠버네티스 REST 를 SDK 없이 httpx 로 부른다.

- 서버 측 apply = `PATCH ...?fieldManager=echo-was&force=true`, 본문 JSON(YAML 의 부분집합),
  Content-Type `application/apply-patch+yaml`.
- 창구(서버 주소·CA)마다 httpx 클라이언트 하나를 재사용한다(keep-alive).
  터널 창구는 새 연결이 1~3초 걸린다.
- 401 이면 토큰을 다시 얻어 한 번 더 부른다. 그래도 401·403 이면 ClusterAuthError.
- 연결 실패·5xx·429 는 TransientError, 그 밖의 4xx(400·409·422 등)는 KubeRequestError.
"""

import json
import ssl
from collections.abc import Callable
from typing import Any

import httpx

from app.executors.base import ClusterEnvironment
from app.executors.kubernetes.access import (
    CLUSTER_UNREACHABLE,
    ClusterAccess,
    ClusterAccessProvider,
    ClusterAuthError,
)
from app.executors.retry import TransientError

FIELD_MANAGER = "echo-was"
APPLY_CONTENT_TYPE = "application/apply-patch+yaml"

KubeHttpFactory = Callable[[str, str | None, float], httpx.AsyncClient]


def default_kube_http_factory(server: str, ca_pem: str | None, timeout: float) -> httpx.AsyncClient:
    verify: ssl.SSLContext | bool = (
        ssl.create_default_context(cadata=ca_pem) if ca_pem is not None else True
    )
    return httpx.AsyncClient(
        base_url=server, verify=verify, timeout=timeout, follow_redirects=False
    )


class KubeRequestError(Exception):
    """창구가 요청을 거절했다(400·409·422 등). 다시 불러도 같다. status 만 담는다."""

    def __init__(self, status_code: int) -> None:
        super().__init__(status_code)
        self.status_code = status_code


class KubeHttpClients:
    """창구별 httpx 클라이언트 묶음. 프로세스가 끝날 때 aclose."""

    def __init__(self, factory: KubeHttpFactory, timeout_seconds: float) -> None:
        self._factory = factory
        self._timeout_seconds = timeout_seconds
        self._clients: dict[tuple[str, str | None], httpx.AsyncClient] = {}

    def get(self, access: ClusterAccess) -> httpx.AsyncClient:
        key = (access.server, access.ca_pem)
        client = self._clients.get(key)
        if client is None:
            client = self._factory(access.server, access.ca_pem, self._timeout_seconds)
            self._clients[key] = client
        return client

    async def aclose(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            await client.aclose()


class KubeApi:
    """환경 하나의 창구."""

    def __init__(
        self,
        environment: ClusterEnvironment,
        provider: ClusterAccessProvider,
        clients: KubeHttpClients,
    ) -> None:
        self._environment = environment
        self._provider = provider
        self._clients = clients

    async def get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any] | None:
        """객체 하나. 404 면 None."""
        response = await self._send("GET", path, params=params)
        if response.status_code == 404:
            return None
        _check(response)
        return _json(response)

    async def get_text(self, path: str, params: dict[str, str] | None = None) -> str | None:
        response = await self._send("GET", path, params=params)
        if response.status_code in (400, 404):
            # 400: 컨테이너가 아직 시작 전이라 로그가 없음.
            return None
        _check(response)
        return response.text

    async def apply(self, path: str, manifest: dict[str, Any]) -> dict[str, Any]:
        response = await self._send(
            "PATCH",
            path,
            params={"fieldManager": FIELD_MANAGER, "force": "true"},
            content=json.dumps(manifest, separators=(",", ":"), sort_keys=True),
            content_type=APPLY_CONTENT_TYPE,
        )
        _check(response)
        return _json(response)

    async def create(self, collection_path: str, manifest: dict[str, Any]) -> bool:
        """만든다. 이미 있으면(409) False.

        apply 권한이 없는 자원(네임스페이스·RoleBinding)에 쓴다.
        """
        response = await self._send(
            "POST",
            collection_path,
            content=json.dumps(manifest, separators=(",", ":"), sort_keys=True),
            content_type="application/json",
        )
        if response.status_code == 409:
            return False
        _check(response)
        return True

    async def delete(self, path: str) -> bool:
        """지운다(백그라운드 전파). 없으면(404) False."""
        response = await self._send("DELETE", path, params={"propagationPolicy": "Background"})
        if response.status_code == 404:
            return False
        _check(response)
        return True

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        content: str | None = None,
        content_type: str | None = None,
    ) -> httpx.Response:
        response = await self._send_once(method, path, params, content, content_type, False)
        if response.status_code == 401:
            # 캐시한 토큰이 만료·취소됐을 수 있다. 새로 얻어 한 번만 다시 부른다.
            response = await self._send_once(method, path, params, content, content_type, True)
        if response.status_code in (401, 403):
            raise ClusterAuthError
        if response.status_code >= 500 or response.status_code == 429:
            raise TransientError(CLUSTER_UNREACHABLE)
        return response

    async def _send_once(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None,
        content: str | None,
        content_type: str | None,
        refresh: bool,
    ) -> httpx.Response:
        access = await self._provider.access(self._environment, refresh=refresh)
        headers = {
            "Authorization": f"Bearer {access.token.get_secret_value()}",
            "Accept": "application/json",
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        try:
            return await self._clients.get(access).request(
                method, path, params=params, content=content, headers=headers
            )
        except httpx.TransportError:
            raise TransientError(CLUSTER_UNREACHABLE) from None


def _check(response: httpx.Response) -> None:
    if not response.is_success:
        raise KubeRequestError(response.status_code)


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise TransientError(CLUSTER_UNREACHABLE) from None
    if not isinstance(body, dict):
        raise TransientError(CLUSTER_UNREACHABLE)
    return body
