"""온프레 앱 주소 DNS 레코드(Cloudflare).

환경별 주소 CNAME → 터널(`<id>.cfargotunnel.com`), 프록시 켬.

- ensure: 없으면 만든다. 같은 값(CNAME·대상·프록시)이면 그대로(멱등). 다른 값이 있으면
  DnsRecordConflictError(남의 레코드를 덮지 않는다).
- remove: comment 가 `echo-was:{연결 id}` 인 레코드만 지운다.

토큰은 비밀 참조(CLOUDFLARE_API_TOKEN_REF)로 받아 호출 때 읽는다. 로그·예외에 넣지 않는다.
"""

from typing import Any

import httpx

from app.clients.secret_client import SecretClient, SecretLookupError
from app.core.secrets import SecretRef
from app.executors.retry import TransientError

DNS_UNREACHABLE = "DNS_UNREACHABLE"
COMMENT_PREFIX = "echo-was:"


class DnsRecordConflictError(Exception):
    """같은 이름에 다른 값의 레코드가 있다."""


class DnsUnauthorizedError(Exception):
    """토큰이 없거나 거절됐다(401·403) 또는 그 밖의 4xx."""


def record_comment(service_environment_id: str) -> str:
    return f"{COMMENT_PREFIX}{service_environment_id}"


class CloudflareDnsClient:
    def __init__(
        self,
        *,
        api_base_url: str,
        zone_id: str,
        token_ref: SecretRef,
        secret_client: SecretClient,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_base_url = api_base_url.rstrip("/")
        self._zone_id = zone_id
        self._token_ref = token_ref
        self._secret_client = secret_client
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def ensure_cname(self, host: str, target: str, comment: str) -> None:
        records = await self._records(host)
        if not records:
            await self._call(
                "POST",
                f"/zones/{self._zone_id}/dns_records",
                json={
                    "type": "CNAME",
                    "name": host,
                    "content": target,
                    "proxied": True,
                    "ttl": 1,
                    "comment": comment,
                },
            )
            return
        if all(
            record.get("type") == "CNAME"
            and record.get("content") == target
            and record.get("proxied") is True
            for record in records
        ):
            return
        raise DnsRecordConflictError

    async def remove(self, host: str, comment: str) -> int:
        """comment 가 같은 레코드만 지운다. 지운 개수."""
        removed = 0
        for record in await self._records(host):
            record_id = record.get("id")
            if record.get("comment") == comment and isinstance(record_id, str):
                await self._call("DELETE", f"/zones/{self._zone_id}/dns_records/{record_id}")
                removed += 1
        return removed

    async def _records(self, host: str) -> list[dict[str, Any]]:
        body = await self._call(
            "GET", f"/zones/{self._zone_id}/dns_records", params={"name": host, "per_page": "50"}
        )
        result = body.get("result")
        if not isinstance(result, list):
            raise TransientError(DNS_UNREACHABLE)
        return [record for record in result if isinstance(record, dict)]

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            token = await self._secret_client.get_secret(self._token_ref)
        except SecretLookupError:
            raise DnsUnauthorizedError from None
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self._api_base_url,
                transport=self._transport,
                timeout=self._timeout_seconds,
                follow_redirects=False,
            )
        try:
            response = await self._http.request(
                method,
                path,
                params=params,
                json=json,
                headers={"Authorization": f"Bearer {token.get_secret_value()}"},
            )
        except httpx.TransportError:
            raise TransientError(DNS_UNREACHABLE) from None
        if response.status_code >= 500 or response.status_code == 429:
            raise TransientError(DNS_UNREACHABLE)
        if method == "DELETE" and response.status_code == 404:
            return {}
        if not response.is_success:
            raise DnsUnauthorizedError
        try:
            body = response.json()
        except ValueError:
            raise TransientError(DNS_UNREACHABLE) from None
        return body if isinstance(body, dict) else {}
