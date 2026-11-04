"""공통 보안 규칙표와 판정(순수 계산, DB·외부 호출 없음).

증거(검사 결과를 공통 형태로 바꾼 것)와 기한부 예외를 받아 통제마다 결과를 정하고, 이전 실행을
막을지 판정한다. 사람 승인 이전과 사전 승인 자동 DR(재난 이전)은 같은 함수·같은 표를 쓴다.
계획 종류(`MigrationPlanMode`)는 판정 기록에만 남고 규칙을 바꾸지 않는다.

- 통제 결과: 범위(조직 + 통제가 정한 서비스·환경·digest·설정 커밋·WAS 버전)가 맞고,
  유효기간 안이고, 그 통제가 받는 출처이고, 지금 규칙표 버전으로 만든 증거만 쓴다.
  가짜(SIMULATED) 증거는 쓰지 않는다. 쓸 증거가 없으면 그 통제의 "없음" 결과
  (보통 UNKNOWN, 사전 점검은 FAIL)다.
- 여러 증거는 가장 나쁜 값을 쓴다: FAIL > UNKNOWN > WARN > PASS > NA.
- AI 결과(`AI_REVIEW`)는 PASS 를 만들 수 없다. 사람이 확인하기 전에는 WARN, ESCALATED 면 FAIL,
  DISMISSED 는 쓰지 않는다. 규칙 결과보다 좋게 바꾸지 못한다.
- 필수 통제가 FAIL·UNKNOWN 이고 유효한 예외가 없으면 막는다. 선택 통제는 막지 않는다.
- 예외는 사유·사람 승인자·만료(최대 7일)가 있고 범위가 맞을 때만 쓴다. 만든 사람과 승인한 사람이
  같으면 조직이 자기 승인을 허용할 때만 유효하다. 예외가 걸린 FAIL·UNKNOWN 은 WARN 으로 보인다.
- 통제를 끄는 설정은 없다. 규칙을 바꾸면 `POLICY_VERSION` 을 올리고, 이전 버전으로 만든 증거는
  다시 정규화해야 쓸 수 있다.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

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

# 규칙표 버전. 판정·사전 승인·이전 계획 지문에 넣는다. 규칙을 바꾸면 날짜를 올린다.
POLICY_VERSION = "sec-rules-2026.11.04"

# 기한부 예외의 최대 길이(만든 시각 → 만료).
EXCEPTION_MAX_DURATION = timedelta(days=7)

# 실행 시점 확인(지금 상태를 다시 읽은 것)으로 인정하는 증거 나이.
EXECUTION_EVIDENCE_MAX_AGE = timedelta(minutes=2)

# 실행 직전 검사 시간 예산 합계. 통제별 예산의 합이 이 값을 넘지 않는다(시험이 확인).
EXECUTION_TIME_BUDGET = timedelta(seconds=30)

_PREPARATION_MAX_AGE = timedelta(days=7)

_SEVERITY = {
    SecurityResult.NA: 0,
    SecurityResult.PASS: 1,
    SecurityResult.WARN: 2,
    SecurityResult.UNKNOWN: 3,
    SecurityResult.FAIL: 4,
}

_MODE_SEVERITY = {
    SecurityFindingMode.REAL: 0,
    SecurityFindingMode.CACHE: 1,
    SecurityFindingMode.SIMULATED: 2,
}

_RESULT_REASON = {
    SecurityResult.FAIL: SecurityReasonCode.CONTROL_FAILED,
    SecurityResult.UNKNOWN: SecurityReasonCode.EVIDENCE_UNKNOWN,
    SecurityResult.WARN: SecurityReasonCode.CONTROL_WARNING,
}

_BLOCKABLE = frozenset({SecurityResult.FAIL, SecurityResult.UNKNOWN})


class ScopeKey(StrEnum):
    """통제가 증거·예외와 판정 대상 사이에서 같아야 한다고 정한 범위 항목(조직은 늘 비교한다)."""

    SERVICE = "SERVICE"
    ENVIRONMENT = "ENVIRONMENT"
    IMAGE_DIGEST = "IMAGE_DIGEST"
    CONFIG_COMMIT = "CONFIG_COMMIT"
    WAS_VERSION = "WAS_VERSION"


@dataclass(frozen=True)
class SecurityScope:
    """증거·예외·판정 대상이 가리키는 범위."""

    organization_id: UUID
    service_id: UUID | None = None
    environment_id: UUID | None = None
    image_digest: str | None = None
    config_commit: str | None = None
    was_version: str | None = None

    def value_of(self, key: ScopeKey) -> UUID | str | None:
        match key:
            case ScopeKey.SERVICE:
                return self.service_id
            case ScopeKey.ENVIRONMENT:
                return self.environment_id
            case ScopeKey.IMAGE_DIGEST:
                return self.image_digest
            case ScopeKey.CONFIG_COMMIT:
                return self.config_commit
            case ScopeKey.WAS_VERSION:
                return self.was_version


@dataclass(frozen=True)
class SecurityTarget:
    """판정 대상: 이전의 도착 환경(또는 매트릭스의 환경 한 칸)과 그때의 서비스·이미지·설정."""

    scope: SecurityScope
    environment_kind: str
    # 조직 스위치. 켜져 있을 때만 예외를 만든 사람이 스스로 승인한 예외가 유효하다.
    allow_self_approval: bool = False


@dataclass(frozen=True)
class SecurityFinding:
    """검사 결과 한 건을 공통 형태로 바꾼 것(보안·거버넌스 2 수집기가 만든다)."""

    finding_id: UUID
    control_id: SecurityControlId
    result: SecurityResult
    mode: SecurityFindingMode
    source: SecurityFindingSource
    scope: SecurityScope
    collected_at: datetime
    expires_at: datetime
    # 검사 원문을 결과로 바꿀 때 쓴 규칙표 버전.
    policy_version: str
    # 사람 점검 기록(MANUAL_REVIEW)은 점검자와 근거 링크가 있어야 쓴다.
    reviewer_user_id: UUID | None = None
    basis_url: str | None = None
    # AI 결과(AI_REVIEW)만 쓴다. None 은 UNCONFIRMED 와 같다.
    confirmation: AiFindingConfirmation | None = None

    @property
    def is_ai(self) -> bool:
        return self.source == SecurityFindingSource.AI_REVIEW


@dataclass(frozen=True)
class SecurityException:
    """통제 하나를 범위 안에서 기한부로 막지 않게 하는 기록. 결과는 PASS 가 아니라 WARN 이다."""

    exception_id: UUID
    control_id: SecurityControlId
    scope: SecurityScope
    reason: str
    requested_by_user_id: UUID
    requested_by_actor_type: ActorType
    approved_by_user_id: UUID | None
    approved_by_actor_type: ActorType | None
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class SecurityControlRule:
    """규칙표 한 줄(보안·거버넌스 1 SPEC §5.1)."""

    control_id: SecurityControlId
    title: str
    # 매트릭스 칸. 이전 한 건의 조건(이미지 일치·DB 쓰기·목적지·전환 후)은 None.
    cell: SecurityMatrixCell | None
    is_mandatory: bool
    phases: frozenset[SecurityCheckPhase]
    scope_keys: frozenset[ScopeKey]
    sources: frozenset[SecurityFindingSource]
    # 수집 시각으로부터 증거를 쓸 수 있는 최대 나이.
    max_age: timedelta
    # 실행 판정 때 더 짧게 보는 나이(실행 직전에 다시 확인하는 통제).
    execution_max_age: timedelta | None = None
    missing_result: SecurityResult = SecurityResult.UNKNOWN
    missing_reason: SecurityReasonCode = SecurityReasonCode.EVIDENCE_MISSING
    not_applicable_kinds: frozenset[str] = frozenset()
    # 실행 직전 검사의 통제별 시간 예산. 넘으면 그 통제는 UNKNOWN 이다(게이트가 지킨다).
    time_budget: timedelta | None = None


@dataclass(frozen=True)
class SecurityControlDecision:
    """통제 하나의 판정."""

    control_id: SecurityControlId
    cell: SecurityMatrixCell | None
    is_mandatory: bool
    # 증거·AI 결과로 정한 값(예외 반영 전).
    result: SecurityResult
    # 화면에 보일 값. 유효한 예외가 걸린 FAIL·UNKNOWN 은 WARN 이다.
    effective_result: SecurityResult
    is_blocking: bool
    reasons: tuple[SecurityReasonCode, ...]
    finding_ids: tuple[UUID, ...]
    exception_id: UUID | None
    mode: SecurityFindingMode | None
    checked_at: datetime | None
    expires_at: datetime | None


@dataclass(frozen=True)
class SecurityBlockingReason:
    control_id: SecurityControlId
    reasons: tuple[SecurityReasonCode, ...]


@dataclass(frozen=True)
class SecurityDecision:
    """판정 한 번의 결과. 정책 버전·증거·예외 ID 와 함께 저장한다."""

    policy_version: str
    phase: SecurityCheckPhase
    plan_mode: MigrationPlanMode | None
    decided_at: datetime
    controls: tuple[SecurityControlDecision, ...]

    @property
    def is_blocked(self) -> bool:
        return any(control.is_blocking for control in self.controls)

    @property
    def blocking_reasons(self) -> tuple[SecurityBlockingReason, ...]:
        return tuple(
            SecurityBlockingReason(control.control_id, control.reasons)
            for control in self.controls
            if control.is_blocking
        )

    @property
    def finding_ids(self) -> tuple[UUID, ...]:
        return tuple(_unique(eid for control in self.controls for eid in control.finding_ids))

    @property
    def exception_ids(self) -> tuple[UUID, ...]:
        return tuple(
            _unique(control.exception_id for control in self.controls if control.exception_id)
        )

    def get_control(self, control_id: SecurityControlId) -> SecurityControlDecision:
        for control in self.controls:
            if control.control_id == control_id:
                return control
        raise KeyError(control_id)


@dataclass(frozen=True)
class SecurityCellSummary:
    """매트릭스 칸 하나의 요약."""

    cell: SecurityMatrixCell
    result: SecurityResult
    has_exception: bool
    mode: SecurityFindingMode | None
    checked_at: datetime | None
    expires_at: datetime | None
    control_ids: tuple[SecurityControlId, ...]


_PREP = frozenset({SecurityCheckPhase.PREPARATION})
_PREP_AND_EXEC = frozenset({SecurityCheckPhase.PREPARATION, SecurityCheckPhase.EXECUTION})
_EXEC = frozenset({SecurityCheckPhase.EXECUTION})
_POST = frozenset({SecurityCheckPhase.POST_SWITCH})

_S = SecurityFindingSource
_K = ScopeKey

RULES: tuple[SecurityControlRule, ...] = (
    SecurityControlRule(
        SecurityControlId.IMG_1,
        "이미지 취약점",
        SecurityMatrixCell.POLICY,
        is_mandatory=True,
        phases=_PREP_AND_EXEC,
        scope_keys=frozenset({_K.IMAGE_DIGEST}),
        sources=frozenset({_S.TRIVY_IMAGE}),
        max_age=_PREPARATION_MAX_AGE,
        time_budget=timedelta(seconds=1),
    ),
    SecurityControlRule(
        SecurityControlId.IMG_2,
        "시크릿 노출",
        SecurityMatrixCell.POLICY,
        is_mandatory=True,
        phases=_PREP,
        scope_keys=frozenset({_K.IMAGE_DIGEST}),
        sources=frozenset({_S.GITLEAKS, _S.TRIVY_SECRET}),
        max_age=_PREPARATION_MAX_AGE,
    ),
    SecurityControlRule(
        SecurityControlId.IMG_3,
        "출발·도착 이미지 일치",
        None,
        is_mandatory=True,
        phases=_EXEC,
        scope_keys=frozenset({_K.SERVICE}),
        sources=frozenset({_S.WAS_STATE}),
        max_age=EXECUTION_EVIDENCE_MAX_AGE,
        time_budget=timedelta(seconds=1),
    ),
    SecurityControlRule(
        SecurityControlId.CFG_1,
        "설정 위반(IaC·매니페스트)",
        SecurityMatrixCell.POLICY,
        is_mandatory=False,
        phases=_PREP,
        scope_keys=frozenset({_K.CONFIG_COMMIT}),
        sources=frozenset({_S.TRIVY_CONFIG}),
        max_age=_PREPARATION_MAX_AGE,
    ),
    SecurityControlRule(
        SecurityControlId.TLS_1,
        "공개 경로 TLS",
        SecurityMatrixCell.ENCRYPTION,
        is_mandatory=True,
        phases=_PREP_AND_EXEC,
        scope_keys=frozenset({_K.SERVICE, _K.ENVIRONMENT}),
        sources=frozenset({_S.TLS_PROBE}),
        max_age=timedelta(hours=1),
        execution_max_age=EXECUTION_EVIDENCE_MAX_AGE,
        time_budget=timedelta(seconds=5),
    ),
    SecurityControlRule(
        SecurityControlId.TLS_2,
        "내부 구간 암호화",
        SecurityMatrixCell.ENCRYPTION,
        is_mandatory=False,
        phases=_PREP,
        scope_keys=frozenset({_K.ENVIRONMENT}),
        sources=frozenset({_S.MANUAL_REVIEW}),
        max_age=_PREPARATION_MAX_AGE,
    ),
    SecurityControlRule(
        SecurityControlId.NET_1,
        "공개 노출·방화벽",
        SecurityMatrixCell.POLICY,
        is_mandatory=False,
        phases=_PREP,
        scope_keys=frozenset({_K.ENVIRONMENT}),
        sources=frozenset({_S.MANUAL_REVIEW, _S.CLOUD_API}),
        max_age=_PREPARATION_MAX_AGE,
    ),
    SecurityControlRule(
        SecurityControlId.NET_2,
        "클러스터 내부 격리",
        SecurityMatrixCell.POLICY,
        is_mandatory=False,
        phases=_PREP,
        scope_keys=frozenset({_K.ENVIRONMENT}),
        sources=frozenset({_S.MANUAL_REVIEW, _S.KUBERNETES_API}),
        max_age=_PREPARATION_MAX_AGE,
    ),
    SecurityControlRule(
        SecurityControlId.IAM_1,
        "실행 신원 범위",
        SecurityMatrixCell.IAM,
        is_mandatory=False,
        phases=_PREP,
        scope_keys=frozenset({_K.ENVIRONMENT}),
        sources=frozenset({_S.MANUAL_REVIEW, _S.CLOUD_API}),
        max_age=_PREPARATION_MAX_AGE,
    ),
    SecurityControlRule(
        SecurityControlId.IAM_2,
        "에이전트 권한 경계",
        SecurityMatrixCell.IAM,
        is_mandatory=True,
        phases=_PREP,
        scope_keys=frozenset({_K.WAS_VERSION}),
        sources=frozenset({_S.WAS_TEST, _S.AUDIT_LOG}),
        max_age=_PREPARATION_MAX_AGE,
    ),
    SecurityControlRule(
        SecurityControlId.SEC_1,
        "비밀 전달 경로",
        SecurityMatrixCell.ENCRYPTION,
        is_mandatory=True,
        phases=_PREP,
        scope_keys=frozenset(),
        sources=frozenset({_S.GITLEAKS, _S.TRIVY_SECRET, _S.MANUAL_REVIEW}),
        max_age=_PREPARATION_MAX_AGE,
    ),
    SecurityControlRule(
        SecurityControlId.AUD_1,
        "플랫폼 감사 기록",
        SecurityMatrixCell.AUDIT_LOG,
        is_mandatory=True,
        phases=_PREP,
        scope_keys=frozenset({_K.WAS_VERSION}),
        sources=frozenset({_S.WAS_TEST, _S.AUDIT_LOG}),
        max_age=_PREPARATION_MAX_AGE,
    ),
    SecurityControlRule(
        SecurityControlId.AUD_2,
        "클라우드 감사 로그",
        SecurityMatrixCell.AUDIT_LOG,
        is_mandatory=False,
        phases=_PREP,
        scope_keys=frozenset({_K.ENVIRONMENT}),
        sources=frozenset({_S.CLOUD_API}),
        max_age=timedelta(hours=24),
        not_applicable_kinds=frozenset({"onprem"}),
    ),
    SecurityControlRule(
        SecurityControlId.CI_1,
        "검사 우회 방지",
        SecurityMatrixCell.POLICY,
        is_mandatory=False,
        phases=_PREP,
        scope_keys=frozenset(),
        sources=frozenset({_S.MANUAL_REVIEW, _S.GITHUB_API}),
        max_age=_PREPARATION_MAX_AGE,
    ),
    SecurityControlRule(
        SecurityControlId.AI_1,
        "사전 점검",
        SecurityMatrixCell.POLICY,
        is_mandatory=True,
        phases=_EXEC,
        scope_keys=frozenset({_K.SERVICE, _K.ENVIRONMENT}),
        sources=frozenset({_S.PRECHECK}),
        max_age=timedelta(hours=24),
        missing_result=SecurityResult.FAIL,
        missing_reason=SecurityReasonCode.PRECHECK_MISSING,
        time_budget=timedelta(seconds=1),
    ),
    SecurityControlRule(
        SecurityControlId.DB_1,
        "DB 쓰기 소유권",
        None,
        is_mandatory=True,
        phases=_EXEC,
        scope_keys=frozenset({_K.SERVICE, _K.ENVIRONMENT}),
        sources=frozenset({_S.DATA_ENGINE}),
        max_age=EXECUTION_EVIDENCE_MAX_AGE,
        time_budget=timedelta(seconds=5),
    ),
    SecurityControlRule(
        SecurityControlId.DST_1,
        "목적지 자격",
        None,
        is_mandatory=True,
        phases=_EXEC,
        scope_keys=frozenset({_K.SERVICE, _K.ENVIRONMENT}),
        sources=frozenset({_S.ENTRYPOINT}),
        max_age=EXECUTION_EVIDENCE_MAX_AGE,
        time_budget=timedelta(seconds=2),
    ),
    SecurityControlRule(
        SecurityControlId.POST_1,
        "전환 후 실제 상태",
        None,
        is_mandatory=False,
        phases=_POST,
        scope_keys=frozenset({_K.SERVICE, _K.ENVIRONMENT}),
        sources=frozenset({_S.ENTRYPOINT, _S.TLS_PROBE}),
        max_age=EXECUTION_EVIDENCE_MAX_AGE,
        time_budget=timedelta(seconds=10),
    ),
)

_RULE_BY_CONTROL = {rule.control_id: rule for rule in RULES}


def get_rule(control_id: SecurityControlId) -> SecurityControlRule:
    return _RULE_BY_CONTROL[control_id]


def is_evaluated_in(rule: SecurityControlRule, phase: SecurityCheckPhase) -> bool:
    """실행 판정은 준비 통제도 함께 본다(유효기간 안의 준비 증거 + 실행 시점 상태)."""
    if phase == SecurityCheckPhase.EXECUTION:
        return bool(rule.phases & _PREP_AND_EXEC)
    return phase in rule.phases


def evaluate_security(
    target: SecurityTarget,
    phase: SecurityCheckPhase,
    findings: Sequence[SecurityFinding],
    exceptions: Sequence[SecurityException],
    now: datetime,
    plan_mode: MigrationPlanMode | None = None,
) -> SecurityDecision:
    """그 시점에 보는 모든 통제를 판정한다. 필수 통제 하나라도 막히면 `is_blocked`."""
    controls = tuple(
        evaluate_control(rule, target, phase, findings, exceptions, now)
        for rule in RULES
        if is_evaluated_in(rule, phase)
    )
    return SecurityDecision(POLICY_VERSION, phase, plan_mode, now, controls)


def evaluate_control(
    rule: SecurityControlRule,
    target: SecurityTarget,
    phase: SecurityCheckPhase,
    findings: Sequence[SecurityFinding],
    exceptions: Sequence[SecurityException],
    now: datetime,
) -> SecurityControlDecision:
    if target.environment_kind in rule.not_applicable_kinds:
        return SecurityControlDecision(
            control_id=rule.control_id,
            cell=rule.cell,
            is_mandatory=rule.is_mandatory,
            result=SecurityResult.NA,
            effective_result=SecurityResult.NA,
            is_blocking=False,
            reasons=(SecurityReasonCode.NOT_APPLICABLE,),
            finding_ids=(),
            exception_id=None,
            mode=None,
            checked_at=None,
            expires_at=None,
        )

    max_age = _max_age(rule, phase)
    rule_findings: list[SecurityFinding] = []
    ai_findings: list[SecurityFinding] = []
    rejections: list[SecurityReasonCode] = []
    for finding in findings:
        if finding.control_id != rule.control_id:
            continue
        rejection = _find_rejection(rule, target, finding, max_age, now)
        if rejection is not None:
            rejections.append(rejection)
        elif finding.is_ai:
            ai_findings.append(finding)
        else:
            rule_findings.append(finding)

    reasons: list[SecurityReasonCode] = []
    if rule_findings:
        rule_result = _worst(finding.result for finding in rule_findings)
        if rule_result in _RESULT_REASON:
            reasons.append(_RESULT_REASON[rule_result])
    else:
        rule_result = rule.missing_result
        reasons.append(rule.missing_reason)
        reasons.extend(rejections)

    results = [rule_result]
    used_ai: list[SecurityFinding] = []
    for finding in ai_findings:
        ai_result = _ai_result(finding)
        if ai_result is None:
            continue
        used_ai.append(finding)
        results.append(ai_result)
        reasons.append(
            SecurityReasonCode.AI_ESCALATED
            if ai_result == SecurityResult.FAIL
            else SecurityReasonCode.AI_UNCONFIRMED
        )
    result = _worst(results)

    used = [*rule_findings, *used_ai]
    expires_at = min((_usable_until(e, max_age) for e in used), default=None)
    applied_exception: SecurityException | None = None
    if result in _BLOCKABLE:
        applied_exception = _find_valid_exception(rule, target, exceptions, now)
        if applied_exception is not None:
            reasons.append(SecurityReasonCode.EXCEPTION_APPLIED)
            expires_at = _earliest(expires_at, applied_exception.expires_at)
        elif _has_expired_exception(rule, target, exceptions, now):
            reasons.append(SecurityReasonCode.EXCEPTION_EXPIRED)

    effective_result = SecurityResult.WARN if applied_exception is not None else result
    return SecurityControlDecision(
        control_id=rule.control_id,
        cell=rule.cell,
        is_mandatory=rule.is_mandatory,
        result=result,
        effective_result=effective_result,
        is_blocking=(rule.is_mandatory and result in _BLOCKABLE and applied_exception is None),
        reasons=tuple(_unique(reasons)),
        finding_ids=tuple(e.finding_id for e in used),
        exception_id=applied_exception.exception_id if applied_exception else None,
        mode=_worst_mode(e.mode for e in used),
        checked_at=min((e.collected_at for e in used), default=None),
        expires_at=expires_at,
    )


def summarize_cell(
    cell: SecurityMatrixCell, decisions: Iterable[SecurityControlDecision]
) -> SecurityCellSummary:
    """칸 결과는 그 칸 통제들의 가장 나쁜 값이다.

    순서: FAIL > 필수 UNKNOWN > WARN(예외 적용 포함) > 선택 UNKNOWN > PASS.
    NA 는 빼고, 모두 NA 면 NA.
    """
    in_cell = [decision for decision in decisions if decision.cell == cell]
    counted = [d for d in in_cell if d.effective_result != SecurityResult.NA]
    worst = max(counted, key=_cell_rank, default=None)
    return SecurityCellSummary(
        cell=cell,
        result=worst.effective_result if worst is not None else SecurityResult.NA,
        has_exception=any(d.exception_id is not None for d in in_cell),
        mode=_worst_mode(d.mode for d in counted if d.mode is not None),
        checked_at=min((d.checked_at for d in counted if d.checked_at), default=None),
        expires_at=min((d.expires_at for d in counted if d.expires_at), default=None),
        control_ids=tuple(d.control_id for d in in_cell),
    )


def _cell_rank(decision: SecurityControlDecision) -> int:
    match decision.effective_result:
        case SecurityResult.FAIL:
            return 5
        case SecurityResult.UNKNOWN:
            return 4 if decision.is_mandatory else 2
        case SecurityResult.WARN:
            return 3
        case SecurityResult.PASS:
            return 1
        case SecurityResult.NA:
            return 0


def _max_age(rule: SecurityControlRule, phase: SecurityCheckPhase) -> timedelta:
    if phase == SecurityCheckPhase.EXECUTION and rule.execution_max_age is not None:
        return min(rule.max_age, rule.execution_max_age)
    return rule.max_age


def _find_rejection(
    rule: SecurityControlRule,
    target: SecurityTarget,
    finding: SecurityFinding,
    max_age: timedelta,
    now: datetime,
) -> SecurityReasonCode | None:
    if not _scope_matches(rule, target.scope, finding.scope):
        return SecurityReasonCode.SCOPE_MISMATCH
    if not finding.is_ai and finding.source not in rule.sources:
        return SecurityReasonCode.SOURCE_NOT_ALLOWED
    if finding.mode == SecurityFindingMode.SIMULATED:
        return SecurityReasonCode.EVIDENCE_SIMULATED
    if finding.policy_version != POLICY_VERSION:
        return SecurityReasonCode.POLICY_VERSION_MISMATCH
    if now >= _usable_until(finding, max_age) or finding.collected_at > now:
        return SecurityReasonCode.EVIDENCE_EXPIRED
    if finding.source == SecurityFindingSource.MANUAL_REVIEW and (
        finding.reviewer_user_id is None or not (finding.basis_url or "").strip()
    ):
        return SecurityReasonCode.EVIDENCE_INCOMPLETE
    return None


def _ai_result(finding: SecurityFinding) -> SecurityResult | None:
    """AI 결과가 판정에 보태는 값. PASS·NA·DISMISSED 는 보태지 않는다."""
    if finding.result in (SecurityResult.PASS, SecurityResult.NA):
        return None
    match finding.confirmation:
        case AiFindingConfirmation.DISMISSED:
            return None
        case AiFindingConfirmation.ESCALATED:
            return SecurityResult.FAIL
        case _:
            return SecurityResult.WARN


def _scope_matches(rule: SecurityControlRule, target: SecurityScope, scope: SecurityScope) -> bool:
    if scope.organization_id != target.organization_id:
        return False
    for key in rule.scope_keys:
        expected = target.value_of(key)
        if expected is None or scope.value_of(key) != expected:
            return False
    return True


def _find_valid_exception(
    rule: SecurityControlRule,
    target: SecurityTarget,
    exceptions: Sequence[SecurityException],
    now: datetime,
) -> SecurityException | None:
    valid = [e for e in exceptions if _is_valid_exception(rule, target, e, now)]
    return min(valid, key=lambda e: e.expires_at, default=None)


def _is_valid_exception(
    rule: SecurityControlRule,
    target: SecurityTarget,
    exception: SecurityException,
    now: datetime,
) -> bool:
    if exception.control_id != rule.control_id or not exception.reason.strip():
        return False
    if exception.requested_by_actor_type != ActorType.USER:
        return False
    if exception.approved_by_actor_type != ActorType.USER or exception.approved_by_user_id is None:
        return False
    is_self_approved = exception.approved_by_user_id == exception.requested_by_user_id
    if is_self_approved and not target.allow_self_approval:
        return False
    if exception.expires_at - exception.created_at > EXCEPTION_MAX_DURATION:
        return False
    if not exception.created_at <= now < exception.expires_at:
        return False
    return _scope_matches(rule, target.scope, exception.scope)


def _has_expired_exception(
    rule: SecurityControlRule,
    target: SecurityTarget,
    exceptions: Sequence[SecurityException],
    now: datetime,
) -> bool:
    return any(
        e.control_id == rule.control_id
        and now >= e.expires_at
        and _scope_matches(rule, target.scope, e.scope)
        for e in exceptions
    )


def _usable_until(finding: SecurityFinding, max_age: timedelta) -> datetime:
    return min(finding.expires_at, finding.collected_at + max_age)


def _earliest(first: datetime | None, second: datetime) -> datetime:
    return second if first is None else min(first, second)


def _worst(results: Iterable[SecurityResult]) -> SecurityResult:
    return max(results, key=_SEVERITY.__getitem__)


def _worst_mode(modes: Iterable[SecurityFindingMode]) -> SecurityFindingMode | None:
    return max(modes, key=_MODE_SEVERITY.__getitem__, default=None)


def _unique[T](values: Iterable[T]) -> list[T]:
    seen: set[T] = set()
    unique: list[T] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique
