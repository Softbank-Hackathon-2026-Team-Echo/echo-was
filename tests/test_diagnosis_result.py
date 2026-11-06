"""배포 실패 진단(CHANGE_PROPOSAL) 결과 검사·저장 모양·가짜 진단 단위 시험(SPEC api-98). DB 없음."""

import json
from typing import Any

import pytest

from app.core.exceptions import ResultInvalidError
from app.core.log_masking import MASK
from app.enums import DiagnosisResult
from app.schemas.runner import DiagnosisRunResultRequest, parse_runner_payload
from app.services.diagnosis_service import (
    SIMULATED_CAUSE,
    SIMULATED_FILE,
    SIMULATED_LINE,
    DiagnosisSimulate,
    simulated_runner_payload,
)
from app.services.run_result import (
    DIAGNOSIS_MISSING_REASON,
    compute_patch_hash,
)

DIFF = "--- a/app/static/index.html\n+++ b/app/static/index.html\n@@ -1 +1 @@\n-a(\n+a()\n"


def _patch(**changes: Any) -> dict[str, Any]:
    return {
        "id": "fix-1",
        "patchHash": compute_patch_hash(DIFF),
        "files": ["app/static/index.html"],
        "diff": DIFF,
        "reason": "close paren",
        "touchesProtectedFiles": False,
        "checks": [{"name": "BUILD", "result": "PASS", "summary": "ok"}],
        "origin": "AI",
    } | changes


def _diagnosis(**changes: Any) -> dict[str, Any]:
    return {
        "result": "DIAGNOSED",
        "category": "CODE",
        "cause": "paren not closed",
        "fixable": True,
        "unfixableReason": None,
        "evidence": [
            {"source": "LOG", "line": 42, "text": "SyntaxError: Unexpected token"},
            {"source": "FILE", "path": "app/static/index.html", "line": 120, "text": "a("},
        ],
    } | changes


def _body(**changes: Any) -> dict[str, Any]:
    return {
        "status": "SUCCEEDED",
        "failureCode": None,
        "simulated": False,
        "sandbox": None,
        "model": None,
        "configDiff": [],
        "featureGaps": [],
        "findings": [],
        "patches": [_patch()],
        "blockedAttempts": [],
        "diagnosis": _diagnosis(),
    } | changes


def _parse(body: dict[str, Any]) -> DiagnosisRunResultRequest:
    return parse_runner_payload(json.dumps(body).encode(), DiagnosisRunResultRequest)


def test_diagnosis_result_is_stored_with_patches() -> None:
    normalized = _parse(_body()).to_normalized()

    stored = normalized.result["diagnosis"]
    assert stored["result"] == "DIAGNOSED"
    assert stored["category"] == "CODE"
    assert stored["fixable"] is True
    assert [item["source"] for item in stored["evidence"]] == ["LOG", "FILE"]
    assert [patch["id"] for patch in normalized.result["patches"]] == ["fix-1"]


def test_missing_diagnosis_becomes_insufficient_evidence() -> None:
    normalized = _parse(_body(patches=[], diagnosis=None)).to_normalized()

    assert normalized.result["diagnosis"] == {
        "result": "INSUFFICIENT_EVIDENCE",
        "category": None,
        "cause": None,
        "fixable": False,
        "unfixableReason": DIAGNOSIS_MISSING_REASON,
        "evidence": [],
    }


def test_diagnosis_strings_are_masked() -> None:
    secret = "ghp_" + "A" * 36
    body = _body(diagnosis=_diagnosis(cause=f"token {secret} leaked"))

    stored = _parse(body).to_normalized().result["diagnosis"]

    assert secret not in stored["cause"]
    assert MASK in stored["cause"]


@pytest.mark.parametrize(
    "body",
    [
        _body(diagnosis=None),  # 패치는 진단 없이 못 붙인다
        _body(diagnosis=_diagnosis(fixable=False, unfixableReason="no")),  # 고칠 수 없는데 패치
        _body(patches=[], diagnosis=_diagnosis(category=None)),  # DIAGNOSED 인데 분류 없음
        _body(patches=[], diagnosis=_diagnosis(result="INSUFFICIENT_EVIDENCE")),  # fixable 참
        _body(patches=[], diagnosis=_diagnosis(fixable=False)),  # 사유 없음
        _body(
            patches=[],
            diagnosis=_diagnosis(evidence=[{"source": "FILE", "line": 1, "text": "x"}]),
        ),  # FILE 인데 path 없음
        _body(diagnosis=_diagnosis(unknown=1)),  # 모르는 칸
    ],
)
def test_invalid_diagnosis_is_rejected(body: dict[str, Any]) -> None:
    with pytest.raises(ResultInvalidError):
        _parse(body)


def test_simulated_payload_passes_the_runner_schema() -> None:
    """가짜 진단도 러너 결과와 같은 검사를 통과한다: 120줄 괄호 원인 + AI 패치 1개."""
    request = _parse(simulated_runner_payload(DiagnosisSimulate()))
    normalized = request.to_normalized()

    stored = normalized.result["diagnosis"]
    assert stored["cause"] == SIMULATED_CAUSE
    assert {"source": "FILE", "path": SIMULATED_FILE, "line": SIMULATED_LINE} == {
        key: stored["evidence"][1][key] for key in ("source", "path", "line")
    }
    assert [patch["origin"] for patch in normalized.result["patches"]] == ["AI"]
    assert normalized.simulated is True


def test_simulated_insufficient_evidence_has_no_patch() -> None:
    payload = simulated_runner_payload(DiagnosisSimulate(DiagnosisResult.INSUFFICIENT_EVIDENCE))
    normalized = _parse(payload).to_normalized()

    assert normalized.result["patches"] == []
    assert normalized.result["diagnosis"]["fixable"] is False
    assert normalized.result["diagnosis"]["unfixableReason"]
