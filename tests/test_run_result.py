"""러너 결과 검사·패치 상태 재계산 단위 시험(SPEC api-runner-prechecks AC-9, AC-8 형식). DB 없음."""

import json
from collections.abc import Callable
from typing import Any

import pytest

from app.core.exceptions import ResultInvalidError
from app.core.log_masking import MASK
from app.enums import (
    AgentPatchBlockReason,
    AgentPatchDecision,
    AgentPatchStatus,
    AgentRunDataClass,
    AgentRunOrigin,
    AgentRunStatus,
)
from app.schemas.runner import (
    MAX_BODY_BYTES,
    MAX_DIFF_BYTES,
    MAX_FINDINGS,
    MAX_PATCHES,
    ApplyPatchResultRequest,
    RunResultRequest,
    SubmitRunEventsRequest,
    openapi_request_body,
    parse_runner_payload,
)
from app.services.run_result import (
    NormalizedRunResult,
    compute_patch_hash,
    compute_patch_status,
    patch_origin,
)

ENVIRONMENT_ID = "11111111-1111-1111-1111-111111111111"
AT = "2026-11-05T00:00:00Z"
PASSED_CHECK = {
    "name": "TESTS",
    "result": "PASS",
    "toolVersion": "pytest 8",
    "summary": "19 passed",
}


def _result(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "status": "SUCCEEDED",
        "failureCode": None,
        "simulated": False,
        "sandbox": None,
        "model": None,
        "configDiff": [
            {
                "key": "region",
                "source": "ap-northeast-1",
                "target": "asia-northeast3",
                "origin": "RULE",
                "severity": "INFO",
            }
        ],
        "featureGaps": [],
        "findings": [_finding()],
        "patches": [],
        "blockedAttempts": [],
    }
    body.update(changes)
    return body


def _finding(**changes: Any) -> dict[str, Any]:
    return {
        "control": "config-diff",
        "environmentId": ENVIRONMENT_ID,
        "result": "WARN",
        "title": "config differs",
        "reason": "region differs",
        "origin": "RULE",
        "suggestedFix": None,
    } | changes


def _patch(**changes: Any) -> dict[str, Any]:
    """patchHash 를 주지 않으면 diff 의 해시다(PD-14)."""
    body = {
        "id": "p1",
        "files": ["Dockerfile"],
        "diff": "--- a/Dockerfile\n+++ b/Dockerfile\n",
        "reason": "base image",
        "touchesProtectedFiles": False,
        "checks": [PASSED_CHECK],
    } | changes
    body.setdefault("patchHash", compute_patch_hash(body["diff"]))
    return body


def _parse(body: dict[str, Any]) -> RunResultRequest:
    return parse_runner_payload(json.dumps(body).encode(), RunResultRequest)


def _normalize(body: dict[str, Any]) -> NormalizedRunResult:
    return _parse(body).to_normalized()


def _rejected(body: dict[str, Any]) -> list[tuple[str, str]]:
    with pytest.raises(ResultInvalidError) as caught:
        _parse(body)
    return [(item.field, item.reason) for item in caught.value.details or []]


def _none_processing(**changes: Any) -> dict[str, Any]:
    return {
        "location": "gcp/asia-northeast3",
        "sandbox": True,
        "modelProvider": "NONE",
        "model": None,
        "modelRegion": None,
        "dataClasses": [],
        "externalTransfer": False,
        "externalTransferBytes": None,
    } | changes


def _model_processing(**changes: Any) -> dict[str, Any]:
    return {
        "location": "aws/ap-northeast-1",
        "sandbox": False,
        "modelProvider": "AWS_BEDROCK",
        "model": "anthropic.claude:1/v@2",
        "modelRegion": "ap-northeast-1",
        "dataClasses": ["CONFIG"],
        "externalTransfer": True,
        "externalTransferBytes": 0,
    } | changes


def test_processing_defaults_to_none_when_omitted() -> None:
    assert _normalize(_result()).processing is None
    assert _normalize(_result(processing=None)).processing is None


