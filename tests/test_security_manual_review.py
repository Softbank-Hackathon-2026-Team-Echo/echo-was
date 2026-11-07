"""사람 점검 기록: 받을 수 있는 통제 목록과 칸 계산(_cell_rank) 순수 시험."""

import uuid
from datetime import UTC, datetime

from app.enums import (
    SecurityCheckPhase,
    SecurityControlId,
    SecurityFindingMode,
    SecurityFindingSource,
    SecurityMatrixCell,
    SecurityResult,
)
from app.services.security_finding_service import build_manual_review
from app.services.security_manual_review_service import manual_review_controls
from app.services.security_rules import (
    POLICY_VERSION,
    SecurityFinding,
    SecurityScope,
    SecurityTarget,
    evaluate_security,
    get_rule,
    summarize_cell,
)

_C = SecurityControlId
NOW = datetime(2026, 11, 8, 1, 0, tzinfo=UTC)
ORG = uuid.uuid4()
ENVIRONMENT = uuid.uuid4()
WAS_VERSION = "2026.11.08"


def test_manual_review_controls_are_rule_controls_with_manual_source() -> None:
    assert set(manual_review_controls()) == {
        _C.TLS_2,
        _C.NET_1,
        _C.NET_2,
        _C.IAM_1,
        _C.SEC_1,
        _C.CI_1,
    }
    for control_id in manual_review_controls():
        assert SecurityFindingSource.MANUAL_REVIEW in get_rule(control_id).sources


def _iam_cell(findings: list[SecurityFinding]) -> SecurityResult:
    target = SecurityTarget(
        scope=SecurityScope(ORG, environment_id=ENVIRONMENT, was_version=WAS_VERSION),
        environment_kind="aws",
    )
    decision = evaluate_security(target, SecurityCheckPhase.PREPARATION, findings, (), NOW)
    return summarize_cell(SecurityMatrixCell.IAM, decision.controls).result


def _iam_2_pass() -> SecurityFinding:
    return SecurityFinding(
        finding_id=uuid.uuid4(),
        control_id=_C.IAM_2,
        result=SecurityResult.PASS,
        mode=SecurityFindingMode.REAL,
        source=SecurityFindingSource.WAS_TEST,
        scope=SecurityScope(ORG, was_version=WAS_VERSION),
        collected_at=NOW,
        expires_at=NOW + get_rule(_C.IAM_2).max_age,
        policy_version=POLICY_VERSION,
    )


def _manual(result: SecurityResult) -> SecurityFinding:
    return build_manual_review(
        control_id=_C.IAM_1,
        result=result,
        scope=SecurityScope(ORG, environment_id=ENVIRONMENT),
        reviewer_user_id=uuid.uuid4(),
        basis_url="https://example.com/review",
        collected_at=NOW,
        summary="확인함",
    ).finding


def test_optional_unknown_keeps_cell_unknown_until_manual_review() -> None:
    assert _iam_cell([_iam_2_pass()]) == SecurityResult.UNKNOWN
    assert _iam_cell([_iam_2_pass(), _manual(SecurityResult.PASS)]) == SecurityResult.PASS
    assert _iam_cell([_iam_2_pass(), _manual(SecurityResult.WARN)]) == SecurityResult.WARN
    assert _iam_cell([_iam_2_pass(), _manual(SecurityResult.FAIL)]) == SecurityResult.FAIL


def test_manual_review_expires_after_rule_max_age() -> None:
    finding = _manual(SecurityResult.PASS)

    assert finding.expires_at == NOW + get_rule(_C.IAM_1).max_age
    assert get_rule(_C.IAM_1).max_age.days == 7
