"""사람 점검 기록 API: POST /security/manual-reviews (C-IAM-1·C-TLS-2 등 사람 점검 통제).

harness 의 team-echo 조직에 개발자·뷰어·에이전트 토큰을 더해 부른다. 시각은 FakeClock 이다.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select

from app.audit.actions import AuditAction
from app.enums import (
    AuditResult,
    OrganizationRole,
    SecurityControlId,
    SecurityFindingMode,
    SecurityFindingReason,
    SecurityFindingSource,
    SecurityReasonCode,
    SecurityResult,
)
from app.models import AuditLog, Environment, Finding
from app.services.security_finding_service import (
    MANUAL_REVIEW_SUMMARY_ITEM_ID,
    SecurityFindingService,
)
from app.services.security_normalizers import NormalizedFinding
from app.services.security_rules import POLICY_VERSION, SecurityFinding, SecurityScope, get_rule
from tests.integration.auth_support import (
    Member,
    create_agent_token,
    create_member,
    create_organization,
    make_client,
    org_path,
)
from tests.integration.deploy_harness import DeployHarness

_C = SecurityControlId
EVIDENCE_URL = "https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-infra/issues/23"
SUMMARY = "실행 서비스 계정 IAM 정책을 콘솔에서 확인: 앱 네임스페이스 밖 권한 없음"
WAS_VERSION = "2026.11.08"


def _path(harness: DeployHarness) -> str:
    return org_path(harness.organization.id, "/security/manual-reviews")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _error(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["code"]


def _details(response: Response) -> set[tuple[str, str]]:
    return {(item["field"], item["reason"]) for item in response.json()["details"]}


def _body(
    environment: Environment | None, control: SecurityControlId = _C.IAM_1, **changes: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "controlId": control.value,
        "environmentId": str(environment.id) if environment is not None else None,
        "result": SecurityResult.PASS.value,
        "summary": SUMMARY,
        "evidenceUrl": EVIDENCE_URL,
    }
    body.update(changes)
    return {key: value for key, value in body.items() if value is not None}


async def _developer(harness: DeployHarness) -> Member:
    return await create_member(
        harness.session_factory, harness.organization, OrganizationRole.DEVELOPER, _now(harness)
    )


def _now(harness: DeployHarness) -> datetime:
    return harness.clock.now()


async def _count_manual_findings(harness: DeployHarness) -> int:
    async with harness.session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(Finding)
            .where(Finding.source == SecurityFindingSource.MANUAL_REVIEW)
        )
    return int(count or 0)


async def _matrix(harness: DeployHarness, client: AsyncClient | None = None) -> dict[str, Any]:
    response = await (client or harness.client).get(
        org_path(harness.organization.id, "/security/matrix")
    )
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


def _row(matrix: dict[str, Any], environment_name: str) -> dict[str, Any]:
    return next(row for row in matrix["rows"] if row["environmentName"] == environment_name)


def _control(cell: dict[str, Any], control_id: SecurityControlId) -> dict[str, Any]:
    return next(c for c in cell["controls"] if c["controlId"] == control_id.value)


async def _record_passing_iam_2(harness: DeployHarness) -> None:
    """필수 통제 C-IAM-2 를 PASS 로 둔다(자기 시험 증거와 같은 모양)."""
    harness.use_settings(was_version=WAS_VERSION)
    collected_at = _now(harness) - timedelta(minutes=1)
    finding = SecurityFinding(
        finding_id=uuid.uuid4(),
        control_id=_C.IAM_2,
        result=SecurityResult.PASS,
        mode=SecurityFindingMode.REAL,
        source=SecurityFindingSource.WAS_TEST,
        scope=SecurityScope(harness.organization.id, was_version=WAS_VERSION),
        collected_at=collected_at,
        expires_at=collected_at + get_rule(_C.IAM_2).max_age,
        policy_version=POLICY_VERSION,
    )
    async with harness.session_factory() as session:
        await SecurityFindingService(session).record(
            NormalizedFinding(finding, SecurityFindingReason.AGENT_WRITES_DENIED, (), 0, None)
        )


async def _post(client: AsyncClient, harness: DeployHarness, body: dict[str, Any]) -> Response:
    return await client.post(_path(harness), json=body)


# --- 만들기: 응답 = 만든 증거, 만료 = 규칙표 max_age(7일), 감사 줄 ---


async def test_record_manual_review_returns_finding_with_basis_summary_and_seven_day_expiry(
    harness: DeployHarness,
) -> None:
    environment = await harness.add_environment("aws-seoul")
    developer = await _developer(harness)
    now = _now(harness)

    async with make_client(session_token=developer.session_token) as client:
        response = await _post(client, harness, _body(environment))

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["controlId"] == "C-IAM-1"
    assert data["result"] == "PASS"
    assert (data["source"], data["origin"], data["mode"]) == ("MANUAL_REVIEW", "HUMAN", "REAL")
    assert data["reason"] == SecurityFindingReason.MANUAL_REVIEW_RECORDED.value
    assert data["environmentId"] == str(environment.id)
    assert data["serviceId"] is None
    assert data["reviewerUserId"] == str(developer.user.id)
    assert data["basisUrl"] == EVIDENCE_URL
    assert data["policyVersion"] == POLICY_VERSION
    assert _parse(data["collectedAt"]) == now
    assert _parse(data["expiresAt"]) == now + timedelta(days=7)
    assert data["items"] == [
        {
            "itemId": MANUAL_REVIEW_SUMMARY_ITEM_ID,
            "severity": None,
            "target": SUMMARY,
            "installedVersion": None,
            "fixedVersion": None,
            "line": None,
            "commit": None,
            "reason": SecurityFindingReason.MANUAL_REVIEW_RECORDED.value,
        }
    ]
    assert data["totalItems"] == 1

    async with harness.session_factory() as session:
        row = await session.get(Finding, uuid.UUID(data["id"]))
        audit = (
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.action == AuditAction.SECURITY_MANUAL_REVIEW_RECORDED
                )
            )
        ).all()
    assert row is not None
    assert (row.organization_id, row.reviewer_user_id) == (
        harness.organization.id,
        developer.user.id,
    )
    assert [
        (a.result, a.actor_user_id, a.target_type, str(a.target_id), a.target_label) for a in audit
    ] == [(AuditResult.SUCCEEDED, developer.user.id, "finding", data["id"], "C-IAM-1")]
    assert audit[0].details == {
        "result": "PASS",
        "environmentId": str(environment.id),
        "expiresAt": (now + timedelta(days=7)).isoformat(),
    }
    # 요약 원문은 증거 행에만 있다.
    assert SUMMARY not in str(audit[0].details)


async def test_record_manual_review_for_organization_wide_control_needs_no_scope(
    harness: DeployHarness,
) -> None:
    response = await _post(harness.client, harness, _body(None, _C.CI_1, result="WARN"))

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert (data["controlId"], data["result"]) == ("C-CI-1", "WARN")
    assert data["environmentId"] is None and data["serviceId"] is None


# --- 매트릭스 재계산: 선택 통제 UNKNOWN 이 사람 점검으로 PASS(_cell_rank) ---


async def test_matrix_iam_cell_turns_from_unknown_to_pass_after_manual_review(
    harness: DeployHarness,
) -> None:
    environment = await harness.add_environment("aws-seoul")
    await _record_passing_iam_2(harness)

    before = _row(await _matrix(harness), "aws-seoul")["cells"]["iam"]
    response = await _post(harness.client, harness, _body(environment, _C.IAM_1))
    after = _row(await _matrix(harness), "aws-seoul")["cells"]["iam"]

    assert response.status_code == 201, response.text
    # 전: 필수 C-IAM-2 PASS + 선택 C-IAM-1 UNKNOWN(EVIDENCE_MISSING) → 칸 UNKNOWN.
    assert before["result"] == SecurityResult.UNKNOWN.value
    assert _control(before, _C.IAM_1)["reasons"] == [SecurityReasonCode.EVIDENCE_MISSING.value]
    assert _control(before, _C.IAM_2)["result"] == SecurityResult.PASS.value
    # 뒤: 두 통제 모두 PASS → 칸 PASS. 칸 만료는 가장 이른 증거 만료다.
    assert after["result"] == SecurityResult.PASS.value
    iam_1 = _control(after, _C.IAM_1)
    assert iam_1["result"] == SecurityResult.PASS.value
    assert iam_1["findingIds"] == [response.json()["data"]["id"]]
    assert iam_1["reasons"] == []


async def test_matrix_encryption_cell_changes_only_in_the_reviewed_environment(
    harness: DeployHarness,
) -> None:
    aws = await harness.add_environment("aws-seoul")
    await harness.add_environment("gcp-seoul")

    before = await _matrix(harness)
    response = await _post(harness.client, harness, _body(aws, _C.TLS_2))
    after = await _matrix(harness)

    assert response.status_code == 201, response.text
    # 서비스가 없는 환경의 암호화 칸은 C-TLS-2 하나다.
    assert _row(before, "aws-seoul")["cells"]["encryption"]["result"] == "UNKNOWN"
    assert _row(after, "aws-seoul")["cells"]["encryption"]["result"] == "PASS"
    # 다른 환경은 범위가 달라 그대로 UNKNOWN(그 증거는 범위 불일치로 버려진다).
    gcp_encryption = _row(after, "gcp-seoul")["cells"]["encryption"]
    assert gcp_encryption["result"] == "UNKNOWN"
    assert _control(gcp_encryption, _C.TLS_2)["reasons"] == [
        SecurityReasonCode.EVIDENCE_MISSING.value,
        SecurityReasonCode.SCOPE_MISMATCH.value,
    ]


async def test_manual_review_fail_makes_cell_fail_and_expires_after_seven_days(
    harness: DeployHarness,
) -> None:
    environment = await harness.add_environment("aws-seoul")

    response = await _post(harness.client, harness, _body(environment, _C.TLS_2, result="FAIL"))
    failed = _row(await _matrix(harness), "aws-seoul")["cells"]["encryption"]
    harness.clock.advance(timedelta(days=7).total_seconds())
    # 관리자 세션도 7일이라 같이 끝난다. 지금 시각의 새 뷰어로 다시 본다.
    viewer = await create_member(
        harness.session_factory, harness.organization, OrganizationRole.VIEWER, _now(harness)
    )
    async with make_client(session_token=viewer.session_token) as viewer_client:
        expired = _row(await _matrix(harness, viewer_client), "aws-seoul")["cells"]["encryption"]

    assert response.status_code == 201, response.text
    assert failed["result"] == "FAIL"
    assert expired["result"] == "UNKNOWN"
    assert _control(expired, _C.TLS_2)["findingIds"] == []


# --- 권한: 뷰어·에이전트 403, 남의 조직 404, 아무것도 남기지 않는다 ---


async def test_record_manual_review_forbids_viewer_and_agent(harness: DeployHarness) -> None:
    environment = await harness.add_environment("aws-seoul")
    now = _now(harness)
    viewer = await create_member(
        harness.session_factory, harness.organization, OrganizationRole.VIEWER, now
    )
    _, agent_token = await create_agent_token(
        harness.session_factory, harness.organization, harness.admin.user, now
    )

    async with make_client(session_token=viewer.session_token) as viewer_client:
        by_viewer = await _post(viewer_client, harness, _body(environment))
    async with make_client(agent_token=agent_token) as agent_client:
        by_agent = await _post(agent_client, harness, _body(environment))
    async with make_client(session_token=harness.admin.session_token, origin=None) as no_origin:
        without_origin = await _post(no_origin, harness, _body(environment))

    assert _error(by_viewer) == (403, "ROLE_FORBIDDEN")
    assert _error(by_agent) == (403, "AGENT_READ_ONLY")
    assert _error(without_origin) == (403, "ORIGIN_NOT_ALLOWED")
    assert await _count_manual_findings(harness) == 0


async def test_record_manual_review_hides_other_organization_environment(
    harness: DeployHarness,
) -> None:
    other = await create_organization(harness.session_factory, "other-org")
    foreign = await harness.add_environment("aws-seoul", organization=other)
    deleted = await harness.add_environment("gcp-seoul", deleted=True)

    foreign_response = await _post(harness.client, harness, _body(foreign))
    deleted_response = await _post(harness.client, harness, _body(deleted))

    assert _error(foreign_response) == (404, "ENVIRONMENT_NOT_FOUND")
    assert _error(deleted_response) == (404, "ENVIRONMENT_NOT_FOUND")
    assert await _count_manual_findings(harness) == 0


# --- 422: 허용 안 된 통제, 증거 조작 방지(근거 링크·요약 필수), 범위 칸 ---


@pytest.mark.parametrize("control", [_C.IMG_1, _C.IAM_2, _C.TLS_1, _C.AUD_2, _C.AI_1])
async def test_record_manual_review_rejects_controls_without_manual_source(
    harness: DeployHarness, control: SecurityControlId
) -> None:
    environment = await harness.add_environment("aws-seoul")

    response = await _post(harness.client, harness, _body(environment, control))

    assert _error(response) == (422, "VALIDATION_ERROR")
    assert _details(response) == {("controlId", "MANUAL_REVIEW_NOT_ALLOWED")}
    assert await _count_manual_findings(harness) == 0


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"evidenceUrl": None}, "evidenceUrl"),
        ({"summary": None}, "summary"),
        ({"evidenceUrl": "   "}, "evidenceUrl"),
        ({"summary": "   "}, "summary"),
        ({"summary": "가" * 1001}, "summary"),
        ({"result": None}, "result"),
        ({"reviewerUserId": str(uuid.uuid4())}, "reviewerUserId"),
    ],
    ids=[
        "no-evidence-url",
        "no-summary",
        "blank-evidence-url",
        "blank-summary",
        "summary-too-long",
        "no-result",
        "reviewer-not-accepted",
    ],
)
async def test_record_manual_review_requires_evidence_and_summary_even_for_self(
    harness: DeployHarness, changes: dict[str, Any], field: str
) -> None:
    environment = await harness.add_environment("aws-seoul")
    body = _body(environment)
    for key, value in changes.items():
        if value is None:
            body.pop(key)
        else:
            body[key] = value

    response = await _post(harness.client, harness, body)

    assert _error(response) == (422, "VALIDATION_ERROR")
    assert field in {item["field"].split(".")[-1] for item in response.json()["details"]}
    assert await _count_manual_findings(harness) == 0


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"evidenceUrl": "http://example.com/review"}, ("evidenceUrl", "EVIDENCE_URL_INVALID")),
        ({"evidenceUrl": "not a url"}, ("evidenceUrl", "EVIDENCE_URL_INVALID")),
        ({"result": "UNKNOWN"}, ("result", "MANUAL_REVIEW_RESULT_INVALID")),
        ({"result": "NA"}, ("result", "MANUAL_REVIEW_RESULT_INVALID")),
        ({"environmentId": None}, ("environmentId", "REQUIRED_FOR_CONTROL")),
        ({"serviceId": str(uuid.uuid4())}, ("serviceId", "NOT_ALLOWED_FOR_CONTROL")),
    ],
    ids=["http", "not-url", "unknown", "na", "no-environment", "service-not-allowed"],
)
async def test_record_manual_review_rejects_rule_violations(
    harness: DeployHarness, changes: dict[str, Any], expected: tuple[str, str]
) -> None:
    environment = await harness.add_environment("aws-seoul")
    body = _body(environment, **{k: v for k, v in changes.items() if v is not None})
    for key in [k for k, v in changes.items() if v is None]:
        body.pop(key)

    response = await _post(harness.client, harness, body)

    assert _error(response) == (422, "VALIDATION_ERROR")
    assert _details(response) == {expected}
    assert await _count_manual_findings(harness) == 0


async def test_record_manual_review_rejects_environment_for_organization_wide_control(
    harness: DeployHarness,
) -> None:
    environment = await harness.add_environment("aws-seoul")

    response = await _post(harness.client, harness, _body(environment, _C.CI_1))

    assert _details(response) == {("environmentId", "NOT_ALLOWED_FOR_CONTROL")}


# --- 교차 검수 반영: 잘못된 근거 주소는 422(500·201 아님), 필수 통제는 받지 않는다 ---


async def _count_manual_review_audits(harness: DeployHarness) -> int:
    async with harness.session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == AuditAction.SECURITY_MANUAL_REVIEW_RECORDED)
        )
    return int(count or 0)


@pytest.mark.parametrize(
    "evidence_url",
    ["https://[", "https://example.com:bad/review", "https:// /review"],
    ids=["unparsable-ipv6-bracket", "non-integer-port", "blank-host"],
)
async def test_record_manual_review_rejects_malformed_evidence_url_with_422(
    harness: DeployHarness, evidence_url: str
) -> None:
    environment = await harness.add_environment("gcp-seoul")

    response = await _post(harness.client, harness, _body(environment, evidenceUrl=evidence_url))

    assert _error(response) == (422, "VALIDATION_ERROR")
    assert _details(response) == {("evidenceUrl", "EVIDENCE_URL_INVALID")}
    assert await _count_manual_findings(harness) == 0
    assert await _count_manual_review_audits(harness) == 0


async def test_record_manual_review_accepts_well_formed_evidence_url_with_port(
    harness: DeployHarness,
) -> None:
    environment = await harness.add_environment("gcp-seoul")
    evidence_url = "https://wiki.example.com:8443/reviews/iam-1?env=gcp-seoul#summary"

    response = await _post(harness.client, harness, _body(environment, evidenceUrl=evidence_url))

    assert response.status_code == 201, response.text
    assert response.json()["data"]["basisUrl"] == evidence_url
    assert await _count_manual_findings(harness) == 1
    assert await _count_manual_review_audits(harness) == 1


@pytest.mark.parametrize("result", [SecurityResult.FAIL, SecurityResult.PASS])
async def test_record_manual_review_rejects_mandatory_control_sec_1(
    harness: DeployHarness, result: SecurityResult
) -> None:
    """C-SEC-1 은 사람 점검 출처가 있어도 필수 통제라 받지 않는다(FAIL 이면 게이트가 막힌다)."""
    environment = await harness.add_environment("aws-seoul")
    service = await harness.create_service()

    response = await _post(
        harness.client,
        harness,
        _body(environment, _C.SEC_1, serviceId=service["id"], result=result.value),
    )

    assert _error(response) == (422, "VALIDATION_ERROR")
    assert _details(response) == {("controlId", "MANUAL_REVIEW_NOT_ALLOWED")}
    assert await _count_manual_findings(harness) == 0
    assert await _count_manual_review_audits(harness) == 0
