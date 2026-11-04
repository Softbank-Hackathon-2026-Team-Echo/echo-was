import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import AgentRunStep, LogLevel
from app.models.base import Base, CreatedAtMixin, UuidPrimaryKeyMixin


def _values(enum: type[StrEnum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum)


AGENT_RUN_EVENT_MESSAGE_MAX_LENGTH = 200
SEQ_UNIQUE_CONSTRAINT = "uq_agent_run_events_run_id_seq"


class AgentRunEvent(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """러너가 낸 진행 이벤트 한 줄(계약 v1 §3.2 events). 지우지 않는다.

    - seq 는 러너가 실행마다 1 부터 매긴다. 같은 seq 를 다시 보내면 무시한다(멱등, unique).
    - message 는 고정 영어 문장, 값은 data(비밀 가리기를 거친 짧은 문자열·숫자)만.
    """

    __tablename__ = "agent_run_events"
    __table_args__ = (
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint(f"step IN ({_values(AgentRunStep)})", name="step_valid"),
        CheckConstraint(f"level IN ({_values(LogLevel)})", name="level_valid"),
        CheckConstraint("data IS NULL OR jsonb_typeof(data) = 'object'", name="data_is_object"),
        UniqueConstraint("run_id", "seq"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="RESTRICT"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    step: Mapped[AgentRunStep] = mapped_column(String(16), nullable=False)
    level: Mapped[LogLevel] = mapped_column(String(8), nullable=False)
    message: Mapped[str] = mapped_column(String(AGENT_RUN_EVENT_MESSAGE_MAX_LENGTH), nullable=False)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
