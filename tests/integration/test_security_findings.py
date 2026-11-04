"""보안 증거 저장·조회(findings). SPEC sec-2-2 AC-1~AC-6."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import RequestFieldInvalidError
from app.enums import (
    AiFindingConfirmation,
    SecurityCheckPhase,
    SecurityControlId,
    SecurityFindingMode,
    SecurityFindingReason,
    SecurityFindingSource,
    SecurityResult,
)
from app.models import Finding, Organization
from app.services.security_finding_service import SecurityFindingService, to_finding_model
from app.services.security_normalizers import (
    NormalizedFinding,
    normalize_gitleaks,
    normalize_trivy_image,
)
from app.services.security_rules import (
    POLICY_VERSION,
    SecurityFinding,
    SecurityScope,
    SecurityTarget,
    evaluate_security,
)
from tests.integration.factories import add_organization, add_user

NOW = datetime.now(UTC).replace(microsecond=0)
DIGEST = "sha256:" + "a" * 64
REPO = "ghcr.io/softbank-hackathon-2026-team-echo/echo-demo-app"
FAKE_SECRET = "not-a-real-secret-value-0002"


def trivy_report(vulnerabilities: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "SchemaVersion": 2,
        "ArtifactName": f"{REPO}@{DIGEST}",
        "Metadata": {"RepoDigests": [f"{REPO}@{DIGEST}"]},
        "Results": [{"Target": "Python", "Vulnerabilities": vulnerabilities}],
    }


FIXABLE_HIGH = {
    "VulnerabilityID": "CVE-2026-97687",
    "PkgName": "urllib3",
    "InstalledVersion": "2.7.0",
    "FixedVersion": "2.8.0",
    "Severity": "HIGH",
}


def scope_for(organization: Organization) -> SecurityScope:
    return SecurityScope(organization_id=organization.id, image_digest=DIGEST)


async def record(
    session_factory: async_sessionmaker[AsyncSession],
    normalized: NormalizedFinding,
    source_ref: str | None = None,
) -> None:
    async with session_factory() as session:
        await SecurityFindingService(session).record(normalized, source_ref)


async def search(
    session_factory: async_sessionmaker[AsyncSession], organization: Organization
) -> list[SecurityFinding]:
    async with session_factory() as session:
        return await SecurityFindingService(session).search_for_judgement(organization.id, NOW)


async def test_record_and_search_round_trip_keeps_scope_and_items(
    session_factory: async_sessionmaker[AsyncSession], organization: Organization
) -> None:
    normalized = normalize_trivy_image(
        trivy_report([FIXABLE_HIGH]),
        scope=scope_for(organization),
        collected_at=NOW - timedelta(minutes=5),
        tool_version="0.75.0",
    )
    await record(session_factory, normalized, "github-actions:echo-ai-agent/image-scan/1")

    found = await search(session_factory, organization)
    assert found == [normalized.finding]
    async with session_factory() as session:
        row = await session.get(Finding, normalized.finding.finding_id)
    assert row is not None
    assert row.reason == SecurityFindingReason.FIXABLE_HIGH_VULNERABILITY
    assert row.tool_version == "0.75.0"
    assert row.source_ref == "github-actions:echo-ai-agent/image-scan/1"
    assert row.total_items == 1
    assert row.items == [
        {
            "item_id": "CVE-2026-97687",
            "severity": "HIGH",
            "target": "urllib3",
            "installed_version": "2.7.0",
            "fixed_version": "2.8.0",
        }
    ]


async def test_search_never_returns_other_organization_findings(
    session_factory: async_sessionmaker[AsyncSession], organization: Organization
) -> None:
    async with session_factory() as session, session.begin():
        other = await add_organization(session, slug="other-org")
    await record(
        session_factory,
        normalize_trivy_image(trivy_report([]), scope=scope_for(other), collected_at=NOW),
    )
    assert await search(session_factory, organization) == []
    assert len(await search(session_factory, other)) == 1


async def test_search_skips_expired_findings(
    session_factory: async_sessionmaker[AsyncSession], organization: Organization
) -> None:
    stale = normalize_trivy_image(
        trivy_report([]), scope=scope_for(organization), collected_at=NOW - timedelta(days=8)
    )
    await record(session_factory, stale)
    assert await search(session_factory, organization) == []


async def test_gitleaks_finding_is_stored_without_secret_text(
    session_factory: async_sessionmaker[AsyncSession], organization: Organization
) -> None:
    leak = {
        "RuleID": "github-pat",
        "File": "app/settings.py",
        "StartLine": 3,
        "Secret": FAKE_SECRET,
        "Match": FAKE_SECRET,
        "Author": "someone",
    }
    normalized = normalize_gitleaks([leak], scope=scope_for(organization), collected_at=NOW)
    await record(session_factory, normalized)
    async with session_factory() as session:
        row = await session.get(Finding, normalized.finding.finding_id)
    assert row is not None
    assert FAKE_SECRET not in str(row.items)
    assert "someone" not in str(row.items)


async def test_record_manual_review_sets_basis_and_expiry(
    session_factory: async_sessionmaker[AsyncSession], organization: Organization
) -> None:
    async with session_factory() as session, session.begin():
        reviewer = await add_user(session, "reviewer")
    async with session_factory() as session:
        finding_id = await SecurityFindingService(session).record_manual_review(
            control_id=SecurityControlId.TLS_2,
            result=SecurityResult.WARN,
            scope=SecurityScope(organization_id=organization.id),
            reviewer_user_id=reviewer.id,
            basis_url="https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-infra/issues/23",
            collected_at=NOW,
        )
    async with session_factory() as session:
        row = await session.get(Finding, finding_id)
    assert row is not None
    assert row.source == SecurityFindingSource.MANUAL_REVIEW
    assert row.reason == SecurityFindingReason.MANUAL_REVIEW_RECORDED
    assert row.reviewer_user_id == reviewer.id
    assert row.expires_at == NOW + timedelta(days=7)
    assert row.policy_version == POLICY_VERSION


@pytest.mark.parametrize(
    ("control_id", "result", "basis_url", "field"),
    [
        (SecurityControlId.IMG_1, SecurityResult.WARN, "https://example.com/r", "controlId"),
        (SecurityControlId.TLS_2, SecurityResult.NA, "https://example.com/r", "result"),
        (SecurityControlId.TLS_2, SecurityResult.UNKNOWN, "https://example.com/r", "result"),
        (SecurityControlId.TLS_2, SecurityResult.WARN, "http://example.com/r", "basisUrl"),
        (SecurityControlId.TLS_2, SecurityResult.WARN, "not a url", "basisUrl"),
    ],
)
async def test_record_manual_review_rejects_invalid_input(
    session_factory: async_sessionmaker[AsyncSession],
    organization: Organization,
    control_id: SecurityControlId,
    result: SecurityResult,
    basis_url: str,
    field: str,
) -> None:
    async with session_factory() as session, session.begin():
        reviewer = await add_user(session, "reviewer")
    async with session_factory() as session:
        with pytest.raises(RequestFieldInvalidError) as raised:
            await SecurityFindingService(session).record_manual_review(
                control_id=control_id,
                result=result,
                scope=SecurityScope(organization_id=organization.id),
                reviewer_user_id=reviewer.id,
                basis_url=basis_url,
                collected_at=NOW,
            )
    assert raised.value.details is not None
    assert raised.value.details[0].field == field


def ai_finding(organization: Organization, result: SecurityResult) -> NormalizedFinding:
    base = normalize_trivy_image(trivy_report([]), scope=scope_for(organization), collected_at=NOW)
    finding = SecurityFinding(
        finding_id=base.finding.finding_id,
        control_id=SecurityControlId.CFG_1,
        result=result,
        mode=SecurityFindingMode.REAL,
        source=SecurityFindingSource.AI_REVIEW,
        scope=base.finding.scope,
        collected_at=NOW,
        expires_at=NOW + timedelta(days=1),
        policy_version=POLICY_VERSION,
    )
    return NormalizedFinding(finding, SecurityFindingReason.MISCONFIGURATION_WARNING, (), 0, None)


async def test_ai_finding_is_stored_unconfirmed(
    session_factory: async_sessionmaker[AsyncSession], organization: Organization
) -> None:
    await record(session_factory, ai_finding(organization, SecurityResult.FAIL))
    [found] = await search(session_factory, organization)
    assert found.confirmation == AiFindingConfirmation.UNCONFIRMED


async def test_database_rejects_ai_pass(
    session_factory: async_sessionmaker[AsyncSession], organization: Organization
) -> None:
    with pytest.raises(IntegrityError, match="ck_findings_ai_result_not_pass"):
        await record(session_factory, ai_finding(organization, SecurityResult.PASS))


async def test_database_rejects_manual_review_without_basis(
    session_factory: async_sessionmaker[AsyncSession], organization: Organization
) -> None:
    base = normalize_trivy_image(trivy_report([]), scope=scope_for(organization), collected_at=NOW)
    model = to_finding_model(base)
    model.source = SecurityFindingSource.MANUAL_REVIEW
    model.control_id = SecurityControlId.TLS_2
    async with session_factory() as session:
        with pytest.raises(IntegrityError, match="ck_findings_manual_review_has_basis"):
            async with session.begin():
                session.add(model)


async def test_recorded_findings_drive_rule_judgement(
    session_factory: async_sessionmaker[AsyncSession], organization: Organization
) -> None:
    await record(
        session_factory,
        normalize_trivy_image(
            trivy_report([FIXABLE_HIGH]), scope=scope_for(organization), collected_at=NOW
        ),
    )
    findings = await search(session_factory, organization)
    target = SecurityTarget(scope=scope_for(organization), environment_kind="gcp")
    decision = evaluate_security(target, SecurityCheckPhase.PREPARATION, findings, [], NOW)
    assert decision.get_control(SecurityControlId.IMG_1).result == SecurityResult.FAIL
    assert decision.is_blocked


async def test_findings_rows_are_scoped_by_organization_index(
    session_factory: async_sessionmaker[AsyncSession], organization: Organization
) -> None:
    await record(
        session_factory,
        normalize_trivy_image(trivy_report([]), scope=scope_for(organization), collected_at=NOW),
    )
    async with session_factory() as session:
        rows = (await session.scalars(select(Finding.organization_id))).all()
    assert rows == [organization.id]
