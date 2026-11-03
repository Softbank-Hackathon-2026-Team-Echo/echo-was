import uuid
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import (
    OPEN_MIGRATION_PLAN_STATUSES,
    MigrationErrorCode,
    MigrationPlanMode,
    MigrationPlanStatus,
    MigrationRollbackStatus,
    MigrationStepPhase,
)
from app.models.base import (
    APPENDED_COLUMN_SORT_ORDER,
    Base,
    TimestampMixin,
    UuidPrimaryKeyMixin,
)

OPEN_PLAN_UNIQUE_INDEX = "uq_migration_plans_service_id_open"
PREVENT_DELETE_TRIGGER = "trg_migration_plans_prevent_delete"
MIN_OBSERVE_SECONDS = 1
MAX_OBSERVE_SECONDS = 600
LAST_STEP_INDEX = 4


def _values(values: Iterable[StrEnum]) -> str:
    return ", ".join(f"'{member.value}'" for member in values)


# 부분 unique 조건과 CHECK 에 쓰는 값 목록. 정의 순서를 고정해 DB 정의와 글자까지 같게 한다.
_OPEN_STATUS_VALUES = _values(
    [status for status in MigrationPlanStatus if status in OPEN_MIGRATION_PLAN_STATUSES]
)
_TERMINAL_STATUS_VALUES = _values(
    [status for status in MigrationPlanStatus if status not in OPEN_MIGRATION_PLAN_STATUSES]
)


class MigrationPlan(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """이전 계획 하나: 서비스의 트래픽을 출발 연결(지금 100%)에서 도착 연결(같은 이미지의 예비)로
    옮기는 계획과 그 실행 기록. 지우지 않는다(DELETE 금지 트리거).

    - steps 는 만들 때 한 번 쓰고 바꾸지 않는다(고정 5단계와 단계별 예상 시간).
    - step_results 는 steps 와 같은 길이이고, 바꿀 때 새 리스트를 통째로 다시 넣는다.
    - fingerprint 는 만든 순간의 상태다. 승인과 일꾼 시작 때 다시 계산해 다르면 STALE 로 끝낸다.
      비밀값·설정 값 자체는 넣지 않고 해시만 넣는다.
    - 상태는 조건부 UPDATE 로만 바꾼다. 서비스당 끝나지 않은 계획은 하나다(부분 unique).
    - current_step·step_phase·observe_until·traffic_requested_at·rollback_requested_at 은 일꾼이
      어디까지 했는지 적어 두는 칸이다. 다시 시작한 일꾼은 이 칸을 보고 같은 멱등 키로 이어 간다.
    - simulated 는 가짜 실행기로 만든 계획인지다(만들 때 저장, 응답 때 다시 계산하지 않는다).
    - mode 는 계획 종류(PLANNED·EMERGENCY·FAILBACK)다. 만들 때 판정해 저장하고 바꾸지 않는다.
    """

    __tablename__ = "migration_plans"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(MigrationPlanStatus)})", name="status_valid"),
        CheckConstraint(f"mode IN ({_values(MigrationPlanMode)})", name="mode_valid"),
        CheckConstraint(f"step_phase IN ({_values(MigrationStepPhase)})", name="step_phase_valid"),
        CheckConstraint(f"error_code IN ({_values(MigrationErrorCode)})", name="error_code_valid"),
        CheckConstraint(
            f"rollback_status IN ({_values(MigrationRollbackStatus)})",
            name="rollback_status_valid",
        ),
        CheckConstraint(
            "target_service_environment_id <> source_service_environment_id",
            name="target_not_source",
        ),
        CheckConstraint(
            f"observe_seconds BETWEEN {MIN_OBSERVE_SECONDS} AND {MAX_OBSERVE_SECONDS}",
            name="observe_seconds_range",
        ),
        CheckConstraint(f"current_step BETWEEN 0 AND {LAST_STEP_INDEX}", name="current_step_range"),
        CheckConstraint("rollback_attempts >= 0", name="rollback_attempts_non_negative"),
        CheckConstraint("expected_seconds >= 0", name="expected_seconds_non_negative"),
        CheckConstraint(
            f"(status IN ({_TERMINAL_STATUS_VALUES})) = (finished_at IS NOT NULL)",
            name="finished_at_consistency",
        ),
        CheckConstraint(
            "(status IN ('ROLLING_BACK', 'ROLLED_BACK', 'FAILED')) = (error_code IS NOT NULL)",
            name="error_code_consistency",
        ),
        CheckConstraint(
            "(approved_by_user_id IS NULL) = (approved_at IS NULL)",
            name="approved_consistency",
        ),
        CheckConstraint("jsonb_typeof(steps) = 'array'", name="steps_is_array"),
        CheckConstraint("jsonb_typeof(step_results) = 'array'", name="step_results_is_array"),
        CheckConstraint(
            "jsonb_typeof(rollback_conditions) = 'object'", name="rollback_conditions_is_object"
        ),
        CheckConstraint("jsonb_typeof(fingerprint) = 'object'", name="fingerprint_is_object"),
        CheckConstraint("jsonb_typeof(simulate) = 'object'", name="simulate_is_object"),
        Index("ix_migration_plans_service_id_created_at", "service_id", "created_at"),
        Index(
            OPEN_PLAN_UNIQUE_INDEX,
            "service_id",
            unique=True,
            postgresql_where=text(f"status IN ({_OPEN_STATUS_VALUES})"),
        ),
    )

    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="RESTRICT"), nullable=False
    )
    source_service_environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("service_environments.id", ondelete="RESTRICT"), nullable=False
    )
    target_service_environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("service_environments.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[MigrationPlanStatus] = mapped_column(String(16), nullable=False)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    step_results: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    rollback_conditions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    observe_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    fingerprint: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # None 은 JSON null 이 아니라 SQL NULL 로 저장한다(simulate 가 없는 계획).
    simulate: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    current_step: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default=text("0")
    )
    step_phase: Mapped[MigrationStepPhase | None] = mapped_column(String(8), nullable=True)
    phase_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    observe_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    traffic_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rollback_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rollback_attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default=text("0")
    )
    error_code: Mapped[MigrationErrorCode | None] = mapped_column(String(32), nullable=True)
    rollback_status: Mapped[MigrationRollbackStatus | None] = mapped_column(
        String(16), nullable=True
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    rejected_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    cancelled_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    aborted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    self_approved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    abort_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # API 서버 8-3 에 더한 칸이라 DB 에서 맨 뒤(믹스인 칸 뒤)에 있다. 그 전 계획은 PLANNED 다.
    mode: Mapped[MigrationPlanMode] = mapped_column(
        String(16),
        nullable=False,
        default=MigrationPlanMode.PLANNED,
        server_default=MigrationPlanMode.PLANNED.value,
        sort_order=APPENDED_COLUMN_SORT_ORDER,
    )
