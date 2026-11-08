"""Trusted snapshot → review → approval binding, independent of DB and model calls."""

from collections.abc import Mapping
from typing import Any

from app.core.review_evidence import REVIEW_POLICY, approval_digest, validate_evidence


def validate_patch_review(
    snapshot: Mapping[str, Any],
    patch: Mapping[str, Any],
    *,
    simulated: bool = False,
) -> None:
    policy = snapshot.get("reviewPolicy")
    evidence = patch.get("reviewEvidence")
    if policy is None and evidence is None:
        return
    if policy not in (None, REVIEW_POLICY) or simulated or evidence is None:
        raise ValueError("REVIEW_REQUIRED")
    commit = (snapshot.get("deployRequest") or {}).get("commitSha")
    if commit is None:
        commit = (snapshot.get("diagnosis") or {}).get("sourceCommitSha")
    validate_evidence(evidence, patch.get("diff", ""), commit, patch.get("files"))


def validate_apply_review(
    snapshot: Mapping[str, Any],
    checks_digest: str,
    *,
    review_required: bool = False,
) -> None:
    patch = snapshot.get("patch") or {}
    if review_required and patch.get("reviewEvidence") is None:
        raise ValueError("REVIEW_REQUIRED")
    validate_patch_review(snapshot, patch)
    if snapshot.get("reviewPolicy") is not None or patch.get("reviewEvidence") is not None:
        if (
            approval_digest(patch) != checks_digest
            or snapshot.get("approvalChecksDigest") != checks_digest
        ):
            raise ValueError("REVIEW_APPROVAL_CHANGED")