@pytest.mark.parametrize(
    "processing",
    [
        _none_processing(),
        _none_processing(location="onprem/seoul-dc1", externalTransferBytes=0),
        _model_processing(),
        _model_processing(modelProvider="LOCAL", externalTransfer=False, externalTransferBytes=0),
        _model_processing(modelRegion=None, externalTransferBytes=None),
        _model_processing(location="a" * 16 + "/" + "b" * 40, model="m" * 64, modelRegion="r" * 40),
    ],
    ids=["none", "onprem", "bedrock", "local", "unknown-region", "at-limits"],
)
def test_processing_accepts_valid_shapes_as_is(processing: dict[str, Any]) -> None:
    assert _normalize(_result(processing=processing)).processing == processing


def test_processing_sorts_data_classes_in_enum_order() -> None:
    every_class = ["SECRETS", "LOGS", "SOURCE_CODE", "FINDINGS", "CONFIG"]

    normalized = _normalize(_result(processing=_model_processing(dataClasses=every_class)))

    assert normalized.processing is not None
    assert normalized.processing["dataClasses"] == list(AgentRunDataClass)
    assert "processing" not in normalized.result


def test_processing_masks_secret_shaped_model_name() -> None:
    secret = "sk-" + "a" * 24

    normalized = _normalize(_result(processing=_model_processing(model=secret)))

    assert normalized.processing is not None
    assert normalized.processing["model"] == MASK


@pytest.mark.parametrize(
    ("build", "changes"),
    [
        (_none_processing, {"unknown": 1}),
        (_none_processing, {"externalTransfer": "false"}),
        (_none_processing, {"sandbox": 1}),
        (_none_processing, {"modelProvider": "GEMINI"}),
        (_none_processing, {"model": "m"}),
        (_none_processing, {"modelRegion": "ap-northeast-1"}),
        (_none_processing, {"dataClasses": ["CONFIG"]}),
        (_none_processing, {"externalTransfer": True}),
        (_none_processing, {"externalTransferBytes": 1}),
        (_none_processing, {"location": ""}),
        (_none_processing, {"location": "gcp"}),
        (_none_processing, {"location": "GCP/asia-northeast3"}),
        (_none_processing, {"location": "gcp/asia_northeast3"}),
        (_none_processing, {"location": "1gcp/asia-northeast3"}),
        (_none_processing, {"location": "gcp/asia-northeast3\n"}),
        (_none_processing, {"location": "a" * 17 + "/r"}),
        (_none_processing, {"location": "gcp/" + "r" * 41}),
        (_model_processing, {"model": None}),
        (_model_processing, {"model": ""}),
        (_model_processing, {"model": "has space"}),
        (_model_processing, {"model": "m" * 65}),
        (_model_processing, {"modelRegion": "AP-NORTHEAST-1"}),
        (_model_processing, {"modelRegion": ""}),
        (_model_processing, {"modelRegion": "r" * 41}),
        (_model_processing, {"dataClasses": ["CONFIG", "CONFIG"]}),
        (_model_processing, {"dataClasses": ["PASSWORDS"]}),
        (_model_processing, {"dataClasses": ["CONFIG"] * 6}),
        (_model_processing, {"externalTransferBytes": -1}),
        (_model_processing, {"externalTransferBytes": 1.5}),
        (_model_processing, {"externalTransferBytes": "1"}),
        (_model_processing, {"externalTransferBytes": True}),
        (_model_processing, {"externalTransferBytes": 2**63}),
        (_model_processing, {"externalTransfer": False, "externalTransferBytes": 5}),
    ],
)
def test_processing_rejects_rule_violations(
    build: Callable[..., dict[str, Any]], changes: dict[str, Any]
) -> None:
    fields = _rejected(_result(processing=build(**changes)))

    assert fields
    assert all(field.startswith("processing") for field, _ in fields)


@pytest.mark.parametrize(
    "missing", ["location", "sandbox", "modelProvider", "dataClasses", "externalTransfer"]
)
def test_processing_requires_field(missing: str) -> None:
    body = _none_processing()
    del body[missing]

    assert _rejected(_result(processing=body))


def test_processing_rejects_snake_case_names() -> None:
    body = _none_processing()
    body["model_provider"] = body.pop("modelProvider")

    assert _rejected(_result(processing=body))


@pytest.mark.parametrize(
    "stored_status",
    [AgentPatchStatus.PROPOSED, AgentPatchStatus.BLOCKED],
    ids=["proposed", "blocked"],
)
def test_patch_status_rejected_comes_first(stored_status: AgentPatchStatus) -> None:
    status = compute_patch_status(stored_status, AgentPatchDecision.REJECTED, None)

    assert status is AgentPatchStatus.REJECTED


