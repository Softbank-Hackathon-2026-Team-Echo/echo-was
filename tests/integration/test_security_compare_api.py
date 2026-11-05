"""이전 전후 보안 비교 API 통합 시험(보안 5-2 SPEC AC-3~AC-6)."""

import uuid
from datetime import timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select

from app.enums import (
    DeployRequestStatus,
    OrganizationRole,
    SecurityControlId,
    SecurityGateStatus,
)
from app.main import app
from app.models import Deployment, SecurityException, ServiceEnvironment
from app.services.security_finding_service import SecurityFindingService
from app.services.security_normalizers import normalize_gitleaks, normalize_trivy_image
from app.services.security_rules import SecurityScope
from tests.integration.auth_support import (
    add_member,
    create_agent_token,
    create_member,
    create_organization,
    create_user,
    make_client,
)
from tests.integration.deploy_harness import DeployHarness

CELLS = ["iam", "encryption", "auditLog", "policy"]
COMPARE_PATH = "/api/v1/orgs/{org_id}/security/compare"


def _path(harness: DeployHarness) -> str:
    return f"/api/v1/orgs/{harness.organization.id}/security/compare"


async def _compare(
    harness: DeployHarness,
    source: uuid.UUID,
    target: uuid.UUID,
    client: AsyncClient | None = None,
    **params: Any,
) -> dict[str, Any]:
    response = await (client or harness.client).get(
        _path(harness), params={"source": str(source), "target": str(target), **params}
    )
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def _matrix(harness: DeployHarness) -> dict[str, Any]:
    response = await harness.client.get(f"/api/v1/orgs/{harness.organization.id}/security/matrix")
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


def _trivy(digest: str) -> dict[str, Any]:
    name = f"registry.echo.test/shop-api@{digest}"
    return {
        "SchemaVersion": 2,
        "ArtifactName": name,
        "Metadata": {"RepoDigests": [name]},
        "Results": [
            {
                "Target": "Python",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-2026-97687",
                        "PkgName": "urllib3",
                        "InstalledVersion": "2.7.0",
                        "FixedVersion": "2.8.0",
                        "Severity": "HIGH",
                    }
                ],
            }
        ],
    }


async def _deploy(harness: DeployHarness, name: str, environment_id: uuid.UUID) -> tuple[str, str]:
    """서비스를 환경에 배포하고 (서비스 id, 현재 digest)."""
    service = await harness.create_service(name)
    request = await harness.deploy(service["id"], environment_id)
    await harness.run_until(request["id"], {DeployRequestStatus.SUCCEEDED})
    async with harness.session_factory() as session:
        digest = await session.scalar(
            select(Deployment.image_digest)
            .join(ServiceEnvironment, ServiceEnvironment.current_deployment_id == Deployment.id)
            .where(ServiceEnvironment.service_id == uuid.UUID(service["id"]))
        )
    assert digest is not None
    return service["id"], digest


async def _record_fixable_high(harness: DeployHarness, service_id: str, digest: str) -> None:
    """그 이미지의 C-IMG-1 FAIL(수정 가능한 HIGH) + C-IMG-2 PASS 증거."""
    scope = SecurityScope(
        organization_id=harness.organization.id,
        service_id=uuid.UUID(service_id),
        image_digest=digest,
    )
    collected_at = harness.clock.now() - timedelta(minutes=1)
    findings = [
        normalize_trivy_image(_trivy(digest), scope=scope, collected_at=collected_at),
        normalize_gitleaks([], scope=scope, collected_at=collected_at),
    ]
    async with harness.session_factory() as session:
        await SecurityFindingService(session).record_all(findings, "github-actions:run/1")


async def _add_organization_exception(
    harness: DeployHarness, control: SecurityControlId
) -> SecurityException:
    """조직 범위 기한부 예외(요청 = 시험 어드민, 승인 = 다른 사용자)."""
    approver = await create_user(harness.session_factory, "compare-approver")
    created_at = harness.clock.now() - timedelta(minutes=5)
    exception = SecurityException(
        organization_id=harness.organization.id,
        control_id=control,
        reason="시연 범위: 증거 수집 전 임시 면제",
        requested_by_user_id=harness.admin.user.id,
        approved_by_user_id=approver.id,
        approved_at=created_at,
        expires_at=created_at + timedelta(days=1),
        created_at=created_at,
        updated_at=created_at,
    )
    async with harness.session_factory() as session, session.begin():
        session.add(exception)
    return exception


def _rows(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["control"]: row for row in data["rows"]}


def _matrix_cells(matrix: dict[str, Any], environment_id: uuid.UUID) -> dict[str, Any]:
    row = next(r for r in matrix["rows"] if r["environmentId"] == str(environment_id))
    cells: dict[str, Any] = row["cells"]
    return cells


# AC-3 두 환경 비교


