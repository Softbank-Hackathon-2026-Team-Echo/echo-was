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

from app.clients.tls_probe import (
    CERT_MIN_REMAINING,
    AddressClass,
    PublicHostTlsChecker,
    SocketTlsProbeClient,
    TlsProbeResult,
    classify_addresses,
    is_public_host_name,
    judge_tls,
)
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


async def test_probe_with_address_connects_to_that_ip_and_verifies_host_name(
    tls_server: StartServer,
) -> None:
    # 인증서는 host(localhost)로 검증하고 접속은 확인한 IP 로만 한다(DNS 를 다시 찾지 않는다).
    port, ca_path = await tls_server()
    client = SocketTlsProbeClient(ssl_context=trusting(ca_path))
    pinned = await client.probe(HOST, port, address="127.0.0.1")
    mismatched = await client.probe("other.localhost", port, address="127.0.0.1")
    assert pinned.reason == SecurityFindingReason.TLS_OK
    assert mismatched.reason == SecurityFindingReason.TLS_HOSTNAME_MISMATCH


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


DOMAIN = "echoport.test"


@pytest.mark.parametrize(
    ("addresses", "expected"),
    [
        (["34.64.10.20"], AddressClass.PUBLIC),
        (["34.64.10.20", "2600:1900:4000::1"], AddressClass.PUBLIC),
        (["10.60.0.6"], AddressClass.PRIVATE),
        (["172.16.0.1", "192.168.1.2", "fd00::1"], AddressClass.PRIVATE),
        (["34.64.10.20", "10.60.0.6"], AddressClass.SUSPICIOUS),
        (["127.0.0.1"], AddressClass.SUSPICIOUS),
        (["169.254.169.254"], AddressClass.SUSPICIOUS),
        (["100.64.0.1"], AddressClass.SUSPICIOUS),
        (["0.0.0.0"], AddressClass.SUSPICIOUS),
        (["::ffff:127.0.0.1"], AddressClass.SUSPICIOUS),
        (["::ffff:10.0.0.1"], AddressClass.PRIVATE),
        (["224.0.0.1"], AddressClass.SUSPICIOUS),
        (["not-an-ip"], AddressClass.SUSPICIOUS),
        ([], AddressClass.SUSPICIOUS),
    ],
)
def test_classify_addresses_accepts_only_all_public_or_all_private(
    addresses: list[str], expected: AddressClass
) -> None:
    assert classify_addresses(addresses) == expected


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        (f"shop-api-m1g2r3t4.{DOMAIN}", True),
        (f"shop-api-m1g2r3t4.gcp-seoul.{DOMAIN}", True),
        (DOMAIN, False),
        (f"shop.{DOMAIN}.evil.test", False),
        (f"Shop.{DOMAIN}", False),
        (f"-shop.{DOMAIN}", False),
        (f"shop_api.{DOMAIN}", False),
        (f"shop..{DOMAIN}", False),
        ("169.254.169.254", False),
    ],
)
def test_is_public_host_name_requires_echo_domain(host: str, expected: bool) -> None:
    assert is_public_host_name(host, DOMAIN) is expected


class _Resolver:
    def __init__(self, addresses_by_host: dict[str, list[str]], delay: float = 0.0) -> None:
        self.addresses_by_host = addresses_by_host
        self.delay = delay
        self.calls: list[str] = []

    async def resolve(self, host: str, port: int) -> list[str]:
        self.calls.append(host)
        await asyncio.sleep(self.delay)
        if host not in self.addresses_by_host:
            raise OSError("not found")
        return self.addresses_by_host[host]


class _Probe:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str | None]] = []

    async def probe(self, host: str, port: int = 443, address: str | None = None) -> TlsProbeResult:
        self.calls.append((host, port, address))
        return TlsProbeResult(host, port, SecurityFindingReason.TLS_OK)


async def test_public_host_checker_probes_only_public_echo_hosts_on_443() -> None:
    public, private, mixed, missing = (f"{n}.{DOMAIN}" for n in ("pub", "priv", "mix", "gone"))
    outside = "pub.other.test"
    resolver = _Resolver(
        {
            public: ["34.64.10.20", "34.64.10.21"],
            private: ["10.60.0.6"],
            mixed: ["34.64.10.20", "169.254.169.254"],
            outside: ["34.64.10.20"],
        }
    )
    probe = _Probe()
    checker = PublicHostTlsChecker(probe, resolver, DOMAIN)

    results = await checker.check_hosts([public, private, mixed, missing, outside, public])

    assert [(r.host, r.reason) for r in results] == [
        (public, SecurityFindingReason.TLS_OK),
        (private, SecurityFindingReason.TLS_HOST_NOT_PUBLIC),
        (mixed, SecurityFindingReason.TLS_HOST_SUSPICIOUS),
        (missing, SecurityFindingReason.TLS_UNREACHABLE),
        (outside, SecurityFindingReason.TLS_HOST_NOT_ALLOWED),
    ]
    assert probe.calls == [(public, 443, "34.64.10.20")]
    # 공개 도메인 밖 주소는 DNS 조회도 하지 않는다.
    assert outside not in resolver.calls


async def test_public_host_checker_times_out_slow_dns() -> None:
    host = f"slow.{DOMAIN}"
    checker = PublicHostTlsChecker(
        _Probe(), _Resolver({host: ["34.64.10.20"]}, delay=1.0), DOMAIN, timeout_seconds=0.05
    )
    (result,) = await checker.check_hosts([host])
    assert result.reason == SecurityFindingReason.TLS_TIMEOUT
