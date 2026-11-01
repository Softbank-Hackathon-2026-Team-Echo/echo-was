from __future__ import annotations

import uuid
from datetime import datetime

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
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import BuildStatus, DeployRequestErrorCode
from app.models.base import Base, TimestampMixin, UuidPrimaryKeyMixin
from app.models.deployment import IMAGE_DIGEST_PATTERN

COMMIT_SHA_PATTERN = "^[0-9a-f]{40}$"
REPO_URL_MAX_LENGTH = 300
EXTERNAL_REF_MAX_LENGTH = 255
# 같은 서비스·저장소·커밋의 실패하지 않은 빌드는 하나뿐이다(빌드 재사용).
ACTIVE_BUILD_UNIQUE_INDEX = "uq_builds_service_id_repo_url_commit_sha_not_failed"
_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in BuildStatus)


class Build(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """서비스·저장소·커밋으로 이미지 하나를 만드는 일. 빌드 1 : 배포 N(같은 이미지를 여러 환경에).

    - id 가 빌드 실행기의 멱등 키다. start_requested_at 을 먼저 커밋하고 실행기를 부른 뒤
      external_ref 를 적는다(일꾼이 다시 시작해도 같은 키로 다시 부른다).
    - SUCCEEDED 일 때만 image_repository·image_digest 가 있다. 배포는 digest 로만 한다.
    - 실패한 빌드 뒤 같은 커밋 요청은 새 빌드를 만든다(부분 unique 가 FAILED 를 뺀다).
    """

    __tablename__ = "builds"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="status_valid"),
        CheckConstraint(f"commit_sha ~ '{COMMIT_SHA_PATTERN}'", name="commit_sha_format"),
        CheckConstraint(f"image_digest ~ '{IMAGE_DIGEST_PATTERN}'", name="image_digest_format"),
        CheckConstraint(
            "(status = 'SUCCEEDED') = (image_digest IS NOT NULL AND image_repository IS NOT NULL)",
            name="image_consistency",
        ),
        CheckConstraint(
            "(status = 'FAILED') = (error_code IS NOT NULL)", name="error_code_consistency"
        ),
        CheckConstraint(
            "(status IN ('SUCCEEDED', 'FAILED')) = (finished_at IS NOT NULL)",
            name="finished_at_consistency",
        ),
        Index(
            ACTIVE_BUILD_UNIQUE_INDEX,
            "service_id",
            "repo_url",
            "commit_sha",
            unique=True,
            postgresql_where=text("status <> 'FAILED'"),
        ),
    )

    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="RESTRICT"), nullable=False
    )
    repo_url: Mapped[str] = mapped_column(String(REPO_URL_MAX_LENGTH), nullable=False)
    commit_sha: Mapped[str] = mapped_column(CHAR(40), nullable=False)
    status: Mapped[BuildStatus] = mapped_column(
        String(16),
        nullable=False,
        default=BuildStatus.QUEUED,
        server_default=text(f"'{BuildStatus.QUEUED.value}'"),
    )
    image_repository: Mapped[str | None] = mapped_column(String(512), nullable=True)
    image_digest: Mapped[str | None] = mapped_column(String(71), nullable=True)
    start_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    external_ref: Mapped[str | None] = mapped_column(String(EXTERNAL_REF_MAX_LENGTH), nullable=True)
    error_code: Mapped[DeployRequestErrorCode | None] = mapped_column(String(32), nullable=True)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
