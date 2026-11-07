"""AI 사전 점검 API(사람 쪽) 요청·응답 스키마.

JSON 이름은 camelCase, 모든 응답 칸은 항상 있고 값이 없으면 null·빈 배열이다. 결과 칸은 저장한
`agent_runs.result`(이미 camelCase·검사·마스킹을 거쳤다)에서 읽는다. 결과가 없는 상태(QUEUED·
CLAIMED·RUNNING·TIMED_OUT·CANCELLED)면 비어 있다.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Self

from pydantic import Field, JsonValue, field_validator

from app.enums import (
    AgentPatchDecision,
    AgentPatchStatus,
    AgentRunKind,
    AgentRunOrigin,
    AgentRunSeverity,
    AgentRunStatus,
    AgentRunStep,
    AiFindingConfirmation,
    DiagnosisCategory,
    DiagnosisEvidenceSource,
    DiagnosisResult,
    LogLevel,
    RedeployFollowupState,
    RepositoryFollowupState,
    SecurityResult,
)
from app.models import AgentPatchApproval, AgentRun, AgentRunEvent
from app.schemas.ai_residency import AgentRunProcessingResponse
from app.schemas.response import ApiModel, ApiRequestModel
from app.schemas.runner import PATCH_HASH_PATTERN
from app.services.apply_followup import (
    parse_redeploy_rejected_code,
    parse_redeploy_request_id,
    project_apply_followup,
)
from app.services.precheck_service import PrecheckLogPage, PrecheckPatchView, PrecheckView
from app.services.run_result import CHECK_PASSED, compute_patch_status, patch_origin

DEFAULT_LOG_PAGE_SIZE = 200
MAX_LOG_PAGE_SIZE = 500
PATCH_DECISION_REASON_MAX_LENGTH = 2_000


class CreatePrecheckRequest(ApiRequestModel):
    service_id: uuid.UUID
    target_environment_id: uuid.UUID


class DecidePrecheckPatchRequest(ApiRequestModel):
    """승인·거절 본문. patchHash 는 화면이 본 패치의 해시다."""

    patch_hash: Annotated[str, Field(pattern=PATCH_HASH_PATTERN)]
    reason: (
        Annotated[str, Field(min_length=1, max_length=PATCH_DECISION_REASON_MAX_LENGTH)] | None
    ) = None

    @field_validator("reason")
    @classmethod
    def _check_reason_storable(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("reason must not contain NUL")
        return value


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


class PrecheckPatchApprovalResponse(ApiModel):
    decision: AgentPatchDecision
    decided_by_user_id: uuid.UUID
    decided_at: datetime
    reason: str | None


class RepositoryFollowupResponse(ApiModel):
    state: RepositoryFollowupState
    reason_code: str
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RedeployFollowupResponse(ApiModel):
    state: RedeployFollowupState
    reason_code: str
    finished_at: datetime | None = None


class ApplyFollowupResponse(ApiModel):
    schema_version: int = 1
    applicable: bool | None
    repository: RepositoryFollowupResponse
    redeploy: RedeployFollowupResponse
    updated_at: datetime | None = None
    deadline_at: datetime | None = None


class PrecheckPatchApplyResponse(ApiModel):
    """반영 실행. 결과(appliedCommitSha·branch)는 러너가 결과를 낸 뒤에만 찬다(SPEC api-10-2).

    simulated 는 반영 실행 표의 값이다(결과 전에는 false). 가짜 모드 결과면 화면이 배지를 단다.
    """

    run_id: uuid.UUID
    status: AgentRunStatus
    failure_code: str | None
    finished_at: datetime | None
    applied_commit_sha: str | None
    branch: str | None
    simulated: bool
    # 저장소 반영 PR(SPEC api-19). AI_APPLY_COMMIT_ENABLED 가 켜져 있고 WAS 가 서비스 저장소에
    # 커밋·PR 을 만들었을 때만 찬다. 꺼짐·가짜 반영·실패면 null.
    pull_request_url: str | None = None
    # 배포 실패 진단의 패치 반영이 성공한 뒤 WAS 가 같은 환경에 만든 재배포 요청(SPEC api-98).
    # 진단이 아닌 사전 점검 패치·반영 전·실패·접수 거절이면 null 이다.
    redeploy_request_id: uuid.UUID | None = None
    # 재배포 접수가 거절된 오류 코드(예: MIGRATION_IN_PROGRESS). 거절이 없으면 null.
    redeploy_rejected_code: str | None = None
    follow_up: ApplyFollowupResponse | None = None


class PrecheckPatchResponse(ApiModel):
    """상세의 patches[] 원소이자 승인·거절 응답. 화면은 원소를 그대로 바꿔 끼운다."""

    id: str
    patch_hash: str
    files: list[str]
    diff: str
    reason: str
    touches_protected_files: bool
    checks: list[PrecheckPatchCheckResponse]
    origin: AgentRunOrigin
    status: AgentPatchStatus
    block_reason: str | None = None
    verification: PrecheckPatchVerificationResponse
    approval: PrecheckPatchApprovalResponse | None = None
    apply: PrecheckPatchApplyResponse | None = None

    @classmethod
    def from_view(
        cls,
        view: PrecheckPatchView,
        *,
        parent_kind: AgentRunKind | None = None,
        now: datetime | None = None,
    ) -> Self:
        stored = view.stored
        approval = view.approval
        apply_run = view.apply_run
        checks = stored["checks"]
        verification = PrecheckPatchVerificationResponse(
            passed=sum(1 for check in checks if check["result"] == CHECK_PASSED),
            total=len(checks),
        )
        return cls.model_validate(
            {
                **stored,
                "origin": patch_origin(stored),
                "status": compute_patch_status(
                    AgentPatchStatus(stored["status"]),
                    AgentPatchDecision(approval.decision) if approval is not None else None,
                    AgentRunStatus(apply_run.status) if apply_run is not None else None,
                ),
                "verification": verification,
                "approval": _approval_response(approval) if approval is not None else None,
                "apply": (
                    _apply_response(apply_run, parent_kind=parent_kind, now=now)
                    if apply_run is not None
                    else None
                ),
            }
        )


def _approval_response(approval: AgentPatchApproval) -> PrecheckPatchApprovalResponse:
    return PrecheckPatchApprovalResponse(
        decision=AgentPatchDecision(approval.decision),
        decided_by_user_id=approval.decided_by_user_id,
        decided_at=approval.decided_at,
        reason=approval.reason,
    )


def _apply_response(
    apply_run: AgentRun, *, now: datetime | None = None, parent_kind: AgentRunKind | None = None
) -> PrecheckPatchApplyResponse:
    result = apply_run.result or {}
    return PrecheckPatchApplyResponse(
        run_id=apply_run.id,
        status=AgentRunStatus(apply_run.status),
        failure_code=apply_run.failure_code,
        finished_at=apply_run.finished_at,
        applied_commit_sha=result.get("appliedCommitSha"),
        branch=result.get("branch"),
        simulated=apply_run.simulated,
        pull_request_url=result.get("pullRequestUrl"),
        redeploy_request_id=parse_redeploy_request_id(result.get("redeployRequestId")),
        redeploy_rejected_code=parse_redeploy_rejected_code(result.get("redeployRejectedCode")),
        follow_up=ApplyFollowupResponse.model_validate(
            project_apply_followup(apply_run, now=now or datetime.now(UTC), parent_kind=parent_kind)
        ),
    )


class PrecheckDiagnosisEvidenceResponse(ApiModel):
    source: DiagnosisEvidenceSource
    line: int | None
    path: str | None
    text: str


class PrecheckDiagnosisResponse(ApiModel):
    """배포 실패 진단(kind = CHANGE_PROPOSAL)의 결과(SPEC api-98). 다른 종류면 상세의 칸이 null.

    deployRequestId·environmentId·sourceCommitSha 는 만들 때 고정한 값이라 결과 전에도 있다.
    결과 칸(result·category·cause·fixable·unfixableReason·evidence)은 결과가 저장된 뒤에만 차고
    그 전(QUEUED·CLAIMED·RUNNING)과 결과 없이 끝난 실행(TIMED_OUT·CANCELLED)은 null·빈 배열이다.
    모든 문자열은 비밀값 모양을 가린 값이다.
    """

    result: DiagnosisResult | None
    category: DiagnosisCategory | None
    cause: str | None
    fixable: bool | None
    unfixable_reason: str | None
    evidence: list[PrecheckDiagnosisEvidenceResponse]
    deploy_request_id: uuid.UUID | None
    environment_id: uuid.UUID | None
    source_commit_sha: str | None

    @classmethod
    def from_run(cls, run: AgentRun) -> Self:
        stored = (run.result or {}).get("diagnosis") or {}
        deploy_request = (run.input_snapshot or {}).get("deployRequest") or {}
        return cls(
            result=stored.get("result"),
            category=stored.get("category"),
            cause=stored.get("cause"),
            fixable=stored.get("fixable"),
            unfixable_reason=stored.get("unfixableReason"),
            evidence=[
                PrecheckDiagnosisEvidenceResponse.model_validate(
                    {"line": None, "path": None, **item}
                )
                for item in stored.get("evidence", [])
            ],
            deploy_request_id=run.deploy_request_id,
            environment_id=run.target_environment_id,
            source_commit_sha=deploy_request.get("commitSha"),
        )


class PrecheckBlockedAttemptResponse(ApiModel):
    action: str
    target: str
    at: datetime
    reason: str
    audit_log_id: uuid.UUID | None = None
    confirmed: bool = False


class PrecheckApplyRunResponse(ApiModel):
    """kind = APPLY_PATCH 인 실행의 반영 정보(SPEC api-10-3). 사전 점검이면 상세의 `apply` 가 null.

    patchId·patchHash·repo·branch 는 만들 때 고정한 입력 사본 값이라 결과 전에도 있다.
    appliedCommitSha 는 러너 결과 값이라 결과 전·실패·커밋 없는 성공이면 null 이다.
    """

    parent_run_id: uuid.UUID | None
    patch_id: str | None
    patch_hash: str | None
    repo: str | None
    branch: str | None
    applied_commit_sha: str | None
    pull_request_url: str | None = None
    redeploy_request_id: uuid.UUID | None = None
    redeploy_rejected_code: str | None = None
    follow_up: ApplyFollowupResponse | None = None

    @classmethod
    def from_run(cls, run: AgentRun, *, now: datetime | None = None) -> Self:
        snapshot = run.input_snapshot or {}
        patch = snapshot.get("patch") or {}
        result = run.result or {}
        return cls(
            parent_run_id=run.parent_run_id,
            patch_id=patch.get("id"),
            patch_hash=patch.get("patchHash"),
            repo=snapshot.get("repo"),
            branch=snapshot.get("branch"),
            applied_commit_sha=result.get("appliedCommitSha"),
            pull_request_url=result.get("pullRequestUrl"),
            redeploy_request_id=parse_redeploy_request_id(result.get("redeployRequestId")),
            redeploy_rejected_code=parse_redeploy_rejected_code(result.get("redeployRejectedCode")),
            follow_up=ApplyFollowupResponse.model_validate(
                project_apply_followup(run, now=now or datetime.now(UTC))
            ),
        )


class PrecheckDetailResponse(ApiModel):
    """사전 점검(PRECHECK)·반영 실행(APPLY_PATCH)·배포 실패 진단(CHANGE_PROPOSAL)의 상세.

    반영 실행이면 `apply` 가 차고 사전 점검 결과 칸(configDiff·findings·patches 등)은 비어 있다.
    배포 실패 진단이면 `diagnosis` 가 차고 패치는 `patches` 에 있다(SPEC api-98).
    """

    id: uuid.UUID
    kind: AgentRunKind
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
    processing: AgentRunProcessingResponse | None
    config_diff: list[PrecheckConfigDiffResponse]
    feature_gaps: list[PrecheckFeatureGapResponse]
    findings: list[PrecheckFindingResponse]
    patches: list[PrecheckPatchResponse]
    blocked_attempts: list[PrecheckBlockedAttemptResponse]
    apply: PrecheckApplyRunResponse | None
    # 배포 실패 진단(kind = CHANGE_PROPOSAL)이면 진단 결과, 그 밖의 종류면 null(SPEC api-98).
    # 나중에 더한 칸이라 OpenAPI 에서는 선택 칸이다 — 응답에는 늘 싣는다.
    diagnosis: PrecheckDiagnosisResponse | None = None

    @classmethod
    def from_view(cls, view: PrecheckView, *, now: datetime | None = None) -> Self:
        run = view.run
        now = now or datetime.now(UTC)
        kind = AgentRunKind(run.kind)
        result = run.result or {}
        sandbox = result.get("sandbox")
        model = result.get("model")
        return cls(
            id=run.id,
            kind=kind,
            apply=(
                PrecheckApplyRunResponse.from_run(run, now=now)
                if kind == AgentRunKind.APPLY_PATCH
                else None
            ),
            diagnosis=(
                PrecheckDiagnosisResponse.from_run(run)
                if kind == AgentRunKind.CHANGE_PROPOSAL
                else None
            ),
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
            processing=(
                AgentRunProcessingResponse.model_validate(run.processing)
                if run.processing is not None
                else None
            ),
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
            patches=[
                PrecheckPatchResponse.from_view(
                    view.decisions.patch_view(item), parent_kind=kind, now=now
                )
                for item in result.get("patches", [])
            ],
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
