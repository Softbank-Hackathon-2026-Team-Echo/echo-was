"""보안 매트릭스·증거 조회 API 통합 시험(보안·거버넌스 4 SPEC AC-1~AC-6)."""

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.enums import (
    DeployRequestStatus,
    SecurityControlId,
    SecurityReasonCode,
    SecurityResult,
)
from app.models import Deployment, ServiceEnvironment
from app.services.security_finding_service import SecurityFindingService
from app.services.security_normalizers import (
    NormalizedFinding,
    normalize_gitleaks,
    normalize_trivy_image,
)
from app.services.security_rules import SecurityScope
from tests.integration.auth_support import create_organization
from tests.integration.deploy_harness import DeployHarness

CELLS = ("iam", "encryption", "auditLog", "policy")
RESULTS = {r.value for r in SecurityResult}
OTHER_DIGEST = "sha256:" + "0" * 64


def _trivy(digest: str, *, fixable_high: bool) -> dict[str, Any]:
    name = f"registry.echo.test/shop-api@{digest}"
    vulnerabilities = (
        [
            {
                "VulnerabilityID": "CVE-2026-97687",
                "PkgName": "urllib3",
                "InstalledVersion": "2.7.0",
                "FixedVersion": "2.8.0",
                "Severity": "HIGH",
            }
        ]
        if fixable_high
        else []
    )
    return {
        "SchemaVersion": 2,
        "ArtifactName": name,
        "Metadata": {"RepoDigests": [name]},
        "Results": [{"Target": "Python", "Vulnerabilities": vulnerabilities}],
    }


async def _matrix(harness: DeployHarness) -> dict[str, Any]:
    response = await harness.client.get(f"/api/v1/orgs/{harness.organization.id}/security/matrix")
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def _findings(harness: DeployHarness, **params: Any) -> dict[str, Any]:
    response = await harness.client.get(
        f"/api/v1/orgs/{harness.organization.id}/security/findings", params=params
    )
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def _deployed(harness: DeployHarness) -> tuple[dict[str, Any], uuid.UUID, str]:
    """서비스 하나를 aws-seoul 에 배포하고 (서비스, 환경 id, 현재 이미지 digest)."""
    service = await harness.create_service("shop-api")
    environment = await harness.add_environment("aws-seoul")
    request = await harness.deploy(service["id"], environment.id)
    await harness.run_until(request["id"], {DeployRequestStatus.SUCCEEDED})
    async with harness.session_factory() as session:
        digest = await session.scalar(
            select(Deployment.image_digest)
            .join(ServiceEnvironment, ServiceEnvironment.current_deployment_id == Deployment.id)
            .where(ServiceEnvironment.service_id == uuid.UUID(service["id"]))
        )
    assert digest is not None
    return service, environment.id, digest


async def _record(harness: DeployHarness, findings: list[NormalizedFinding]) -> list[uuid.UUID]:
    async with harness.session_factory() as session:
        return await SecurityFindingService(session).record_all(findings, "github-actions:run/1")


def _image_findings(
    harness: DeployHarness, service_id: str, digest: str, *, fixable_high: bool
) -> list[NormalizedFinding]:
    scope = SecurityScope(
        organization_id=harness.organization.id,
        service_id=uuid.UUID(service_id),
        image_digest=digest,
    )
    now = harness.clock.now() - timedelta(minutes=1)
    return [
        normalize_trivy_image(
            _trivy(digest, fixable_high=fixable_high), scope=scope, collected_at=now
        ),
        normalize_gitleaks([], scope=scope, collected_at=now),
    ]


def _control(cell: dict[str, Any], control_id: SecurityControlId) -> dict[str, Any]:
    return next(c for c in cell["controls"] if c["controlId"] == control_id.value)


# --- AC-1 환경마다 4칸, 빈칸 없음, 미수집은 UNKNOWN ---


async def test_matrix_has_four_cells_per_environment_and_no_blanks(
    harness: DeployHarness,
) -> None:
    await harness.add_environment("aws-seoul")
    await harness.add_environment("gcp-seoul")

    matrix = await _matrix(harness)

    assert matrix["phase"] == "PREPARATION"
    assert matrix["policyVersion"].startswith("sec-rules-")
    assert [row["environmentName"] for row in matrix["rows"]] == ["aws-seoul", "gcp-seoul"]
    for row in matrix["rows"]:
        assert set(row["cells"]) == set(CELLS)
        for cell in row["cells"].values():
            assert cell["result"] in RESULTS
            assert cell["controls"], "칸마다 판정한 통제가 있다"
    # 아무 증거도 없으면 PASS 가 아니라 UNKNOWN 이다.
    first = matrix["rows"][0]["cells"]
    assert {first[name]["result"] for name in CELLS} == {SecurityResult.UNKNOWN.value}
    iam_2 = _control(first["iam"], SecurityControlId.IAM_2)
    assert SecurityReasonCode.EVIDENCE_MISSING.value in iam_2["reasons"]


