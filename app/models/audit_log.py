import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import ActorType, AuditResult
from app.models.base import Base, CreatedAtMixin, UuidPrimaryKeyMixin

_ACTOR_TYPE_VALUES = ", ".join(f"'{value.value}'" for value in ActorType)
_RESULT_VALUES = ", ".join(f"'{value.value}'" for value in AuditResult)
AUDIT_ACTION_MAX_LENGTH = 300
AUDIT_REASON_MAX_LENGTH = 64
REQUEST_ID_MAX_LENGTH = 128


class AuditLog(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """막힌 요청 한 건(이번에는 403 만). 지우지 않는다(DELETE 금지 트리거).

    - action: 메서드 + 경로 틀(`POST /api/v1/orgs/{org_id}/services`). path 는 실제 경로.
    - reason: 오류 코드(ROLE_FORBIDDEN 등).
    - 행위자는 사람(actor_user_id) 또는 에이전트 토큰(actor_agent_token_id) 중 하나다.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint(f"actor_type IN ({_ACTOR_TYPE_VALUES})", name="actor_type_valid"),
        CheckConstraint(f"result IN ({_RESULT_VALUES})", name="result_valid"),
        CheckConstraint(
            f"(actor_type = '{ActorType.USER.value}' AND actor_user_id IS NOT NULL "
            "AND actor_agent_token_id IS NULL) OR "
            f"(actor_type = '{ActorType.AGENT.value}' AND actor_agent_token_id IS NOT NULL "
            "AND actor_user_id IS NULL)",
            name="actor_consistency",
        ),
        Index("ix_audit_logs_organization_id_created_at", "organization_id", "created_at"),
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True
    )
    actor_type: Mapped[ActorType] = mapped_column(String(16), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    actor_agent_token_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_tokens.id", ondelete="RESTRICT"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(AUDIT_ACTION_MAX_LENGTH), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(String(AUDIT_REASON_MAX_LENGTH), nullable=False)
    result: Mapped[AuditResult] = mapped_column(
        String(16),
        nullable=False,
        default=AuditResult.DENIED,
        server_default=text(f"'{AuditResult.DENIED.value}'"),
    )
    request_id: Mapped[str | None] = mapped_column(String(REQUEST_ID_MAX_LENGTH), nullable=True)
