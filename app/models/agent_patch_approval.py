import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import AgentPatchDecision
from app.models.base import Base, CreatedAtMixin, UuidPrimaryKeyMixin


def _values(enum: type[StrEnum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum)


AGENT_PATCH_ID_MAX_LENGTH = 64


class AgentPatchApproval(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """AI 패치 하나에 대한 사람의 결정(계약 v1 §4). 패치마다 한 번, 지우지 않는다.

    - 지문 = patch_hash + checks_digest + policy_version. 결정할 때 다시 계산해 같아야 한다.
    - 승인하면 WAS 가 반영 실행(APPLY_PATCH)을 만들고 apply_run_id 로 잇는다.
    - 개발자 이상의 사람만 결정한다(에이전트 불가). 자기 승인은 조직 스위치를 따른다.
    """

    __tablename__ = "agent_patch_approvals"
    __table_args__ = (
        CheckConstraint(f"decision IN ({_values(AgentPatchDecision)})", name="decision_valid"),
        CheckConstraint("patch_hash ~ '^sha256:[0-9a-f]{64}$'", name="patch_hash_format"),
        CheckConstraint("checks_digest ~ '^sha256:[0-9a-f]{64}$'", name="checks_digest_format"),
        CheckConstraint(
            f"decision = '{AgentPatchDecision.APPROVED.value}' OR apply_run_id IS NULL",
            name="apply_run_only_when_approved",
        ),
        UniqueConstraint("run_id", "patch_id"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="RESTRICT"), nullable=False
    )
    patch_id: Mapped[str] = mapped_column(String(AGENT_PATCH_ID_MAX_LENGTH), nullable=False)
    patch_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    checks_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    decision: Mapped[AgentPatchDecision] = mapped_column(String(16), nullable=False)
    decided_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    apply_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="RESTRICT"), nullable=True
    )
