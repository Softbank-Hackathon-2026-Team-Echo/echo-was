"""영속된 반영 후속 증거의 읽기 투영. DB·설정·외부 호출·재개를 하지 않는다."""

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from app.core import exceptions
from app.enums import AgentRunKind, AgentRunStatus
from app.models import AgentRun
from app.services.apply_commit_service import COMMIT_CLAIM_TTL

REPOSITORY_FINISHED_AT_KEY = "repositoryFinishedAt"
REPOSITORY_SKIP_REASON_KEY = "repositorySkipReason"
REDEPLOY_FINISHED_AT_KEY = "redeployFinishedAt"
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
# 접수 writer는 AppError.code 또는 INTERNAL_ERROR만 저장한다. 본문을 코드로 추측하지 않는다.
_REJECTION_CODES = frozenset(
    cls.code
    for cls in vars(exceptions).values()
    if isinstance(cls, type) and issubclass(cls, exceptions.AppError)
)


def parse_redeploy_rejected_code(value: Any) -> str | None:
    """공개 복사 칸과 상태 투영에서 같은 고정 오류 코드 집합을 사용한다."""
    if value is None:
        return None
    return value if isinstance(value, str) and value in _REJECTION_CODES else "UNKNOWN"


def parse_redeploy_request_id(value: Any) -> uuid.UUID | None:
    """저장된 표준 UUID만 읽는다. 공백 제거·하이픈 보정·새 ID 생성은 하지 않는다."""
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        return None
    return uuid.UUID(value)


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _claim_is_confirmed(started: datetime | None, now: datetime) -> bool:
    """표현 불가능한 UTC/TTL 경계는 만료 여부도 불명이다. 시각을 클램프하지 않는다."""
    if started is None or now.tzinfo is None:
        return False
    try:
        observed = now.astimezone(UTC)
        expires = started + COMMIT_CLAIM_TTL
        return started <= observed < expires
    except (ValueError, OverflowError):
        return False


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
        if _claim_is_confirmed(started, now):
            return _phase("IN_PROGRESS", "CLAIMED")
        return _phase("UNKNOWN", "CLAIM_UNCONFIRMED")
    if result.get("commitClaimedAt") is not None:
        return _phase("UNKNOWN", "CALLBACK_INTERRUPTED")
    if result.get("commitState") is not None or result.get("repoCommitted") is not None:
        return _phase("UNKNOWN", "STATE_UNAVAILABLE")
    return _phase("PENDING", "AWAITING_CALLBACK")


def _redeploy(run: AgentRun, result: dict[str, Any], repository: dict[str, Any]) -> dict[str, Any]:
    raw_request_id = result.get("redeployRequestId")
    request_id = parse_redeploy_request_id(raw_request_id)
    rejected = parse_redeploy_rejected_code(result.get("redeployRejectedCode"))
    if raw_request_id is not None and (request_id is None or rejected is not None):
        return _phase("UNKNOWN", "STATE_UNAVAILABLE")
    if request_id is not None:
        return _phase("CREATED", "REQUEST_CREATED")
    if rejected:
        return (
            _phase("REJECTED", rejected)
            if rejected != "UNKNOWN"
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
