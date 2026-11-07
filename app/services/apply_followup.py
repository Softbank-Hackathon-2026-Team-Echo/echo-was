"""영속된 반영 후속 증거의 읽기 투영. DB·설정·외부 호출·재개를 하지 않는다."""

import re
from datetime import UTC, datetime
from typing import Any

from app.enums import AgentRunKind, AgentRunStatus
from app.models import AgentRun
from app.services.apply_commit_service import COMMIT_CLAIM_TTL

REPOSITORY_FINISHED_AT_KEY = "repositoryFinishedAt"
REPOSITORY_SKIP_REASON_KEY = "repositorySkipReason"
REDEPLOY_FINISHED_AT_KEY = "redeployFinishedAt"
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _phase(state: str, reason: str) -> dict[str, Any]:
    return {"state": state, "reasonCode": reason, "finishedAt": None}


def _applicable(run: AgentRun, parent_kind: AgentRunKind | None) -> bool | None:
    if parent_kind is not None:
        return parent_kind == AgentRunKind.CHANGE_PROPOSAL
    snapshot = getattr(run, "input_snapshot", None) or {}
    if isinstance(snapshot.get("diagnosis"), dict):
        return True
    if snapshot.get("schemaVersion") == 1 and snapshot.get("parentRunId"):
        return False
    return None


def project_apply_followup(
    run: AgentRun, *, now: datetime, parent_kind: AgentRunKind | None = None
) -> dict[str, Any]:
    """시각은 관측 시각, 기한은 보장값이 없으므로 null. claim TTL은 SLA가 아니다."""
    result = run.result or {}
    applicable = _applicable(run, parent_kind)
    repository = _phase("UNKNOWN", "STATE_UNAVAILABLE")
    redeploy = _phase("UNKNOWN", "STATE_UNAVAILABLE")
    if applicable is False:
        repository = _phase("NOT_REQUIRED", "NOT_DIAGNOSIS")
        redeploy = _phase("NOT_REQUIRED", "NOT_DIAGNOSIS")
    elif applicable:
        repository = _repository(run, result, now)
        redeploy = _redeploy(run, result, repository)
    claimed_at = _time(result.get("commitClaimedAt")) if applicable else None
    repository["startedAt"] = claimed_at
    repository["finishedAt"] = _time(result.get(REPOSITORY_FINISHED_AT_KEY)) if applicable else None
    redeploy["finishedAt"] = _time(result.get(REDEPLOY_FINISHED_AT_KEY)) if applicable else None
    times = [t for t in (claimed_at, repository["finishedAt"], redeploy["finishedAt"]) if t]
    return {
        "schemaVersion": 1,
        "applicable": applicable,
        "repository": repository,
        "redeploy": redeploy,
        "updatedAt": max(times) if times else None,
        "deadlineAt": None,
    }


def _repository(run: AgentRun, result: dict[str, Any], now: datetime) -> dict[str, Any]:
    if result.get("repoCommitted") is True:
        if (
            run.simulated
            or result.get("commitState") == "FAILED"
            or str(run.failure_code).startswith("REPO_")
            or not _SHA.fullmatch(str(result.get("appliedCommitSha")))
        ):
            return _phase("UNKNOWN", "STATE_UNAVAILABLE")
        return _phase("SUCCEEDED", "COMMITTED")
    code = run.failure_code or ""
    if result.get("commitState") == "FAILED" or code.startswith("REPO_"):
        if not _CODE.fullmatch(code):
            return _phase("UNKNOWN", "STATE_UNAVAILABLE")
        return _phase("UNKNOWN" if code == "REPO_WRITE_FAILED" else "FAILED", code)
    if run.status not in {
        AgentRunStatus.SUCCEEDED,
        AgentRunStatus.QUEUED,
        AgentRunStatus.CLAIMED,
        AgentRunStatus.RUNNING,
    }:
        return _phase("NOT_REQUIRED", "APPLY_NOT_SUCCEEDED")
    if run.status != AgentRunStatus.SUCCEEDED:
        return _phase("PENDING", "WAITING_FOR_APPLY")
    if run.simulated:
        return _phase("NOT_REQUIRED", "SIMULATED")
    if result.get(REPOSITORY_SKIP_REASON_KEY) == "COMMIT_DISABLED":
        return _phase("NOT_REQUIRED", "COMMIT_DISABLED")
    if result.get("commitState") == "IN_PROGRESS":
        started = _time(result.get("commitClaimedAt"))
        if started is not None and started <= now < started + COMMIT_CLAIM_TTL:
            return _phase("IN_PROGRESS", "CLAIMED")
        return _phase("UNKNOWN", "CLAIM_UNCONFIRMED")
    if result.get("commitClaimedAt") is not None:
        return _phase("UNKNOWN", "CALLBACK_INTERRUPTED")
    if result.get("commitState") is not None or result.get("repoCommitted") is not None:
        return _phase("UNKNOWN", "STATE_UNAVAILABLE")
    return _phase("PENDING", "AWAITING_CALLBACK")


def _redeploy(run: AgentRun, result: dict[str, Any], repository: dict[str, Any]) -> dict[str, Any]:
    request_id = result.get("redeployRequestId")
    rejected = result.get("redeployRejectedCode")
    if request_id and rejected:
        return _phase("UNKNOWN", "STATE_UNAVAILABLE")
    if request_id:
        return _phase("CREATED", "REQUEST_CREATED")
    if rejected:
        return (
            _phase("REJECTED", rejected)
            if _CODE.fullmatch(str(rejected))
            else _phase("UNKNOWN", "STATE_UNAVAILABLE")
        )
    if run.status not in {
        AgentRunStatus.SUCCEEDED,
        AgentRunStatus.QUEUED,
        AgentRunStatus.CLAIMED,
        AgentRunStatus.RUNNING,
    }:
        reason = (
            "REPOSITORY_NOT_CONFIRMED"
            if str(run.failure_code).startswith("REPO_")
            else "APPLY_NOT_SUCCEEDED"
        )
        return _phase("NOT_REQUIRED", reason)
    if "redeployRequestId" in result:
        return _phase("UNKNOWN", "STATE_UNAVAILABLE")
    return _phase(
        "PENDING",
        "AWAITING_LINK" if run.status == AgentRunStatus.SUCCEEDED else "WAITING_FOR_APPLY",
    )
