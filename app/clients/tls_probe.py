"""공개 경로 TLS 접속 시험(C-TLS-1·C-POST-1 의 증거 원문).

주소 하나에 TLS 로 한 번 붙어 인증서 검증·호스트명·TLS 버전·만료를 본다. HTTP 요청은 보내지 않고
클라우드 자격도 쓰지 않는다(네트워크만). 결과는 이유 코드 하나다.

- 인증서 검증 실패 → `TLS_CERT_INVALID`, 호스트명 불일치 → `TLS_HOSTNAME_MISMATCH`
- TLS 1.2 미만 → `TLS_VERSION_TOO_LOW`, 그 밖의 핸드셰이크 실패 → `TLS_HANDSHAKE_FAILED`
- 만료까지 7일 미만 → `TLS_EXPIRES_SOON`
- 시간 초과 → `TLS_TIMEOUT`, DNS·연결 실패 → `TLS_UNREACHABLE`(확인하지 못한 것, UNKNOWN)

`PublicHostTlsChecker` 는 Echo 가 만든 공개 주소에만 붙는다(WAS 가 내부 주소로 나가지 않게).
주소가 공개 도메인 아래인지 보고, DNS 로 받은 IP 를 확인한 뒤 그 IP 로 바로 접속한다(확인과 접속
사이에 DNS 가 바뀌어도 확인한 IP 로만 간다). 포트는 443 만 쓴다.

- DNS 결과가 모두 공인 IP → 첫 IP 로 TLS 시험
- 모두 사설 대역(10/8·172.16/12·192.168/16·fc00::/7) → 접속하지 않음, `TLS_HOST_NOT_PUBLIC`
- 루프백·링크 로컬(메타데이터)·예약 대역이 있거나 공인·사설이 섞임 → 접속하지 않음,
  `TLS_HOST_SUSPICIOUS`
- 공개 도메인 아래가 아님 → 접속하지 않음, `TLS_HOST_NOT_ALLOWED`
- DNS 실패 → `TLS_UNREACHABLE`, 주소 하나의 확인 전체가 제한 시간을 넘으면 `TLS_TIMEOUT`
"""

import asyncio
import contextlib
import ipaddress
import socket
import ssl
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from app.core.clock import Clock, SystemClock
from app.enums import SecurityFindingReason

# 만료까지 이보다 적게 남으면 실패로 본다.
CERT_MIN_REMAINING = timedelta(days=7)

DEFAULT_TIMEOUT_SECONDS = 5.0

_ALLOWED_VERSIONS = frozenset({"TLSv1.2", "TLSv1.3"})

# OpenSSL X509_V_ERR_HOSTNAME_MISMATCH
_HOSTNAME_MISMATCH_CODE = 62

_PROTOCOL_VERSION_ERRORS = frozenset(
    {"UNSUPPORTED_PROTOCOL", "TLSV1_ALERT_PROTOCOL_VERSION", "NO_PROTOCOLS_AVAILABLE"}
)


@dataclass(frozen=True)
class TlsProbeResult:
    host: str
    port: int
    reason: SecurityFindingReason
    tls_version: str | None = None
    not_after: datetime | None = None


class TlsProbeClient(Protocol):
    async def probe(self, host: str, port: int = 443, address: str | None = None) -> TlsProbeResult:
        """address 가 있으면 DNS 를 다시 찾지 않고 그 IP 로 붙는다(인증서는 host 로 검증)."""
        ...


class SocketTlsProbeClient:
    """표준 라이브러리 ssl 로 붙는 실제 구현. 시험은 신뢰할 CA 를 담은 ssl_context 를 넣는다."""

    def __init__(
        self,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        ssl_context: ssl.SSLContext | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._ssl_context = ssl_context or ssl.create_default_context()
        self._ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._clock = clock or SystemClock()

    async def probe(self, host: str, port: int = 443, address: str | None = None) -> TlsProbeResult:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    address or host, port, ssl=self._ssl_context, server_hostname=host
                ),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            return TlsProbeResult(host, port, SecurityFindingReason.TLS_TIMEOUT)
        except ssl.SSLCertVerificationError as exc:
            reason = (
                SecurityFindingReason.TLS_HOSTNAME_MISMATCH
                if exc.verify_code == _HOSTNAME_MISMATCH_CODE
                else SecurityFindingReason.TLS_CERT_INVALID
            )
            return TlsProbeResult(host, port, reason)
        except ssl.SSLError as exc:
            reason = (
                SecurityFindingReason.TLS_VERSION_TOO_LOW
                if exc.reason in _PROTOCOL_VERSION_ERRORS
                else SecurityFindingReason.TLS_HANDSHAKE_FAILED
            )
            return TlsProbeResult(host, port, reason)
        except OSError:
            return TlsProbeResult(host, port, SecurityFindingReason.TLS_UNREACHABLE)

        try:
            ssl_object = writer.get_extra_info("ssl_object")
            tls_version = ssl_object.version() if ssl_object is not None else None
            cert = ssl_object.getpeercert() if ssl_object is not None else None
        finally:
            writer.close()
            # 닫는 중 오류는 판정과 관계없다(이미 필요한 값을 읽었다).
            with contextlib.suppress(OSError, ssl.SSLError):
                await writer.wait_closed()

        not_after = _read_not_after(cert)
        return TlsProbeResult(
            host,
            port,
            judge_tls(tls_version, not_after, self._clock.now()),
            tls_version=tls_version,
            not_after=not_after,
        )


