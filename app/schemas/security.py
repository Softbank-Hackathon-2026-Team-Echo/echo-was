"""보안 매트릭스·증거 응답(보안·거버넌스 4, 계약 4-7). 비밀값 원문은 없다(정규화가 이미 버렸다)."""

import uuid
from datetime import datetime
from typing import Any, Literal

from app.enums import (
    AiFindingConfirmation,
    SecurityCheckPhase,
    SecurityControlId,
    SecurityFindingMode,
    SecurityFindingSource,
    SecurityGateStatus,
    SecurityMatrixCell,
    SecurityReasonCode,
    SecurityResult,
)
from app.models import Finding
from app.schemas.migration_plan import UserRefResponse
from app.schemas.response import ApiModel
from app.services.security_matrix_service import (
    MatrixCellView,
    MatrixControlView,
    MatrixExceptionView,
    MatrixRowView,
    SecurityMatrixView,
)

type FindingOrigin = Literal["RULE", "HUMAN", "AI"]


class SecurityMatrixExceptionResponse(ApiModel):
    """칸·통제에 적용된 기한부 예외 요약. 범위·요청자는 예외 API 에서 본다."""

    id: uuid.UUID
    control_id: SecurityControlId
    reason: str
    expires_at: datetime
    approved_by: UserRefResponse | None

    @classmethod
    def from_view(cls, view: MatrixExceptionView) -> "SecurityMatrixExceptionResponse":
        return cls(
            id=view.exception_id,
            control_id=view.control_id,
            reason=view.reason,
            expires_at=view.expires_at,
            approved_by=UserRefResponse.from_model(view.approved_by),
        )


def _exception_response(
    view: MatrixExceptionView | None,
) -> SecurityMatrixExceptionResponse | None:
    return SecurityMatrixExceptionResponse.from_view(view) if view is not None else None


class SecurityControlResponse(ApiModel):
    """칸 안의 통제 하나의 판정. 서비스 단위 통제는 서비스마다 한 줄이다."""

    control_id: SecurityControlId
    title: str
    # 예외 반영 전 증거 값. 게이트가 막는지는 gate_status 로 본다.
    result: SecurityResult
    is_mandatory: bool
    reasons: list[SecurityReasonCode]
    finding_ids: list[uuid.UUID]
    mode: SecurityFindingMode | None
    checked_at: datetime | None
    expires_at: datetime | None
    service_id: uuid.UUID | None
    gate_status: SecurityGateStatus
    exception: SecurityMatrixExceptionResponse | None

    @classmethod
    def from_view(cls, view: MatrixControlView) -> "SecurityControlResponse":
        return cls(
            control_id=view.control_id,
            title=view.title,
            result=view.result,
            is_mandatory=view.is_mandatory,
            reasons=list(view.reasons),
            finding_ids=list(view.finding_ids),
            mode=view.mode,
            checked_at=view.checked_at,
            expires_at=view.expires_at,
            service_id=view.service_id,
            gate_status=view.gate_status,
            exception=_exception_response(view.exception),
        )


class SecurityCellResponse(ApiModel):
    # 예외 반영 전 증거 값의 최악값. 게이트가 막는지는 gate_status 로 본다.
    result: SecurityResult
    finding_count: int
    has_exception: bool
    mode: SecurityFindingMode | None
    checked_at: datetime | None
    expires_at: datetime | None
    controls: list[SecurityControlResponse]
    gate_status: SecurityGateStatus
    # 칸에 적용된 예외 중 가장 먼저 만료되는 것.
    exception: SecurityMatrixExceptionResponse | None

    @classmethod
    def from_view(cls, view: MatrixCellView) -> "SecurityCellResponse":
        return cls(
            result=view.result,
            finding_count=view.finding_count,
            has_exception=view.has_exception,
            mode=view.mode,
            checked_at=view.checked_at,
            expires_at=view.expires_at,
            controls=[SecurityControlResponse.from_view(control) for control in view.controls],
            gate_status=view.gate_status,
            exception=_exception_response(view.exception),
        )


class SecurityMatrixCellsResponse(ApiModel):
    iam: SecurityCellResponse
    encryption: SecurityCellResponse
    audit_log: SecurityCellResponse
    policy: SecurityCellResponse


class SecurityMatrixServiceResponse(ApiModel):
    service_id: uuid.UUID
    name: str
    image_digest: str


