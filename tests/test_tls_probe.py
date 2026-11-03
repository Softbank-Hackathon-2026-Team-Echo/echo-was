"""공개 경로 TLS 접속 시험. 로컬 TLS 서버(시험용 CA·인증서)로 실제 핸드셰이크를 한다.

SPEC sec-2 AC-4.
"""

import asyncio
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.clients.tls_probe import CERT_MIN_REMAINING, SocketTlsProbeClient, judge_tls
from app.enums import SecurityFindingReason

HOST = "localhost"


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def write_certificates(
    tmp_path: Path, *, san: str, valid_for: timedelta
) -> tuple[Path, Path, Path]:
    """시험용 CA 와 그 CA 가 서명한 서버 인증서를 만든다. (CA, 인증서, 키) 경로."""
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("echo-test-ca"))
        .issuer_name(_name("echo-test-ca"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(san))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + valid_for)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(san)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path, cert_path, key_path = (tmp_path / n for n in ("ca.pem", "cert.pem", "key.pem"))
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_path, cert_path, key_path


async def _close_quietly(_: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    writer.close()


StartServer = Callable[..., Awaitable[tuple[int, Path]]]


@pytest.fixture
async def tls_server(tmp_path: Path) -> AsyncIterator[StartServer]:
    servers: list[asyncio.Server] = []

    async def start(
        *, san: str = HOST, valid_for: timedelta = timedelta(days=30)
    ) -> tuple[int, Path]:
        ca_path, cert_path, key_path = write_certificates(tmp_path, san=san, valid_for=valid_for)
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(cert_path, key_path)
        server = await asyncio.start_server(_close_quietly, "127.0.0.1", 0, ssl=context)
        servers.append(server)
        port = server.sockets[0].getsockname()[1]
        return port, ca_path

    yield start
    for server in servers:
        server.close()
        await server.wait_closed()


def trusting(ca_path: Path) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(ca_path))


async def test_probe_valid_certificate_is_ok(tls_server: StartServer) -> None:
    port, ca_path = await tls_server()
    result = await SocketTlsProbeClient(ssl_context=trusting(ca_path)).probe(HOST, port)
    assert result.reason == SecurityFindingReason.TLS_OK
    assert result.tls_version in {"TLSv1.2", "TLSv1.3"}
    assert result.not_after is not None


async def test_probe_certificate_expiring_soon_fails(tls_server: StartServer) -> None:
    port, ca_path = await tls_server(valid_for=timedelta(days=3))
    result = await SocketTlsProbeClient(ssl_context=trusting(ca_path)).probe(HOST, port)
    assert result.reason == SecurityFindingReason.TLS_EXPIRES_SOON


async def test_probe_untrusted_certificate_is_invalid(tls_server: StartServer) -> None:
    port, _ = await tls_server()
    result = await SocketTlsProbeClient().probe(HOST, port)
    assert result.reason == SecurityFindingReason.TLS_CERT_INVALID


async def test_probe_hostname_mismatch_is_reported(tls_server: StartServer) -> None:
    port, ca_path = await tls_server(san="other.example.test")
    result = await SocketTlsProbeClient(ssl_context=trusting(ca_path)).probe(HOST, port)
    assert result.reason == SecurityFindingReason.TLS_HOSTNAME_MISMATCH


async def test_probe_closed_port_is_unreachable() -> None:
    server = await asyncio.start_server(_close_quietly, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    result = await SocketTlsProbeClient(timeout_seconds=2).probe("127.0.0.1", port)
    assert result.reason == SecurityFindingReason.TLS_UNREACHABLE


async def test_probe_silent_server_times_out() -> None:
    async def hold(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()  # 핸드셰이크에 답하지 않는다
        writer.close()

    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        result = await SocketTlsProbeClient(timeout_seconds=0.3).probe("127.0.0.1", port)
    finally:
        server.close()
    assert result.reason == SecurityFindingReason.TLS_TIMEOUT


@pytest.mark.parametrize(
    ("version", "remaining", "reason"),
    [
        ("TLSv1.3", timedelta(days=30), SecurityFindingReason.TLS_OK),
        ("TLSv1.2", CERT_MIN_REMAINING, SecurityFindingReason.TLS_OK),
        (
            "TLSv1.2",
            CERT_MIN_REMAINING - timedelta(seconds=1),
            SecurityFindingReason.TLS_EXPIRES_SOON,
        ),
        ("TLSv1.1", timedelta(days=30), SecurityFindingReason.TLS_VERSION_TOO_LOW),
        (None, timedelta(days=30), SecurityFindingReason.TLS_VERSION_TOO_LOW),
    ],
)
def test_judge_tls_checks_version_and_remaining_validity(
    version: str | None, remaining: timedelta, reason: SecurityFindingReason
) -> None:
    now = datetime(2026, 11, 5, tzinfo=UTC)
    assert judge_tls(version, now + remaining, now) == reason


def test_judge_tls_without_expiry_is_invalid() -> None:
    now = datetime(2026, 11, 5, tzinfo=UTC)
    assert judge_tls("TLSv1.3", None, now) == SecurityFindingReason.TLS_CERT_INVALID
