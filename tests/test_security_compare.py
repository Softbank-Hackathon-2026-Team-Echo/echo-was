"""이전 전후 보안 비교 계산(보안 5-2 SPEC AC-1·AC-2). DB 없이 규칙표 판정에서 칸을 만든다."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import RequestFieldInvalidError
from app.enums import (
    ActorType,
    SecurityCheckPhase,
    SecurityCompareChange,
    SecurityControlId,
    SecurityFindingMode,
    SecurityFindingSource,
    SecurityGateStatus,
    SecurityMatrixCell,
    SecurityResult,
)
from app.services.security_compare_service import (
    SecurityCompareService,
    compare_cell,
    compare_rows,
    summarize_compare,
)
from app.services.security_matrix_service import (
    CELL_ORDER,
    MatrixCellView,
    MatrixRowView,
    build_cell_view,
)
from app.services.security_rules import (
    POLICY_VERSION,
    SecurityControlDecision,
    SecurityException,
    SecurityFinding,
    SecurityScope,
    SecurityTarget,
    evaluate_control,
    get_rule,
)

NOW = datetime(2026, 11, 5, 14, 0, tzinfo=UTC)
ORG = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
ENV = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
SERVICE = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
REQUESTER = uuid.UUID("00000000-0000-0000-0000-000000000001")
APPROVER = uuid.UUID("00000000-0000-0000-0000-000000000002")
DIGEST = "sha256:" + "a" * 64

# 통제마다 증거 출처·범위(판정 대상과 맞는 값).
_SOURCES = {
    SecurityControlId.SEC_1: SecurityFindingSource.WAS_STATE,
    SecurityControlId.IAM_1: SecurityFindingSource.CLOUD_API,
    SecurityControlId.TLS_1: SecurityFindingSource.TLS_PROBE,
}
_TARGET = SecurityTarget(
    scope=SecurityScope(ORG, service_id=SERVICE, environment_id=ENV, image_digest=DIGEST),
    environment_kind="aws",
)
BLOCKED = SecurityGateStatus.BLOCKED
WAIVED = SecurityGateStatus.WAIVED
GATE_PASS = SecurityGateStatus.PASS


def _decision(
    control: SecurityControlId, result: SecurityResult | None, *, exception: bool = False
) -> SecurityControlDecision:
    """규칙표 판정 하나. result=None 은 증거 없음(UNKNOWN), exception=True 면 유효 예외 하나."""
    rule = get_rule(control)
    findings = (
        [
            SecurityFinding(
                finding_id=uuid.uuid5(ORG, f"{control}-{result}"),
                control_id=control,
                result=result,
                mode=SecurityFindingMode.REAL,
                source=_SOURCES[control],
                scope=_TARGET.scope,
                collected_at=NOW - timedelta(minutes=1),
                expires_at=NOW + timedelta(hours=1),
                policy_version=POLICY_VERSION,
            )
        ]
        if result is not None
        else []
    )
    exceptions = (
        [
            SecurityException(
                exception_id=uuid.uuid5(ORG, f"exception-{control}"),
                control_id=control,
                scope=_TARGET.scope,
                reason="시연 범위 임시 면제",
                requested_by_user_id=REQUESTER,
                requested_by_actor_type=ActorType.USER,
                approved_by_user_id=APPROVER,
                approved_by_actor_type=ActorType.USER,
                created_at=NOW - timedelta(minutes=5),
                expires_at=NOW + timedelta(days=1),
            )
        ]
        if exception
        else []
    )
    return evaluate_control(
        rule, _TARGET, SecurityCheckPhase.PREPARATION, findings, exceptions, NOW
    )


def _cell(*decisions: SecurityControlDecision) -> MatrixCellView:
    cell = decisions[0].cell
    assert cell is not None
    return build_cell_view(cell, ((d, None) for d in decisions))


def _empty(cell: SecurityMatrixCell) -> MatrixCellView:
    return build_cell_view(cell, ())


def _row(cells: dict[SecurityMatrixCell, MatrixCellView]) -> MatrixRowView:
    return MatrixRowView(
        environment_id=ENV,
        environment_name="aws-tokyo",
        environment_kind="aws",
        services=(),
        cells=tuple(cells.get(cell) or _empty(cell) for cell in CELL_ORDER),
    )


# AC-1 blocksMigration = 게이트의 is_blocking(필수 + FAIL·UNKNOWN + 유효 예외 없음)

_RESULTS = [SecurityResult.PASS, SecurityResult.WARN, SecurityResult.FAIL, None]


@pytest.mark.parametrize("exception", [False, True], ids=["no-exception", "exception"])
@pytest.mark.parametrize("result", _RESULTS, ids=["PASS", "WARN", "FAIL", "UNKNOWN"])
@pytest.mark.parametrize(
    "control",
    [SecurityControlId.SEC_1, SecurityControlId.IAM_1],
    ids=["mandatory", "optional"],
)
def test_blocks_migration_follows_gate_rule(
    control: SecurityControlId, result: SecurityResult | None, exception: bool
) -> None:
    decision = _decision(control, result, exception=exception)
    source = _cell(_decision(control, SecurityResult.PASS))

    row = compare_cell(source, _cell(decision))

    failing = result in (SecurityResult.FAIL, None)
    expected = get_rule(control).is_mandatory and failing and not exception
    assert row.blocks_migration is expected
    assert row.blocks_migration is decision.is_blocking
    assert row.blocking_control_ids == ((control,) if expected else ())
    waived = failing and exception
    assert row.target.gate_status == (BLOCKED if expected else WAIVED if waived else GATE_PASS)


def test_source_side_block_does_not_block_migration() -> None:
    source = _cell(_decision(SecurityControlId.SEC_1, None))
    target = _cell(_decision(SecurityControlId.SEC_1, SecurityResult.PASS))

    row = compare_cell(source, target)

    assert source.gate_status == BLOCKED
    assert row.blocks_migration is False
    assert row.blocking_control_ids == ()
    assert row.change == SecurityCompareChange.BETTER


def test_blocking_control_ids_list_only_blocking_controls_once() -> None:
    target = _cell(
        _decision(SecurityControlId.SEC_1, None, exception=True),
        _decision(SecurityControlId.TLS_1, None),
        _decision(SecurityControlId.TLS_1, SecurityResult.FAIL),
    )

    row = compare_cell(_empty(SecurityMatrixCell.ENCRYPTION), target)

    assert row.blocks_migration is True
    assert row.blocking_control_ids == (SecurityControlId.TLS_1,)


def test_optional_fail_without_exception_does_not_block() -> None:
    target = _cell(_decision(SecurityControlId.IAM_1, SecurityResult.FAIL))

    row = compare_cell(_cell(_decision(SecurityControlId.IAM_1, SecurityResult.PASS)), target)

    assert (row.target.result, row.target.gate_status) == (SecurityResult.FAIL, GATE_PASS)
    assert row.blocks_migration is False
    assert row.change == SecurityCompareChange.WORSE


# AC-2 변화·요약


@pytest.mark.parametrize(
    ("source", "target", "change"),
    [
        (SecurityResult.PASS, SecurityResult.PASS, SecurityCompareChange.SAME),
        (SecurityResult.PASS, SecurityResult.WARN, SecurityCompareChange.WORSE),
        (SecurityResult.WARN, SecurityResult.FAIL, SecurityCompareChange.WORSE),
        (SecurityResult.FAIL, SecurityResult.PASS, SecurityCompareChange.BETTER),
        (None, SecurityResult.WARN, SecurityCompareChange.BETTER),
        (SecurityResult.WARN, None, SecurityCompareChange.WORSE),
    ],
    ids=["same", "pass-warn", "warn-fail", "fail-pass", "unknown-warn", "warn-unknown"],
)
def test_change_compares_result_severity(
    source: SecurityResult | None, target: SecurityResult | None, change: SecurityCompareChange
) -> None:
    row = compare_cell(
        _cell(_decision(SecurityControlId.IAM_1, source)),
        _cell(_decision(SecurityControlId.IAM_1, target)),
    )

    assert row.change == change


def test_change_uses_gate_status_when_results_are_equal() -> None:
    waived = _cell(_decision(SecurityControlId.SEC_1, None, exception=True))
    blocked = _cell(_decision(SecurityControlId.SEC_1, None))

    worse = compare_cell(waived, blocked)
    better = compare_cell(blocked, waived)

    assert waived.result == blocked.result == SecurityResult.UNKNOWN
    assert (worse.change, worse.blocks_migration) == (SecurityCompareChange.WORSE, True)
    assert (better.change, better.blocks_migration) == (SecurityCompareChange.BETTER, False)


def test_rows_keep_cell_order_and_summary_counts_them() -> None:
    source = _row(
        {
            SecurityMatrixCell.IAM: _cell(_decision(SecurityControlId.IAM_1, SecurityResult.PASS)),
            SecurityMatrixCell.ENCRYPTION: _cell(_decision(SecurityControlId.SEC_1, None)),
        }
    )
    target = _row(
        {
            SecurityMatrixCell.IAM: _cell(_decision(SecurityControlId.IAM_1, SecurityResult.FAIL)),
            SecurityMatrixCell.ENCRYPTION: _cell(
                _decision(SecurityControlId.SEC_1, SecurityResult.PASS)
            ),
        }
    )

    rows = compare_rows(source, target)
    summary = summarize_compare(rows)

    assert [row.cell for row in rows] == list(CELL_ORDER)
    assert [row.change for row in rows] == [
        SecurityCompareChange.WORSE,
        SecurityCompareChange.BETTER,
        SecurityCompareChange.SAME,
        SecurityCompareChange.SAME,
    ]
    assert (summary.worse_on_target, summary.same, summary.better) == (1, 2, 1)
    assert summary.blocking == 0
    assert summary.worse_on_target + summary.same + summary.better == len(CELL_ORDER)


def test_summary_counts_blocking_rows() -> None:
    blocked = _cell(_decision(SecurityControlId.SEC_1, None))
    rows = compare_rows(
        _row({}),
        _row({SecurityMatrixCell.ENCRYPTION: blocked}),
    )

    summary = summarize_compare(rows)

    assert [row.blocks_migration for row in rows] == [False, True, False, False]
    assert summary.blocking == 1
    assert (summary.worse_on_target, summary.same) == (1, 3)


class _NoMatrix:
    async def get_rows(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("same environment must be rejected before reading")


async def test_same_environment_is_rejected_before_reading() -> None:
    service = SecurityCompareService(_NoMatrix())  # type: ignore[arg-type]

    with pytest.raises(RequestFieldInvalidError) as raised:
        await service.compare(ORG, ENV, ENV)

    assert raised.value.code == "VALIDATION_ERROR"
    assert [(d.field, d.reason) for d in raised.value.details or []] == [
        ("target", "SAME_ENVIRONMENT")
    ]
