"""후속 읽기 계약. 실제 serializer, DB/외부 쓰기 없음."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.enums import AgentRunKind, AgentRunStatus
from app.models import AgentRun
from app.schemas.precheck import _apply_response

NOW = datetime(2026, 11, 7, 10, tzinfo=UTC)
NOT_DIAGNOSIS_FOLLOWUP = {
    "schemaVersion": 1,
    "applicable": False,
    "repository": {
        "state": "NOT_REQUIRED",
        "reasonCode": "NOT_DIAGNOSIS",
        "startedAt": None,
        "finishedAt": None,
    },
    "redeploy": {"state": "NOT_REQUIRED", "reasonCode": "NOT_DIAGNOSIS", "finishedAt": None},
    "updatedAt": None,
    "deadlineAt": None,
}


def make_run(**fields: object) -> AgentRun:
    values = {
        "id": uuid.UUID(int=1),
        "kind": AgentRunKind.APPLY_PATCH,
        "status": AgentRunStatus.SUCCEEDED,
        "parent_run_id": uuid.UUID(int=2),
        "simulated": False,
        "finished_at": NOW,
        "failure_code": None,
        "input_snapshot": {"diagnosis": {"sourceCommitSha": "b" * 40}},
        "result": {"appliedCommitSha": "a" * 40},
    }
    return AgentRun(**(values | fields))


def test_followup_finished_sandbox_still_waits_for_callback() -> None:
    run = make_run()
    payload = _apply_response(run).model_dump(mode="json", by_alias=True)
    assert payload["status"] == "SUCCEEDED"
    assert payload["finishedAt"] is not None
    assert payload["redeployRequestId"] is None
    followup = payload["followUp"]
    assert followup["schemaVersion"] == 1
    assert followup["applicable"] is True
    assert followup["repository"]["state"] == "PENDING"
    assert followup["redeploy"]["state"] == "PENDING"
    assert followup["deadlineAt"] is None
    assert followup["updatedAt"] is None
    # Sandbox deadline/finishedAt are deliberately not follow-up timestamps.
    assert followup["repository"]["finishedAt"] is None
    assert followup["redeploy"]["finishedAt"] is None


@pytest.mark.parametrize(
    ("fields", "applicable", "repo", "redeploy", "reason"),
    [
        ({"input_snapshot": {}}, None, "UNKNOWN", "UNKNOWN", "STATE_UNAVAILABLE"),
        (
            {"input_snapshot": {"schemaVersion": 1, "parentRunId": "p"}},
            False,
            "NOT_REQUIRED",
            "NOT_REQUIRED",
            "NOT_DIAGNOSIS",
        ),
        (
            {"status": AgentRunStatus.RUNNING, "finished_at": None},
            True,
            "PENDING",
            "PENDING",
            "WAITING_FOR_APPLY",
        ),
        (
            {"status": AgentRunStatus.FAILED, "failure_code": "RUNNER_ERROR"},
            True,
            "NOT_REQUIRED",
            "NOT_REQUIRED",
            "APPLY_NOT_SUCCEEDED",
        ),
        ({"simulated": True}, True, "NOT_REQUIRED", "PENDING", "SIMULATED"),
        (
            {"result": {"repositorySkipReason": "COMMIT_DISABLED"}},
            True,
            "NOT_REQUIRED",
            "PENDING",
            "COMMIT_DISABLED",
        ),
        ({"result": {"commitState": "DONE"}}, True, "UNKNOWN", "PENDING", "STATE_UNAVAILABLE"),
        (
            {"result": {"repoCommitted": True, "appliedCommitSha": "c" * 40}},
            True,
            "SUCCEEDED",
            "PENDING",
            "COMMITTED",
        ),
        (
            {
                "status": AgentRunStatus.FAILED,
                "failure_code": "REPO_PATCH_CONFLICT",
                "result": {"commitState": "FAILED"},
            },
            True,
            "FAILED",
            "NOT_REQUIRED",
            "REPO_PATCH_CONFLICT",
        ),
        (
            {
                "status": AgentRunStatus.FAILED,
                "failure_code": "REPO_WRITE_FAILED",
                "result": {"commitState": "FAILED"},
            },
            True,
            "UNKNOWN",
            "NOT_REQUIRED",
            "REPO_WRITE_FAILED",
        ),
    ],
)
def test_followup_repository_evidence_not_sandbox_success(
    fields: dict[str, object], applicable: bool | None, repo: str, redeploy: str, reason: str
) -> None:
    run = make_run(**fields)
    before = dict(run.result or {})
    response = _apply_response(run).model_dump(mode="json", by_alias=True)["followUp"]
    assert response["applicable"] is applicable
    assert response["repository"]["state"] == repo
    assert response["repository"]["reasonCode"] == reason
    assert response["redeploy"]["state"] == redeploy
    assert run.result == before


@pytest.mark.parametrize(
    ("state", "claimed", "expected", "reason"),
    [
        ("IN_PROGRESS", NOW.isoformat(), "IN_PROGRESS", "CLAIMED"),
        ("IN_PROGRESS", (NOW - timedelta(minutes=10)).isoformat(), "UNKNOWN", "CLAIM_UNCONFIRMED"),
        ("IN_PROGRESS", "not-time", "UNKNOWN", "CLAIM_UNCONFIRMED"),
        ("IN_PROGRESS", "2026-11-07T10:00:00", "UNKNOWN", "CLAIM_UNCONFIRMED"),
        ("IN_PROGRESS", (NOW + timedelta(seconds=1)).isoformat(), "UNKNOWN", "CLAIM_UNCONFIRMED"),
        (None, NOW.isoformat(), "UNKNOWN", "CALLBACK_INTERRUPTED"),
    ],
)
def test_followup_claim_is_observation_not_sla(
    state: str | None, claimed: str, expected: str, reason: str
) -> None:
    run = make_run(result={"commitState": state, "commitClaimedAt": claimed})
    response = _apply_response(run, now=NOW).model_dump(mode="json", by_alias=True)["followUp"]
    assert response["repository"]["state"] == expected
    assert response["repository"]["reasonCode"] == reason
    assert response["deadlineAt"] is None


@pytest.mark.parametrize(
    ("result", "expected", "reason"),
    [
        ({"redeployRequestId": str(uuid.UUID(int=9))}, "CREATED", "REQUEST_CREATED"),
        (
            {"redeployRequestId": None, "redeployRejectedCode": "INTERNAL_ERROR"},
            "REJECTED",
            "INTERNAL_ERROR",
        ),
        (
            {"redeployRequestId": None, "redeployRejectedCode": "MIGRATION_IN_PROGRESS"},
            "REJECTED",
            "MIGRATION_IN_PROGRESS",
        ),
        ({"redeployRequestId": None}, "UNKNOWN", "STATE_UNAVAILABLE"),
        (
            {"redeployRequestId": str(uuid.UUID(int=9)), "redeployRejectedCode": "INTERNAL_ERROR"},
            "UNKNOWN",
            "STATE_UNAVAILABLE",
        ),
    ],
)
def test_followup_redeploy_receipt_not_deployment_outcome(
    result: dict[str, object], expected: str, reason: str
) -> None:
    run = make_run(result=result)
    payload = _apply_response(run).model_dump(mode="json", by_alias=True)
    assert payload["followUp"]["redeploy"]["state"] == expected
    assert payload["followUp"]["redeploy"]["reasonCode"] == reason
    assert payload["redeployRequestId"] == result.get("redeployRequestId")


def test_followup_parent_and_standalone_apply_share_contract() -> None:
    from app.schemas.precheck import PrecheckApplyRunResponse

    run = make_run(result={"redeployRequestId": str(uuid.UUID(int=9))})
    standalone = PrecheckApplyRunResponse.from_run(run).model_dump(mode="json", by_alias=True)
    nested = _apply_response(run).model_dump(mode="json", by_alias=True)
    assert standalone["followUp"] == nested["followUp"]
    assert standalone["redeployRequestId"] == nested["redeployRequestId"]
    assert standalone["pullRequestUrl"] == nested["pullRequestUrl"]
    assert standalone["redeployRejectedCode"] == nested["redeployRejectedCode"]


def test_followup_parent_kind_resolves_legacy_without_new_persisted_fields() -> None:
    run = make_run(input_snapshot={})
    assert _apply_response(run, parent_kind=AgentRunKind.PRECHECK).follow_up.applicable is False
    assert (
        _apply_response(run, parent_kind=AgentRunKind.CHANGE_PROPOSAL).follow_up.applicable is True
    )


def test_followup_conflicting_repository_evidence_is_not_success() -> None:
    run = make_run(
        status=AgentRunStatus.FAILED,
        failure_code="REPO_WRITE_FAILED",
        result={"repoCommitted": True, "appliedCommitSha": "c" * 40, "commitState": "FAILED"},
    )
    followup = _apply_response(run).model_dump(mode="json", by_alias=True)["followUp"]
    assert followup["repository"]["state"] == "UNKNOWN"
    assert followup["repository"]["reasonCode"] == "STATE_UNAVAILABLE"
