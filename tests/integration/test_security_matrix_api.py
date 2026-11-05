"""보안 매트릭스·증거 조회 API 통합 시험(보안·거버넌스 4 SPEC AC-1~AC-6, 보안 6 SPEC AC-1~AC-3)."""

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from app.enums import (
    DeployRequestStatus,
    SecurityCheckPhase,
    SecurityControlId,
    SecurityFindingMode,
    SecurityFindingReason,
    SecurityFindingSource,
    SecurityGateStatus,
    SecurityReasonCode,
    SecurityResult,
)
from app.models import Deployment, Organization, SecurityException, ServiceEnvironment, User
from app.repositories.finding_repository import FindingRepository
from app.repositories.security_exception_repository import SecurityExceptionRepository
from app.services.security_exception_service import to_security_exception
from app.services.security_finding_service import SecurityFindingService, to_security_finding
from app.services.security_normalizers import (
    NormalizedFinding,
    normalize_gitleaks,
    normalize_trivy_image,
)
from app.services.security_rules import (
    EXCEPTION_MAX_DURATION,
    POLICY_VERSION,
    SecurityFinding,
    SecurityScope,
    SecurityTarget,
    evaluate_security,
    get_rule,
)
from tests.integration.auth_support import create_organization, create_user
from tests.integration.deploy_harness import DeployHarness

CELLS = ("iam", "encryption", "auditLog", "policy")
RESULTS = {r.value for r in SecurityResult}
OTHER_DIGEST = "sha256:" + "0" * 64
WAS_VERSION = "2026.11.05"
EXCEPTION_REASON = "시연 범위: 증거 수집 전 임시 면제"
BLOCKED = SecurityGateStatus.BLOCKED.value
WAIVED = SecurityGateStatus.WAIVED.value
GATE_PASS = SecurityGateStatus.PASS.value


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


# --- 보안 6: 기한부 예외 반영 ---


async def _approver(harness: DeployHarness) -> User:
    return await create_user(harness.session_factory, "exception-approver")


async def _add_exception(
    harness: DeployHarness,
    control: SecurityControlId,
    approved_by: User | None,
    *,
    requested_by: User | None = None,
    expires_in: timedelta = timedelta(days=1),
    created_ago: timedelta = timedelta(minutes=5),
    organization_id: uuid.UUID | None = None,
    service_id: uuid.UUID | None = None,
    environment_id: uuid.UUID | None = None,
    image_digest: str | None = None,
    was_version: str | None = None,
    revoked: bool = False,
) -> SecurityException:
    created_at = harness.clock.now() - created_ago
    requester = requested_by or harness.admin.user
    exception = SecurityException(
        organization_id=organization_id or harness.organization.id,
        control_id=control,
        service_id=service_id,
        environment_id=environment_id,
        image_digest=image_digest,
        was_version=was_version,
        reason=EXCEPTION_REASON,
        requested_by_user_id=requester.id,
        approved_by_user_id=approved_by.id if approved_by is not None else None,
        approved_at=created_at if approved_by is not None else None,
        revoked_by_user_id=requester.id if revoked else None,
        revoked_at=created_at if revoked else None,
        expires_at=created_at + expires_in,
        created_at=created_at,
        updated_at=created_at,
    )
    async with harness.session_factory() as session, session.begin():
        session.add(exception)
    return exception


async def _allow_self_approval(harness: DeployHarness, *, allowed: bool) -> None:
    async with harness.session_factory() as session, session.begin():
        await session.execute(
            update(Organization)
            .where(Organization.id == harness.organization.id)
            .values(allow_self_approval=allowed)
        )


def _row(matrix: dict[str, Any], environment_name: str) -> dict[str, Any]:
    return next(row for row in matrix["rows"] if row["environmentName"] == environment_name)


def _assert_no_exception(cell: dict[str, Any], control: dict[str, Any]) -> None:
    assert cell["hasException"] is False
    assert cell["exception"] is None
    assert control["exception"] is None
    assert SecurityReasonCode.EXCEPTION_APPLIED.value not in control["reasons"]


def _evidence_values(matrix: dict[str, Any]) -> list[Any]:
    """예외가 바꾸면 안 되는 값: 칸·통제의 result 와 쓴 증거."""
    return [
        (
            row["environmentName"],
            name,
            cell["result"],
            cell["findingCount"],
            [(c["controlId"], c["result"], c["findingIds"]) for c in cell["controls"]],
        )
        for row in matrix["rows"]
        for name, cell in row["cells"].items()
    ]


