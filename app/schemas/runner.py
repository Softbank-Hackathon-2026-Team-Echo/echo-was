"""러너 경로 요청·응답 스키마(WAS ↔ 러너 계약 v1 + SPEC api-runner-prechecks).

이벤트·결과 본문은 검증 실패를 VALIDATION_ERROR 가 아니라 422 RESULT_INVALID 로 돌려줘야 해서
FastAPI 본문 인자로 받지 않고 parse_runner_payload 로 읽는다. 모르는 칸은 거부하고, JSON 이름은
camelCase 다. 응답 details 에는 칸 경로와 이유만 넣고 입력 값은 넣지 않는다.
"""

import json
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AwareDatetime,
    Field,
    JsonValue,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)

from app.core.exceptions import ErrorField, ResultInvalidError
from app.enums import (
    AgentRunDataClass,
    AgentRunKind,
    AgentRunModelProvider,
    AgentRunOrigin,
    AgentRunSeverity,
    AgentRunStatus,
    AgentRunStep,
    DiagnosisCategory,
    DiagnosisEvidenceSource,
    DiagnosisResult,
    LogLevel,
    SecurityResult,
)
from app.models.agent_run_event import AGENT_RUN_EVENT_MESSAGE_MAX_LENGTH
from app.schemas.response import ApiModel, ApiRequestModel
from app.services.agent_run_service import ClaimedRun, RunEventCommand
from app.services.run_result import (
    NormalizedApplyResult,
    NormalizedRunResult,
    compute_patch_hash,
    normalize_apply_result,
    normalize_diagnosis_result,
    normalize_run_result,
)

MAX_BODY_BYTES = 1024 * 1024
MAX_EVENTS_PER_REQUEST = 100
MAX_EVENT_SEQ = 2_147_483_647
MAX_STRING_LENGTH = 2_000
MAX_DIFF_BYTES = 200 * 1024
MAX_CONFIG_DIFFS = 200
MAX_FEATURE_GAPS = 100
MAX_FINDINGS = 100
MAX_PATCHES = 10
MAX_BLOCKED_ATTEMPTS = 50
MAX_EVIDENCE = 20
MAX_EVIDENCE_LINE = 10_000_000
MAX_MODEL_ID_LENGTH = 64
MAX_PROMPT_VERSION_LENGTH = 32
FAILURE_CODE_PATTERN = "^[A-Z][A-Z0-9_]{0,31}$"
PATCH_HASH_PATTERN = "^sha256:[0-9a-f]{64}$"
COMMIT_SHA_PATTERN = "^[0-9a-f]{40}$"
MAX_BRANCH_LENGTH = 255
CHECK_RESULT_PATTERN = "^[A-Z][A-Z0-9_]{0,31}$"
PROCESSING_LOCATION_PATTERN = "^[a-z][a-z0-9-]{0,15}/[a-z0-9-]{1,40}$"
PROCESSING_MODEL_PATTERN = "^[A-Za-z0-9._:/@-]{1,64}$"
PROCESSING_MODEL_REGION_PATTERN = "^[a-z0-9-]{1,40}$"
MAX_PROCESSING_DATA_CLASSES = 5
# 아주 큰 정수는 JSON 으로 다시 쓸 때 Python 의 자릿수 제한에 걸린다(422 대신 500 이 된다).
MAX_EXTERNAL_TRANSFER_BYTES = 2**63 - 1
BODY_TOO_LARGE_REASON = "BODY_TOO_LARGE"
NUL_CHARACTER_REASON = "NUL_CHARACTER"
_BODY_FIELD = "body"

Text = Annotated[str, Field(min_length=1, max_length=MAX_STRING_LENGTH)]
OptionalText = Annotated[str, Field(max_length=MAX_STRING_LENGTH)] | None
ResultStatus = Literal[AgentRunStatus.SUCCEEDED, AgentRunStatus.FAILED]
FindingResult = Literal[
    SecurityResult.PASS, SecurityResult.WARN, SecurityResult.FAIL, SecurityResult.UNKNOWN
]