async def test_environment_without_services_skips_service_controls(
    harness: DeployHarness,
) -> None:
    await harness.add_environment("aws-seoul")

    policy = (await _matrix(harness))["rows"][0]["cells"]["policy"]

    control_ids = {c["controlId"] for c in policy["controls"]}
    assert SecurityControlId.IMG_1.value not in control_ids
    assert SecurityControlId.NET_1.value in control_ids


async def test_onprem_environment_marks_cloud_audit_control_not_applicable(
    harness: DeployHarness,
) -> None:
    await harness.add_environment("onprem-k3s")

    audit = (await _matrix(harness))["rows"][0]["cells"]["auditLog"]

    assert _control(audit, SecurityControlId.AUD_2)["result"] == SecurityResult.NA.value


# --- AC-2 서비스 이미지 증거가 그 환경 칸에 들어간다 ---


async def test_policy_cell_fails_on_fixable_high_for_deployed_image(
    harness: DeployHarness,
) -> None:
    service, environment_id, digest = await _deployed(harness)
    finding_ids = await _record(
        harness, _image_findings(harness, service["id"], digest, fixable_high=True)
    )

    row = (await _matrix(harness))["rows"][0]

    assert row["environmentId"] == str(environment_id)
    assert row["services"] == [
        {"serviceId": service["id"], "name": "shop-api", "imageDigest": digest}
    ]
    policy = row["cells"]["policy"]
    assert policy["result"] == SecurityResult.FAIL.value
    assert policy["findingCount"] == 2
    assert policy["mode"] == "REAL"
    image = _control(policy, SecurityControlId.IMG_1)
    assert image["result"] == SecurityResult.FAIL.value
    assert image["serviceId"] == service["id"]
    assert image["findingIds"] == [str(finding_ids[0])]
    assert SecurityReasonCode.CONTROL_FAILED.value in image["reasons"]
    assert image["checkedAt"] is not None and image["expiresAt"] is not None
    assert _control(policy, SecurityControlId.IMG_2)["result"] == SecurityResult.PASS.value


async def test_evidence_for_other_digest_is_not_used(harness: DeployHarness) -> None:
    service, _, _ = await _deployed(harness)
    await _record(harness, _image_findings(harness, service["id"], OTHER_DIGEST, fixable_high=True))

    policy = (await _matrix(harness))["rows"][0]["cells"]["policy"]

    image = _control(policy, SecurityControlId.IMG_1)
    assert image["result"] == SecurityResult.UNKNOWN.value
    assert SecurityReasonCode.SCOPE_MISMATCH.value in image["reasons"]
    assert policy["result"] != SecurityResult.FAIL.value


# --- AC-3 증거 목록: 조건·만료·조직 격리, 비밀값 원문 없음 ---


async def test_findings_list_returns_evidence_with_items_and_origin(
    harness: DeployHarness,
) -> None:
    service, _, digest = await _deployed(harness)
    finding_ids = await _record(
        harness, _image_findings(harness, service["id"], digest, fixable_high=True)
    )

    page = await _findings(harness, controlId="C-IMG-1")

    assert page["total"] == 1
    item = page["items"][0]
    assert item["id"] == str(finding_ids[0])
    assert (item["controlId"], item["result"], item["origin"], item["mode"]) == (
        "C-IMG-1",
        "FAIL",
        "RULE",
        "REAL",
    )
    assert item["imageDigest"] == digest
    assert item["evidenceRef"] == "github-actions:run/1"
    assert item["items"][0]["itemId"] == "CVE-2026-97687"
    assert item["items"][0]["fixedVersion"] == "2.8.0"
    assert item["totalItems"] == 1


async def test_findings_list_filters_by_ids_and_hides_other_organizations(
    harness: DeployHarness,
) -> None:
    service, _, digest = await _deployed(harness)
    finding_ids = await _record(
        harness, _image_findings(harness, service["id"], digest, fixable_high=False)
    )
    other = await create_organization(harness.session_factory, "other-org")
    other_scope = SecurityScope(organization_id=other.id, image_digest=digest)
    await _record(
        harness,
        [normalize_gitleaks([], scope=other_scope, collected_at=harness.clock.now())],
    )

    everything = await _findings(harness)
    picked = await _findings(harness, findingId=[str(finding_ids[1])])

    assert everything["total"] == 2
    assert [item["id"] for item in picked["items"]] == [str(finding_ids[1])]


async def test_findings_list_hides_expired_unless_asked(harness: DeployHarness) -> None:
    service, _, digest = await _deployed(harness)
    await _record(harness, _image_findings(harness, service["id"], digest, fixable_high=False))
    # 증거(수집 1분 전, 유효 7일)는 지나고 로그인 세션(7일)은 남는 시각.
    harness.clock.advance((timedelta(days=7) - timedelta(seconds=30)).total_seconds())

    assert (await _findings(harness))["total"] == 0
    assert (await _findings(harness, includeExpired="true"))["total"] == 2
    # 만료된 증거는 매트릭스에서도 쓰지 않는다.
    policy = (await _matrix(harness))["rows"][0]["cells"]["policy"]
    assert _control(policy, SecurityControlId.IMG_1)["result"] == SecurityResult.UNKNOWN.value
