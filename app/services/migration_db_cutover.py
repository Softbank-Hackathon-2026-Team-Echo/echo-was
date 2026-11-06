"""DB 쓰기 전환 단계(API 서버 17)의 규칙: Job 요청 값, 단계 결과 JSON, 판정.

판정(judge_db_cutover)은 외부 호출 없이 Job 종료·결과 행만으로 정한다.

| 상황 | 결과 | 되돌리기 |
|---|---|---|
| 종료 0 + 이 계획 행 + 누락 0·중복 0 | 통과 | - |
| 종료 0 + 행 없음 + 이유 ALREADY_WRITER | 통과(이미 도착이 쓰기 담당) | - |
| 종료 0 + 누락·중복 > 0, 행 없음·못 읽음 | DATA_INTEGRITY_FAILED | 안 함(쓰기 담당 = 도착) |
| 종료 1 + 출발이 쓰기 담당(데이터 확인) 또는 로그 NO_CHANGE | DB_CUTOVER_FAILED | 출발 100% |
| 종료 1 + 그 밖(도착이 쓰기 담당·근거 없음) | DB_CUTOVER_UNKNOWN | 안 함(사람 확인) |
| Job 을 분명히 못 만듦(거절, 또는 같은 이름 404 + 결과 행 없음) | DB_CUTOVER_FAILED | 출발 100% |
| 생성 응답 유실 + 같은 이름 조회·결과 행 확인 불가 | DB_CUTOVER_UNKNOWN | 안 함(사람 확인) |
| 종료 2 · 알 수 없는 종료 · Job 사라짐(행 없음) | DB_CUTOVER_PARTIAL | 안 함(부분 상태 복구) |
| 상한 초과(행 없음) | DB_CUTOVER_TIMEOUT | 안 함(쓰기 담당 모름) |
| 상태 조회가 연속으로 실패(행 없음) | DB_CUTOVER_UNKNOWN | 안 함(사람 확인) |

원칙: 컷오버 결과(쓰기 담당)를 모르면 가중치를 되돌리지 않는다 — 트래픽과 DB 쓰기 담당이 갈리는 것이
가장 나쁘다. 종료 1 은 「바뀐 것 없음」의 증거가 아니다(이미 도착이 쓰기 담당인데 사전 확인이
실패해도, PAUSE 뒤 RESUME 이 실패해도 종료 1 이다). 그래서 종료 1 도 이 계획 결과 행 →
지금 쓰기 담당 → 로그
표식 순으로 확인하고, 출발이 쓰기 담당이라는 근거가 있을 때만 원복한다.

종료 2 가 아닌 알 수 없는 끝(종료 코드 모름·사라짐·상한 초과)이라도 이 계획 행이 있으면 전환은 끝난
것이라 행으로 판정한다. 롤백 Job(KIND=rollback)은 자동으로 부르지 않는다: 종료 1 은 바뀐 것이 없고,
부분 상태는 rollback.sh 도 판단할 수 없어(종료 2) 멈추며, 누락·중복은 사람의 판단이다(12.0 결정 3).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.enums import CutoverKind, MigrationErrorCode, MigrationPlanMode
from app.executors.base import (
    CutoverRun,
    CutoverRunResult,
    CutoverRunTarget,
    CutoverRunUnavailable,
    DbCutoverJobKind,
    DbCutoverRequest,
)

EXIT_DONE = 0
EXIT_NO_CHANGE = 1
EXIT_PARTIAL = 2
REASON_ALREADY_WRITER = "ALREADY_WRITER"
REASON_RESULT_MISSING = "RESULT_MISSING"
REASON_RESULT_UNAVAILABLE = "RESULT_UNAVAILABLE"
REASON_ROWS_MISMATCH = "ROWS_MISMATCH"
REASON_JOB_LOST = "JOB_LOST"
REASON_TIMED_OUT = "TIMED_OUT"
# 종료 1 의 뜻(인프라 로그 표식). NO_CHANGE 만 「출발이 쓰기 담당 그대로」의 증거다.
REASON_NO_CHANGE = "NO_CHANGE"
REASON_PRECHECK_FAILED = "PRECHECK_FAILED"
# 결과를 확인하지 못한 이유(DB_CUTOVER_UNKNOWN).
REASON_WRITER_UNCONFIRMED = "WRITER_UNCONFIRMED"
REASON_TARGET_IS_WRITER = "TARGET_IS_WRITER"
REASON_START_UNCONFIRMED = "START_UNCONFIRMED"
REASON_POLL_ERRORS = "POLL_ERRORS"

# 단계 결과 cutover 칸의 키(응답과 같은 camelCase).
CUTOVER_JOB_NAME = "jobName"
CUTOVER_JOB_KIND = "jobKind"
CUTOVER_ALLOW_PROMOTE = "allowPromote"
CUTOVER_EXIT_CODE = "exitCode"
CUTOVER_REASON = "reason"
CUTOVER_FINISHED = "jobFinished"
CUTOVER_LOST = "jobLost"
CUTOVER_START_FAILURE = "startFailure"
CUTOVER_RUN = "run"
# 생성 응답을 잃은 적이 있다(만들었을 수 있음). 원복 전에 같은 이름 Job·결과 행을 확인한다.
CUTOVER_START_UNCERTAIN = "startUncertain"
# 같은 이름 GET 으로 Job 을 다시 찾았다(생성 응답 유실 뒤 재부착).
CUTOVER_START_CONFIRMED = "startConfirmed"
# 상태 조회가 연속으로 실패한 횟수(성공하면 0).
CUTOVER_POLL_ERRORS = "pollErrors"
# 종료 1 뒤 확인한 지금 쓰기 담당(SOURCE·TARGET·UNKNOWN).
CUTOVER_WRITER = "writer"
WRITER_SOURCE = "SOURCE"
WRITER_TARGET = "TARGET"
WRITER_UNKNOWN = "UNKNOWN"


class CutoverRecovery(StrEnum):
    """실패했을 때 처리기가 할 일."""

    # 쓰기 담당이 그대로라 트래픽만 출발 100% 로 되돌린다(기존 롤백).
    ROLLBACK_TRAFFIC = "ROLLBACK_TRAFFIC"
    # 아무것도 자동으로 되돌리지 않고 FAILED(rollbackStatus SKIPPED)로 끝낸다.
    MANUAL = "MANUAL"


@dataclass(frozen=True)
class CutoverVerdict:
    failure: MigrationErrorCode | None
    recovery: CutoverRecovery | None
    reason: str | None
    run: CutoverRun | None

    @property
    def passed(self) -> bool:
        return self.failure is None


def db_cutover_request(
    plan_id: uuid.UUID,
    mode: MigrationPlanMode | str,
    target_environment_id: uuid.UUID,
    target_environment_kind: str,
    *,
    promote_consented: bool,
) -> DbCutoverRequest:
    """계획 종류별 Job 값. 복귀는 rollback, 재난 긴급 이전은 A 에 닿지 않으면 승격 허용.

    승격은 재난 긴급 이전이고 만들 때 승격 허용 표시를 단 단계(승인 때 acknowledgePromote 로 동의를
    받은 계획, promote_consented)일 때만 허용한다.
    """
    plan_mode = MigrationPlanMode(mode)
    is_failback = plan_mode is MigrationPlanMode.FAILBACK
    return DbCutoverRequest(
        plan_id=plan_id,
        job_kind=DbCutoverJobKind.ROLLBACK if is_failback else DbCutoverJobKind.CUTOVER,
        allow_promote=plan_mode is MigrationPlanMode.EMERGENCY and promote_consented,
        target=CutoverRunTarget(target_environment_id, target_environment_kind),
        result_kind=CutoverKind.ROLLBACK if is_failback else CutoverKind.CUTOVER,
    )


def judge_db_cutover(
    *,
    exit_code: int | None,
    reason: str | None,
    finished: bool,
    lost: bool,
    timed_out: bool,
    run_result: CutoverRunResult | None,
    writer: str | None = None,
    poll_failed: bool = False,
) -> CutoverVerdict:
    """Job 이 끝났거나(finished) 사라졌거나(lost) 상한을 넘었을 때(timed_out) 판정한다.

    writer 는 종료 1 뒤 확인한 지금 쓰기 담당(WRITER_*), poll_failed 는 상태 조회가 연속으로 실패해
    끊은 경우다.
    """
    run = run_result if isinstance(run_result, CutoverRun) else None
    if finished and exit_code == EXIT_NO_CHANGE:
        if run is not None:
            # 이 계획 행이 있으면 전환은 끝났다(종료 코드와 어긋나도 행이 근거다).
            return _judge_run(run, reason)
        if writer == WRITER_SOURCE or reason == REASON_NO_CHANGE:
            return _fail(
                MigrationErrorCode.DB_CUTOVER_FAILED, CutoverRecovery.ROLLBACK_TRAFFIC, reason
            )
        return _fail(
            MigrationErrorCode.DB_CUTOVER_UNKNOWN,
            CutoverRecovery.MANUAL,
            REASON_TARGET_IS_WRITER if writer == WRITER_TARGET else REASON_WRITER_UNCONFIRMED,
        )
    if finished and exit_code == EXIT_PARTIAL:
        return _fail(MigrationErrorCode.DB_CUTOVER_PARTIAL, CutoverRecovery.MANUAL, None, run)
    if finished and exit_code == EXIT_DONE:
        if run is not None:
            return _judge_run(run, reason)
        if reason == REASON_ALREADY_WRITER:
            return CutoverVerdict(None, None, reason, None)
        missing = (
            REASON_RESULT_UNAVAILABLE
            if isinstance(run_result, CutoverRunUnavailable)
            else REASON_RESULT_MISSING
        )
        return _fail(MigrationErrorCode.DATA_INTEGRITY_FAILED, CutoverRecovery.MANUAL, missing)
    # 종료 코드를 모름·Job 사라짐·상한 초과: 이 계획 행이 있으면 전환은 끝났다.
    if run is not None:
        return _judge_run(run, reason)
    if poll_failed and not finished and not lost:
        return _fail(
            MigrationErrorCode.DB_CUTOVER_UNKNOWN, CutoverRecovery.MANUAL, REASON_POLL_ERRORS
        )
    if timed_out and not finished and not lost:
        return _fail(
            MigrationErrorCode.DB_CUTOVER_TIMEOUT, CutoverRecovery.MANUAL, REASON_TIMED_OUT
        )
    return _fail(
        MigrationErrorCode.DB_CUTOVER_PARTIAL,
        CutoverRecovery.MANUAL,
        REASON_JOB_LOST if lost else None,
    )


def _judge_run(run: CutoverRun, reason: str | None) -> CutoverVerdict:
    if (
        (run.missing_rows or 0) == 0
        and (run.duplicate_rows or 0) == 0
        and run.rows_total is not None
    ):
        return CutoverVerdict(None, None, reason, run)
    return _fail(
        MigrationErrorCode.DATA_INTEGRITY_FAILED, CutoverRecovery.MANUAL, REASON_ROWS_MISMATCH, run
    )


def _fail(
    code: MigrationErrorCode,
    recovery: CutoverRecovery,
    reason: str | None,
    run: CutoverRun | None = None,
) -> CutoverVerdict:
    return CutoverVerdict(code, recovery, reason, run)


def run_to_json(run: CutoverRun) -> dict[str, Any]:
    """결과 행 → 단계 결과 cutover.run(응답 camelCase)."""
    return {
        "kind": run.kind.value,
        "pausedAt": run.paused_at.isoformat(),
        "resumedAt": run.resumed_at.isoformat(),
        "writePauseMs": run.write_pause_ms,
        "drainMs": run.drain_ms,
        "catchupMs": run.catchup_ms,
        "switchMs": run.switch_ms,
        "rowsTotal": run.rows_total,
        "missingRows": run.missing_rows,
        "duplicateRows": run.duplicate_rows,
        "mode": run.mode,
        "rpoEstimateMs": run.rpo_estimate_ms,
    }


def run_from_json(data: dict[str, Any]) -> CutoverRun:
    return CutoverRun(
        kind=CutoverKind(data["kind"]),
        paused_at=datetime.fromisoformat(data["pausedAt"]),
        resumed_at=datetime.fromisoformat(data["resumedAt"]),
        write_pause_ms=int(data["writePauseMs"]),
        drain_ms=data.get("drainMs"),
        catchup_ms=data.get("catchupMs"),
        switch_ms=data.get("switchMs"),
        rows_total=data.get("rowsTotal"),
        missing_rows=data.get("missingRows"),
        duplicate_rows=data.get("duplicateRows"),
        mode=data.get("mode"),
        rpo_estimate_ms=data.get("rpoEstimateMs"),
    )


def stored_cutover_run(step_results: list[dict[str, Any]]) -> CutoverRun | None:
    """DB 쓰기 전환 단계가 적어 둔 결과 행(없으면 None)."""
    for result in step_results:
        cutover = result.get("cutover")
        if isinstance(cutover, dict) and isinstance(cutover.get(CUTOVER_RUN), dict):
            return run_from_json(cutover[CUTOVER_RUN])
    return None


def cutover_log_data(cutover: dict[str, Any]) -> dict[str, Any]:
    """로그 줄·감사 data: Job 이름·종류·종료 코드·이유와 결과 숫자."""
    run = cutover.get(CUTOVER_RUN) or {}
    return {
        key: value
        for key, value in {
            CUTOVER_JOB_NAME: cutover.get(CUTOVER_JOB_NAME),
            CUTOVER_JOB_KIND: cutover.get(CUTOVER_JOB_KIND),
            CUTOVER_EXIT_CODE: cutover.get(CUTOVER_EXIT_CODE),
            CUTOVER_REASON: cutover.get(CUTOVER_REASON),
            CUTOVER_WRITER: cutover.get(CUTOVER_WRITER),
            CUTOVER_START_UNCERTAIN: cutover.get(CUTOVER_START_UNCERTAIN),
            CUTOVER_START_CONFIRMED: cutover.get(CUTOVER_START_CONFIRMED),
            CUTOVER_POLL_ERRORS: cutover.get(CUTOVER_POLL_ERRORS) or None,
            "writePauseMs": run.get("writePauseMs"),
            "rowsTotal": run.get("rowsTotal"),
            "missingRows": run.get("missingRows"),
            "duplicateRows": run.get("duplicateRows"),
            "mode": run.get("mode"),
        }.items()
        if value is not None
    }
