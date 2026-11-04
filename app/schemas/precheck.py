"""AI 사전 점검 API(사람 쪽) 요청·응답 스키마.

JSON 이름은 camelCase, 모든 응답 칸은 항상 있고 값이 없으면 null·빈 배열이다. 결과 칸은 저장한
`agent_runs.result`(이미 camelCase·검사·마스킹을 거쳤다)에서 읽는다. 결과가 없는 상태(QUEUED·
CLAIMED·RUNNING·TIMED_OUT·CANCELLED)면 비어 있다.
"""

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import Field, JsonValue

from app.enums import (
    AgentPatchStatus,
    AgentRunOrigin,
    AgentRunSeverity,
    AgentRunStatus,
    AgentRunStep,
    AiFindingConfirmation,
    LogLevel,
    SecurityResult,
)
from app.models import AgentRunEvent
from app.schemas.response import ApiModel, ApiRequestModel
from app.services.precheck_service import PrecheckLogPage, PrecheckView
from app.services.run_result import CHECK_PASSED

DEFAULT_LOG_PAGE_SIZE = 200
MAX_LOG_PAGE_SIZE = 500


class CreatePrecheckRequest(ApiRequestModel):
    service_id: uuid.UUID
    target_environment_id: uuid.UUID


class PrecheckStepResponse(ApiModel):
    name: AgentRunStep
    first_at: datetime
    last_at: datetime
    level: LogLevel


class PrecheckModelResponse(ApiModel):
    id: str
    effort: str | None = None
    prompt_version: str | None = None
    refusal: bool = False


class PrecheckConfigDiffResponse(ApiModel):
    key: str
    source: JsonValue = None
    target: JsonValue = None
    origin: AgentRunOrigin
    severity: AgentRunSeverity


class PrecheckFeatureGapResponse(ApiModel):
    source: str
    target: str
    gap: str
    action: str
    has_replacement: bool
    severity: AgentRunSeverity
    origin: AgentRunOrigin


class PrecheckFindingResponse(ApiModel):
    control: str
    environment_id: uuid.UUID | None = None
    result: SecurityResult
    title: str
    reason: str
    origin: AgentRunOrigin
    suggested_fix: str | None = None
    confirmation: AiFindingConfirmation | None = None


class PrecheckPatchCheckResponse(ApiModel):
    name: str
    result: str
    tool_version: str | None = None
    policy_version: str | None = None
    summary: str | None = None


class PrecheckPatchVerificationResponse(ApiModel):
    passed: int
    total: int


class PrecheckPatchResponse(ApiModel):
    id: str
    patch_hash: str
    files: list[str]
    diff: str
    reason: str
    touches_protected_files: bool
    checks: list[PrecheckPatchCheckResponse]
    status: AgentPatchStatus
    block_reason: str | None = None
    verification: PrecheckPatchVerificationResponse

    @classmethod
    def from_stored(cls, stored: Mapping[str, Any]) -> Self:
        checks = stored["checks"]
        verification = PrecheckPatchVerificationResponse(
            passed=sum(1 for check in checks if check["result"] == CHECK_PASSED),
            total=len(checks),
        )
        return cls.model_validate({**stored, "verification": verification})


class PrecheckBlockedAttemptResponse(ApiModel):
    action: str
    target: str
    at: datetime
    reason: str
    audit_log_id: uuid.UUID | None = None
    confirmed: bool = False


class PrecheckDetailResponse(ApiModel):
    id: uuid.UUID
    service_id: uuid.UUID
    source_environment_id: uuid.UUID | None
    target_environment_id: uuid.UUID | None
    status: AgentRunStatus
    failure_code: str | None
    simulated: bool
    ai_reviewed: bool
    requested_by_user_id: uuid.UUID
    created_at: datetime
    claimed_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    deadline_at: datetime
    lease_until: datetime | None
    input_hash: str
    sandbox_id: str | None
    steps: list[PrecheckStepResponse]
    model: PrecheckModelResponse | None
    config_diff: list[PrecheckConfigDiffResponse]
    feature_gaps: list[PrecheckFeatureGapResponse]
    findings: list[PrecheckFindingResponse]
    patches: list[PrecheckPatchResponse]
    blocked_attempts: list[PrecheckBlockedAttemptResponse]

    @classmethod
    def from_view(cls, view: PrecheckView) -> Self:
        run = view.run
        result = run.result or {}
        sandbox = result.get("sandbox")
        model = result.get("model")
        return cls(
            id=run.id,
            service_id=run.service_id,
            source_environment_id=run.source_environment_id,
            target_environment_id=run.target_environment_id,
            status=AgentRunStatus(run.status),
            failure_code=run.failure_code,
            simulated=run.simulated,
            ai_reviewed=run.model_id is not None,
            requested_by_user_id=run.requested_by_user_id,
            created_at=run.created_at,
            claimed_at=run.claimed_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            deadline_at=run.deadline_at,
            lease_until=run.lease_until,
            input_hash=run.input_hash,
            sandbox_id=sandbox["id"] if sandbox else None,
            steps=[
                PrecheckStepResponse(
                    name=step.name,
                    first_at=step.first_at,
                    last_at=step.last_at,
                    level=step.level,
                )
                for step in view.steps
            ],
            model=PrecheckModelResponse.model_validate(model) if model else None,
            config_diff=[
                PrecheckConfigDiffResponse.model_validate(item)
                for item in result.get("configDiff", [])
            ],
            feature_gaps=[
                PrecheckFeatureGapResponse.model_validate(item)
                for item in result.get("featureGaps", [])
            ],
            findings=[
                PrecheckFindingResponse.model_validate(item) for item in result.get("findings", [])
            ],
            patches=[PrecheckPatchResponse.from_stored(item) for item in result.get("patches", [])],
            blocked_attempts=[
                PrecheckBlockedAttemptResponse.model_validate(item)
                for item in result.get("blockedAttempts", [])
            ],
        )


class PrecheckLogLineResponse(ApiModel):
    seq: int
    at: datetime
    step: AgentRunStep
    level: LogLevel
    message: str
    data: dict[str, Any] | None

    @classmethod
    def from_model(cls, event: AgentRunEvent) -> Self:
        return cls(
            seq=event.seq,
            at=event.at,
            step=AgentRunStep(event.step),
            level=LogLevel(event.level),
            message=event.message,
            data=event.data,
        )


class PrecheckLogPageResponse(ApiModel):
    """after 다음 줄부터. nextAfter 를 다음 호출의 after 로 넘긴다(새 줄이 없으면 그대로다)."""

    items: list[PrecheckLogLineResponse]
    next_after: Annotated[int, Field(ge=0)]

    @classmethod
    def from_page(cls, page: PrecheckLogPage) -> Self:
        return cls(
            items=[PrecheckLogLineResponse.from_model(item) for item in page.items],
            next_after=page.next_after,
        )
