import uuid
from datetime import datetime

from sqlalchemy import CHAR, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UuidPrimaryKeyMixin
from app.models.user_session import TOKEN_HASH_LENGTH

AGENT_TOKEN_NAME_MAX_LENGTH = 100


class AgentToken(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """AI 에이전트 출입증. 조직 하나에 묶이고 그 조직을 읽기만 한다.

    원문은 발급 응답에 한 번만 싣고, DB 에는 SHA-256 과 끝 4자리만 둔다.
    만든 사람이 조직을 떠나도 토큰은 남는다(조직의 개발자가 폐기한다).
    """

    __tablename__ = "agent_tokens"
    __table_args__ = (
        Index("ix_agent_tokens_organization_id_created_at", "organization_id", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(AGENT_TOKEN_NAME_MAX_LENGTH), nullable=False)
    token_hash: Mapped[str] = mapped_column(CHAR(TOKEN_HASH_LENGTH), nullable=False, unique=True)
    last4: Mapped[str] = mapped_column(CHAR(4), nullable=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