def _check_json_value_size(value: JsonValue) -> JsonValue:
    if len(json.dumps(value, ensure_ascii=False)) > MAX_STRING_LENGTH:
        raise ValueError(f"value must be at most {MAX_STRING_LENGTH} characters of JSON")
    return value


def _contains_nul(value: Any) -> bool:
    """PostgreSQL 은 문자열·JSONB 에 NUL 을 담지 못한다. 저장 전에 422 로 막는다."""
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, list):
        return any(_contains_nul(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_nul(key) or _contains_nul(item) for key, item in value.items())
    return False


def parse_runner_payload[T: ApiRequestModel](raw: bytes, model: type[T]) -> T:
    """러너 본문을 읽는다. 크기·형식·칸 검사 실패는 모두 422 RESULT_INVALID."""
    if len(raw) > MAX_BODY_BYTES:
        raise ResultInvalidError([ErrorField(_BODY_FIELD, BODY_TOO_LARGE_REASON)])
    try:
        parsed = model.model_validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False, include_context=False)
        raise ResultInvalidError(
            [
                ErrorField(
                    ".".join(str(part) for part in error["loc"]) or _BODY_FIELD, error["msg"]
                )
                for error in errors
            ]
        ) from None
    if _contains_nul(parsed.model_dump(mode="json", by_alias=True)):
        raise ResultInvalidError([ErrorField(_BODY_FIELD, NUL_CHARACTER_REASON)])
    return parsed


def _inline_schema(model: type[ApiRequestModel]) -> dict[str, Any]:
    """모델의 JSON 스키마. `$defs` 참조를 그 자리에 풀어 넣는다."""
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def inline(value: Any) -> Any:
        if isinstance(value, list):
            return [inline(item) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str):
            return inline(definitions[reference.rsplit("/", 1)[-1]])
        return {key: inline(item) for key, item in value.items()}

    inlined: dict[str, Any] = inline(schema)
    return inlined


def openapi_request_body(*models: type[ApiRequestModel]) -> dict[str, Any]:
    """FastAPI 본문 인자가 아닌 경로의 OpenAPI 요청 본문. 모델이 여럿이면 oneOf 다."""
    schemas = [_inline_schema(model) for model in models]
    schema = schemas[0] if len(schemas) == 1 else {"oneOf": schemas}
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": schema}},
        }
    }


# --- 가져가기 ---


class ClaimRunRequest(ApiRequestModel):
    runner_id: Annotated[str, Field(min_length=1, max_length=63)]
    capabilities: Annotated[list[Annotated[str, Field(max_length=32)]], Field(max_length=16)]


class ClaimedRunResponse(ApiModel):
    id: uuid.UUID
    kind: AgentRunKind
    org_id: uuid.UUID
    service_id: uuid.UUID
    source_environment_id: uuid.UUID | None
    target_environment_id: uuid.UUID | None
    input_snapshot: dict[str, Any]
    input_hash: str
    scenario_ref: str | None
    deadline_at: datetime
    lease_until: datetime | None


class ClaimRunResponse(ApiModel):
    """가져갈 실행이 없으면 run·runToken 이 null 이다. runToken 은 여기서 한 번만 준다."""

    run: ClaimedRunResponse | None = None
    run_token: str | None = None

    @classmethod
    def from_claimed(cls, claimed: ClaimedRun | None) -> Self:
        if claimed is None:
            return cls()
        run = claimed.run
        return cls(
            run=ClaimedRunResponse(
                id=run.id,
                kind=AgentRunKind(run.kind),
                org_id=run.organization_id,
                service_id=run.service_id,
                source_environment_id=run.source_environment_id,
                target_environment_id=run.target_environment_id,
                input_snapshot=run.input_snapshot,
                input_hash=run.input_hash,
                scenario_ref=run.scenario_ref,
                deadline_at=run.deadline_at,
                lease_until=run.lease_until,
            ),
            run_token=claimed.run_token.get_secret_value(),
        )


class HeartbeatResponse(ApiModel):
    status: AgentRunStatus
    lease_until: datetime | None


# --- 이벤트 ---


