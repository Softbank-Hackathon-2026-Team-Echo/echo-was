"""이전 계획 API 요청·응답 스키마.

JSON 이름은 camelCase, 모든 응답 칸은 항상 있고 값이 없으면 null.
"""

import uuid
from datetime import datetime
from typing import Any, Self

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from app.enums import (
    LogLevel,
    MigrationErrorCode,
    MigrationPlanStatus,
    MigrationRollbackStatus,
    MigrationSimulateReason,
    MigrationStepKind,
    MigrationStepStatus,
)
from app.models import User
from app.models.migration_plan import LAST_STEP_INDEX, MAX_OBSERVE_SECONDS, MIN_OBSERVE_SECONDS
from app.schemas.response import ApiModel, ApiRequestModel
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
    RESULT_FAILURE_CODE,
    RESULT_FINISHED_AT,
    RESULT_METRICS,
    RESULT_STARTED_AT,
    RESULT_STATUS,
    STEP_EXPECTED_SECONDS,
    STEP_INDEX,
    STEP_KIND,
    STEP_OBSERVE_SECONDS,
    STEP_SOURCE_WEIGHT,
    STEP_TARGET_WEIGHT,
)

SIMULATE_STEP_MISMATCH_REASON = "SIMULATE_REASON_STEP_MISMATCH"


class RollbackConditionsRequest(ApiRequestModel):
    """판정 실패 기준. 빼면 기본값(오류율 1.0%, p95 800ms, 복제 지연 1.0초)이다."""

    max_error_rate_percent: float = Field(default=DEFAULT_MAX_ERROR_RATE_PERCENT, ge=0, le=100)
    max_p95_ms: int = Field(default=DEFAULT_MAX_P95_MS, ge=1, le=60000)
    max_replication_lag_seconds: float = Field(
        default=DEFAULT_MAX_REPLICATION_LAG_SECONDS, ge=0, le=600
    )


class MigrationSimulateRequest(ApiRequestModel):
    """가짜 실행기 모드에서만 받는다. failAtStep 단계를 reason 으로 일부러 실패시킨다.

    DATA_LAG 는 데이터 확인 단계(3)만, 그 밖의 reason 은 비율 단계(1·2·4)만 고를 수 있다.
    """

    fail_at_step: int = Field(ge=1, le=LAST_STEP_INDEX)
    reason: MigrationSimulateReason

    @model_validator(mode="after")
    def _check_reason_matches_step(self) -> Self:
        is_data_step = self.fail_at_step == DATA_SYNC_STEP
        if is_data_step != (self.reason is MigrationSimulateReason.DATA_LAG):
            raise PydanticCustomError("simulate_step_mismatch", SIMULATE_STEP_MISMATCH_REASON)
        return self


class CreateMigrationPlanRequest(ApiRequestModel):
    """이전 계획 만들기. observeSeconds 를 빼면 설정 기본값(5초)이다."""

    target_environment_id: uuid.UUID
    observe_seconds: int | None = Field(
        default=None, ge=MIN_OBSERVE_SECONDS, le=MAX_OBSERVE_SECONDS
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
    """단계 실제 결과의 지표. 그 단계가 재지 않은 값은 null."""

    health_status: str | None
    error_rate_percent: float | None
    p95_ms: float | None
    request_count: int | None
    replication_lag_seconds: float | None


class StepResultResponse(ApiModel):
    status: MigrationStepStatus
    started_at: datetime | None
    finished_at: datetime | None
    metrics: StepMetricsResponse | None
    failure_code: MigrationErrorCode | None


class MigrationStepResponse(ApiModel):
    """계획 값(index·kind·비율·지켜보기·예상 시간)과 실제 결과(result)를 나란히 싣는다."""

    index: int
    kind: MigrationStepKind
    source_weight: int | None
    target_weight: int | None
    observe_seconds: int | None
    expected_seconds: int
    result: StepResultResponse


class RollbackConditionsResponse(ApiModel):
    max_error_rate_percent: float
    max_p95_ms: int
    max_replication_lag_seconds: float
    # 헬스체크 실패는 늘 롤백 조건이다.
    health_check: bool = True


class MigrationPlanSummaryResponse(ApiModel):
    """이전 계획 목록 한 항목. simulated=true 면 가짜 실행기로 진행하는 계획이다."""

    id: uuid.UUID
    service_id: uuid.UUID
    source_environment_id: uuid.UUID
    target_environment_id: uuid.UUID
    source_service_environment_id: uuid.UUID
    target_service_environment_id: uuid.UUID
    status: MigrationPlanStatus
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
    rollback_conditions: RollbackConditionsResponse
    observe_seconds: int
    steps: list[MigrationStepResponse]
    rejected_by: UserRefResponse | None
    cancelled_by: UserRefResponse | None
    aborted_by: UserRefResponse | None
    abort_requested_at: datetime | None
    error_message: str | None

    @classmethod
    def from_view(cls, view: MigrationPlanView) -> Self:
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
            observe_seconds=plan.observe_seconds,
            steps=[
                _step_response(step, result)
                for step, result in zip(plan.steps, plan.step_results, strict=True)
            ],
            rejected_by=UserRefResponse.from_model(view.user(plan.rejected_by_user_id)),
            cancelled_by=UserRefResponse.from_model(view.user(plan.cancelled_by_user_id)),
            aborted_by=UserRefResponse.from_model(view.user(plan.aborted_by_user_id)),
            abort_requested_at=plan.abort_requested_at,
            error_message=MIGRATION_ERROR_MESSAGES[error_code] if error_code else None,
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


def _step_response(step: dict[str, Any], result: dict[str, Any]) -> MigrationStepResponse:
    metrics = result.get(RESULT_METRICS)
    return MigrationStepResponse(
        index=int(step[STEP_INDEX]),
        kind=MigrationStepKind(step[STEP_KIND]),
        source_weight=step[STEP_SOURCE_WEIGHT],
        target_weight=step[STEP_TARGET_WEIGHT],
        observe_seconds=step[STEP_OBSERVE_SECONDS],
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
