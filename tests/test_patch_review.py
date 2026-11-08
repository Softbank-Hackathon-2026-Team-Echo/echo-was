"""Runner/WAS golden contract and adversarial evidence validation."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from app.core.review_evidence import REVIEW_POLICY, approval_digest, fingerprint, review_context
from app.schemas.runner import RunPatchRequest
from app.services.patch_review import validate_apply_review, validate_patch_review


def reviewed_patch(commit: str = "1" * 40) -> dict[str, Any]:
    patch: dict[str, Any] = json.loads(
        (Path(__file__).parent / "fixtures/review/approved-patch.json").read_text()
    )
    # Synthetic reviewer response for this test commit, not a real model attestation.
    evidence = patch["reviewEvidence"]
    evidence["sourceCommitSha"] = commit
    evidence["review"]["contextHash"] = fingerprint(review_context(evidence, patch["diff"]))
    evidence["evidenceHash"] = fingerprint(
        {k: v for k, v in evidence.items() if k != "evidenceHash"}
    )
    return patch


def snapshot() -> dict[str, Any]:
    return {"reviewPolicy": REVIEW_POLICY, "deployRequest": {"commitSha": "1" * 40}}


def test_golden_contract_accepted_and_normalized() -> None:
    patch = reviewed_patch()
    request = RunPatchRequest.model_validate(patch)
    assert request.review_evidence == patch["reviewEvidence"]
    validate_patch_review(snapshot(), patch)


@pytest.mark.parametrize(
    "change", ["absent", "version", "commit", "path", "verdict", "unknown", "null", "nul", "huge"]
)
def test_evidence_rejected(change: str) -> None:
    patch = reviewed_patch()
    evidence = patch["reviewEvidence"]
    if change == "absent":
        patch.pop("reviewEvidence")
    elif change == "version":
        evidence["version"] = "syntax-review-v0"
    elif change == "commit":
        evidence["sourceCommitSha"] = "2" * 40
    elif change == "path":
        evidence["testDefinition"]["path"] = []
    elif change == "verdict":
        evidence["review"]["verdict"] = "REJECT"
    elif change == "unknown":
        evidence["extra"] = "not allowed"
    elif change == "null":
        patch["reviewEvidence"] = None
    elif change == "nul":
        evidence["review"]["reason"] = "bad\0value"
    else:
        evidence["review"]["reason"] = "가" * 20_000
    with pytest.raises(ValueError):
        validate_patch_review(snapshot(), patch)


def test_apply_binding_prevents_stripping_review_and_changing_test() -> None:
    patch = reviewed_patch()
    digest = approval_digest(patch)
    data = {
        "patch": patch,
        "reviewPolicy": REVIEW_POLICY,
        "diagnosis": {"sourceCommitSha": "1" * 40},
        "approvalChecksDigest": digest,
    }
    validate_apply_review(data, digest, review_required=True)
    tampered = copy.deepcopy(data)
    tampered.pop("reviewPolicy")
    tampered["patch"].pop("reviewEvidence")
    with pytest.raises(ValueError):
        validate_apply_review(tampered, digest, review_required=True)
    data["patch"]["checks"][0]["summary"] = "changed"
    with pytest.raises(ValueError):
        validate_apply_review(data, digest, review_required=True)


def test_legacy_and_simulated_boundaries() -> None:
    validate_patch_review({}, {})
    validate_apply_review({"patch": {}}, "legacy")
    with pytest.raises(ValueError):
        validate_patch_review(snapshot(), reviewed_patch(), simulated=True)
