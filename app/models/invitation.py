import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import InvitationStatus, OrganizationRole
from app.models.base import Base, TimestampMixin, UuidPrimaryKeyMixin
from app.models.organization import ROLE_MAX_LENGTH, ROLE_VALUES
from app.models.user import GITHUB_LOGIN_MAX_LENGTH

_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in InvitationStatus)
PENDING_CONDITION = f"status = '{InvitationStatus.PENDING.value}'"


class Invitation(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """GitHub 숫자 번호 × 역할 대기. 그 사람이 로그인하거나 /me 를 부르면 멤버가 된다.

    같은 조직·같은 번호의 PENDING 초대는 하나다(재초대는 앞 초대를 REVOKED 로 닫는다).
    github_login 은 초대할 때의 아이디로 표시용이다.
    """

    __tablename__ = "invitations"
    __table_args__ = (
        CheckConstraint(f"role IN ({ROLE_VALUES})", name="role_valid"),
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="status_valid"),
        CheckConstraint(
            f"(status = '{InvitationStatus.ACCEPTED.value}') = (accepted_at IS NOT NULL)",
            name="accepted_at_consistency",
        ),
        Index(
            "uq_invitations_organization_id_github_id_pending",
            "organization_id",
            "github_id",
            unique=True,
            postgresql_where=text(PENDING_CONDITION),
        ),
        Index("ix_invitations_github_id", "github_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    github_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    github_login: Mapped[str] = mapped_column(String(GITHUB_LOGIN_MAX_LENGTH), nullable=False)
    role: Mapped[OrganizationRole] = mapped_column(String(ROLE_MAX_LENGTH), nullable=False)
    status: Mapped[InvitationStatus] = mapped_column(String(16), nullable=False)
    invited_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