def _passing_was_finding(
    harness: DeployHarness, control: SecurityControlId, was_version: str
) -> NormalizedFinding:
    collected_at = harness.clock.now() - timedelta(minutes=1)
    finding = SecurityFinding(
        finding_id=uuid.uuid4(),
        control_id=control,
        result=SecurityResult.PASS,
        mode=SecurityFindingMode.REAL,
        source=SecurityFindingSource.WAS_TEST,
        scope=SecurityScope(harness.organization.id, was_version=was_version),
        collected_at=collected_at,
        expires_at=collected_at + get_rule(control).max_age,
        policy_version=POLICY_VERSION,
    )
    return NormalizedFinding(finding, SecurityFindingReason.NO_ISSUES, (), 0, None)


# AC-1 예외가 칸에 붙는다


async def test_organization_exception_marks_encryption_cell_in_every_environment(
    harness: DeployHarness,
) -> None:
    await _deployed(harness)
    await harness.add_environment("gcp-seoul")
    before = await _matrix(harness)
    approver = await _approver(harness)

    exception = await _add_exception(harness, SecurityControlId.SEC_1, approver)
    matrix = await _matrix(harness)

    for name in ("aws-seoul", "gcp-seoul"):
        cell = _row(matrix, name)["cells"]["encryption"]
        assert cell["hasException"] is True
        assert cell["exception"]["id"] == str(exception.id)
        assert cell["exception"]["controlId"] == SecurityControlId.SEC_1.value
        assert cell["exception"]["reason"] == EXCEPTION_REASON
        assert datetime.fromisoformat(cell["exception"]["expiresAt"]) == exception.expires_at
        assert cell["exception"]["approvedBy"] == {
            "id": str(approver.id),
            "login": "exception-approver",
        }
        control = _control(cell, SecurityControlId.SEC_1)
        assert control["gateStatus"] == WAIVED
        assert SecurityReasonCode.EXCEPTION_APPLIED.value in control["reasons"]
        assert control["exception"] == cell["exception"]
        previous = _control(_row(before, name)["cells"]["encryption"], SecurityControlId.SEC_1)
        assert control["result"] == previous["result"] == SecurityResult.UNKNOWN.value
        assert datetime.fromisoformat(cell["expiresAt"]) == exception.expires_at
    # 서비스 없는 환경은 필수 통제가 C-SEC-1 뿐이다. 서비스가 있으면 증거 없는 C-TLS-1 이 막는다.
    without_service = _row(matrix, "gcp-seoul")["cells"]["encryption"]
    assert without_service["gateStatus"] == WAIVED
    deployed = _row(matrix, "aws-seoul")["cells"]["encryption"]
    assert _control(deployed, SecurityControlId.TLS_1)["gateStatus"] == BLOCKED
    assert deployed["gateStatus"] == BLOCKED


@pytest.mark.parametrize(
    ("server_version", "gate_status", "is_applied"),
    [(WAS_VERSION, WAIVED, True), ("2026.11.04", BLOCKED, False), (None, BLOCKED, False)],
    ids=["same", "different", "none"],
)
async def test_was_version_exception_applies_only_when_server_version_matches(
    harness: DeployHarness, server_version: str | None, gate_status: str, is_applied: bool
) -> None:
    await harness.add_environment("aws-seoul")
    harness.use_settings(was_version=server_version)
    exception = await _add_exception(
        harness, SecurityControlId.IAM_2, await _approver(harness), was_version=WAS_VERSION
    )

    cell = (await _matrix(harness))["rows"][0]["cells"]["iam"]

    control = _control(cell, SecurityControlId.IAM_2)
    assert cell["gateStatus"] == control["gateStatus"] == gate_status
    if is_applied:
        assert cell["hasException"] is True
        assert cell["exception"]["id"] == str(exception.id)
        assert control["exception"]["id"] == str(exception.id)
    else:
        _assert_no_exception(cell, control)


@pytest.mark.parametrize(
    ("allowed", "gate_status", "is_applied"),
    [(True, WAIVED, True), (False, BLOCKED, False)],
    ids=["allowed", "not-allowed"],
)
async def test_self_approved_exception_follows_organization_switch(
    harness: DeployHarness, allowed: bool, gate_status: str, is_applied: bool
) -> None:
    await harness.add_environment("aws-seoul")
    await _allow_self_approval(harness, allowed=allowed)
    await _add_exception(harness, SecurityControlId.SEC_1, harness.admin.user)

    cell = (await _matrix(harness))["rows"][0]["cells"]["encryption"]

    control = _control(cell, SecurityControlId.SEC_1)
    assert cell["gateStatus"] == control["gateStatus"] == gate_status
    if is_applied:
        assert cell["hasException"] is True
    else:
        _assert_no_exception(cell, control)


# AC-2 안 맞는 예외는 무시된다


