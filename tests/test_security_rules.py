"""공통 보안 규칙표·판정(순수 계산). SPEC sec-3 AC-1~AC-12."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.enums import (
    ActorType,
    AiFindingConfirmation,
    MigrationPlanMode,
    SecurityCheckPhase,
    SecurityControlId,
    SecurityFindingMode,
    SecurityFindingSource,
    SecurityMatrixCell,
    SecurityReasonCode,
    SecurityResult,
)
from app.services.security_rules import (
    EXCEPTION_MAX_DURATION,
    EXECUTION_TIME_BUDGET,
    POLICY_VERSION,
    RULES,
    SecurityControlDecision,
    SecurityException,
    SecurityFinding,
    SecurityScope,
    SecurityTarget,
    evaluate_security,
    get_rule,
    is_evaluated_in,
    summarize_cell,
)

NOW = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)
ORG = UUID("00000000-0000-0000-0000-0000000000a1")
OTHER_ORG = UUID("00000000-0000-0000-0000-0000000000a2")
SERVICE = UUID("00000000-0000-0000-0000-0000000000b1")
ENV_GCP = UUID("00000000-0000-0000-0000-0000000000c1")
ENV_AWS = UUID("00000000-0000-0000-0000-0000000000c2")
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
CONFIG_COMMIT = "c" * 40
WAS_VERSION = "b7c108c"
REVIEWER = UUID("00000000-0000-0000-0000-0000000000d1")
REQUESTER = UUID("00000000-0000-0000-0000-0000000000d2")

C = SecurityControlId
R = SecurityResult
S = SecurityFindingSource
EXEC = SecurityCheckPhase.EXECUTION
PREP = SecurityCheckPhase.PREPARATION
POST = SecurityCheckPhase.POST_SWITCH

TARGET_SCOPE = SecurityScope(
    organization_id=ORG,
    service_id=SERVICE,
    environment_id=ENV_GCP,
    image_digest=DIGEST,
    config_commit=CONFIG_COMMIT,
    was_version=WAS_VERSION,
)
TARGET = SecurityTarget(scope=TARGET_SCOPE, environment_kind="gcp")

# 통제마다 받을 수 있는 출처 하나(사람 점검 기록이 있으면 그것).
SOURCE_FOR = {
    C.IMG_1: S.TRIVY_IMAGE,
    C.IMG_2: S.GITLEAKS,
    C.IMG_3: S.WAS_STATE,
    C.CFG_1: S.TRIVY_CONFIG,
    C.TLS_1: S.TLS_PROBE,
    C.TLS_2: S.MANUAL_REVIEW,
    C.NET_1: S.MANUAL_REVIEW,
    C.NET_2: S.MANUAL_REVIEW,
    C.IAM_1: S.MANUAL_REVIEW,
    C.IAM_2: S.WAS_TEST,
    C.SEC_1: S.MANUAL_REVIEW,
    C.AUD_1: S.WAS_TEST,
    C.AUD_2: S.CLOUD_API,
    C.CI_1: S.MANUAL_REVIEW,
    C.AI_1: S.PRECHECK,
    C.DB_1: S.DATA_ENGINE,
    C.DST_1: S.ENTRYPOINT,
    C.POST_1: S.ENTRYPOINT,
}


def make_finding(
    control_id: SecurityControlId,
    result: SecurityResult = R.PASS,
    *,
    source: SecurityFindingSource | None = None,
    scope: SecurityScope = TARGET_SCOPE,
    age: timedelta = timedelta(seconds=30),
    lifetime: timedelta = timedelta(days=30),
    mode: SecurityFindingMode = SecurityFindingMode.REAL,
    policy_version: str = POLICY_VERSION,
    confirmation: AiFindingConfirmation | None = None,
) -> SecurityFinding:
    chosen = source or SOURCE_FOR[control_id]
    is_manual = chosen == S.MANUAL_REVIEW
    return SecurityFinding(
        finding_id=uuid4(),
        control_id=control_id,
        result=result,
        mode=mode,
        source=chosen,
        scope=scope,
        collected_at=NOW - age,
        expires_at=NOW - age + lifetime,
        policy_version=policy_version,
        reviewer_user_id=REVIEWER if is_manual else None,
        basis_url="https://example.com/review/1" if is_manual else None,
        confirmation=confirmation,
    )


def passing_findings(phase: SecurityCheckPhase = EXEC) -> list[SecurityFinding]:
    return [make_finding(rule.control_id) for rule in RULES if is_evaluated_in(rule, phase)]


def without(
    findings: list[SecurityFinding], control_id: SecurityControlId
) -> list[SecurityFinding]:
    return [e for e in findings if e.control_id != control_id]


def make_exception(
    control_id: SecurityControlId,
    *,
    approved_by: UUID | None = REVIEWER,
    approver_type: ActorType | None = ActorType.USER,
    requester_type: ActorType = ActorType.USER,
    created_ago: timedelta = timedelta(hours=1),
    duration: timedelta = timedelta(days=2),
    reason: str = "D04~D07 결정 전, 쓰기 정지 구간에서 수동 확인",
    scope: SecurityScope = TARGET_SCOPE,
) -> SecurityException:
    return SecurityException(
        exception_id=uuid4(),
        control_id=control_id,
        scope=scope,
        reason=reason,
        requested_by_user_id=REQUESTER,
        requested_by_actor_type=requester_type,
        approved_by_user_id=approved_by,
        approved_by_actor_type=approver_type,
        created_at=NOW - created_ago,
        expires_at=NOW - created_ago + duration,
    )


def decide(
    findings: list[SecurityFinding],
    exceptions: list[SecurityException] | None = None,
    *,
    phase: SecurityCheckPhase = EXEC,
    target: SecurityTarget = TARGET,
    plan_mode: MigrationPlanMode | None = MigrationPlanMode.PLANNED,
):
    return evaluate_security(target, phase, findings, exceptions or [], NOW, plan_mode)


# --- 규칙표 모양 (AC-1) ---


def test_rules_cover_every_control_exactly_once() -> None:
    assert sorted(rule.control_id for rule in RULES) == sorted(SecurityControlId)


def test_rules_match_matrix_cells_of_spec() -> None:
    cells = {cell: {r.control_id for r in RULES if r.cell == cell} for cell in SecurityMatrixCell}
    assert cells == {
        SecurityMatrixCell.IAM: {C.IAM_1, C.IAM_2},
        SecurityMatrixCell.ENCRYPTION: {C.TLS_1, C.TLS_2, C.SEC_1},
        SecurityMatrixCell.AUDIT_LOG: {C.AUD_1, C.AUD_2},
        SecurityMatrixCell.POLICY: {
            C.IMG_1,
            C.IMG_2,
            C.CFG_1,
            C.NET_1,
            C.NET_2,
            C.CI_1,
            C.AI_1,
        },
    }
    assert {r.control_id for r in RULES if r.cell is None} == {C.IMG_3, C.DB_1, C.DST_1, C.POST_1}


def test_rules_mandatory_set_matches_spec() -> None:
    assert {r.control_id for r in RULES if r.is_mandatory} == {
        C.IMG_1,
        C.IMG_2,
        C.IMG_3,
        C.TLS_1,
        C.IAM_2,
        C.SEC_1,
        C.AUD_1,
        C.AI_1,
        C.DB_1,
        C.DST_1,
    }


def test_execution_time_budgets_fit_in_total_budget() -> None:
    budgets = [r.time_budget for r in RULES if is_evaluated_in(r, EXEC) and r.time_budget]
    assert sum(budgets, timedelta()) <= EXECUTION_TIME_BUDGET


def test_policy_version_has_dated_format() -> None:
    assert POLICY_VERSION.startswith("sec-rules-")
    datetime.strptime(POLICY_VERSION.removeprefix("sec-rules-"), "%Y.%m.%d")


# --- 통과·차단 (AC-2·3) ---


def test_evaluate_all_passing_finding_is_not_blocked() -> None:
    decision = decide(passing_findings())
    assert not decision.is_blocked
    assert decision.policy_version == POLICY_VERSION
    assert all(c.result == R.PASS for c in decision.controls)


def test_evaluate_mandatory_fail_blocks_with_control_failed() -> None:
    findings = [*without(passing_findings(), C.IMG_1), make_finding(C.IMG_1, R.FAIL)]
    decision = decide(findings)
    assert decision.is_blocked
    assert [(b.control_id, b.reasons) for b in decision.blocking_reasons] == [
        (C.IMG_1, (SecurityReasonCode.CONTROL_FAILED,))
    ]


def test_evaluate_missing_mandatory_finding_is_unknown_and_blocks() -> None:
    decision = decide(without(passing_findings(), C.TLS_1))
    control = decision.get_control(C.TLS_1)
    assert control.result == R.UNKNOWN
    assert control.is_blocking
    assert control.reasons == (SecurityReasonCode.EVIDENCE_MISSING,)


def test_evaluate_collector_unknown_blocks_mandatory() -> None:
    findings = [*without(passing_findings(), C.TLS_1), make_finding(C.TLS_1, R.UNKNOWN)]
    control = decide(findings).get_control(C.TLS_1)
    assert control.is_blocking
    assert control.reasons == (SecurityReasonCode.EVIDENCE_UNKNOWN,)


def test_evaluate_optional_fail_is_shown_but_does_not_block() -> None:
    findings = [*without(passing_findings(), C.CFG_1), make_finding(C.CFG_1, R.FAIL)]
    decision = decide(findings)
    assert decision.get_control(C.CFG_1).result == R.FAIL
    assert not decision.is_blocked


def test_evaluate_uses_worst_of_several_findings() -> None:
    findings = [
        *without(passing_findings(), C.IMG_2),
        make_finding(C.IMG_2, R.PASS, source=S.GITLEAKS),
        make_finding(C.IMG_2, R.FAIL, source=S.TRIVY_SECRET),
    ]
    control = decide(findings).get_control(C.IMG_2)
    assert control.result == R.FAIL
    assert len(control.finding_ids) == 2


# --- 쓸 수 없는 증거 (AC-4) ---


@pytest.mark.parametrize(
    ("finding", "reason"),
    [
        (make_finding(C.IMG_1, age=timedelta(days=8)), SecurityReasonCode.EVIDENCE_EXPIRED),
        (
            make_finding(C.IMG_1, age=timedelta(hours=2), lifetime=timedelta(hours=1)),
            SecurityReasonCode.EVIDENCE_EXPIRED,
        ),
        (
            make_finding(C.IMG_1, scope=replace(TARGET_SCOPE, image_digest=OTHER_DIGEST)),
            SecurityReasonCode.SCOPE_MISMATCH,
        ),
        (
            make_finding(C.IMG_1, scope=replace(TARGET_SCOPE, organization_id=OTHER_ORG)),
            SecurityReasonCode.SCOPE_MISMATCH,
        ),
        (
            make_finding(C.IMG_1, mode=SecurityFindingMode.SIMULATED),
            SecurityReasonCode.EVIDENCE_SIMULATED,
        ),
        (
            make_finding(C.IMG_1, policy_version="sec-rules-2026.10.01"),
            SecurityReasonCode.POLICY_VERSION_MISMATCH,
        ),
        (
            make_finding(C.IMG_1, source=S.MANUAL_REVIEW),
            SecurityReasonCode.SOURCE_NOT_ALLOWED,
        ),
    ],
)
def test_evaluate_unusable_finding_never_passes(
    finding: SecurityFinding, reason: SecurityReasonCode
) -> None:
    decision = decide([*without(passing_findings(), C.IMG_1), finding])
    control = decision.get_control(C.IMG_1)
    assert control.result == R.UNKNOWN
    assert control.is_blocking
    assert control.reasons == (SecurityReasonCode.EVIDENCE_MISSING, reason)
    assert control.finding_ids == ()


def test_evaluate_manual_review_without_basis_is_incomplete() -> None:
    incomplete = replace(make_finding(C.SEC_1), basis_url=None)
    control = decide([*without(passing_findings(), C.SEC_1), incomplete]).get_control(C.SEC_1)
    assert control.is_blocking
    assert SecurityReasonCode.EVIDENCE_INCOMPLETE in control.reasons


def test_evaluate_manual_review_warn_on_optional_control_is_warn() -> None:
    findings = [*without(passing_findings(), C.TLS_2), make_finding(C.TLS_2, R.WARN)]
    decision = decide(findings)
    assert decision.get_control(C.TLS_2).result == R.WARN
    assert not decision.is_blocked


def test_evaluate_cache_finding_inside_ttl_is_used_and_marked() -> None:
    cached = make_finding(C.IMG_1, mode=SecurityFindingMode.CACHE, age=timedelta(days=6))
    control = decide([*without(passing_findings(), C.IMG_1), cached]).get_control(C.IMG_1)
    assert control.result == R.PASS
    assert control.mode == SecurityFindingMode.CACHE
    assert control.checked_at == cached.collected_at
    assert control.expires_at == cached.collected_at + timedelta(days=7)


def test_evaluate_tls_needs_fresh_probe_at_execution_but_not_preparation() -> None:
    probe = make_finding(C.TLS_1, age=timedelta(minutes=30))
    prep = decide([*without(passing_findings(PREP), C.TLS_1), probe], phase=PREP)
    execution = decide([*without(passing_findings(), C.TLS_1), probe])
    assert prep.get_control(C.TLS_1).result == R.PASS
    assert execution.get_control(C.TLS_1).reasons == (
        SecurityReasonCode.EVIDENCE_MISSING,
        SecurityReasonCode.EVIDENCE_EXPIRED,
    )
    assert execution.is_blocked


# --- AI 결과 (AC-5) ---


def test_evaluate_ai_pass_cannot_make_control_pass() -> None:
    ai_pass = make_finding(C.IMG_1, R.PASS, source=S.AI_REVIEW)
    control = decide([*without(passing_findings(), C.IMG_1), ai_pass]).get_control(C.IMG_1)
    assert control.result == R.UNKNOWN
    assert control.is_blocking


def test_evaluate_unconfirmed_ai_finding_is_warn_only() -> None:
    ai_warn = make_finding(C.CFG_1, R.FAIL, source=S.AI_REVIEW)
    decision = decide([*passing_findings(), ai_warn])
    control = decision.get_control(C.CFG_1)
    assert control.result == R.WARN
    assert SecurityReasonCode.AI_UNCONFIRMED in control.reasons
    assert not decision.is_blocked


def test_evaluate_unconfirmed_ai_finding_on_mandatory_control_does_not_block() -> None:
    ai_fail = make_finding(C.IMG_1, R.FAIL, source=S.AI_REVIEW)
    decision = decide([*passing_findings(), ai_fail])
    assert decision.get_control(C.IMG_1).result == R.WARN
    assert not decision.is_blocked


def test_evaluate_escalated_ai_finding_on_mandatory_control_blocks() -> None:
    escalated = make_finding(
        C.IMG_1, R.WARN, source=S.AI_REVIEW, confirmation=AiFindingConfirmation.ESCALATED
    )
    decision = decide([*passing_findings(), escalated])
    control = decision.get_control(C.IMG_1)
    assert control.result == R.FAIL
    assert control.is_blocking
    assert SecurityReasonCode.AI_ESCALATED in control.reasons


def test_evaluate_dismissed_ai_finding_is_ignored() -> None:
    dismissed = make_finding(
        C.IMG_1, R.FAIL, source=S.AI_REVIEW, confirmation=AiFindingConfirmation.DISMISSED
    )
    control = decide([*passing_findings(), dismissed]).get_control(C.IMG_1)
    assert control.result == R.PASS
    assert dismissed.finding_id not in control.finding_ids


def test_evaluate_ai_cannot_improve_rule_fail() -> None:
    findings = [
        *without(passing_findings(), C.IMG_1),
        make_finding(C.IMG_1, R.FAIL),
        make_finding(C.IMG_1, R.WARN, source=S.AI_REVIEW),
    ]
    control = decide(findings).get_control(C.IMG_1)
    assert control.result == R.FAIL
    assert control.is_blocking


# --- 기한부 예외 (AC-6) ---


def test_evaluate_valid_exception_unblocks_unknown_and_shows_warn() -> None:
    exception = make_exception(C.DB_1)
    decision = decide(without(passing_findings(), C.DB_1), [exception])
    control = decision.get_control(C.DB_1)
    assert control.result == R.UNKNOWN
    assert control.effective_result == R.WARN
    assert not control.is_blocking
    assert control.exception_id == exception.exception_id
    assert control.expires_at == exception.expires_at
    assert SecurityReasonCode.EXCEPTION_APPLIED in control.reasons
    assert decision.exception_ids == (exception.exception_id,)
    assert not decision.is_blocked


def test_evaluate_exception_also_covers_rule_fail() -> None:
    findings = [*without(passing_findings(), C.IMG_1), make_finding(C.IMG_1, R.FAIL)]
    control = decide(findings, [make_exception(C.IMG_1)]).get_control(C.IMG_1)
    assert control.result == R.FAIL
    assert control.effective_result == R.WARN
    assert not control.is_blocking


@pytest.mark.parametrize(
    "exception",
    [
        make_exception(C.DB_1, created_ago=timedelta(days=3), duration=timedelta(days=2)),
        make_exception(C.DB_1, duration=EXCEPTION_MAX_DURATION + timedelta(seconds=1)),
        make_exception(C.DB_1, approved_by=None, approver_type=None),
        make_exception(C.DB_1, approver_type=ActorType.AGENT),
        make_exception(C.DB_1, requester_type=ActorType.AGENT),
        make_exception(C.DB_1, approved_by=REQUESTER),
        make_exception(C.DB_1, reason="  "),
        make_exception(C.DB_1, scope=replace(TARGET_SCOPE, environment_id=ENV_AWS)),
        make_exception(C.DST_1),
    ],
    ids=[
        "expired",
        "longer-than-7-days",
        "not-approved",
        "agent-approver",
        "agent-requester",
        "self-approved",
        "blank-reason",
        "other-environment",
        "other-control",
    ],
)
def test_evaluate_invalid_exception_does_not_unblock(exception: SecurityException) -> None:
    decision = decide(without(passing_findings(), C.DB_1), [exception])
    control = decision.get_control(C.DB_1)
    assert control.is_blocking
    assert control.exception_id is None


def test_evaluate_expired_exception_is_reported() -> None:
    expired = make_exception(C.DB_1, created_ago=timedelta(days=3), duration=timedelta(days=2))
    control = decide(without(passing_findings(), C.DB_1), [expired]).get_control(C.DB_1)
    assert SecurityReasonCode.EXCEPTION_EXPIRED in control.reasons


def test_evaluate_self_approved_exception_needs_organization_switch() -> None:
    exception = make_exception(C.DB_1, approved_by=REQUESTER)
    target = replace(TARGET, allow_self_approval=True)
    decision = decide(without(passing_findings(), C.DB_1), [exception], target=target)
    assert not decision.get_control(C.DB_1).is_blocking


# --- 사전 점검 (AC-7) ---


def test_evaluate_missing_precheck_is_fail() -> None:
    control = decide(without(passing_findings(), C.AI_1)).get_control(C.AI_1)
    assert control.result == R.FAIL
    assert control.is_blocking
    assert control.reasons == (SecurityReasonCode.PRECHECK_MISSING,)


def test_evaluate_precheck_for_other_environment_does_not_count() -> None:
    other = make_finding(C.AI_1, scope=replace(TARGET_SCOPE, environment_id=ENV_AWS))
    control = decide([*without(passing_findings(), C.AI_1), other]).get_control(C.AI_1)
    assert control.reasons == (
        SecurityReasonCode.PRECHECK_MISSING,
        SecurityReasonCode.SCOPE_MISMATCH,
    )


# --- 준비 완료와 지금 트래픽 수신 자격 (AC-8) ---


def test_preparation_does_not_judge_traffic_qualification() -> None:
    prep_ids = {c.control_id for c in decide(passing_findings(PREP), phase=PREP).controls}
    assert {C.DST_1, C.DB_1, C.IMG_3, C.AI_1, C.POST_1}.isdisjoint(prep_ids)


def test_ready_environment_still_blocks_without_destination_qualification() -> None:
    prep = passing_findings(PREP)
    assert not decide(prep, phase=PREP).is_blocked
    execution = decide(without(passing_findings(), C.DST_1))
    assert execution.is_blocked
    assert [b.control_id for b in execution.blocking_reasons] == [C.DST_1]


def test_source_unreachable_never_becomes_pass() -> None:
    unreachable = make_finding(C.DB_1, R.UNKNOWN)
    decision = decide([*without(passing_findings(), C.DB_1), unreachable])
    assert decision.get_control(C.DB_1).result == R.UNKNOWN
    assert decision.is_blocked


# --- 사람 승인·사전 승인 자동 DR 같은 기준 (AC-9) ---


@pytest.mark.parametrize(
    "findings",
    [
        passing_findings(),
        without(passing_findings(), C.DB_1),
        without(passing_findings(), C.AI_1),
        [*without(passing_findings(), C.IMG_1), make_finding(C.IMG_1, R.FAIL)],
    ],
)
def test_planned_and_emergency_modes_get_identical_judgement(
    findings: list[SecurityFinding],
) -> None:
    planned = decide(findings, plan_mode=MigrationPlanMode.PLANNED)
    emergency = decide(findings, plan_mode=MigrationPlanMode.EMERGENCY)
    assert planned.controls == emergency.controls
    assert planned.is_blocked == emergency.is_blocked
    assert emergency.plan_mode == MigrationPlanMode.EMERGENCY


# --- 환경 종류 (AC-10) ---


def test_aws_and_gcp_with_same_normalized_finding_get_same_results() -> None:
    aws_scope = replace(TARGET_SCOPE, environment_id=ENV_AWS)
    aws_target = SecurityTarget(scope=aws_scope, environment_kind="aws")
    aws_findings = [replace(e, scope=aws_scope) for e in passing_findings()]
    gcp = decide(passing_findings())
    aws = decide(aws_findings, target=aws_target)
    assert [(c.control_id, c.result) for c in gcp.controls] == [
        (c.control_id, c.result) for c in aws.controls
    ]


def test_onprem_cloud_audit_log_is_not_applicable() -> None:
    target = replace(TARGET, environment_kind="onprem")
    decision = decide(without(passing_findings(PREP), C.AUD_2), phase=PREP, target=target)
    control = decision.get_control(C.AUD_2)
    assert control.result == R.NA
    assert control.reasons == (SecurityReasonCode.NOT_APPLICABLE,)
    assert not decision.is_blocked


# --- 전환 직후 (AC-11) ---


def test_post_switch_judges_only_post_check_and_never_blocks() -> None:
    decision = decide([make_finding(C.POST_1, R.FAIL)], phase=POST)
    assert [c.control_id for c in decision.controls] == [C.POST_1]
    assert decision.get_control(C.POST_1).result == R.FAIL
    assert not decision.is_blocked


# --- 매트릭스 칸 (AC-12) ---


def cell_decision(
    control_id: SecurityControlId,
    result: SecurityResult,
    *,
    exception: bool = False,
    mode: SecurityFindingMode | None = SecurityFindingMode.REAL,
    checked_at: datetime | None = NOW,
    expires_at: datetime | None = NOW + timedelta(days=1),
) -> SecurityControlDecision:
    rule = get_rule(control_id)
    return SecurityControlDecision(
        control_id=control_id,
        cell=rule.cell,
        is_mandatory=rule.is_mandatory,
        result=result,
        effective_result=R.WARN if exception else result,
        is_blocking=False,
        reasons=(),
        finding_ids=(),
        exception_id=uuid4() if exception else None,
        mode=mode,
        checked_at=checked_at,
        expires_at=expires_at,
    )


@pytest.mark.parametrize(
    ("decisions", "expected"),
    [
        ([cell_decision(C.TLS_1, R.PASS), cell_decision(C.TLS_2, R.FAIL)], R.FAIL),
        ([cell_decision(C.TLS_1, R.UNKNOWN), cell_decision(C.TLS_2, R.WARN)], R.UNKNOWN),
        ([cell_decision(C.TLS_2, R.UNKNOWN), cell_decision(C.SEC_1, R.WARN)], R.WARN),
        ([cell_decision(C.TLS_2, R.UNKNOWN), cell_decision(C.TLS_1, R.PASS)], R.UNKNOWN),
        ([cell_decision(C.TLS_1, R.PASS), cell_decision(C.SEC_1, R.PASS)], R.PASS),
        ([cell_decision(C.TLS_1, R.FAIL, exception=True)], R.WARN),
        ([], R.NA),
    ],
    ids=[
        "fail-first",
        "mandatory-unknown-over-warn",
        "warn-over-optional-unknown",
        "optional-unknown-over-pass",
        "all-pass",
        "fail-with-exception-is-warn",
        "empty-is-na",
    ],
)
def test_summarize_cell_uses_worst_value_order(
    decisions: list[SecurityControlDecision], expected: SecurityResult
) -> None:
    assert summarize_cell(SecurityMatrixCell.ENCRYPTION, decisions).result == expected


def test_summarize_cell_reports_exception_mode_and_earliest_expiry() -> None:
    earliest = NOW + timedelta(hours=1)
    summary = summarize_cell(
        SecurityMatrixCell.ENCRYPTION,
        [
            cell_decision(C.TLS_1, R.PASS, mode=SecurityFindingMode.CACHE, expires_at=earliest),
            cell_decision(C.SEC_1, R.FAIL, exception=True, checked_at=NOW - timedelta(days=1)),
            cell_decision(C.IAM_1, R.FAIL),
        ],
    )
    assert summary.result == R.WARN
    assert summary.has_exception
    assert summary.mode == SecurityFindingMode.CACHE
    assert summary.expires_at == earliest
    assert summary.checked_at == NOW - timedelta(days=1)
    assert summary.control_ids == (C.TLS_1, C.SEC_1)


def test_summarize_cell_ignores_not_applicable() -> None:
    summary = summarize_cell(
        SecurityMatrixCell.AUDIT_LOG,
        [cell_decision(C.AUD_2, R.NA, mode=None), cell_decision(C.AUD_1, R.PASS)],
    )
    assert summary.result == R.PASS


@pytest.mark.parametrize("control", [C.IMG_1, C.AI_1, C.CFG_1])
def test_timed_out_control_ignores_pass_evidence_and_keeps_mandatory_policy(
    control: SecurityControlId,
) -> None:
    decision = evaluate_security(
        TARGET,
        EXEC,
        passing_findings(),
        [],
        NOW,
        timed_out_controls=frozenset({control}),
    )
    result = decision.get_control(control)
    assert result.result == R.UNKNOWN
    assert result.reasons == (SecurityReasonCode.CONTROL_TIMEOUT,)
    assert result.finding_ids == ()
    assert result.is_blocking == get_rule(control).is_mandatory
    assert all(item.result == R.PASS for item in decision.controls if item.control_id != control)