class RunEventRequest(ApiRequestModel):
    seq: Annotated[int, Field(ge=1, le=MAX_EVENT_SEQ)]
    at: AwareDatetime
    step: AgentRunStep
    level: LogLevel
    message: Annotated[str, Field(min_length=1, max_length=AGENT_RUN_EVENT_MESSAGE_MAX_LENGTH)]
    data: dict[str, JsonValue] | None = None

    @field_validator("data")
    @classmethod
    def _check_data_size(cls, value: dict[str, JsonValue] | None) -> dict[str, JsonValue] | None:
        if value is not None:
            _check_json_value_size(value)
        return value


class SubmitRunEventsRequest(ApiRequestModel):
    events: Annotated[list[RunEventRequest], Field(min_length=1, max_length=MAX_EVENTS_PER_REQUEST)]

    def to_commands(self) -> list[RunEventCommand]:
        return [
            RunEventCommand(
                seq=event.seq,
                at=event.at,
                step=event.step,
                level=event.level,
                message=event.message,
                data=event.data,
            )
            for event in self.events
        ]


class SubmitRunEventsResponse(ApiModel):
    accepted_up_to: int


# --- 결과 ---


class RunSandboxRequest(ApiRequestModel):
    id: Text
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    network: Text
    deleted_at: AwareDatetime | None = None


class RunModelRequest(ApiRequestModel):
    id: Annotated[str, Field(min_length=1, max_length=MAX_MODEL_ID_LENGTH)]
    effort: OptionalText = None
    prompt_version: Annotated[str, Field(max_length=MAX_PROMPT_VERSION_LENGTH)] | None = None
    refusal: bool = False


class RunConfigDiffRequest(ApiRequestModel):
    key: Text
    source: JsonValue
    target: JsonValue
    origin: AgentRunOrigin
    severity: AgentRunSeverity

    @field_validator("source", "target")
    @classmethod
    def _check_value_size(cls, value: JsonValue) -> JsonValue:
        return _check_json_value_size(value)


class RunFeatureGapRequest(ApiRequestModel):
    source: Text
    target: Text
    gap: Text
    action: Text
    has_replacement: bool
    severity: AgentRunSeverity
    origin: AgentRunOrigin


class RunFindingRequest(ApiRequestModel):
    control: Text
    environment_id: uuid.UUID | None = None
    result: FindingResult
    title: Text
    reason: Text
    origin: AgentRunOrigin
    suggested_fix: OptionalText = None

    @model_validator(mode="after")
    def _check_ai_cannot_pass(self) -> Self:
        if self.origin is AgentRunOrigin.AI and self.result is SecurityResult.PASS:
            raise ValueError("an AI finding cannot be PASS")
        return self


class RunPatchCheckRequest(ApiRequestModel):
    name: Annotated[str, Field(min_length=1, max_length=64)]
    result: Annotated[str, Field(pattern=CHECK_RESULT_PATTERN)]
    tool_version: OptionalText = None
    policy_version: OptionalText = None
    summary: OptionalText = None


class RunPatchRequest(ApiRequestModel):
    id: Annotated[str, Field(min_length=1, max_length=64)]
    patch_hash: Annotated[str, Field(pattern=PATCH_HASH_PATTERN)]
    files: Annotated[list[Text], Field(min_length=1)]
    diff: str
    reason: Text
    touches_protected_files: bool
    checks: list[RunPatchCheckRequest]
    origin: AgentRunOrigin = AgentRunOrigin.AI

    @field_validator("diff")
    @classmethod
    def _check_diff_size(cls, value: str) -> str:
        if len(value.encode()) > MAX_DIFF_BYTES:
            raise ValueError(f"diff must be at most {MAX_DIFF_BYTES} bytes")
        return value

    @model_validator(mode="after")
    def _check_patch_hash_matches_diff(self) -> Self:
        """PD-14: patchHash 는 diff 의 해시여야 한다. 승인 지문이 diff 에 묶이게 다시 계산한다."""
        if self.patch_hash != compute_patch_hash(self.diff):
            raise ValueError("patchHash must be sha256 of diff")
        return self


class RunBlockedAttemptRequest(ApiRequestModel):
    action: Text
    target: Text
    at: AwareDatetime
    reason: Text


