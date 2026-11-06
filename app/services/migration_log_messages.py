"""이전 실행 로그와 실패 응답의 고정 문장.

외부 응답 원문은 넣지 않는다. 줄마다 코드·수준·문장이 정해져 있고, FAILED·ROLLBACK_STARTED·
JUDGE_FAILED·DATA_SYNC_FAILED 줄은 원인 코드의 문장을 붙인다.
"""

from dataclasses import dataclass
from typing import Any

from app.enums import LogLevel, MigrationErrorCode, MigrationLogCode

# 원인 코드별 고정 문장(계획 응답 errorMessage·로그 줄).
MIGRATION_ERROR_MESSAGES: dict[MigrationErrorCode, str] = {
    MigrationErrorCode.PREFLIGHT_FAILED: (
        "The preflight check failed: the target environment was not healthy or not on the same "
        "image."
    ),
    MigrationErrorCode.TRAFFIC_APPLY_FAILED: "The traffic router did not apply the new weights.",
    MigrationErrorCode.HEALTH_CHECK_FAILED: (
        "The target environment health check did not return a 2xx response."
    ),
    MigrationErrorCode.METRICS_UNAVAILABLE: (
        "Request counters or metrics for the target environment were unavailable, so the step "
        "could not be judged."
    ),
    MigrationErrorCode.THRESHOLD_EXCEEDED: (
        "The target environment returned more 5xx responses than the rollback condition allows."
    ),
    MigrationErrorCode.DATA_LAG_EXCEEDED: (
        "The data replication lag exceeded the rollback condition."
    ),
    MigrationErrorCode.ABORTED: "A person aborted the migration.",
    MigrationErrorCode.WORKER_LOST: "The migration worker stopped repeatedly during this plan.",
    MigrationErrorCode.INTERNAL_ERROR: "An internal error occurred.",
    MigrationErrorCode.TARGET_NOT_RECEIVING: (
        "The target environment did not receive its share of requests (check the entrypoint "
        "weights)."
    ),
    MigrationErrorCode.SECURITY_GATE_BLOCKED: (
        "The security gate blocked the migration: a required security control failed or could "
        "not be confirmed. Traffic was not changed."
    ),
    MigrationErrorCode.DB_CUTOVER_FAILED: (
        "The database cutover job did not move the database writer (nothing changed), so traffic "
        "is sent back to the source environment."
    ),
    MigrationErrorCode.DB_CUTOVER_TIMEOUT: (
        "The database cutover job did not finish in time. The database writer is unknown, so "
        "nothing was rolled back automatically."
    ),
    MigrationErrorCode.DB_CUTOVER_PARTIAL: (
        "The database cutover job stopped midway. Nothing was rolled back automatically; follow "
        "the partial-state recovery runbook."
    ),
    MigrationErrorCode.DATA_INTEGRITY_FAILED: (
        "The database writer moved to the target, but the ledger check found missing or duplicate "
        "rows (or no result). Nothing was rolled back automatically."
    ),
    MigrationErrorCode.DB_CUTOVER_UNKNOWN: (
        "The result of the database cutover could not be confirmed (which database is the writer "
        "is unknown). Traffic and the database were left as they are; a person must check the "
        "writer before changing anything."
    ),
}

# 트래픽 적음으로 통과한 JUDGE_PASSED 줄(data.lowTraffic = true)에 붙이는 문장.
LOW_TRAFFIC_PASS_SUFFIX = "(Low traffic: passed on the health check.)"


@dataclass(frozen=True)
class MigrationLogEntry:
    level: LogLevel
    message: str


_INFO, _WARN, _ERROR = LogLevel.INFO, LogLevel.WARN, LogLevel.ERROR
_C = MigrationLogCode

