"""러너가 낸 결과를 저장할 모양으로 바꾼다(순수 계산, DB·네트워크 없음).

스키마 검사(모르는 칸·크기·AI PASS)는 요청 스키마가 끝낸 뒤다. 여기서는
- 패치 상태를 다시 계산한다. 러너 값을 믿지 않는다(보호 파일·검사 없음·PASS 아닌 검사 → BLOCKED).
- AI finding 은 사람 확인 전(UNCONFIRMED)으로, 차단 시도는 "확인 안 됨"으로 둔다.
- status·failureCode·simulated·processing 은 표의 칸으로만 두고 result JSON 에서 뺀다.
- processing 의 dataClasses 는 enum 정의 순서로 정렬한다.
- 모든 문자열의 비밀값 모양을 가린다.
- 패치 해시는 `sha256:` + diff 문자열(UTF-8)의 SHA-256 이다(PD-14). 러너·WAS 가 같이 계산한다.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from app.core.log_masking import mask_secrets_in_json
from app.enums import (
    AgentPatchBlockReason,
    AgentPatchDecision,
    AgentPatchStatus,
    AgentRunDataClass,
    AgentRunOrigin,
    AgentRunStatus,
    AiFindingConfirmation,
    DiagnosisResult,
)

CHECK_PASSED = "PASS"
# 진단 결과 없이 끝난 진단 실행의 unfixableReason(고정 문장, 화면에 그대로 보인다).
DIAGNOSIS_MISSING_REASON = "Runner did not report a diagnosis."
DIAGNOSIS_RUN_FAILED_REASON = "Diagnosis run failed"
_PASSTHROUGH_KEYS = ("sandbox", "model", "configDiff", "featureGaps")
_OPEN_APPLY_STATUSES = frozenset(
    {AgentRunStatus.QUEUED, AgentRunStatus.CLAIMED, AgentRunStatus.RUNNING}
)


@dataclass(frozen=True)
class NormalizedRunResult:
    status: AgentRunStatus
    failure_code: str | None
    simulated: bool
    model_id: str | None
    prompt_version: str | None
    result: dict[str, Any]
    processing: dict[str, Any] | None

    @property
    def is_ai_reviewed(self) -> bool:
        return self.model_id is not None


@dataclass(frozen=True)
class NormalizedApplyResult:
    """반영 실행(APPLY_PATCH) 결과. status·failureCode·simulated 는 표의 칸, 나머지는 result."""

    status: AgentRunStatus
    failure_code: str | None
    simulated: bool
    result: dict[str, Any]

    @property
    def applied_commit_sha(self) -> str | None:
        value: str | None = self.result["appliedCommitSha"]
        return value


def compute_patch_hash(diff: str) -> str:
    """패치 지문의 해시(PD-14): `sha256:` + diff 문자열 UTF-8 바이트의 SHA-256."""
    return f"sha256:{hashlib.sha256(diff.encode()).hexdigest()}"


def normalize_apply_result(payload: Mapping[str, Any]) -> NormalizedApplyResult:
    """검사를 통과한 ApplyPatchResult(camelCase JSON 값)를 저장할 값으로 바꾼다."""
    result: dict[str, Any] = {
        "appliedCommitSha": payload["appliedCommitSha"],
        "branch": payload["branch"],
    }
    return NormalizedApplyResult(
        status=AgentRunStatus(payload["status"]),
        failure_code=payload["failureCode"],
        simulated=payload["simulated"],
        result=result,
    )


def patch_block_reason(patch: Mapping[str, Any]) -> AgentPatchBlockReason | None:
    """패치를 막을 이유. 없으면 승인 대기(PROPOSED)다."""
    if patch["touchesProtectedFiles"]:
        return AgentPatchBlockReason.PROTECTED_FILES_TOUCHED
    checks = patch["checks"]
    if not checks:
        return AgentPatchBlockReason.CHECKS_MISSING
    if any(check["result"] != CHECK_PASSED for check in checks):
        return AgentPatchBlockReason.CHECK_NOT_PASSED
    return None


def patch_origin(patch: Mapping[str, Any]) -> AgentRunOrigin:
    """저장된 패치의 출처. 이 칸이 생기기 전에 저장한 결과에는 없어 AI 로 본다."""
    return AgentRunOrigin(patch.get("origin", AgentRunOrigin.AI))


def compute_patch_status(
    stored_status: AgentPatchStatus,
    decision: AgentPatchDecision | None,
    apply_status: AgentRunStatus | None,
) -> AgentPatchStatus:
    """응답의 패치 상태. 저장하지 않고 검증 상태 + 결정 + 반영 실행 상태로 계산한다.

    거절을 맨 먼저 본다. 승인됐어도 반영 실행이 없거나(스위치 꺼짐) 아직 열려 있으면 APPROVED 다.
    """
    if decision == AgentPatchDecision.REJECTED:
        return AgentPatchStatus.REJECTED
    if stored_status == AgentPatchStatus.BLOCKED or decision is None:
        return stored_status
    if apply_status is None or apply_status in _OPEN_APPLY_STATUSES:
        return AgentPatchStatus.APPROVED
    if apply_status == AgentRunStatus.SUCCEEDED:
        return AgentPatchStatus.APPLIED
    return AgentPatchStatus.APPLY_FAILED


def _with_recalculated_status(patch: Mapping[str, Any]) -> dict[str, Any]:
    reason = patch_block_reason(patch)
    status = AgentPatchStatus.BLOCKED if reason is not None else AgentPatchStatus.PROPOSED
    return {**patch, "status": status.value, "blockReason": reason.value if reason else None}


def _with_confirmation(finding: Mapping[str, Any]) -> dict[str, Any]:
    is_ai = finding["origin"] == AgentRunOrigin.AI
    confirmation = AiFindingConfirmation.UNCONFIRMED.value if is_ai else None
    return {**finding, "confirmation": confirmation}


def _normalized_processing(processing: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if processing is None:
        return None
    class_order = list(AgentRunDataClass)
    data_classes = sorted(
        processing["dataClasses"], key=lambda value: class_order.index(AgentRunDataClass(value))
    )
    masked: dict[str, Any] = mask_secrets_in_json({**processing, "dataClasses": data_classes})
    return masked


def missing_diagnosis(reason: str = DIAGNOSIS_MISSING_REASON) -> dict[str, Any]:
    """러너가 진단을 보내지 않았거나(지금 러너) 실패로 끝났을 때 저장하는 진단: 근거 부족."""
    return {
        "result": DiagnosisResult.INSUFFICIENT_EVIDENCE.value,
        "category": None,
        "cause": None,
        "fixable": False,
        "unfixableReason": reason,
        "evidence": [],
    }


def normalize_diagnosis_result(payload: Mapping[str, Any]) -> NormalizedRunResult:
    """검사를 통과한 진단 결과(RunResult + diagnosis)를 저장할 값으로 바꾼다(SPEC api-98).

    diagnosis 가 없으면 근거 부족(INSUFFICIENT_EVIDENCE)으로 마무리한다. 이때 요청 스키마가 패치를
    이미 막았다(패치는 DIAGNOSED·fixable 진단에만 붙는다). 모든 문자열의 비밀값 모양을 가린다.
    """
    base = normalize_run_result(payload)
    diagnosis = payload.get("diagnosis")
    if diagnosis is None:
        reason = (
            DIAGNOSIS_MISSING_REASON
            if payload["status"] == AgentRunStatus.SUCCEEDED
            else f"{DIAGNOSIS_RUN_FAILED_REASON}: {payload['failureCode']}"
        )
        stored = missing_diagnosis(reason)
    else:
        stored = mask_secrets_in_json(dict(diagnosis))
    return replace(base, result={**base.result, "diagnosis": stored})


def normalize_run_result(payload: Mapping[str, Any]) -> NormalizedRunResult:
    """검사를 통과한 RunResult(camelCase JSON 값)를 저장할 값으로 바꾼다."""
    result: dict[str, Any] = {key: payload[key] for key in _PASSTHROUGH_KEYS}
    if payload.get("sbom") is not None:
        result["sbom"] = payload["sbom"]
    result["findings"] = [_with_confirmation(item) for item in payload["findings"]]
    result["patches"] = [_with_recalculated_status(item) for item in payload["patches"]]
    result["blockedAttempts"] = [
        {**item, "auditLogId": None, "confirmed": False} for item in payload["blockedAttempts"]
    ]
    masked: dict[str, Any] = mask_secrets_in_json(result)
    model = masked["model"]
    return NormalizedRunResult(
        status=AgentRunStatus(payload["status"]),
        failure_code=payload["failureCode"],
        simulated=payload["simulated"],
        model_id=model["id"] if model else None,
        prompt_version=model["promptVersion"] if model else None,
        result=masked,
        processing=_normalized_processing(payload["processing"]),
    )