def test_patch_status_blocked_without_decision_is_blocked() -> None:
    status = compute_patch_status(AgentPatchStatus.BLOCKED, None, None)

    assert status is AgentPatchStatus.BLOCKED


def test_patch_status_proposed_without_decision_is_proposed() -> None:
    status = compute_patch_status(AgentPatchStatus.PROPOSED, None, None)

    assert status is AgentPatchStatus.PROPOSED


@pytest.mark.parametrize(
    "apply_status",
    [None, AgentRunStatus.QUEUED, AgentRunStatus.CLAIMED, AgentRunStatus.RUNNING],
    ids=["no-run", "queued", "claimed", "running"],
)
def test_patch_status_approved_without_finished_apply_is_approved(
    apply_status: AgentRunStatus | None,
) -> None:
    status = compute_patch_status(
        AgentPatchStatus.PROPOSED, AgentPatchDecision.APPROVED, apply_status
    )

    assert status is AgentPatchStatus.APPROVED


def test_patch_status_approved_with_succeeded_apply_is_applied() -> None:
    status = compute_patch_status(
        AgentPatchStatus.PROPOSED, AgentPatchDecision.APPROVED, AgentRunStatus.SUCCEEDED
    )

    assert status is AgentPatchStatus.APPLIED


@pytest.mark.parametrize(
    "apply_status",
    [AgentRunStatus.FAILED, AgentRunStatus.TIMED_OUT, AgentRunStatus.CANCELLED],
    ids=["failed", "timed-out", "cancelled"],
)
def test_patch_status_approved_with_unsuccessful_apply_is_apply_failed(
    apply_status: AgentRunStatus,
) -> None:
    status = compute_patch_status(
        AgentPatchStatus.PROPOSED, AgentPatchDecision.APPROVED, apply_status
    )

    assert status is AgentPatchStatus.APPLY_FAILED


def test_patch_origin_defaults_to_ai_when_absent() -> None:
    assert patch_origin({}) is AgentRunOrigin.AI
    assert patch_origin({"origin": "RULE"}) is AgentRunOrigin.RULE


def test_result_patch_origin_missing_is_stored_as_ai_and_status_stays_recalculated() -> None:
    normalized = _normalize(_result(patches=[_patch(), _patch(id="p2", origin="RULE")]))

    patches = normalized.result["patches"]
    assert [(item["origin"], item["status"]) for item in patches] == [
        ("AI", "PROPOSED"),
        ("RULE", "PROPOSED"),
    ]


def test_result_validation_accepts_contract_shape_and_null_model() -> None:
    normalized = _normalize(_result())

    assert normalized.status is AgentRunStatus.SUCCEEDED
    assert normalized.failure_code is None
    assert normalized.simulated is False
    assert normalized.model_id is None
    assert normalized.is_ai_reviewed is False
    assert normalized.result["configDiff"][0]["key"] == "region"
    # status·failureCode·simulated 는 칸으로만 둔다.
    assert not {"status", "failureCode", "simulated"} & set(normalized.result)


def test_result_validation_keeps_model_columns() -> None:
    model = {"id": "claude-opus-5-5", "effort": "medium", "promptVersion": "v1", "refusal": False}

    normalized = _normalize(_result(model=model))

    assert (normalized.model_id, normalized.prompt_version) == ("claude-opus-5-5", "v1")
    assert normalized.is_ai_reviewed is True
    assert normalized.result["model"] == model


@pytest.mark.parametrize(
    ("source", "target"),
    [(None, "b"), ("a", None), (1, 2), (["A"], ["B"])],
)
def test_result_validation_accepts_any_json_diff_values(source: Any, target: Any) -> None:
    diff = {"key": "k", "source": source, "target": target, "origin": "RULE", "severity": "WARN"}

    assert _normalize(_result(configDiff=[diff])).result["configDiff"][0]["source"] == source


def test_result_validation_rejects_unknown_field() -> None:
    assert _rejected(_result(extra="nope")) == [("extra", "Extra inputs are not permitted")]
    assert _rejected(_result(findings=[_finding(status="x")]))[0][0] == "findings.0.status"


def test_result_validation_rejects_snake_case_names() -> None:
    body = _result()
    body["failure_code"] = body.pop("failureCode")

    assert ("failure_code", "Extra inputs are not permitted") in _rejected(body)


