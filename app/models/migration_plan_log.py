import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import LogLevel
from app.models.base import Base, CreatedAtMixin, UuidPrimaryKeyMixin
from app.models.migration_plan import LAST_STEP_INDEX

_LEVEL_VALUES = ", ".join(f"'{value.value}'" for value in LogLevel)
MIGRATION_LOG_MESSAGE_MAX_LENGTH = 500
MIGRATION_LOG_CODE_MAX_LENGTH = 48
PREVENT_DELETE_TRIGGER = "trg_migration_plan_logs_prevent_delete"
SEQ_UNIQUE_CONSTRAINT = "uq_migration_plan_logs_migration_plan_id_seq"


class MigrationPlanLog(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """이전 실행 로그 한 줄. 지우지 않는다(DELETE 금지 트리거).

    - 그 줄을 일으킨 상태 변경과 같은 트랜잭션에서, 변경이 반영됐을 때만 쓴다.
    - seq 는 계획마다 1 부터 빈틈없이 늘어난다. 서비스 행 잠금을 쥔 채 max(seq) + 1 로 정한다.
    - step 은 계획 단계 번호(steps[i].index)다. 계획 전체에 대한 줄(만들기·승인 등)은 NULL.
    - message 는 코드별 고정 문장, data 는 비율·지표 숫자(낡은 계획이면 달라진 항목 이름)만 담는다.
    """

    __tablename__ = "migration_plan_logs"
    __table_args__ = (
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint(f"step BETWEEN 0 AND {LAST_STEP_INDEX}", name="step_range"),
        CheckConstraint(f"level IN ({_LEVEL_VALUES})", name="level_valid"),
        CheckConstraint("jsonb_typeof(data) = 'object'", name="data_is_object"),
        UniqueConstraint("migration_plan_id", "seq"),
    )

    migration_plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("migration_plans.id", ondelete="RESTRICT"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    step: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    logged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    level: Mapped[LogLevel] = mapped_column(String(8), nullable=False)
    code: Mapped[str] = mapped_column(String(MIGRATION_LOG_CODE_MAX_LENGTH), nullable=False)
    message: Mapped[str] = mapped_column(String(MIGRATION_LOG_MESSAGE_MAX_LENGTH), nullable=False)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False)