MIGRATION_LOG_ENTRIES: dict[MigrationLogCode, MigrationLogEntry] = {
    _C.DISASTER_TRIGGERED: MigrationLogEntry(
        _WARN, "A disaster affected the serving environment. Emergency migration plan requested."
    ),
    _C.PLAN_CREATED: MigrationLogEntry(_INFO, "Migration plan created. Waiting for approval."),
    _C.APPROVED: MigrationLogEntry(_INFO, "Migration plan approved. Execution starts now."),
    _C.SELF_APPROVED: MigrationLogEntry(
        _INFO, "Migration plan approved by its creator (self approval). Execution starts now."
    ),
    _C.REJECTED: MigrationLogEntry(_INFO, "Migration plan rejected."),
    _C.CANCELLED: MigrationLogEntry(_INFO, "Migration plan cancelled. Traffic was not changed."),
    _C.EXPIRED: MigrationLogEntry(_INFO, "Migration plan expired before it was approved."),
    _C.STALE: MigrationLogEntry(
        _WARN, "The service or its environments changed after this plan was made. Not executed."
    ),
    _C.ABORT_REQUESTED: MigrationLogEntry(_WARN, "Abort requested."),
    _C.STARTED: MigrationLogEntry(_INFO, "Migration started."),
    _C.PREFLIGHT_OK: MigrationLogEntry(
        _INFO, "Preflight passed: the target environment is healthy and runs the same image."
    ),
    _C.PREFLIGHT_FAILED: MigrationLogEntry(
        _ERROR, MIGRATION_ERROR_MESSAGES[MigrationErrorCode.PREFLIGHT_FAILED]
    ),
    _C.WEIGHT_APPLIED: MigrationLogEntry(_INFO, "Traffic weights applied."),
    _C.WEIGHT_APPLY_FAILED: MigrationLogEntry(
        _WARN, MIGRATION_ERROR_MESSAGES[MigrationErrorCode.TRAFFIC_APPLY_FAILED]
    ),
    _C.JUDGE_PASSED: MigrationLogEntry(
        _INFO,
        "Step judged healthy: the target handled the required requests within the 5xx limit "
        "and passed the health check.",
    ),
    # JUDGE_FAILED·DATA_SYNC_FAILED·ROLLBACK_STARTED·FAILED 는 원인 코드 문장을 붙여 쓴다.
    _C.JUDGE_FAILED: MigrationLogEntry(_WARN, "Step judged unhealthy."),
    _C.DATA_SYNC_OK: MigrationLogEntry(
        _INFO, "Data replication lag is within the rollback condition."
    ),
    _C.DATA_SYNC_FAILED: MigrationLogEntry(_WARN, "Data replication check failed."),
    _C.DATA_SYNC_SKIPPED: MigrationLogEntry(
        _WARN,
        "Emergency migration: the source database could not be reached, so the replication check "
        "was skipped. The database cutover may promote the target (approved by the operator).",
    ),
    _C.SUCCEEDED: MigrationLogEntry(
        _INFO, "Migration succeeded. The target environment now serves all traffic."
    ),
    _C.ROLLBACK_STARTED: MigrationLogEntry(
        _WARN, "Rolling back: sending all traffic to the source environment."
    ),
    _C.ROLLBACK_APPLIED: MigrationLogEntry(
        _INFO, "Rolled back. The source environment serves all traffic again."
    ),
    _C.ROLLBACK_APPLY_FAILED: MigrationLogEntry(
        _WARN, "The traffic router did not apply the rollback weights. Retrying."
    ),
    _C.ROLLBACK_FAILED: MigrationLogEntry(
        _ERROR, "Rollback failed: the traffic router did not apply the rollback weights."
    ),
    _C.FAILED: MigrationLogEntry(_ERROR, "Migration failed."),
    _C.COUNTERS_RESET: MigrationLogEntry(
        _INFO,
        "The entrypoint request counters went down (restart). Counting continues from the "
        "requests already counted.",
    ),
    _C.LOG_TRUNCATED: MigrationLogEntry(
        _INFO, "Log line limit reached. Further lines for this plan are not recorded."
    ),
    _C.SECURITY_GATE_PASSED: MigrationLogEntry(
        _INFO,
        "Security gate passed: required security controls passed or have an approved "
        "time-limited exception.",
    ),
    _C.SECURITY_GATE_BLOCKED: MigrationLogEntry(
        _WARN,
        "Security gate blocked this step: a required security control failed or could not be "
        "confirmed.",
    ),
    _C.DB_CUTOVER_JOB_CREATED: MigrationLogEntry(
        _INFO, "Database cutover job created. Waiting for it to finish."
    ),
    _C.DB_CUTOVER_JOB_FINISHED: MigrationLogEntry(_INFO, "Database cutover job finished."),
    _C.DB_CUTOVER_OK: MigrationLogEntry(
        _INFO,
        "Database cutover passed: the target is the database writer with no missing or "
        "duplicate rows.",
    ),
    # DB_CUTOVER_FAILED 는 원인 코드 문장을 붙여 쓴다.
    _C.DB_CUTOVER_FAILED: MigrationLogEntry(_ERROR, "Database cutover failed."),
    _C.DB_CUTOVER_MANUAL_REQUIRED: MigrationLogEntry(
        _WARN,
        "Traffic and the database were left as they are. A person must check the database writer.",
    ),
}

_WITH_CAUSE = frozenset(
    {_C.JUDGE_FAILED, _C.DATA_SYNC_FAILED, _C.ROLLBACK_STARTED, _C.FAILED, _C.DB_CUTOVER_FAILED}
)


def migration_log_message(
    code: MigrationLogCode,
    error_code: MigrationErrorCode | None = None,
    data: dict[str, Any] | None = None,
) -> str:
    message = MIGRATION_LOG_ENTRIES[code].message
    if code in _WITH_CAUSE and error_code is not None:
        return f"{message} {MIGRATION_ERROR_MESSAGES[error_code]}"
    if code is _C.JUDGE_PASSED and data is not None and data.get("lowTraffic") is True:
        return f"{message} {LOW_TRAFFIC_PASS_SUFFIX}"
    return message