async def test_compare_cells_equal_matrix_rows_and_mark_target_blocks(
    harness: DeployHarness,
) -> None:
    aws = await harness.add_environment("aws-seoul")
    gcp = await harness.add_environment("gcp-seoul")
    service_id, digest = await _deploy(harness, "shop-api", aws.id)
    await _record_fixable_high(harness, service_id, digest)

    matrix = await _matrix(harness)
    away = await _compare(harness, aws.id, gcp.id)
    toward = await _compare(harness, gcp.id, aws.id)

    for data, source, target in ((away, aws, gcp), (toward, gcp, aws)):
        assert data["simulated"] is False
        assert data["phase"] == "PREPARATION"
        assert data["policyVersion"] == matrix["policyVersion"]
        assert data["serviceId"] is None
        assert [row["control"] for row in data["rows"]] == CELLS
        assert data["source"]["environmentId"] == str(source.id)
        assert data["target"]["environmentName"] == target.name
        for row in data["rows"]:
            assert row["source"] == _matrix_cells(matrix, source.id)[row["control"]]
            assert row["target"] == _matrix_cells(matrix, target.id)[row["control"]]
            blocked = row["target"]["gateStatus"] == SecurityGateStatus.BLOCKED.value
            assert row["blocksMigration"] is blocked
            assert (len(row["blockingControlIds"]) > 0) is blocked
        summary = data["summary"]
        assert summary["blocking"] == sum(row["blocksMigration"] for row in data["rows"])
        assert summary["worseOnTarget"] + summary["same"] + summary["better"] == len(CELLS)
    assert [s["serviceId"] for s in away["source"]["services"]] == [service_id]
    assert away["target"]["services"] == []

    # 출발 aws 의 FAIL 이미지 → 서비스가 없는 gcp 로 가면 policy 칸이 나아지고 막지 않는다.
    policy_away = _rows(away)["policy"]
    assert policy_away["source"]["result"] == "FAIL"
    assert (policy_away["change"], policy_away["blocksMigration"]) == ("BETTER", False)
    # 반대로 FAIL 이미지가 도는 aws 로 가면 C-IMG-1 이 막는다.
    policy_toward = _rows(toward)["policy"]
    assert (policy_toward["change"], policy_toward["blocksMigration"]) == ("WORSE", True)
    assert SecurityControlId.IMG_1.value in policy_toward["blockingControlIds"]
    assert SecurityControlId.IMG_2.value not in policy_toward["blockingControlIds"]


async def test_exception_lifts_mandatory_unknown_on_target(harness: DeployHarness) -> None:
    aws = await harness.add_environment("aws-seoul")
    gcp = await harness.add_environment("gcp-seoul")

    before = _rows(await _compare(harness, aws.id, gcp.id))["encryption"]
    exception = await _add_organization_exception(harness, SecurityControlId.SEC_1)
    after = _rows(await _compare(harness, aws.id, gcp.id))["encryption"]

    # 서비스가 없는 환경의 encryption 필수 통제는 C-SEC-1 뿐이다(증거 없음 → UNKNOWN).
    assert before["blocksMigration"] is True
    assert before["blockingControlIds"] == [SecurityControlId.SEC_1.value]
    assert after["blocksMigration"] is False
    assert after["blockingControlIds"] == []
    assert after["target"]["gateStatus"] == SecurityGateStatus.WAIVED.value
    assert after["target"]["exception"]["id"] == str(exception.id)
    assert after["target"]["result"] == before["target"]["result"] == "UNKNOWN"
    assert after["change"] == "SAME"


async def test_service_id_counts_only_that_service(harness: DeployHarness) -> None:
    aws = await harness.add_environment("aws-seoul")
    gcp = await harness.add_environment("gcp-seoul")
    shop_id, shop_digest = await _deploy(harness, "shop-api", aws.id)
    cart_id, _ = await _deploy(harness, "cart-api", aws.id)
    await _record_fixable_high(harness, shop_id, shop_digest)

    everyone = await _compare(harness, aws.id, gcp.id)
    shop = await _compare(harness, aws.id, gcp.id, serviceId=shop_id)
    cart = await _compare(harness, aws.id, gcp.id, serviceId=cart_id)

    def service_ids(data: dict[str, Any]) -> set[str | None]:
        policy = _rows(data)["policy"]["source"]
        return {c["serviceId"] for c in policy["controls"] if c["serviceId"] is not None}

    assert service_ids(everyone) == {shop_id, cart_id}
    assert service_ids(shop) == {shop_id}
    assert service_ids(cart) == {cart_id}
    assert shop["serviceId"] == shop_id
    assert [s["serviceId"] for s in shop["source"]["services"]] == [shop_id]
    assert _rows(shop)["policy"]["source"]["result"] == "FAIL"
    # cart-api 는 검사 증거가 없어 UNKNOWN(필수) — FAIL 이미지는 shop-api 것이다.
    assert _rows(cart)["policy"]["source"]["result"] == "UNKNOWN"


