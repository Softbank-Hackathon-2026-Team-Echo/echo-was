"""사람에게 보이는 CVE 수정 작업 영수증. 로컬 경로·stderr는 노출하지 않는다."""

import uuid
from typing import Literal

from app.schemas.response import ApiModel, ApiRequestModel


class CveDiagnosisRequest(ApiRequestModel):
    operation: Literal["START", "CANCEL"] = "START"


class CveAutofixReceiptResponse(ApiModel):
    run_id: uuid.UUID
    source_finding_id: uuid.UUID
    cve_id: str
    status: Literal["RUNNING", "READY_FOR_REVIEW", "FAILED", "TIMED_OUT", "CANCELLED", "STALE"]
    input_hash: str
    failure_code: str | None = None
    executor: str = "DETERMINISTIC_DEPENDENCY_BUMP"
    ai_reviewed: bool = False
    event_simulated: bool = True
    execution_simulated: bool | None = None
    model_mocked: bool = False
    source_head: str | None = None
    source_artifact_kind: Literal["FIXTURE_SOURCE_TREE"] = "FIXTURE_SOURCE_TREE"
    source_artifact_digest: str | None = None
    real_image_digest: None = None
    real_image_verified: Literal[False] = False
    deployment_ready: Literal[False] = False
    vulnerabilities_resolved: Literal[False] = False
    patch_hash: str | None = None
    manifest_sha256: str | None = None
    lock_sha256: str | None = None
    rescan_sha256: str | None = None
    scan_scope: str | None = None
    files: list[str] = []
    checks: list[str] = []
    deadline_at: str | None = None
    finished_at: str | None = None
    merge_performed: bool = False
    deploy_performed: bool = False
