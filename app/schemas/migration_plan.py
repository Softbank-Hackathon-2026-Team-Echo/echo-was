"""이전 계획 API 요청·응답 스키마.

JSON 이름은 camelCase, 모든 응답 칸은 항상 있고 값이 없으면 null.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from app.core.config import (
    MIGRATION_MAX_MAX_WAIT_SECONDS,
    MIGRATION_MAX_REQUIRED_REQUESTS,
    MIGRATION_MIN_MAX_WAIT_SECONDS,
    MIGRATION_MIN_REQUIRED_REQUESTS,
    MIGRATION_SHIFT_STEP_COUNT,
)
from app.enums import (
    CutoverKind,
    LogLevel,
    MigrationErrorCode,
    MigrationPlanMode,
    MigrationPlanStatus,
    MigrationRollbackStatus,
    MigrationSimulateReason,
    MigrationStepKind,
    MigrationStepPhase,
    MigrationStepStatus,
)
from app.executors.base import CutoverRun
from app.models import MigrationPlan, User
from app.models.migration_plan import LAST_STEP_INDEX
from app.schemas.response import ApiModel, ApiRequestModel
from app.services.migration_counting import StepCounting
from app.services.migration_log_messages import MIGRATION_ERROR_MESSAGES
from app.services.migration_plan_service import MigrationLogPageView, MigrationPlanView
from app.services.migration_plan_support import (
    CONDITION_MAX_ERROR_RATE,
    CONDITION_MAX_P95_MS,
    CONDITION_MAX_REPLICATION_LAG,
    DATA_SYNC_STEP,
    DEFAULT_MAX_ERROR_RATE_PERCENT,
    DEFAULT_MAX_P95_MS,
    DEFAULT_MAX_REPLICATION_LAG_SECONDS,
    RESULT_COUNTING,
    RESULT_FAILURE_CODE,
    RESULT_FINISHED_AT,
    RESULT_METRICS,
    RESULT_STARTED_AT,
    RESULT_STATUS,
    SHIFT_STEPS,
    STEP_EXPECTED_SECONDS,
    STEP_INDEX,
    STEP_KIND,
    STEP_REQUIRED_REQUESTS,
    STEP_SOURCE_WEIGHT,
    STEP_TARGET_WEIGHT,
    step_kind,
)

SIMULATE_STEP_MISMATCH_REASON = "SIMULATE_REASON_STEP_MISMATCH"
SIMULATE_REASON_NOT_SUPPORTED = "SIMULATE_REASON_NOT_SUPPORTED"
# to_camel 은 errors_5xx 를 errors5Xx 로 만든다. 계약 이름(errors5xx)을 직접 준다.
ERRORS_5XX_ALIAS = "errors5xx"
ALLOWED_ERRORS_5XX_ALIAS = "allowedErrors5xx"

# simulate 이유마다 고를 수 있는 단계. 단계 4 는 출발 비율이 0이라 도착 미수신을 만들 수 없다.
_SIMULATE_STEPS: dict[MigrationSimulateReason, frozenset[int]] = {
    MigrationSimulateReason.ERROR_RATE: frozenset(SHIFT_STEPS),
    MigrationSimulateReason.HEALTH: frozenset(SHIFT_STEPS),
    MigrationSimulateReason.TRAFFIC: frozenset(SHIFT_STEPS),
    MigrationSimulateReason.LOW_TRAFFIC: frozenset(SHIFT_STEPS),
    MigrationSimulateReason.TARGET_NOT_RECEIVING: frozenset(SHIFT_STEPS[:2]),
    MigrationSimulateReason.DATA_LAG: frozenset({DATA_SYNC_STEP}),
}
# 저장된 옛 계획을 읽으려고 enum 값만 남긴 이유(요청으로는 받지 않는다).
_REMOVED_SIMULATE_REASONS = frozenset({MigrationSimulateReason.LATENCY})

type RequiredRequestsItem = Annotated[
    int, Field(ge=MIGRATION_MIN_REQUIRED_REQUESTS, le=MIGRATION_MAX_REQUIRED_REQUESTS)
]


class RollbackConditionsRequest(ApiRequestModel):
    """판정 실패 기준. 빼면 기본값(오류율 1.0%, p95 800ms, 복제 지연 1.0초)이다."""

    max_error_rate_percent: float = Field(default=DEFAULT_MAX_ERROR_RATE_PERCENT, ge=0, le=100)
    max_p95_ms: int = Field(default=DEFAULT_MAX_P95_MS, ge=1, le=60000)
    max_replication_lag_seconds: float = Field(
        default=DEFAULT_MAX_REPLICATION_LAG_SECONDS, ge=0, le=600
    )


class MigrationSimulateRequest(ApiRequestModel):
    """가짜 실행기 모드에서만 받는다. failAtStep 단계를 reason 대로 실패(또는 트래픽 적음)시킨다.

    DATA_LAG 는 데이터 확인 단계(3)만, TARGET_NOT_RECEIVING 은 비율 단계 1·2만, 그 밖의 reason 은
    비율 단계(1·2·4)만 고를 수 있다. LATENCY 는 받지 않는다(p95 는 판정에 쓰지 않는다).
    """

    fail_at_step: int = Field(ge=1, le=LAST_STEP_INDEX)
    reason: MigrationSimulateReason

    @field_validator("reason")
    @classmethod
    def _reject_removed_reason(cls, value: MigrationSimulateReason) -> MigrationSimulateReason:
        if value in _REMOVED_SIMULATE_REASONS:
            raise PydanticCustomError("simulate_reason_removed", SIMULATE_REASON_NOT_SUPPORTED)
        return value

    @model_validator(mode="after")
    def _check_reason_matches_step(self) -> Self:
        if self.fail_at_step not in _SIMULATE_STEPS[self.reason]:
            raise PydanticCustomError("simulate_step_mismatch", SIMULATE_STEP_MISMATCH_REASON)
        return self


class CreateMigrationPlanRequest(ApiRequestModel):
    """이전 계획 만들기.

    requiredRequests(비율 10·50·100% 단계 순 필요 건수 3개)·maxWaitSeconds(단계 최대 대기)를
    빼면 설정 기본값(20·50·50건, 30초)이다.
    """

    target_environment_id: uuid.UUID
    required_requests: list[RequiredRequestsItem] | None = Field(
        default=None, min_length=MIGRATION_SHIFT_STEP_COUNT, max_length=MIGRATION_SHIFT_STEP_COUNT
    )
    max_wait_seconds: int | None = Field(
        default=None, ge=MIGRATION_MIN_MAX_WAIT_SECONDS, le=MIGRATION_MAX_MAX_WAIT_SECONDS
    )
    rollback_conditions: RollbackConditionsRequest = Field(
        default_factory=RollbackConditionsRequest
    )
    simulate: MigrationSimulateRequest | None = None


class UserRefResponse(ApiModel):
    id: uuid.UUID
    login: str

    @classmethod
    def from_model(cls, user: User | None) -> Self | None:
        return cls(id=user.id, login=user.github_login) if user is not None else None


class StepMetricsResponse(ApiModel):
    """단계 실제 결과의 지표. 그 단계가 재지 않은 값은 null.

    비율 단계: requestCount·errors5xx = 도착이 센 요청·5xx, allowedErrors5xx = 허용 5xx,
    errorRatePercent = 5xx ÷ 요청 × 100(요청 0이면 null), sourceRequestCount = 출발이 센 요청,
    lowTraffic = 최대 대기 안에 건수를 못 채웠지만 서비스 전체가 조용해 헬스체크로 통과했는가,
    p95Ms = 참고 값(판정에 쓰지 않음). 데이터 확인 단계는 replicationLagSeconds 만.
    """

    health_status: str | None = None
    error_rate_percent: float | None = None
    p95_ms: float | None = None
    request_count: int | None = None
    errors_5xx: int | None = Field(default=None, alias=ERRORS_5XX_ALIAS)
    allowed_errors_5xx: int | None = Field(default=None, alias=ALLOWED_ERRORS_5XX_ALIAS)
    source_request_count: int | None = None
    low_traffic: bool | None = None
    replication_lag_seconds: float | None = None


class StepResultResponse(ApiModel):
    status: MigrationStepStatus
    started_at: datetime | None
    finished_at: datetime | None
    metrics: StepMetricsResponse | None
    failure_code: MigrationErrorCode | None


class MigrationStepResponse(ApiModel):
    """계획 값(index·kind·비율·필요 건수·최대 대기·예상 시간)과 실제 결과(result)를 나란히 싣는다.

    requiredRequests·maxWaitSeconds 는 비율 단계만 값이 있다. 8-1 전에 만든 계획의
    requiredRequests 는 null 이다.
    """

    index: int
    kind: MigrationStepKind
    source_weight: int | None
    target_weight: int | None
    required_requests: int | None
    max_wait_seconds: int | None
    expected_seconds: int
    result: StepResultResponse


class RollbackConditionsResponse(ApiModel):
    max_error_rate_percent: float
    max_p95_ms: int
    max_replication_lag_seconds: float
    # 헬스체크 실패는 늘 롤백 조건이다.
    health_check: bool = True
    # maxP95Ms 는 참고용이다(판정에 쓰지 않는다).
    p95_advisory: bool = True


class MigrationProgressResponse(ApiModel):
    """지금 비율 단계의 건수 세기 진행(예: 도착 처리 14/50건 · 5xx 0/허용 1).

    requests·errors5xx = 도착이 센 요청·5xx, sourceRequests = 출발이 센 요청, startedAt = 비율을
    적용해 세기 시작한 시각, deadlineAt = 최대 대기 끝, updatedAt = 마지막 카운터 읽기 시각.
    """

    step: int
    target_weight: int
    requests: int
    required_requests: int
    errors_5xx: int = Field(alias=ERRORS_5XX_ALIAS)
    allowed_errors_5xx: int = Field(alias=ALLOWED_ERRORS_5XX_ALIAS)
    source_requests: int
    started_at: datetime
    deadline_at: datetime | None
    updated_at: datetime

    @classmethod
    def from_plan(cls, plan: MigrationPlan) -> Self | None:
        """RUNNING 이고 지금 단계가 비율 단계의 OBSERVE·JUDGE 일 때만 값이 있다."""
        index = plan.current_step
        if (
            plan.status != MigrationPlanStatus.RUNNING
            or step_kind(plan.steps, index) is not MigrationStepKind.SHIFT
            or plan.step_phase not in (MigrationStepPhase.OBSERVE, MigrationStepPhase.JUDGE)
        ):
            return None
        stored = plan.step_results[index].get(RESULT_COUNTING)
        if stored is None:
            return None
        counting = StepCounting.from_json(stored)
        target = counting.target.counted
        return cls(
            step=index,
            target_weight=int(plan.steps[index][STEP_TARGET_WEIGHT]),
            requests=target.requests,
            required_requests=counting.required_requests,
            errors_5xx=target.errors_5xx,
            allowed_errors_5xx=counting.allowed_errors_5xx,
            source_requests=counting.source.counted.requests,
            started_at=counting.started_at,
            deadline_at=plan.observe_until,
            updated_at=counting.updated_at,
        )


class MigrationPlanResultResponse(ApiModel):
    """인프라 스크립트가 고객 DB 에 남긴 컷오버·롤백 결과(API 서버 16).

    행이 없거나 읽지 못하면 칸이 전부 null 이다. measuredBy 는 행이 있을 때 값을 잰 쪽이다.
    """

    write_pause_ms: int | None
    missing_rows: int | None
    duplicate_rows: int | None
    rows_total: int | None
    kind: CutoverKind | None
    measured_by: Literal["INFRA_SCRIPT"] | None
    paused_at: datetime | None
    resumed_at: datetime | None
    drain_ms: int | None
    catchup_ms: int | None
    switch_ms: int | None

    @classmethod
    def from_run(cls, run: CutoverRun) -> Self:
        return cls(
            write_pause_ms=run.write_pause_ms,
            missing_rows=run.missing_rows,
            duplicate_rows=run.duplicate_rows,
            rows_total=run.rows_total,
            kind=run.kind,
            measured_by="INFRA_SCRIPT",
            paused_at=run.paused_at,
            resumed_at=run.resumed_at,
            drain_ms=run.drain_ms,
            catchup_ms=run.catchup_ms,
            switch_ms=run.switch_ms,
        )

    @classmethod
    def empty(cls) -> Self:
        return cls(
            write_pause_ms=None,
            missing_rows=None,
            duplicate_rows=None,
            rows_total=None,
            kind=None,
            measured_by=None,
            paused_at=None,
            resumed_at=None,
            drain_ms=None,
            catchup_ms=None,
            switch_ms=None,
        )

    @classmethod
    def from_plan(cls, plan: MigrationPlan, run: CutoverRun | None) -> Self | None:
        """끝나지 않은 계획은 null, 끝난 계획은 늘 객체다."""
        if not MigrationPlanStatus(plan.status).is_terminal:
            return None
        return cls.from_run(run) if run is not None else cls.empty()


class MigrationPlanSummaryResponse(ApiModel):
    """이전 계획 목록 한 항목. simulated=true 면 가짜 실행기로 진행하는 계획이다.

    mode 는 서버가 만들 때 판정해 저장한 계획 종류다(PLANNED·FAILBACK. EMERGENCY 는 API 서버 9
    부터 쓴다).
    """

    id: uuid.UUID
    service_id: uuid.UUID
    source_environment_id: uuid.UUID
    target_environment_id: uuid.UUID
    source_service_environment_id: uuid.UUID
    target_service_environment_id: uuid.UUID
    status: MigrationPlanStatus
    mode: MigrationPlanMode
    current_step: int
    expected_seconds: int
    created_by: UserRefResponse
    approved_by: UserRefResponse | None
    self_approved: bool
    error_code: MigrationErrorCode | None
    rollback_status: MigrationRollbackStatus | None
    simulated: bool
    created_at: datetime
    approved_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    expires_at: datetime

    @classmethod
    def from_view(cls, view: MigrationPlanView) -> Self:
        return cls(**_summary_fields(view))


class MigrationPlanDetailResponse(MigrationPlanSummaryResponse):
    """단건 응답. maxWaitSeconds 는 계획 행의 observe_seconds 칸(새 계획은 단계 최대 대기, 8-1 전
    계획은 그 계획의 지켜보기 초)이다. progress 는 비율 단계를 세는 동안만 값이 있다(1초 폴링용).
    result 는 고객 DB 의 컷오버 결과이고 상세 GET 만 읽는다(쓰기 응답은 끝난 계획이면 칸 전부 null).
    """

    rollback_conditions: RollbackConditionsResponse
    max_wait_seconds: int
    steps: list[MigrationStepResponse]
    progress: MigrationProgressResponse | None
    rejected_by: UserRefResponse | None
    cancelled_by: UserRefResponse | None
    aborted_by: UserRefResponse | None
    abort_requested_at: datetime | None
    error_message: str | None
    result: MigrationPlanResultResponse | None

    @classmethod
    def from_view(cls, view: MigrationPlanView, cutover_run: CutoverRun | None = None) -> Self:
        plan = view.plan
        conditions = plan.rollback_conditions
        error_code = MigrationErrorCode(plan.error_code) if plan.error_code else None
        return cls(
            **_summary_fields(view),
            rollback_conditions=RollbackConditionsResponse(
                max_error_rate_percent=float(conditions[CONDITION_MAX_ERROR_RATE]),
                max_p95_ms=int(conditions[CONDITION_MAX_P95_MS]),
                max_replication_lag_seconds=float(conditions[CONDITION_MAX_REPLICATION_LAG]),
            ),
            max_wait_seconds=plan.observe_seconds,
            steps=[
                _step_response(step, result, plan.observe_seconds)
                for step, result in zip(plan.steps, plan.step_results, strict=True)
            ],
            progress=MigrationProgressResponse.from_plan(plan),
            rejected_by=UserRefResponse.from_model(view.user(plan.rejected_by_user_id)),
            cancelled_by=UserRefResponse.from_model(view.user(plan.cancelled_by_user_id)),
            aborted_by=UserRefResponse.from_model(view.user(plan.aborted_by_user_id)),
            abort_requested_at=plan.abort_requested_at,
            error_message=MIGRATION_ERROR_MESSAGES[error_code] if error_code else None,
            result=MigrationPlanResultResponse.from_plan(plan, cutover_run),
        )


def _summary_fields(view: MigrationPlanView) -> dict[str, object]:
    plan = view.plan
    created_by = UserRefResponse.from_model(view.user(plan.created_by_user_id))
    assert created_by is not None
    return {
        "id": plan.id,
        "service_id": plan.service_id,
        "source_environment_id": view.source_environment_id,
        "target_environment_id": view.target_environment_id,
        "source_service_environment_id": plan.source_service_environment_id,
        "target_service_environment_id": plan.target_service_environment_id,
        "status": plan.status,
        "mode": plan.mode,
        "current_step": plan.current_step,
        "expected_seconds": plan.expected_seconds,
        "created_by": created_by,
        "approved_by": UserRefResponse.from_model(view.user(plan.approved_by_user_id)),
        "self_approved": plan.self_approved,
        "error_code": plan.error_code,
        "rollback_status": plan.rollback_status,
        "simulated": plan.simulated,
        "created_at": plan.created_at,
        "approved_at": plan.approved_at,
        "started_at": plan.started_at,
        "finished_at": plan.finished_at,
        "expires_at": plan.expires_at,
    }


def _step_response(
    step: dict[str, Any], result: dict[str, Any], max_wait_seconds: int
) -> MigrationStepResponse:
    metrics = result.get(RESULT_METRICS)
    kind = MigrationStepKind(step[STEP_KIND])
    is_shift = kind is MigrationStepKind.SHIFT
    return MigrationStepResponse(
        index=int(step[STEP_INDEX]),
        kind=kind,
        source_weight=step[STEP_SOURCE_WEIGHT],
        target_weight=step[STEP_TARGET_WEIGHT],
        required_requests=step.get(STEP_REQUIRED_REQUESTS),
        max_wait_seconds=max_wait_seconds if is_shift else None,
        expected_seconds=int(step[STEP_EXPECTED_SECONDS]),
        result=StepResultResponse(
            status=MigrationStepStatus(result[RESULT_STATUS]),
            started_at=result.get(RESULT_STARTED_AT),
            finished_at=result.get(RESULT_FINISHED_AT),
            metrics=StepMetricsResponse.model_validate(metrics) if metrics else None,
            failure_code=result.get(RESULT_FAILURE_CODE),
        ),
    )


class MigrationLogLineResponse(ApiModel):
    seq: int
    step: int | None
    at: datetime
    level: LogLevel
    code: str
    message: str
    data: dict[str, Any] | None


class MigrationLogPageResponse(ApiModel):
    """실행 로그 한 페이지. simulated=true 면 가짜 실행기로 만든 기록이다.

    nextCursor(마지막 seq)로 이어 읽는다.
    """

    simulated: bool
    items: list[MigrationLogLineResponse]
    next_cursor: str | None
    has_more: bool

    @classmethod
    def from_view(cls, view: MigrationLogPageView) -> Self:
        return cls(
            simulated=view.simulated,
            items=[
                MigrationLogLineResponse(
                    seq=line.seq,
                    step=line.step,
                    at=line.logged_at,
                    level=line.level,
                    code=line.code,
                    message=line.message,
                    data=line.data,
                )
                for line in view.lines
            ],
            next_cursor=view.next_cursor,
            has_more=view.has_more,
        )
