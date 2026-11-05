"""공개 경로 TLS 주기 시험(C-TLS-1 준비 증거) 통합 시험.

이전 게이트가 실행 직전에 만든 C-TLS-1 증거는 1시간 뒤 만료되고, 그 뒤 매트릭스 encryption 칸은
`EVIDENCE_MISSING` 으로 막힌다. 일꾼의 주기 시험이 서비스 + 환경 범위 증거를 다시 남기는지 본다.
"""

import asyncio
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.clients.tls_probe import PublicHostTlsChecker
from app.enums import (
    DeployRequestStatus,
    SecurityControlId,
    SecurityFindingMode,
    SecurityFindingReason,
    SecurityFindingSource,
    SecurityGateStatus,
    SecurityReasonCode,
    SecurityResult,
)
from app.models import Finding, Service, ServiceEnvironment
from app.services.periodic_tls_probe import PERIODIC_TLS_SOURCE_REF, PeriodicTlsProbe
from app.services.security_finding_service import SecurityFindingService
from app.services.security_normalizers import NormalizedFinding
from app.services.security_rules import POLICY_VERSION, SecurityFinding, SecurityScope, get_rule
from tests.integration.deploy_harness import DeployHarness
from tests.integration.security_gate_support import FakeHostResolver, FakeTlsProbeClient


def _probe(harness: DeployHarness) -> tuple[PeriodicTlsProbe, FakeTlsProbeClient]:
    client = FakeTlsProbeClient()
    checker = PublicHostTlsChecker(client, FakeHostResolver(), harness.settings.public_base_domain)
    return (
        PeriodicTlsProbe(harness.session_factory, checker, harness.clock, harness.settings),
        client,
    )


async def _deployed(harness: DeployHarness) -> tuple[uuid.UUID, uuid.UUID, tuple[str, str]]:
    """서비스 하나를 aws-seoul 에 배포하고 (서비스 id, 환경 id, (대표 주소, 환경 주소))."""
    service = await harness.create_service("shop-api")
    environment = await harness.add_environment("aws-seoul")
    request = await harness.deploy(service["id"], environment.id)
    await harness.run_until(request["id"], {DeployRequestStatus.SUCCEEDED})
    service_id = uuid.UUID(service["id"])
    async with harness.session_factory() as session:
        main_host = await session.scalar(
            select(Service.public_host).where(Service.id == service_id)
        )
        environment_host = await session.scalar(
            select(ServiceEnvironment.public_host).where(
                ServiceEnvironment.service_id == service_id
            )
        )
    assert main_host is not None and environment_host is not None
    return service_id, environment.id, (main_host, environment_host)


async def _record_delivery_path_pass(harness: DeployHarness) -> None:
    """encryption 칸의 다른 필수 통제(C-SEC-1, 조직 범위) PASS 증거."""
    collected_at = harness.clock.now() - timedelta(minutes=1)
    finding = SecurityFinding(
        finding_id=uuid.uuid4(),
        control_id=SecurityControlId.SEC_1,
        result=SecurityResult.PASS,
        mode=SecurityFindingMode.REAL,
        source=SecurityFindingSource.GITLEAKS,
        scope=SecurityScope(harness.organization.id),
        collected_at=collected_at,
        expires_at=collected_at + get_rule(SecurityControlId.SEC_1).max_age,
        policy_version=POLICY_VERSION,
    )
    async with harness.session_factory() as session:
        await SecurityFindingService(session).record_all(
            [NormalizedFinding(finding, SecurityFindingReason.NO_ISSUES, (), 0, None)]
        )


async def _encryption(harness: DeployHarness) -> dict[str, Any]:
    response = await harness.client.get(f"/api/v1/orgs/{harness.organization.id}/security/matrix")
    assert response.status_code == 200, response.text
    cell: dict[str, Any] = response.json()["data"]["rows"][0]["cells"]["encryption"]
    return cell


def _tls_control(cell: dict[str, Any]) -> dict[str, Any]:
    return next(c for c in cell["controls"] if c["controlId"] == SecurityControlId.TLS_1.value)


async def _tls_findings(harness: DeployHarness) -> list[Finding]:
    async with harness.session_factory() as session:
        rows = await session.scalars(
            select(Finding)
            .where(Finding.control_id == SecurityControlId.TLS_1.value)
            .order_by(Finding.collected_at, Finding.id)
        )
        return list(rows.all())