async def test_unknown_or_foreign_service_is_not_found(harness: DeployHarness) -> None:
    aws = await harness.add_environment("aws-seoul")
    gcp = await harness.add_environment("gcp-seoul")
    other = await create_organization(harness.session_factory, "other-team")
    await add_member(harness.session_factory, other, harness.admin.user, OrganizationRole.ADMIN)
    created = await harness.client.post(
        f"/api/v1/orgs/{other.id}/services",
        json={"name": "shop-api", "repoUrl": "https://github.com/team-echo/shop-api"},
    )
    assert created.status_code == 201, created.text
    foreign_service_id = created.json()["data"]["id"]

    for service_id in (str(uuid.uuid4()), foreign_service_id):
        response = await harness.client.get(
            _path(harness),
            params={"source": str(aws.id), "target": str(gcp.id), "serviceId": service_id},
        )
        assert (response.status_code, response.json()["code"]) == (404, "SERVICE_NOT_FOUND")


# AC-4 거절


async def test_same_environment_is_rejected(harness: DeployHarness) -> None:
    aws = await harness.add_environment("aws-seoul")

    response = await harness.client.get(
        _path(harness), params={"source": str(aws.id), "target": str(aws.id)}
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["details"] == [{"field": "target", "reason": "SAME_ENVIRONMENT"}]


async def test_missing_or_malformed_query_is_rejected(harness: DeployHarness) -> None:
    aws = await harness.add_environment("aws-seoul")

    missing = await harness.client.get(_path(harness), params={"source": str(aws.id)})
    malformed = await harness.client.get(
        _path(harness), params={"source": "aws-seoul", "target": str(aws.id)}
    )

    for response in (missing, malformed):
        assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


async def test_environment_outside_organization_is_not_found(harness: DeployHarness) -> None:
    aws = await harness.add_environment("aws-seoul")
    other = await create_organization(harness.session_factory, "other-team")
    foreign = await harness.add_environment("gcp-seoul", organization=other)
    deleted = await harness.add_environment("onprem-k3s", deleted=True)

    for bad in (foreign.id, deleted.id, uuid.uuid4()):
        for source, target in ((aws.id, bad), (bad, aws.id)):
            response = await harness.client.get(
                _path(harness), params={"source": str(source), "target": str(target)}
            )
            assert (response.status_code, response.json()["code"]) == (
                404,
                "ENVIRONMENT_NOT_FOUND",
            ), (source, target)


# AC-5 권한


async def test_viewer_and_agent_can_read_and_outsider_cannot(harness: DeployHarness) -> None:
    aws = await harness.add_environment("aws-seoul")
    gcp = await harness.add_environment("gcp-seoul")
    now = harness.clock.now()
    viewer = await create_member(
        harness.session_factory, harness.organization, OrganizationRole.VIEWER, now
    )
    _, agent_token = await create_agent_token(
        harness.session_factory, harness.organization, harness.admin.user, now
    )
    other = await create_organization(harness.session_factory, "other-team")
    outsider = await create_member(harness.session_factory, other, OrganizationRole.ADMIN, now)

    async with (
        make_client(session_token=viewer.session_token) as viewer_client,
        make_client(agent_token=agent_token) as agent_client,
        make_client(session_token=outsider.session_token) as outsider_client,
    ):
        for client in (viewer_client, agent_client):
            data = await _compare(harness, aws.id, gcp.id, client=client)
            assert len(data["rows"]) == len(CELLS)
        denied = await outsider_client.get(
            _path(harness), params={"source": str(aws.id), "target": str(gcp.id)}
        )
        anonymous = await make_client().get(
            _path(harness), params={"source": str(aws.id), "target": str(gcp.id)}
        )

    assert (denied.status_code, denied.json()["code"]) == (404, "ORGANIZATION_NOT_FOUND")
    assert anonymous.status_code == 401


# AC-6 OpenAPI


def test_openapi_documents_compare() -> None:
    schema = app.openapi()
    operation = schema["paths"][COMPARE_PATH]["get"]
    params = {p["name"]: p for p in operation["parameters"] if p["in"] == "query"}
    assert params["source"]["required"] is True
    assert params["target"]["required"] is True
    assert params["serviceId"].get("required", False) is False

    schemas = schema["components"]["schemas"]
    response = schemas["SecurityCompareResponse"]
    assert {"rows", "summary", "simulated", "checkedAt", "source", "target"} <= set(
        response["required"]
    )
    row = schemas["SecurityCompareRowResponse"]
    assert {"control", "source", "target", "blocksMigration", "change"} <= set(row["required"])
    assert row["properties"]["control"]["enum"] == CELLS
    assert row["properties"]["source"] == {"$ref": "#/components/schemas/SecurityCellResponse"}
    assert schemas["SecurityCompareChange"]["enum"] == ["WORSE", "SAME", "BETTER"]
    summary = schemas["SecurityCompareSummaryResponse"]
    assert sorted(summary["required"]) == ["better", "blocking", "same", "worseOnTarget"]