class SecurityMatrixRowResponse(ApiModel):
    environment_id: uuid.UUID
    environment_name: str
    environment_kind: str
    services: list[SecurityMatrixServiceResponse]
    cells: SecurityMatrixCellsResponse

    @classmethod
    def from_view(cls, view: MatrixRowView) -> "SecurityMatrixRowResponse":
        cells = {cell.cell: SecurityCellResponse.from_view(cell) for cell in view.cells}
        return cls(
            environment_id=view.environment_id,
            environment_name=view.environment_name,
            environment_kind=view.environment_kind,
            services=[
                SecurityMatrixServiceResponse(
                    service_id=s.service_id, name=s.service_name, image_digest=s.image_digest
                )
                for s in view.services
            ],
            cells=SecurityMatrixCellsResponse(
                iam=cells[SecurityMatrixCell.IAM],
                encryption=cells[SecurityMatrixCell.ENCRYPTION],
                audit_log=cells[SecurityMatrixCell.AUDIT_LOG],
                policy=cells[SecurityMatrixCell.POLICY],
            ),
        )


class SecurityMatrixResponse(ApiModel):
    policy_version: str
    phase: SecurityCheckPhase
    checked_at: datetime
    rows: list[SecurityMatrixRowResponse]

    @classmethod
    def from_view(cls, view: SecurityMatrixView) -> "SecurityMatrixResponse":
        return cls(
            policy_version=view.policy_version,
            phase=view.phase,
            checked_at=view.checked_at,
            rows=[SecurityMatrixRowResponse.from_view(row) for row in view.rows],
        )


class FindingItemResponse(ApiModel):
    """근거 줄(취약점·비밀 위치 등). 비밀값 원문·매치 문자열은 없다."""

    item_id: str
    severity: str | None = None
    target: str | None = None
    installed_version: str | None = None
    fixed_version: str | None = None
    line: int | None = None
    commit: str | None = None
    reason: str | None = None


class SecurityFindingResponse(ApiModel):
    id: uuid.UUID
    control_id: SecurityControlId
    result: SecurityResult
    reason: str | None
    source: SecurityFindingSource
    # 규칙(검사 도구) · 사람 점검 · AI. AI 결과는 PASS 가 없고 사람이 확인해야 차단 사유가 된다.
    origin: FindingOrigin
    mode: SecurityFindingMode
    confirmation: AiFindingConfirmation | None
    policy_version: str
    tool_version: str | None
    service_id: uuid.UUID | None
    environment_id: uuid.UUID | None
    image_digest: str | None
    config_commit: str | None
    was_version: str | None
    collected_at: datetime
    expires_at: datetime
    # 원문 위치(검사 실행 등). 원문은 서버에도 저장하지 않는다.
    evidence_ref: str | None
    reviewer_user_id: uuid.UUID | None
    basis_url: str | None
    items: list[FindingItemResponse]
    total_items: int

    @classmethod
    def from_model(cls, model: Finding) -> "SecurityFindingResponse":
        source = SecurityFindingSource(model.source)
        return cls(
            id=model.id,
            control_id=SecurityControlId(model.control_id),
            result=SecurityResult(model.result),
            reason=model.reason,
            source=source,
            origin=_origin(source),
            mode=SecurityFindingMode(model.mode),
            confirmation=(
                AiFindingConfirmation(model.confirmation) if model.confirmation else None
            ),
            policy_version=model.policy_version,
            tool_version=model.tool_version,
            service_id=model.service_id,
            environment_id=model.environment_id,
            image_digest=model.image_digest,
            config_commit=model.config_commit,
            was_version=model.was_version,
            collected_at=model.collected_at,
            expires_at=model.expires_at,
            evidence_ref=model.source_ref,
            reviewer_user_id=model.reviewer_user_id,
            basis_url=model.basis_url,
            items=[FindingItemResponse.model_validate(item) for item in _items(model.items)],
            total_items=model.total_items,
        )


def _origin(source: SecurityFindingSource) -> FindingOrigin:
    match source:
        case SecurityFindingSource.AI_REVIEW:
            return "AI"
        case SecurityFindingSource.MANUAL_REVIEW:
            return "HUMAN"
        case _:
            return "RULE"


def _items(raw: Any) -> list[dict[str, Any]]:
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
