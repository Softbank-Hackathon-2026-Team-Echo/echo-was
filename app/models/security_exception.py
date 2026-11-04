import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import SecurityControlId
from app.models.base import Base, TimestampMixin, UuidPrimaryKeyMixin


def _values(enum: type[StrEnum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum)


SECURITY_EXCEPTION_MAX_DAYS = 7
SECURITY_EXCEPTION_REASON_MAX_LENGTH = 1000


class SecurityException(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """기한부 예외 한 건: 통제 하나를 범위 안에서 최대 7일 막지 않게 한다(보안·거버넌스 3).

    - 사람만 요청·승인한다(둘 다 users FK). 승인 전에는 판정에 쓰지 않는다.
    - 자기 승인(요청자 = 승인자)은 조직 스위치가 켜져 있을 때만 판정이 받아 준다(규칙표).
    - 범위 칸은 그 통제가 정한 것만 채운다(findings 와 같은 방식). 걸린 통제는 PASS 가 아니라 WARN.
    - 철회하면 그 뒤로 쓰지 않는다. 지우지 않는다.
    """

    __tablename__ = "security_exceptions"
    __table_args__ = (
        CheckConstraint(f"control_id IN ({_values(SecurityControlId)})", name="control_id_valid"),
        CheckConstraint(
            f"char_length(btrim(reason)) BETWEEN 1 AND {SECURITY_EXCEPTION_REASON_MAX_LENGTH}",
            name="reason_present",
        ),
        CheckConstraint(
            "(approved_by_user_id IS NULL) = (approved_at IS NULL)", name="approval_consistency"
        ),
        CheckConstraint(
            "(revoked_by_user_id IS NULL) = (revoked_at IS NULL)", name="revocation_consistency"
        ),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        CheckConstraint(
            f"expires_at <= created_at + interval '{SECURITY_EXCEPTION_MAX_DAYS} days'",
            name="max_duration",
        ),
        CheckConstraint(
            "image_digest IS NULL OR image_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="image_digest_format",
        ),
        Index(
            "ix_security_exceptions_organization_id_control_id_expires_at",
            "organization_id",
            "control_id",
            "expires_at",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    control_id: Mapped[SecurityControlId] = mapped_column(String(16), nullable=False)
    service_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("services.id", ondelete="RESTRICT"), nullable=True
    )
    environment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("environments.id", ondelete="RESTRICT"), nullable=True
    )
    image_digest: Mapped[str | None] = mapped_column(String(71), nullable=True)
    config_commit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    was_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