def judge_tls(
    tls_version: str | None, not_after: datetime | None, now: datetime
) -> SecurityFindingReason:
    """검증을 통과한 연결의 버전·만료를 본다."""
    if tls_version not in _ALLOWED_VERSIONS:
        return SecurityFindingReason.TLS_VERSION_TOO_LOW
    if not_after is None:
        return SecurityFindingReason.TLS_CERT_INVALID
    if not_after - now < CERT_MIN_REMAINING:
        return SecurityFindingReason.TLS_EXPIRES_SOON
    return SecurityFindingReason.TLS_OK


def _read_not_after(cert: object) -> datetime | None:
    if not isinstance(cert, dict):
        return None
    value = cert.get("notAfter")
    if not isinstance(value, str):
        return None
    return datetime.fromtimestamp(ssl.cert_time_to_seconds(value), UTC)


PUBLIC_TLS_PORT = 443

_PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


class AddressClass(StrEnum):
    """DNS 로 받은 IP 묶음의 분류."""

    PUBLIC = "PUBLIC"
    PRIVATE = "PRIVATE"
    SUSPICIOUS = "SUSPICIOUS"


class HostResolver(Protocol):
    async def resolve(self, host: str, port: int) -> list[str]:
        """host 의 IP 주소 목록(문자열). 찾지 못하면 OSError."""
        ...


class SystemHostResolver:
    """운영체제 DNS(getaddrinfo)로 찾는다."""

    async def resolve(self, host: str, port: int) -> list[str]:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return list(dict.fromkeys(str(info[4][0]) for info in infos))


def classify_addresses(addresses: Sequence[str]) -> AddressClass:
    """모두 공인이면 PUBLIC, 모두 사설 대역이면 PRIVATE, 그 밖(섞임·루프백·예약 등)은 SUSPICIOUS."""
    if not addresses:
        return AddressClass.SUSPICIOUS
    classes = {_classify_address(address) for address in addresses}
    if classes == {AddressClass.PUBLIC}:
        return AddressClass.PUBLIC
    if classes == {AddressClass.PRIVATE}:
        return AddressClass.PRIVATE
    return AddressClass.SUSPICIOUS


def _classify_address(address: str) -> AddressClass:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return AddressClass.SUSPICIOUS
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_global and not ip.is_multicast:
        return AddressClass.PUBLIC
    if any(ip in network for network in _PRIVATE_NETWORKS):
        return AddressClass.PRIVATE
    return AddressClass.SUSPICIOUS


def is_public_host_name(host: str, public_base_domain: str) -> bool:
    """Echo 가 만든 주소 모양인가: 소문자 이름이고 공개 도메인의 하위 이름이다."""
    if not host.endswith(f".{public_base_domain}") or host != host.lower():
        return False
    labels = host.split(".")
    return all(
        0 < len(label) <= 63
        and label[0] != "-"
        and label[-1] != "-"
        and all(ch.isascii() and (ch.isalnum() or ch == "-") for ch in label)
        for label in labels
    )


class PublicHostTlsChecker:
    """Echo 가 만든 공개 주소만 TLS 시험한다. 주소마다 결과 하나(접속하지 않은 이유 포함)."""

    def __init__(
        self,
        probe_client: TlsProbeClient,
        resolver: HostResolver,
        public_base_domain: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._probe_client = probe_client
        self._resolver = resolver
        self._public_base_domain = public_base_domain
        self._timeout_seconds = timeout_seconds

    async def check_hosts(self, hosts: Sequence[str]) -> list[TlsProbeResult]:
        unique_hosts = list(dict.fromkeys(hosts))
        return list(await asyncio.gather(*(self._check(host) for host in unique_hosts)))

    async def _check(self, host: str) -> TlsProbeResult:
        port = PUBLIC_TLS_PORT
        if not is_public_host_name(host, self._public_base_domain):
            return TlsProbeResult(host, port, SecurityFindingReason.TLS_HOST_NOT_ALLOWED)
        try:
            async with asyncio.timeout(self._timeout_seconds):
                try:
                    addresses = await self._resolver.resolve(host, port)
                except OSError:
                    return TlsProbeResult(host, port, SecurityFindingReason.TLS_UNREACHABLE)
                match classify_addresses(addresses):
                    case AddressClass.PRIVATE:
                        return TlsProbeResult(host, port, SecurityFindingReason.TLS_HOST_NOT_PUBLIC)
                    case AddressClass.SUSPICIOUS:
                        return TlsProbeResult(host, port, SecurityFindingReason.TLS_HOST_SUSPICIOUS)
                    case AddressClass.PUBLIC:
                        return await self._probe_client.probe(host, port, address=addresses[0])
        except TimeoutError:
            return TlsProbeResult(host, port, SecurityFindingReason.TLS_TIMEOUT)
