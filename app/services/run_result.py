"""러너가 낸 결과를 저장할 모양으로 바꾼다(순수 계산, DB·네트워크 없음).

스키마 검사(모르는 칸·크기·AI PASS)는 요청 스키마가 끝낸 뒤다. 여기서는
- 패치 상태를 다시 계산한다. 러너 값을 믿지 않는다(보호 파일·검사 없음·PASS 아닌 검사 → BLOCKED).
- AI finding 은 사람 확인 전(UNCONFIRMED)으로, 차단 시도는 "확인 안 됨"으로 둔다.
- status·failureCode·simulated·processing 은 표의 칸으로만 두고 result JSON 에서 뺀다.
- processing 의 dataClasses 는 enum 정의 순서로 정렬한다.
- 모든 문자열의 비밀값 모양을 가린다.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.core.log_masking import mask_secrets_in_json
from app.enums import (
    AgentPatchBlockReason,
    AgentPatchStatus,
    AgentRunDataClass,
    AgentRunOrigin,
    AgentRunStatus,
    AiFindingConfirmation,
)

CHECK_PASSED = "PASS"
_PASSTHROUGH_KEYS = ("sandbox", "model", "configDiff", "featureGaps")


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


def normalize_run_result(payload: Mapping[str, Any]) -> NormalizedRunResult:
    """검사를 통과한 RunResult(camelCase JSON 값)를 저장할 값으로 바꾼다."""
    result: dict[str, Any] = {key: payload[key] for key in _PASSTHROUGH_KEYS}
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
