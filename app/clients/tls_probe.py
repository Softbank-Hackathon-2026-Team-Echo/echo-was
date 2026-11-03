"""공개 경로 TLS 접속 시험(C-TLS-1·C-POST-1 의 증거 원문).

주소 하나에 TLS 로 한 번 붙어 인증서 검증·호스트명·TLS 버전·만료를 본다. HTTP 요청은 보내지 않고
클라우드 자격도 쓰지 않는다(네트워크만). 결과는 이유 코드 하나다.

- 인증서 검증 실패 → `TLS_CERT_INVALID`, 호스트명 불일치 → `TLS_HOSTNAME_MISMATCH`
- TLS 1.2 미만 → `TLS_VERSION_TOO_LOW`, 그 밖의 핸드셰이크 실패 → `TLS_HANDSHAKE_FAILED`
- 만료까지 7일 미만 → `TLS_EXPIRES_SOON`
- 시간 초과 → `TLS_TIMEOUT`, DNS·연결 실패 → `TLS_UNREACHABLE`(확인하지 못한 것, UNKNOWN)
"""

import asyncio
import contextlib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    async def probe(self, host: str, port: int = 443) -> TlsProbeResult: ...


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

    async def probe(self, host: str, port: int = 443) -> TlsProbeResult:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=self._ssl_context, server_hostname=host),
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