async def test_periodic_probe_records_service_environment_evidence_and_unblocks_matrix(
    harness: DeployHarness,
) -> None:
    service_id, environment_id, hosts = await _deployed(harness)
    await _record_delivery_path_pass(harness)
    before = await _encryption(harness)
    assert _tls_control(before)["gateStatus"] == SecurityGateStatus.BLOCKED.value
    assert _tls_control(before)["reasons"] == [SecurityReasonCode.EVIDENCE_MISSING.value]
    probe, client = _probe(harness)

    recorded = await probe.run_once()

    assert recorded == 1
    # 이전 게이트와 같은 주소(대표 주소 + 그 환경 주소)를 시험한다.
    assert sorted(host for host, _, _ in client.calls) == sorted(hosts)
    [finding] = await _tls_findings(harness)
    assert finding.source == SecurityFindingSource.TLS_PROBE.value
    assert finding.source_ref == PERIODIC_TLS_SOURCE_REF
    assert finding.result == SecurityResult.PASS.value
    assert (finding.service_id, finding.environment_id) == (service_id, environment_id)
    after = await _encryption(harness)
    control = _tls_control(after)
    assert control["result"] == SecurityResult.PASS.value
    assert control["gateStatus"] == SecurityGateStatus.PASS.value
    assert control["findingIds"] == [str(finding.id)]
    assert after["gateStatus"] == SecurityGateStatus.PASS.value


async def test_failed_probe_is_recorded_as_is_and_keeps_blocking(harness: DeployHarness) -> None:
    _, _, (_, environment_host) = await _deployed(harness)
    probe, client = _probe(harness)
    client.reason_by_host[environment_host] = SecurityFindingReason.TLS_CERT_INVALID

    await probe.run_once()

    [finding] = await _tls_findings(harness)
    assert finding.result == SecurityResult.FAIL.value
    control = _tls_control(await _encryption(harness))
    assert control["result"] == SecurityResult.FAIL.value
    assert control["gateStatus"] == SecurityGateStatus.BLOCKED.value
    assert SecurityReasonCode.CONTROL_FAILED.value in control["reasons"]


async def test_evidence_expires_after_an_hour_and_next_round_refreshes_it(
    harness: DeployHarness,
) -> None:
    await _deployed(harness)
    probe, _ = _probe(harness)
    await probe.run_once()

    harness.clock.advance(timedelta(hours=1, seconds=1).total_seconds())
    expired = _tls_control(await _encryption(harness))
    assert expired["gateStatus"] == SecurityGateStatus.BLOCKED.value
    assert expired["reasons"] == [SecurityReasonCode.EVIDENCE_MISSING.value]

    await probe.run_once()

    refreshed = _tls_control(await _encryption(harness))
    assert refreshed["result"] == SecurityResult.PASS.value
    assert len(await _tls_findings(harness)) == 2


async def test_connection_without_current_deployment_is_not_probed(
    harness: DeployHarness,
) -> None:
    service = await harness.create_service("shop-api")
    environment = await harness.add_environment("aws-seoul")
    await harness.deploy(service["id"], environment.id)  # 아직 현재 배포 없음
    probe, client = _probe(harness)

    recorded = await probe.run_once()

    assert recorded == 0
    assert client.calls == []
    assert await _tls_findings(harness) == []


class _CrashOnceProbe(PeriodicTlsProbe):
    """첫 주기는 실패하고 둘째 주기에 멈춤 신호를 켠다. 주기 대기는 없앤다."""

    def __init__(self, harness: DeployHarness, stop: asyncio.Event) -> None:
        checker = PublicHostTlsChecker(
            FakeTlsProbeClient(), FakeHostResolver(), harness.settings.public_base_domain
        )
        super().__init__(harness.session_factory, checker, harness.clock, harness.settings)
        self._interval = 0
        self._stop = stop
        self.rounds = 0

    async def run_once(self) -> int:
        self.rounds += 1
        if self.rounds == 1:
            raise RuntimeError("probe crashed")
        self._stop.set()
        return 0


async def test_run_keeps_going_after_a_crashed_round(harness: DeployHarness) -> None:
    stop = asyncio.Event()
    probe = _CrashOnceProbe(harness, stop)

    await asyncio.wait_for(probe.run(stop), timeout=5)

    assert probe.rounds == 2