def test_result_validation_requires_simulated() -> None:
    body = _result()
    del body["simulated"]

    assert _rejected(body) == [("simulated", "Field required")]


@pytest.mark.parametrize("result", ["WARN", "FAIL", "UNKNOWN"])
def test_result_validation_accepts_ai_finding_without_pass(result: str) -> None:
    normalized = _normalize(_result(findings=[_finding(origin="AI", result=result)]))

    assert normalized.result["findings"][0]["confirmation"] == "UNCONFIRMED"


def test_result_validation_rejects_ai_pass_and_accepts_rule_pass() -> None:
    ((field, reason),) = _rejected(_result(findings=[_finding(origin="AI", result="PASS")]))

    assert field == "findings.0"
    assert "AI finding cannot be PASS" in reason
    normalized = _normalize(_result(findings=[_finding(origin="RULE", result="PASS")]))
    assert normalized.result["findings"][0]["confirmation"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"findings": [_finding() for _ in range(MAX_FINDINGS + 1)]},
        {"patches": [_patch(id=f"p{index}") for index in range(MAX_PATCHES + 1)]},
        {
            "configDiff": [
                {"key": "k", "source": None, "target": None, "origin": "RULE", "severity": "INFO"}
            ]
            * 201
        },
        {
            "featureGaps": [
                {
                    "source": "a",
                    "target": "b",
                    "gap": "c",
                    "action": "d",
                    "hasReplacement": True,
                    "severity": "INFO",
                    "origin": "AI",
                }
            ]
            * 101
        },
        {"blockedAttempts": [{"action": "a", "target": "b", "at": AT, "reason": "c"}] * 51},
        {"patches": [_patch(diff="x" * (MAX_DIFF_BYTES + 1))]},
        {"findings": [_finding(title="x" * 2001)]},
    ],
    ids=["findings", "patches", "configDiff", "featureGaps", "blockedAttempts", "diff", "string"],
)
def test_result_validation_rejects_over_limits(changes: dict[str, Any]) -> None:
    assert _rejected(_result(**changes))


def test_result_validation_accepts_diff_at_limit() -> None:
    assert _normalize(_result(patches=[_patch(diff="x" * MAX_DIFF_BYTES)])).result["patches"]


def test_result_validation_rejects_body_over_one_megabyte() -> None:
    raw = b" " * (MAX_BODY_BYTES + 1)

    with pytest.raises(ResultInvalidError) as caught:
        parse_runner_payload(raw, RunResultRequest)

    assert [(item.field, item.reason) for item in caught.value.details or []] == [
        ("body", "BODY_TOO_LARGE")
    ]


def test_result_validation_rejects_malformed_json_without_echoing_input() -> None:
    with pytest.raises(ResultInvalidError) as caught:
        parse_runner_payload(b'{"status": "secret-value', RunResultRequest)

    assert "secret-value" not in repr(caught.value.details)


def test_result_validation_rejects_nul_character() -> None:
    assert _rejected(_result(findings=[_finding(title="a\x00b")])) == [("body", "NUL_CHARACTER")]


def test_result_validation_succeeded_with_failure_code_is_rejected() -> None:
    ((_, reason),) = _rejected(_result(failureCode="BOOM"))

    assert "failureCode must be null" in reason


def test_result_validation_failed_requires_failure_code() -> None:
    ((_, reason),) = _rejected(_result(status="FAILED"))

    assert "failureCode is required" in reason
    normalized = _normalize(_result(status="FAILED", failureCode="RUNNER_ERROR"))
    assert (normalized.status, normalized.failure_code) == (AgentRunStatus.FAILED, "RUNNER_ERROR")


@pytest.mark.parametrize("code", ["lower", "1BAD", "A" * 33, "HAS SPACE", ""])
def test_result_validation_rejects_bad_failure_code(code: str) -> None:
    assert _rejected(_result(status="FAILED", failureCode=code))


@pytest.mark.parametrize("status", ["QUEUED", "RUNNING", "TIMED_OUT", "CANCELLED"])
def test_result_validation_rejects_other_statuses(status: str) -> None:
    assert _rejected(_result(status=status))


def test_result_validation_rejects_duplicate_patch_ids() -> None:
    ((_, reason),) = _rejected(_result(patches=[_patch(), _patch()]))

    assert "patch id must be unique" in reason