@pytest.mark.parametrize("kind", ["pending", "revoked", "other-organization"])
async def test_unusable_exception_is_ignored(harness: DeployHarness, kind: str) -> None:
    await harness.add_environment("aws-seoul")
    approver = await _approver(harness)
    match kind:
        case "pending":
            await _add_exception(harness, SecurityControlId.SEC_1, None)
        case "revoked":
            await _add_exception(harness, SecurityControlId.SEC_1, approver, revoked=True)
        case _:
            other = await create_organization(harness.session_factory, "other-org")
            await _add_exception(
                harness, SecurityControlId.SEC_1, approver, organization_id=other.id
            )

    cell = (await _matrix(harness))["rows"][0]["cells"]["encryption"]

    control = _control(cell, SecurityControlId.SEC_1)
    _assert_no_exception(cell, control)
    assert control["gateStatus"] == BLOCKED
    assert cell["gateStatus"] == BLOCKED
    assert SecurityReasonCode.EXCEPTION_EXPIRED.value not in control["reasons"]


async def test_expired_exception_adds_reason_and_stays_blocked(harness: DeployHarness) -> None:
    await harness.add_environment("aws-seoul")
    await _add_exception(
        harness,
        SecurityControlId.SEC_1,
        await _approver(harness),
        created_ago=timedelta(days=2),
        expires_in=timedelta(days=1),
    )

    cell = (await _matrix(harness))["rows"][0]["cells"]["encryption"]

    control = _control(cell, SecurityControlId.SEC_1)
    _assert_no_exception(cell, control)
    assert control["gateStatus"] == BLOCKED
    assert SecurityReasonCode.EXCEPTION_EXPIRED.value in control["reasons"]


async def test_environment_scoped_exception_applies_only_to_that_environment(
    harness: DeployHarness,
) -> None:
    covered = await harness.add_environment("aws-seoul")
    await harness.add_environment("gcp-seoul")
    exception = await _add_exception(
        harness, SecurityControlId.IAM_1, await _approver(harness), environment_id=covered.id
    )

    matrix = await _matrix(harness)

    covered_cell = _row(matrix, "aws-seoul")["cells"]["iam"]
    assert covered_cell["hasException"] is True
    assert covered_cell["exception"]["id"] == str(exception.id)
    # 선택 통제의 예외도 WAIVED 다. 칸은 예외 없는 필수 C-IAM-2 때문에 BLOCKED 가 먼저다.
    assert _control(covered_cell, SecurityControlId.IAM_1)["gateStatus"] == WAIVED
    assert covered_cell["gateStatus"] == BLOCKED
    other_cell = _row(matrix, "gcp-seoul")["cells"]["iam"]
    other_control = _control(other_cell, SecurityControlId.IAM_1)
    _assert_no_exception(other_cell, other_control)
    assert other_control["gateStatus"] == GATE_PASS


async def test_cell_exception_is_the_one_expiring_first(harness: DeployHarness) -> None:
    environment = await harness.add_environment("aws-seoul")
    harness.use_settings(was_version=WAS_VERSION)
    approver = await _approver(harness)
    later = await _add_exception(
        harness,
        SecurityControlId.IAM_1,
        approver,
        environment_id=environment.id,
        expires_in=timedelta(days=3),
    )
    earlier = await _add_exception(
        harness, SecurityControlId.IAM_2, approver, was_version=WAS_VERSION
    )

    cell = (await _matrix(harness))["rows"][0]["cells"]["iam"]

    assert cell["exception"]["id"] == str(earlier.id)
    assert _control(cell, SecurityControlId.IAM_1)["exception"]["id"] == str(later.id)
    assert _control(cell, SecurityControlId.IAM_2)["exception"]["id"] == str(earlier.id)
    assert cell["gateStatus"] == WAIVED


# AC-3 게이트와 같고 기존 값은 그대로다