class RunProcessingRequest(ApiRequestModel):
    """어디서 처리했고 무엇이 밖으로 나갔나(러너 신고). model 칸과 서로 맞추지 않는다.

    모델이 거절·실패하면 결과의 model 은 null 이어도 자료는 이미 나갔을 수 있다.
    """

    location: Annotated[str, Field(pattern=PROCESSING_LOCATION_PATTERN)]
    sandbox: StrictBool
    model_provider: AgentRunModelProvider
    model: Annotated[str, Field(pattern=PROCESSING_MODEL_PATTERN)] | None = None
    model_region: Annotated[str, Field(pattern=PROCESSING_MODEL_REGION_PATTERN)] | None = None
    data_classes: Annotated[list[AgentRunDataClass], Field(max_length=MAX_PROCESSING_DATA_CLASSES)]
    external_transfer: StrictBool
    external_transfer_bytes: (
        Annotated[int, Field(ge=0, le=MAX_EXTERNAL_TRANSFER_BYTES, strict=True)] | None
    ) = None

    @model_validator(mode="after")
    def _check_provider_and_transfer(self) -> Self:
        if len(self.data_classes) != len(set(self.data_classes)):
            raise ValueError("dataClasses must be unique")
        if self.model_provider is AgentRunModelProvider.NONE:
            if self.model is not None:
                raise ValueError("model must be null when modelProvider is NONE")
            if self.model_region is not None:
                raise ValueError("modelRegion must be null when modelProvider is NONE")
            if self.data_classes:
                raise ValueError("dataClasses must be empty when modelProvider is NONE")
            if self.external_transfer:
                raise ValueError("externalTransfer must be false when modelProvider is NONE")
        elif self.model is None:
            raise ValueError("model is required unless modelProvider is NONE")
        if not self.external_transfer and self.external_transfer_bytes:
            raise ValueError("externalTransferBytes must be null or 0 without externalTransfer")
        return self


class RunResultRequest(ApiRequestModel):
    """결과 한 번. simulated 는 값을 지어냈는지 여부(필수), model 이 null 이면 AI 검토 없음이다."""

    status: ResultStatus
    failure_code: Annotated[str, Field(pattern=FAILURE_CODE_PATTERN)] | None = None
    simulated: bool
    sandbox: RunSandboxRequest | None = None
    model: RunModelRequest | None = None
    processing: RunProcessingRequest | None = None
    config_diff: Annotated[list[RunConfigDiffRequest], Field(max_length=MAX_CONFIG_DIFFS)] = []
    feature_gaps: Annotated[list[RunFeatureGapRequest], Field(max_length=MAX_FEATURE_GAPS)] = []
    findings: Annotated[list[RunFindingRequest], Field(max_length=MAX_FINDINGS)] = []
    patches: Annotated[list[RunPatchRequest], Field(max_length=MAX_PATCHES)] = []
    blocked_attempts: Annotated[
        list[RunBlockedAttemptRequest], Field(max_length=MAX_BLOCKED_ATTEMPTS)
    ] = []

    @model_validator(mode="after")
    def _check_failure_code_and_patch_ids(self) -> Self:
        if self.status == AgentRunStatus.SUCCEEDED and self.failure_code is not None:
            raise ValueError("failureCode must be null when status is SUCCEEDED")
        if self.status == AgentRunStatus.FAILED and self.failure_code is None:
            raise ValueError("failureCode is required when status is FAILED")
        patch_ids = [patch.id for patch in self.patches]
        if len(patch_ids) != len(set(patch_ids)):
            raise ValueError("patch id must be unique")
        return self

    def to_normalized(self) -> NormalizedRunResult:
        return normalize_run_result(self.model_dump(mode="json", by_alias=True))


class RunDiagnosisEvidenceRequest(ApiRequestModel):
    """진단 근거 한 줄. LOG 는 실패 로그(logTail)의 줄 번호, FILE 은 저장소 파일 경로·줄 번호다."""

    source: DiagnosisEvidenceSource
    line: Annotated[int, Field(ge=1, le=MAX_EVIDENCE_LINE)] | None = None
    path: Annotated[str, Field(min_length=1, max_length=MAX_STRING_LENGTH)] | None = None
    text: Text

    @model_validator(mode="after")
    def _check_file_has_path(self) -> Self:
        if self.source is DiagnosisEvidenceSource.FILE and self.path is None:
            raise ValueError("path is required when source is FILE")
        return self


