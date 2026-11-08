"""허용된 내부 대상만 관측한다. 공개 TLS 검사기의 허용 범위는 바꾸지 않는다."""

import asyncio
import ipaddress
import json
import socket
import ssl
from datetime import datetime
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from app.clients.environment_iam import check
from app.clients.secret_client import SecretClient
from app.clients.tls_probe import SocketTlsProbeClient
from app.core.clock import Clock
from app.core.secrets import parse_secret_ref
from app.enums import SecurityFindingReason, SecurityFindingSource, SecurityResult
from app.executors.kubernetes.api import KubeApi
from app.security_environment.types import (
    EVIDENCE_TTL,
    EvidenceCheck,
    TransportObservation,
    TransportTarget,
    unknown,
)

TLS = SecurityFindingSource.TLS_PROBE
DATA = SecurityFindingSource.DATA_ENGINE
MAX_OBSERVATION_BYTES = 65536


def safe_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return safe_address(str(address.ipv4_mapped))
    return not (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    )


async def resolve_managed(host: str, port: int) -> str:
    addresses = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ips = {str(item[4][0]) for item in addresses}
    if not ips or not all(safe_address(ip) for ip in ips):
        raise ValueError("unsafe address")
    return sorted(ips)[0]


async def inspect_http(target: TransportTarget, now: datetime) -> list[EvidenceCheck]:
    parsed = urlsplit(target.url or "")
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    address = await resolve_managed(host, port)
    if parsed.scheme == "http":
        # 접근 가능한 실제 평문 경로임을 확인하며 인증 정보나 변경 요청은 보내지 않는다.
        _, writer = await asyncio.wait_for(asyncio.open_connection(address, port), 5)
        writer.close()
        await writer.wait_closed()
        public = ipaddress.ip_address(address).is_global
        return [
            check(
                target.id,
                "PUBLIC_PLAINTEXT_TRANSPORT" if public else "PLAINTEXT_TRANSPORT",
                SecurityResult.FAIL if public else SecurityResult.WARN,
                now,
                TLS,
            )
        ]
    context = ssl.create_default_context(cafile=target.ca_file)
    result = await SocketTlsProbeClient(5, context).probe(host, port, address)
    if result.reason == SecurityFindingReason.TLS_OK:
        return [check(target.id, "TLS_VERIFIED", SecurityResult.PASS, now, TLS)]
    status = (
        SecurityResult.UNKNOWN
        if result.reason
        in (SecurityFindingReason.TLS_TIMEOUT, SecurityFindingReason.TLS_UNREACHABLE)
        else SecurityResult.WARN
    )
    return [check(target.id, result.reason.value, status, now, TLS)]


async def inspect_entrypoint(
    target: TransportTarget, secrets: SecretClient, clock: Clock
) -> list[EvidenceCheck]:
    parsed = urlsplit(target.url or "")
    # 내부 관리 API는 고정 IP만 허용해 HTTP 연결의 DNS 재해석을 막는다.
    host = parsed.hostname or ""
    if not safe_address(host):
        raise ValueError("unsafe entrypoint address")
    secret = await secrets.get_secret(parse_secret_ref(target.token_ref or ""))
    context = ssl.create_default_context(cafile=target.ca_file)
    async with httpx.AsyncClient(
        timeout=5, verify=context, trust_env=False, follow_redirects=False
    ) as http:
        async with http.stream(
            "GET",
            (target.url or "") + "/security/transport",
            params={"route": target.route},
            headers={"Authorization": "Bearer " + secret.get_secret_value()},
        ) as response:
            response.raise_for_status()
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_OBSERVATION_BYTES:
                    raise ValueError("oversized observation")
    observation = TransportObservation.model_validate_json(bytes(raw))
    return select_observation(observation, target, clock.now(), TLS)


def select_observation(
    observation: TransportObservation,
    target: TransportTarget,
    now: datetime,
    source: SecurityFindingSource,
) -> list[EvidenceCheck]:
    if (
        observation.revision != target.snapshot_revision
        or observation.observed_at > now
        or now - observation.observed_at >= EVIDENCE_TTL
    ):
        return [unknown(target.id, "OBSERVATION_EXPIRED_OR_CHANGED", now, source)]
    expected = target.route or target.id
    selected = [item for item in observation.checks if item.id == expected]
    if (
        not selected
        or len({item.reason for item in selected}) != len(selected)
        or any(
            item.source != source
            or item.observed_at > observation.observed_at
            or now - item.observed_at >= EVIDENCE_TTL
            for item in selected
        )
    ):
        return [unknown(target.id, "OBSERVATION_INCOMPLETE", now, source)]
    return [item.model_copy(update={"id": target.id}) for item in selected]


async def inspect_database(
    api: KubeApi, target: TransportTarget, clock: Clock
) -> list[EvidenceCheck]:
    # 고정 관리 namespace의 RBAC으로 관측 결과 생성 주체를 제한한다.
    if target.snapshot_namespace != "echo-security":
        return [unknown(target.id, "UNTRUSTED_SNAPSHOT_NAMESPACE", clock.now(), DATA)]
    obj = await api.get(
        f"/api/v1/namespaces/{target.snapshot_namespace}/configmaps/{target.snapshot_name}"
    )
    now = clock.now()
    raw = (obj or {}).get("data", {}).get("observation.json")
    if not isinstance(raw, str) or len(raw.encode()) > MAX_OBSERVATION_BYTES:
        return [unknown(target.id, "OBSERVATION_MISSING", now, DATA)]
    try:
        observation = TransportObservation.model_validate(json.loads(raw))
    except (ValueError, ValidationError):
        return [unknown(target.id, "OBSERVATION_INVALID", now, DATA)]
    return select_observation(observation, target, now, DATA)