async def test_matrix_controls_match_direct_rule_evaluation(harness: DeployHarness) -> None:
    service, environment_id, digest = await _deployed(harness)
    service_id = uuid.UUID(service["id"])
    harness.use_settings(was_version=WAS_VERSION)
    await _allow_self_approval(harness, allowed=True)
    await _record(harness, _image_findings(harness, service["id"], digest, fixable_high=True))
    approver = await _approver(harness)
    await _add_exception(harness, SecurityControlId.IMG_1, approver, image_digest=digest)
    await _add_exception(harness, SecurityControlId.IAM_2, approver, was_version=WAS_VERSION)
    await _add_exception(harness, SecurityControlId.SEC_1, harness.admin.user)
    await _add_exception(harness, SecurityControlId.CI_1, None)
    await _add_exception(harness, SecurityControlId.AUD_2, approver, environment_id=environment_id)

    row = (await _matrix(harness))["rows"][0]

    now = harness.clock.now()
    organization_id = harness.organization.id
    async with harness.session_factory() as session, session.begin():
        finding_models = await FindingRepository(session).search_unexpired_by_organization(
            organization_id, now
        )
        exception_models = await SecurityExceptionRepository(session).search_for_judgement(
            organization_id, now - EXCEPTION_MAX_DURATION
        )
    decision = evaluate_security(
        SecurityTarget(
            scope=SecurityScope(
                organization_id,
                service_id=service_id,
                environment_id=environment_id,
                image_digest=digest,
                was_version=WAS_VERSION,
            ),
            environment_kind=row["environmentKind"],
            allow_self_approval=True,
        ),
        SecurityCheckPhase.PREPARATION,
        [to_security_finding(model) for model in finding_models],
        [to_security_exception(model) for model in exception_models],
        now,
    )
    lines = {c["controlId"]: c for cell in row["cells"].values() for c in cell["controls"]}
    assert set(lines) == {control.control_id.value for control in decision.controls}
    for control in decision.controls:
        line = lines[control.control_id.value]
        assert (line["gateStatus"] == BLOCKED) == control.is_blocking
        exception_id = line["exception"]["id"] if line["exception"] else None
        assert exception_id == (str(control.exception_id) if control.exception_id else None)
        assert line["result"] == control.result.value
        assert line["reasons"] == [reason.value for reason in control.reasons]
    assert {line["gateStatus"] for line in lines.values()} == {BLOCKED, WAIVED, GATE_PASS}


async def test_exception_does_not_change_evidence_results(harness: DeployHarness) -> None:
    service, environment_id, digest = await _deployed(harness)
    harness.use_settings(was_version=WAS_VERSION)
    await _record(harness, _image_findings(harness, service["id"], digest, fixable_high=True))
    before = await _matrix(harness)
    approver = await _approver(harness)

    await _add_exception(harness, SecurityControlId.IMG_1, approver, image_digest=digest)
    await _add_exception(harness, SecurityControlId.IAM_2, approver, was_version=WAS_VERSION)
    await _add_exception(harness, SecurityControlId.SEC_1, approver)
    await _add_exception(
        harness,
        SecurityControlId.TLS_1,
        approver,
        service_id=uuid.UUID(service["id"]),
        environment_id=environment_id,
    )
    after = await _matrix(harness)

    assert _evidence_values(after) == _evidence_values(before)
    cells = after["rows"][0]["cells"]
    # 칸은 WARN 이 아니라 증거 값 그대로이고, 게이트 관점만 WAIVED 다.
    policy = cells["policy"]
    assert policy["result"] == SecurityResult.FAIL.value
    assert policy["hasException"] is True
    assert _control(policy, SecurityControlId.IMG_1)["gateStatus"] == WAIVED
    assert cells["encryption"]["result"] == SecurityResult.UNKNOWN.value
    assert cells["encryption"]["gateStatus"] == WAIVED


async def test_cell_without_exception_blocks_on_mandatory_unknown(harness: DeployHarness) -> None:
    await harness.add_environment("onprem-k3s")

    cells = (await _matrix(harness))["rows"][0]["cells"]

    iam = cells["iam"]
    _assert_no_exception(iam, _control(iam, SecurityControlId.IAM_2))
    assert iam["gateStatus"] == BLOCKED
    assert _control(iam, SecurityControlId.IAM_1)["gateStatus"] == GATE_PASS
    # NA 통제는 막지 않는다.
    assert _control(cells["auditLog"], SecurityControlId.AUD_2)["gateStatus"] == GATE_PASS


async def test_was_version_evidence_counts_and_passing_control_ignores_exception(
    harness: DeployHarness,
) -> None:
    await harness.add_environment("aws-seoul")
    harness.use_settings(was_version=WAS_VERSION)
    await _record(harness, [_passing_was_finding(harness, SecurityControlId.IAM_2, WAS_VERSION)])
    await _add_exception(
        harness, SecurityControlId.IAM_2, await _approver(harness), was_version=WAS_VERSION
    )

    cell = (await _matrix(harness))["rows"][0]["cells"]["iam"]

    control = _control(cell, SecurityControlId.IAM_2)
    assert control["result"] == SecurityResult.PASS.value
    _assert_no_exception(cell, control)
    assert cell["findingCount"] == 1
    # 선택 통제 C-IAM-1 의 UNKNOWN 은 칸 값에 남아도 게이트를 막지 않는다.
    assert cell["result"] == SecurityResult.UNKNOWN.value
    assert cell["gateStatus"] == GATE_PASS