@pytest.mark.parametrize(
    ("patch", "status", "block_reason"),
    [
        (_patch(), AgentPatchStatus.PROPOSED, None),
        (
            _patch(touchesProtectedFiles=True),
            AgentPatchStatus.BLOCKED,
            AgentPatchBlockReason.PROTECTED_FILES_TOUCHED,
        ),
        (
            _patch(checks=[PASSED_CHECK, {"name": "TRIVY", "result": "FAIL"}]),
            AgentPatchStatus.BLOCKED,
            AgentPatchBlockReason.CHECK_NOT_PASSED,
        ),
        (_patch(checks=[]), AgentPatchStatus.BLOCKED, AgentPatchBlockReason.CHECKS_MISSING),
    ],
    ids=["proposed", "protected", "check-failed", "no-checks"],
)
def test_result_patch_status_is_recalculated(
    patch: dict[str, Any], status: AgentPatchStatus, block_reason: AgentPatchBlockReason | None
) -> None:
    stored = _normalize(_result(patches=[patch])).result["patches"][0]

    assert stored["status"] == status
    assert stored["blockReason"] == block_reason


def test_result_rejects_runner_supplied_patch_status() -> None:
    body = _result(patches=[_patch(status="PROPOSED", touchesProtectedFiles=True)])

    assert _rejected(body) == [("patches.0.status", "Extra inputs are not permitted")]


def test_result_blocked_attempts_are_stored_unconfirmed() -> None:
    attempt = {"action": "POST /x", "target": "service a", "at": AT, "reason": "AGENT_READ_ONLY"}

    stored = _normalize(_result(blockedAttempts=[attempt])).result["blockedAttempts"][0]

    assert (stored["auditLogId"], stored["confirmed"]) == (None, False)


@pytest.mark.parametrize("prefix", ["echo_run_", "echo_rnr_", "echo_agt_", "ghp_"])
def test_result_normalized_masks_token_shaped_strings(prefix: str) -> None:
    token = prefix + "Ab1" * 10

    normalized = _normalize(
        _result(
            findings=[_finding(reason=f"leaked {token} here", suggestedFix=f"rotate {token}")],
            patches=[_patch(diff=f"+TOKEN={token}\n")],
        )
    )

    dumped = json.dumps(normalized.result)
    assert token not in dumped
    assert normalized.result["findings"][0]["reason"] == f"leaked {MASK} here"


@pytest.mark.parametrize(
    "model", [RunResultRequest, SubmitRunEventsRequest, ApplyPatchResultRequest]
)
def test_openapi_request_body_has_no_dangling_references(
    model: type[RunResultRequest] | type[SubmitRunEventsRequest] | type[ApplyPatchResultRequest],
) -> None:
    body = openapi_request_body(model)

    assert "$ref" not in json.dumps(body)
    assert "$defs" not in json.dumps(body)
    assert body["requestBody"]["content"]["application/json"]["schema"]["properties"]


def _events_body(**changes: Any) -> dict[str, Any]:
    event = {
        "seq": 1,
        "at": AT,
        "step": "SANDBOX_CREATE",
        "level": "INFO",
        "message": "sandbox created",
    } | changes
    return {"events": [event]}


def test_events_validation_accepts_contract_shape() -> None:
    parsed = parse_runner_payload(json.dumps(_events_body()).encode(), SubmitRunEventsRequest)

    assert parsed.to_commands()[0].seq == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"seq": 0},
        {"seq": 2_147_483_648},
        {"message": "x" * 201},
        {"message": ""},
        {"step": "NOPE"},
        {"level": "DEBUG"},
        {"at": "2026-11-05T00:00:00"},
        {"unknown": 1},
        {"data": {"big": "x" * 2001}},
        {"data": ["not", "object"]},
    ],
    ids=[
        "seq-zero",
        "seq-too-big",
        "message-201",
        "message-empty",
        "step",
        "level",
        "naive-at",
        "unknown",
        "data-too-big",
        "data-not-object",
    ],
)
def test_events_validation_rejects(changes: dict[str, Any]) -> None:
    raw = json.dumps(_events_body(**changes)).encode()

    with pytest.raises(ResultInvalidError):
        parse_runner_payload(raw, SubmitRunEventsRequest)


@pytest.mark.parametrize("count", [0, 101])
def test_events_validation_rejects_batch_size(count: int) -> None:
    event = _events_body()["events"][0]
    raw = json.dumps({"events": [event] * count}).encode()

    with pytest.raises(ResultInvalidError):
        parse_runner_payload(raw, SubmitRunEventsRequest)