class RunDiagnosisRequest(ApiRequestModel):
    """배포 실패 진단(SPEC api-98). deployRequestId·environmentId·sourceCommitSha 는 WAS 가 채운다.

    - DIAGNOSED 면 category·cause 가 필수다. 그 밖의 결론이면 fixable 이 false 다.
    - fixable 이 false 면 unfixableReason 이 필수다(화면이 패치 대신 사유를 보인다).
    """

    result: DiagnosisResult
    category: DiagnosisCategory | None = None
    cause: OptionalText = None
    fixable: StrictBool
    unfixable_reason: OptionalText = None
    evidence: Annotated[list[RunDiagnosisEvidenceRequest], Field(max_length=MAX_EVIDENCE)] = []

    @model_validator(mode="after")
    def _check_result_fields(self) -> Self:
        if self.result is DiagnosisResult.DIAGNOSED:
            if self.category is None or not self.cause:
                raise ValueError("category and cause are required when result is DIAGNOSED")
        elif self.fixable:
            raise ValueError("fixable must be false unless result is DIAGNOSED")
        if not self.fixable and not self.unfixable_reason:
            raise ValueError("unfixableReason is required when fixable is false")
        return self


class DiagnosisRunResultRequest(RunResultRequest):
    """배포 실패 진단(CHANGE_PROPOSAL) 결과: RunResult + diagnosis(SPEC api-98).

    diagnosis 를 빼면(진단을 못 하는 러너) WAS 가 근거 부족(INSUFFICIENT_EVIDENCE)으로 마무리한다.
    패치는 DIAGNOSED·fixable 진단에만 붙일 수 있다. 패치 모양·검사는 사전 점검과 같다.
    """

    diagnosis: RunDiagnosisRequest | None = None

    @model_validator(mode="after")
    def _check_patches_need_fixable_diagnosis(self) -> Self:
        if self.patches and (self.diagnosis is None or not self.diagnosis.fixable):
            raise ValueError("patches require a fixable DIAGNOSED diagnosis")
        return self

    def to_normalized(self) -> NormalizedRunResult:
        return normalize_diagnosis_result(self.model_dump(mode="json", by_alias=True))


class ApplyPatchResultRequest(ApiRequestModel):
    """반영 실행(APPLY_PATCH) 결과. 계약 2.1 `{appliedCommitSha, branch}` + 공통 칸만.

    성공이면 branch 가 필수다. appliedCommitSha 는 러너가 커밋하지 못했으면(git 없는 사본) null,
    실패면 항상 null 이다(SPEC 3절).
    지문(승인 행 patchHash ↔ 입력 사본 diff 재계산)은 서비스가 본다.
    """

    status: ResultStatus
    failure_code: Annotated[str, Field(pattern=FAILURE_CODE_PATTERN)] | None = None
    simulated: bool
    applied_commit_sha: Annotated[str, Field(pattern=COMMIT_SHA_PATTERN)] | None = None
    branch: Annotated[str, Field(min_length=1, max_length=MAX_BRANCH_LENGTH)] | None = None

    @model_validator(mode="after")
    def _check_status_fields(self) -> Self:
        if self.status == AgentRunStatus.SUCCEEDED:
            if self.failure_code is not None:
                raise ValueError("failureCode must be null when status is SUCCEEDED")
            if self.branch is None:
                raise ValueError("branch is required when status is SUCCEEDED")
        if self.status == AgentRunStatus.FAILED:
            if self.failure_code is None:
                raise ValueError("failureCode is required when status is FAILED")
            if self.applied_commit_sha is not None:
                raise ValueError("appliedCommitSha must be null when status is FAILED")
        return self

    def to_normalized(self) -> NormalizedApplyResult:
        return normalize_apply_result(self.model_dump(mode="json", by_alias=True))


class SubmitRunResultResponse(ApiModel):
    status: AgentRunStatus
