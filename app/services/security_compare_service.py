"""이전 전후 보안 비교(보안 5-2): 두 환경의 매트릭스 칸을 나란히 놓는다.

- 두 칸 값은 보안 매트릭스(`SecurityMatrixService.get_rows`)가 계산한 행 그대로다. 새 판정은 없다.
- `blocks_migration` = 도착 칸의 게이트 관점이 BLOCKED. 칸이 BLOCKED 인 것은 그 칸 통제 판정
  중 하나라도 규칙표 `evaluate_control` 의 `is_blocking`(필수 + FAIL·UNKNOWN + 유효 예외 없음)일
  때이고, 이전 게이트도 같은 함수로 막는다. 막는 규칙을 여기서 다시 쓰지 않는다.
- 준비(PREPARATION) 시점 판정이다. 실행 전용 통제와 실행 직전 재확인은 게이트만 본다.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from app.core.exceptions import RequestFieldInvalidError
from app.enums import (
    SecurityCheckPhase,
    SecurityCompareChange,
    SecurityControlId,
    SecurityGateStatus,
    SecurityMatrixCell,
    SecurityResult,
)
from app.services.security_matrix_service import (
    MatrixCellView,
    MatrixRowView,
    SecurityMatrixService,
)

# 같은 환경을 두 번 고른 요청의 사유(이전 계획·사전 점검과 같은 코드).
SAME_ENVIRONMENT_REASON = "SAME_ENVIRONMENT"

# 비교 열쇠. 결과는 규칙표의 "가장 나쁜 값" 순서, 결과가 같으면 게이트 관점으로 가른다.
_RESULT_RANK = {
    SecurityResult.NA: 0,
    SecurityResult.PASS: 1,
    SecurityResult.WARN: 2,
    SecurityResult.UNKNOWN: 3,
    SecurityResult.FAIL: 4,
}
_GATE_RANK = {
    SecurityGateStatus.PASS: 0,
    SecurityGateStatus.WAIVED: 1,
    SecurityGateStatus.BLOCKED: 2,
}


@dataclass(frozen=True)
class SecurityCompareRowView:
    cell: SecurityMatrixCell
    source: MatrixCellView
    target: MatrixCellView
    blocks_migration: bool
    # 도착 칸에서 막은 통제(중복 없음, 칸 통제 순서).
    blocking_control_ids: tuple[SecurityControlId, ...]
    change: SecurityCompareChange


@dataclass(frozen=True)
class SecurityCompareSummaryView:
    blocking: int
    worse_on_target: int
    same: int
    better: int


@dataclass(frozen=True)
class SecurityCompareView:
    policy_version: str
    phase: SecurityCheckPhase
    checked_at: datetime
    service_id: uuid.UUID | None
    source: MatrixRowView
    target: MatrixRowView
    rows: tuple[SecurityCompareRowView, ...]
    summary: SecurityCompareSummaryView


class SecurityCompareService:
    def __init__(self, matrix: SecurityMatrixService) -> None:
        self._matrix = matrix

    async def compare(
        self,
        organization_id: uuid.UUID,
        source_environment_id: uuid.UUID,
        target_environment_id: uuid.UUID,
        *,
        service_id: uuid.UUID | None = None,
    ) -> SecurityCompareView:
        """출발·도착 환경의 4칸 비교. 같은 환경이면 422, 조직 밖 환경·서비스는 404."""
        if source_environment_id == target_environment_id:
            raise RequestFieldInvalidError("target", SAME_ENVIRONMENT_REASON)
        view = await self._matrix.get_rows(
            organization_id,
            (source_environment_id, target_environment_id),
            service_id=service_id,
        )
        source, target = view.rows
        rows = compare_rows(source, target)
        return SecurityCompareView(
            policy_version=view.policy_version,
            phase=view.phase,
            checked_at=view.checked_at,
            service_id=service_id,
            source=source,
            target=target,
            rows=rows,
            summary=summarize_compare(rows),
        )


def compare_rows(
    source: MatrixRowView, target: MatrixRowView
) -> tuple[SecurityCompareRowView, ...]:
    """두 행의 칸을 매트릭스 칸 순서대로 짝짓는다."""
    target_cells = {cell.cell: cell for cell in target.cells}
    return tuple(
        compare_cell(source_cell, target_cells[source_cell.cell]) for source_cell in source.cells
    )


def compare_cell(source: MatrixCellView, target: MatrixCellView) -> SecurityCompareRowView:
    blocking = tuple(
        dict.fromkeys(
            control.control_id
            for control in target.controls
            if control.gate_status == SecurityGateStatus.BLOCKED
        )
    )
    return SecurityCompareRowView(
        cell=source.cell,
        source=source,
        target=target,
        blocks_migration=target.gate_status == SecurityGateStatus.BLOCKED,
        blocking_control_ids=blocking,
        change=_change(source, target),
    )


def summarize_compare(rows: tuple[SecurityCompareRowView, ...]) -> SecurityCompareSummaryView:
    return SecurityCompareSummaryView(
        blocking=sum(row.blocks_migration for row in rows),
        worse_on_target=sum(row.change == SecurityCompareChange.WORSE for row in rows),
        same=sum(row.change == SecurityCompareChange.SAME for row in rows),
        better=sum(row.change == SecurityCompareChange.BETTER for row in rows),
    )


def _change(source: MatrixCellView, target: MatrixCellView) -> SecurityCompareChange:
    before, after = _rank(source), _rank(target)
    if after > before:
        return SecurityCompareChange.WORSE
    if after < before:
        return SecurityCompareChange.BETTER
    return SecurityCompareChange.SAME


def _rank(cell: MatrixCellView) -> tuple[int, int]:
    return _RESULT_RANK[cell.result], _GATE_RANK[cell.gate_status]
