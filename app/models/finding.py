import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import (
    AiFindingConfirmation,
    SecurityControlId,
    SecurityFindingMode,
    SecurityFindingSource,
    SecurityResult,
)
from app.models.base import Base, TimestampMixin, UuidPrimaryKeyMixin


def _values(enum: type[StrEnum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum)


_AI = SecurityFindingSource.AI_REVIEW.value
_MANUAL = SecurityFindingSource.MANUAL_REVIEW.value
FINDING_REASON_MAX_LENGTH = 64
FINDING_POLICY_VERSION_MAX_LENGTH = 32
FINDING_TOOL_VERSION_MAX_LENGTH = 32
FINDING_SOURCE_REF_MAX_LENGTH = 500
FINDING_WAS_VERSION_MAX_LENGTH = 64


class Finding(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """보안 증거 한 건(검사 원문을 공통 형태로 바꾼 것). 보안 규칙표가 판정에 쓴다.

    - 범위 칸(service·environment·image_digest·config_commit·was_version)은 통제가 정한 것만 채운다.
    - items: 근거 줄(취약점·설정 위반·비밀 발견 위치·TLS 주소) 목록. 비밀값 원문은 담지 않는다.
    - AI 결과(source = AI_REVIEW)는 PASS 를 가질 수 없고 사람 확인 상태(confirmation)가 있다.
    - 사람 점검 기록(source = MANUAL_REVIEW)은 점검자와 근거 링크가 있어야 한다.
    - 결과는 고치지 않는다. 다시 검사하면 새 행이다(사람 확인 상태만 바뀐다).
    """

    __tablename__ = "findings"
    __table_args__ = (
        CheckConstraint(f"control_id IN ({_values(SecurityControlId)})", name="control_id_valid"),
        CheckConstraint(f"result IN ({_values(SecurityResult)})", name="result_valid"),
        CheckConstraint(f"mode IN ({_values(SecurityFindingMode)})", name="mode_valid"),
        CheckConstraint(f"source IN ({_values(SecurityFindingSource)})", name="source_valid"),
        CheckConstraint(
            f"confirmation IS NULL OR confirmation IN ({_values(AiFindingConfirmation)})",
            name="confirmation_valid",
        ),
        CheckConstraint(
            f"(source = '{_AI}') = (confirmation IS NOT NULL)", name="ai_confirmation_consistency"
        ),
        CheckConstraint(
            f"source <> '{_AI}' OR result IN ('WARN', 'FAIL', 'UNKNOWN')", name="ai_result_not_pass"
        ),
        CheckConstraint(
            f"source <> '{_MANUAL}' OR (reviewer_user_id IS NOT NULL AND basis_url IS NOT NULL)",
            name="manual_review_has_basis",
        ),
        CheckConstraint("expires_at > collected_at", name="expires_after_collected"),
        CheckConstraint(
            "image_digest IS NULL OR image_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="image_digest_format",
        ),
        CheckConstraint("jsonb_typeof(items) = 'array'", name="items_is_array"),
        CheckConstraint("total_items >= 0", name="total_items_non_negative"),
        Index(
            "ix_findings_organization_id_control_id_collected_at",
            "organization_id",
            "control_id",
            "collected_at",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    service_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("services.id", ondelete="RESTRICT"), nullable=True
    )
    environment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("environments.id", ondelete="RESTRICT"), nullable=True
    )
    image_digest: Mapped[str | None] = mapped_column(String(71), nullable=True)
    config_commit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    was_version: Mapped[str | None] = mapped_column(
        String(FINDING_WAS_VERSION_MAX_LENGTH), nullable=True
    )
    control_id: Mapped[SecurityControlId] = mapped_column(String(16), nullable=False)
    result: Mapped[SecurityResult] = mapped_column(String(16), nullable=False)
    mode: Mapped[SecurityFindingMode] = mapped_column(String(16), nullable=False)
    source: Mapped[SecurityFindingSource] = mapped_column(String(32), nullable=False)
    # SecurityFindingReason 값. 값이 늘어도 마이그레이션이 없도록 CHECK 를 걸지 않는다.
    reason: Mapped[str] = mapped_column(String(FINDING_REASON_MAX_LENGTH), nullable=False)
    policy_version: Mapped[str] = mapped_column(
        String(FINDING_POLICY_VERSION_MAX_LENGTH), nullable=False
    )
    tool_version: Mapped[str | None] = mapped_column(
        String(FINDING_TOOL_VERSION_MAX_LENGTH), nullable=True
    )
    # 원문 위치(예: 검사 워크플로 실행·산출물). 외부 시스템 표기 그대로.
    source_ref: Mapped[str | None] = mapped_column(
        String(FINDING_SOURCE_REF_MAX_LENGTH), nullable=True
    )
    items: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    total_items: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    reviewer_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    basis_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmation: Mapped[AiFindingConfirmation | None] = mapped_column(String(16), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
