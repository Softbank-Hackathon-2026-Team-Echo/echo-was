"""판정 조회가 다른 범위·반복 PASS 때문에 유효 증거를 버리지 않는지 확인한다."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.enums import (
    SecurityCheckPhase,
    SecurityControlId,
    SecurityFindingMode,
    SecurityFindingReason,
    SecurityFindingSource,
    SecurityResult,
)
from app.models import Finding, Organization
from app.services.security_finding_service import SecurityFindingService, to_finding_model
from app.services.security_normalizers import NormalizedFinding
from app.services.security_rules import (
    POLICY_VERSION,
    SecurityFinding,
    SecurityScope,
    SecurityTarget,
    evaluate_control,
    get_rule,
)
from tests.integration.migration_support import Migration
from tests.integration.security_gate_support import prepare_passing_gate, target_digest

NOW = datetime(2026, 11, 7, tzinfo=UTC)
FLOOD_COUNT = 600


def _finding(
    scope: SecurityScope,
    control: SecurityControlId,
    source: SecurityFindingSource,
    result: SecurityResult,
    collected_at: datetime,
) -> Finding:
    finding = SecurityFinding(
        finding_id=uuid.uuid4(),
        control_id=control,
        source=source,
        result=result,
        mode=SecurityFindingMode.REAL,
        scope=scope,
        policy_version=POLICY_VERSION,
        collected_at=collected_at,
        expires_at=collected_at + timedelta(hours=12),
    )
    return to_finding_model(
        NormalizedFinding(finding, SecurityFindingReason.NO_ISSUES, (), 0, None)
    )


@pytest.mark.parametrize("expected", [SecurityResult.PASS, SecurityResult.FAIL])
@pytest.mark.parametrize(
    ("control", "source", "scope_field"),
    [
        (SecurityControlId.IMG_1, SecurityFindingSource.TRIVY_IMAGE, "image_digest"),
        (SecurityControlId.IAM_2, SecurityFindingSource.WAS_TEST, "was_version"),
        (SecurityControlId.CFG_1, SecurityFindingSource.TRIVY_CONFIG, "config_commit"),
    ],
)
async def test_other_scopes_cannot_hide_required_evidence(
    session_factory: async_sessionmaker[AsyncSession],
    organization: Organization,
    control: SecurityControlId,
    source: SecurityFindingSource,
    scope_field: str,
    expected: SecurityResult,
) -> None:
    scope = SecurityScope(
        organization.id,
        image_digest="sha256:" + "a" * 64,
        was_version="was-current",
        config_commit="a" * 40,
    )
    other = replace(
        scope,
        **{
            scope_field: {
                "image_digest": "sha256:" + "b" * 64,
                "was_version": "was-other",
                "config_commit": "b" * 40,
            }[scope_field]
        },
    )
    needed = _finding(scope, control, source, expected, NOW - timedelta(minutes=10))
    async with session_factory() as session, session.begin():
        session.add_all(
            [
                needed,
                *[
                    _finding(
                        other, control, source, SecurityResult.PASS, NOW - timedelta(minutes=1)
                    )
                    for _ in range(FLOOD_COUNT)
                ],
            ]
        )

    async with session_factory() as session:
        findings = await SecurityFindingService(session).search_for_judgement(organization.id, NOW)
    decision = evaluate_control(
        get_rule(control),
        SecurityTarget(scope, "gcp"),
        SecurityCheckPhase.PREPARATION,
        findings,
        [],
        NOW,
    )
    assert decision.result == expected
    assert needed.id in decision.finding_ids


@pytest.mark.parametrize("surface", ["matrix", "approval"])
async def test_repeated_pass_cannot_hide_existing_fail(
    migration: Migration, surface: Literal["matrix", "approval"]
) -> None:
    await prepare_passing_gate(migration)
    harness = migration.harness
    now = harness.clock.now()
    scope = SecurityScope(harness.organization.id, image_digest=await target_digest(migration))
    failed = _finding(
        scope,
        SecurityControlId.IMG_1,
        SecurityFindingSource.TRIVY_IMAGE,
        SecurityResult.FAIL,
        now - timedelta(minutes=10),
    )
    failed.reason = SecurityFindingReason.FIXABLE_HIGH_VULNERABILITY
    async with harness.session_factory() as session, session.begin():
        session.add_all(
            [
                failed,
                *[
                    _finding(
                        scope,
                        SecurityControlId.IMG_1,
                        SecurityFindingSource.TRIVY_IMAGE,
                        SecurityResult.PASS,
                        now - timedelta(seconds=1),
                    )
                    for _ in range(FLOOD_COUNT)
                ],
            ]
        )
    if surface == "matrix":
        response = await harness.client.get(
            f"/api/v1/orgs/{harness.organization.id}/security/matrix"
        )
        assert response.status_code == 200, response.text
        controls = [
            control
            for row in response.json()["data"]["rows"]
            for control in row["cells"]["policy"]["controls"]
            if control["controlId"] == "C-IMG-1"
        ]
        assert controls and all(c["result"] == "FAIL" for c in controls)
        assert all(str(failed.id) in c["findingIds"] for c in controls)
    else:
        plan = await migration.create_plan()
        response = await migration.act(plan["id"], "approve")
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "SECURITY_GATE_BLOCKED"
        assert any(d["field"] == "C-IMG-1" for d in response.json()["details"])