# --- 패치 해시(PD-14)·반영 결과(PD-9, SPEC api-10-2) ---

APPLY_DIFF = "--- a/Dockerfile\n+++ b/Dockerfile\n@@ -1 +1 @@\n-FROM a\n+FROM b\n"
COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"


def test_compute_patch_hash_is_sha256_of_utf8_diff() -> None:
    assert compute_patch_hash("abc") == (
        "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
    assert compute_patch_hash("한글") == compute_patch_hash("한글")
    assert compute_patch_hash("a") != compute_patch_hash("a\n")


def test_patch_hash_that_is_not_hash_of_diff_is_rejected() -> None:
    body = _result(patches=[_patch(patchHash="sha256:" + "a" * 64)])

    ((field, reason),) = _rejected(body)

    assert field == "patches.0"
    assert "patchHash must be sha256 of diff" in reason


def test_patch_hash_is_checked_against_raw_diff_before_masking() -> None:
    token = "ghp_" + "A" * 36
    normalized = _normalize(_result(patches=[_patch(diff=f"+TOKEN={token}\n")]))

    stored = normalized.result["patches"][0]
    assert stored["patchHash"] == compute_patch_hash(f"+TOKEN={token}\n")
    assert token not in stored["diff"]


def _apply_body(**changes: Any) -> dict[str, Any]:
    return {
        "status": "SUCCEEDED",
        "failureCode": None,
        "simulated": False,
        "patchHash": compute_patch_hash(APPLY_DIFF),
        "appliedCommitSha": COMMIT_SHA,
        "branch": "applied/11111111-1111-1111-1111-111111111111",
    } | changes


def _parse_apply(body: dict[str, Any]) -> ApplyPatchResultRequest:
    return parse_runner_payload(json.dumps(body).encode(), ApplyPatchResultRequest)


def _apply_rejected(body: dict[str, Any]) -> list[tuple[str, str]]:
    with pytest.raises(ResultInvalidError) as caught:
        _parse_apply(body)
    return [(item.field, item.reason) for item in caught.value.details or []]


def test_apply_result_is_normalized_into_columns_and_result() -> None:
    normalized = _parse_apply(_apply_body(simulated=True)).to_normalized()

    assert normalized.status is AgentRunStatus.SUCCEEDED
    assert (normalized.failure_code, normalized.simulated) == (None, True)
    assert normalized.result == {
        "patchHash": compute_patch_hash(APPLY_DIFF),
        "appliedCommitSha": COMMIT_SHA,
        "branch": "applied/11111111-1111-1111-1111-111111111111",
    }
    assert normalized.applied_commit_sha == COMMIT_SHA


def test_apply_result_failed_may_omit_commit_and_branch() -> None:
    body = _apply_body(status="FAILED", failureCode="BUILD_FAILED")
    del body["appliedCommitSha"], body["branch"]

    normalized = _parse_apply(body).to_normalized()

    assert normalized.status is AgentRunStatus.FAILED
    assert normalized.result["appliedCommitSha"] is None
    assert normalized.result["branch"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"appliedCommitSha": None},
        {"branch": None},
        {"failureCode": "BOOM"},
        {"status": "FAILED"},
        {"status": "TIMED_OUT"},
        {"appliedCommitSha": "ABC"},
        {"appliedCommitSha": COMMIT_SHA[:39]},
        {"patchHash": "sha256:abc"},
        {"branch": ""},
        {"simulated": None},
        {"checks": []},
        {"applied_commit_sha": COMMIT_SHA},
    ],
    ids=[
        "success-without-commit",
        "success-without-branch",
        "success-with-failure-code",
        "failed-without-failure-code",
        "status-not-allowed",
        "commit-uppercase",
        "commit-short",
        "hash-format",
        "branch-empty",
        "simulated-missing",
        "unknown-field",
        "snake-case",
    ],
)
def test_apply_result_shape_violations_are_rejected(changes: dict[str, Any]) -> None:
    assert _apply_rejected(_apply_body(**changes))


def test_apply_result_requires_patch_hash_and_simulated() -> None:
    for key in ("patchHash", "simulated", "status"):
        body = _apply_body()
        del body[key]
        assert _apply_rejected(body)
