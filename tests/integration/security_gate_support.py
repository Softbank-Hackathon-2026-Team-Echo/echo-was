"""보안 게이트 통합 시험 도구: 가짜 DNS·TLS 시험, 증거·기한부 예외·AI 사전 점검 행 넣기.

enable_gate() 로 harness 에 실제 PublicHostTlsChecker(가짜 DNS·TLS)를 넣으면 API 승인과 일꾼
실행 시작이 게이트를 거친다. 기본 DNS 는 모든 주소를 공인 IP 하나로, TLS 는 모두 TLS_OK 로 답한다.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select, update

from app.clients.tls_probe import PublicHostTlsChecker, TlsProbeResult
from app.enums import (
    AgentRunKind,
    AgentRunStatus,
    SecurityControlId,
    SecurityFindingMode,
    SecurityFindingReason,
    SecurityFindingSource,
    SecurityResult,
)
from app.models import (
    AgentRun,
    Deployment,
    Finding,
    SecurityException,
    Service,
    ServiceEnvironment,
)
from app.services.security_finding_service import SecurityFindingService
from app.services.security_normalizers import NormalizedFinding
from app.services.security_rules import POLICY_VERSION, SecurityFinding, SecurityScope, get_rule
from tests.integration.migration_support import Migration

# 공인 IP(가짜 DNS 가 돌려준다. 실제로 접속하지 않는다).
PUBLIC_IP = "34.64.10.20"
PRIVATE_IP = "10.60.0.6"
WAS_VERSION = "was-gate-test"


class FakeHostResolver:
    def __init__(self) -> None:
        self.addresses_by_host: dict[str, list[str]] = {}
        self.default: list[str] | None = [PUBLIC_IP]

    async def resolve(self, host: str, port: int) -> list[str]:
        addresses = self.addresses_by_host.get(host, self.default)
        if addresses is None:
            raise OSError("name not resolved")
        return addresses


@dataclass
class FakeTlsProbeClient:
    reason_by_host: dict[str, SecurityFindingReason] = field(default_factory=dict)
    calls: list[tuple[str, int, str | None]] = field(default_factory=list)

    async def probe(self, host: str, port: int = 443, address: str | None = None) -> TlsProbeResult:
        self.calls.append((host, port, address))
        reason = self.reason_by_host.get(host, SecurityFindingReason.TLS_OK)
        return TlsProbeResult(host, port, reason, tls_version="TLSv1.3")


@dataclass(frozen=True)
class GateFakes:
    resolver: FakeHostResolver
    probe: FakeTlsProbeClient


def enable_gate(migration: Migration, was_version: str = WAS_VERSION) -> GateFakes:
    harness = migration.harness
    fakes = GateFakes(FakeHostResolver(), FakeTlsProbeClient())
    harness.tls_checker = PublicHostTlsChecker(
        fakes.probe, fakes.resolver, harness.settings.public_base_domain
    )
    harness.use_settings(security_gate_enabled=True, was_version=was_version)
    return fakes


async def connection_hosts(migration: Migration) -> tuple[str, str]:
    """(대표 주소, 도착 gcp-seoul 연결 주소)."""
    service_id = uuid.UUID(migration.service_id)
    async with migration.harness.session_factory() as session:
        main_host = await session.scalar(
            select(Service.public_host).where(Service.id == service_id)
        )
        target_host = await session.scalar(
            select(ServiceEnvironment.public_host).where(
                ServiceEnvironment.service_id == service_id,
                ServiceEnvironment.environment_id == migration.gcp.id,
            )
        )
    assert main_host is not None and target_host is not None
    return main_host, target_host


async def target_digest(migration: Migration) -> str:
    async with migration.harness.session_factory() as session:
        digest = await session.scalar(
            select(Deployment.image_digest)
            .join(
                ServiceEnvironment,
                ServiceEnvironment.current_deployment_id == Deployment.id,
            )
            .where(
                ServiceEnvironment.service_id == uuid.UUID(migration.service_id),
                ServiceEnvironment.environment_id == migration.gcp.id,
            )
        )
    assert digest is not None
    return digest


def _finding(
    control: SecurityControlId,
    source: SecurityFindingSource,
    scope: SecurityScope,
    now: datetime,
) -> NormalizedFinding:
    collected_at = now - timedelta(minutes=1)
    finding = SecurityFinding(
        finding_id=uuid.uuid4(),
        control_id=control,
        result=SecurityResult.PASS,
        mode=SecurityFindingMode.REAL,
        source=source,
        scope=scope,
        collected_at=collected_at,
        expires_at=collected_at + get_rule(control).max_age,
        policy_version=POLICY_VERSION,
    )
    return NormalizedFinding(finding, SecurityFindingReason.NO_ISSUES, (), 0, None)


async def record_passing_evidence(
    migration: Migration,
    was_version: str = WAS_VERSION,
    *,
    include_delivery_path: bool = True,
) -> None:
    """준비 단계 필수 통제(C-IMG-1·C-IMG-2·C-SEC-1·C-IAM-2·C-AUD-1)의 PASS 증거.

    include_delivery_path=False 면 C-SEC-1 은 넣지 않는다(빌드 검사에서 만든 증거로 시험할 때).
    """
    organization_id = migration.harness.organization.id
    digest = await target_digest(migration)
    now = migration.harness.clock.now()
    by_digest = SecurityScope(organization_id, image_digest=digest)
    by_version = SecurityScope(organization_id, was_version=was_version)
    findings = [
        _finding(SecurityControlId.IMG_1, SecurityFindingSource.TRIVY_IMAGE, by_digest, now),
        _finding(SecurityControlId.IMG_2, SecurityFindingSource.GITLEAKS, by_digest, now),
        _finding(SecurityControlId.IAM_2, SecurityFindingSource.WAS_TEST, by_version, now),
        _finding(SecurityControlId.AUD_1, SecurityFindingSource.WAS_TEST, by_version, now),
    ]
    if include_delivery_path:
        findings.append(
            _finding(
                SecurityControlId.SEC_1,
                SecurityFindingSource.GITLEAKS,
                SecurityScope(organization_id),
                now,
            )
        )
    async with migration.harness.session_factory() as session:
        await SecurityFindingService(session).record_all(findings)


async def add_exception(
    migration: Migration,
    control: SecurityControlId,
    *,
    expires_in: timedelta = timedelta(days=1),
    approved: bool = True,
) -> uuid.UUID:
    """서비스·도착 환경 범위의 기한부 예외(요청 개발자 A, 승인 개발자 B)."""
    now = migration.harness.clock.now()
    created_at = now - timedelta(minutes=5)
    exception = SecurityException(
        organization_id=migration.harness.organization.id,
        control_id=control,
        service_id=uuid.UUID(migration.service_id),
        environment_id=migration.gcp.id,
        reason="D04~D07 결정 전, 시연은 쓰기 정지 구간에서 수동 확인",
        requested_by_user_id=migration.author.user.id,
        approved_by_user_id=migration.approver.user.id if approved else None,
        approved_at=created_at if approved else None,
        expires_at=created_at + expires_in,
        created_at=created_at,
        updated_at=created_at,
    )
    async with migration.harness.session_factory() as session, session.begin():
        session.add(exception)
    return exception.id


async def revoke_exception(migration: Migration, exception_id: uuid.UUID) -> None:
    now = migration.harness.clock.now()
    async with migration.harness.session_factory() as session, session.begin():
        await session.execute(
            update(SecurityException)
            .where(SecurityException.id == exception_id)
            .values(
                revoked_at=now,
                revoked_by_user_id=migration.approver.user.id,
                updated_at=now,
            )
        )


async def add_precheck(
    migration: Migration,
    *,
    finished_ago: timedelta = timedelta(hours=1),
    simulated: bool = False,
) -> uuid.UUID:
    """서비스 → gcp-seoul 의 성공한 AI 사전 점검 실행."""
    finished_at = migration.harness.clock.now() - finished_ago
    created_at = finished_at - timedelta(minutes=10)
    run = AgentRun(
        organization_id=migration.harness.organization.id,
        service_id=uuid.UUID(migration.service_id),
        kind=AgentRunKind.PRECHECK,
        status=AgentRunStatus.SUCCEEDED,
        source_environment_id=migration.aws.id,
        target_environment_id=migration.gcp.id,
        input_snapshot={},
        input_hash="sha256:" + "0" * 64,
        requested_by_user_id=migration.author.user.id,
        deadline_at=created_at + timedelta(minutes=15),
        started_at=created_at,
        finished_at=finished_at,
        result={},
        simulated=simulated,
        created_at=created_at,
        updated_at=finished_at,
    )
    async with migration.harness.session_factory() as session, session.begin():
        session.add(run)
    return run.id


async def prepare_passing_gate(migration: Migration) -> GateFakes:
    """게이트를 켜고 모든 필수 통제가 통과하도록 증거·예외(C-DB-1·C-DST-1)·사전 점검을 넣는다."""
    fakes = enable_gate(migration)
    await record_passing_evidence(migration)
    await add_exception(migration, SecurityControlId.DB_1)
    await add_exception(migration, SecurityControlId.DST_1)
    await add_precheck(migration)
    return fakes


async def gate_findings(migration: Migration, control: SecurityControlId) -> list[Finding]:
    async with migration.harness.session_factory() as session:
        rows = await session.scalars(
            select(Finding)
            .where(
                Finding.control_id == control,
                Finding.source_ref.like("migration-plan:%"),
            )
            .order_by(Finding.collected_at, Finding.id)
        )
        return list(rows.all())
