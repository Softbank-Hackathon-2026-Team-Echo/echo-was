import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import AgentRunKind, AgentRunStatus
from app.models.base import Base, TimestampMixin, UuidPrimaryKeyMixin


def _values(enum: type[StrEnum] | tuple[StrEnum, ...]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum)


OPEN_STATUSES = (AgentRunStatus.QUEUED, AgentRunStatus.CLAIMED, AgentRunStatus.RUNNING)
CLAIMED_STATUSES = (AgentRunStatus.CLAIMED, AgentRunStatus.RUNNING)
FINISHED_STATUSES = (
    AgentRunStatus.SUCCEEDED,
    AgentRunStatus.FAILED,
    AgentRunStatus.TIMED_OUT,
    AgentRunStatus.CANCELLED,
)
OPEN_RUN_UNIQUE_INDEX = "uq_agent_runs_service_id_open"
AGENT_RUN_FAILURE_CODE_MAX_LENGTH = 32
AGENT_RUN_SCENARIO_REF_MAX_LENGTH = 200


class AgentRun(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """AI 실행 한 건(WAS ↔ 러너 계약 v1 §2). 사전 점검·승인한 패치 반영.

    - 사람이 만들면 QUEUED. 러너가 가져가면 CLAIMED(실행 토큰 해시·임대), 시작하면 RUNNING.
    - 끝 상태(SUCCEEDED·FAILED·TIMED_OUT·CANCELLED)에는 finished_at 이 있다. 자동 재시도 없음.
    - 같은 서비스에 열린 실행(QUEUED·CLAIMED·RUNNING)은 하나뿐이다(부분 unique).
    - input_snapshot·result 에는 비밀값이 없다(만들 때 비밀 참조만, 결과는 WAS 가 검증한다).
    - 상태는 조건부 UPDATE 로만 바꾼다. 지우지 않는다.
    """

    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(f"kind IN ({_values(AgentRunKind)})", name="kind_valid"),
        CheckConstraint(f"status IN ({_values(AgentRunStatus)})", name="status_valid"),
        CheckConstraint(
            f"status NOT IN ({_values(CLAIMED_STATUSES)}) OR (claimed_by_runner_id IS NOT NULL "
            "AND run_token_hash IS NOT NULL AND lease_until IS NOT NULL)",
            name="claim_consistency",
        ),
        CheckConstraint(
            f"(status IN ({_values(FINISHED_STATUSES)})) = (finished_at IS NOT NULL)",
            name="finished_consistency",
        ),
        CheckConstraint(
            "failure_code IS NULL OR status IN ('FAILED', 'TIMED_OUT')",
            name="failure_code_only_when_failed",
        ),
        CheckConstraint(
            "result IS NULL OR (status IN ('SUCCEEDED', 'FAILED') "
            "AND jsonb_typeof(result) = 'object')",
            name="result_only_when_reported",
        ),
        CheckConstraint(
            f"kind <> '{AgentRunKind.PRECHECK.value}' OR target_environment_id IS NOT NULL",
            name="precheck_has_target",
        ),
        CheckConstraint(
            f"kind <> '{AgentRunKind.APPLY_PATCH.value}' OR parent_run_id IS NOT NULL",
            name="apply_patch_has_parent",
        ),
        CheckConstraint("deadline_at > created_at", name="deadline_after_created"),
        CheckConstraint("input_hash ~ '^sha256:[0-9a-f]{64}$'", name="input_hash_format"),
        CheckConstraint(
            "run_token_hash IS NULL OR run_token_hash ~ '^[0-9a-f]{64}$'",
            name="run_token_hash_format",
        ),
        CheckConstraint("jsonb_typeof(input_snapshot) = 'object'", name="input_is_object"),
        Index(
            OPEN_RUN_UNIQUE_INDEX,
            "service_id",
            unique=True,
            postgresql_where=text(f"status IN ({_values(OPEN_STATUSES)})"),
        ),
        Index(
            "uq_agent_runs_run_token_hash",
            "run_token_hash",
            unique=True,
            postgresql_where=text("run_token_hash IS NOT NULL"),
        ),
        Index(
            "ix_agent_runs_queued_created_at",
            "created_at",
            postgresql_where=text(f"status = '{AgentRunStatus.QUEUED.value}'"),
        ),
        Index("ix_agent_runs_organization_id_created_at", "organization_id", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[AgentRunKind] = mapped_column(String(16), nullable=False)
    status: Mapped[AgentRunStatus] = mapped_column(
        String(16),
        nullable=False,
        default=AgentRunStatus.QUEUED,
        server_default=text(f"'{AgentRunStatus.QUEUED.value}'"),
    )
    source_environment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("environments.id", ondelete="RESTRICT"), nullable=True
    )
    target_environment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("environments.id", ondelete="RESTRICT"), nullable=True
    )
    # APPLY_PATCH 는 패치를 낸 사전 점검 실행을 가리킨다.
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="RESTRICT"), nullable=True
    )
    scenario_ref: Mapped[str | None] = mapped_column(
        String(AGENT_RUN_SCENARIO_REF_MAX_LENGTH), nullable=True
    )
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    input_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    claimed_by_runner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runners.id", ondelete="RESTRICT"), nullable=True
    )
    run_token_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(
        String(AGENT_RUN_FAILURE_CODE_MAX_LENGTH), nullable=True
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    simulated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
