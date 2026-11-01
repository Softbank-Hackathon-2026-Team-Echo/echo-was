from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.enums import DeploymentStatus
from app.models.base import Base, TimestampMixin, UuidPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.service_environment import ServiceEnvironment

IMAGE_DIGEST_PATTERN = "^sha256:[0-9a-f]{64}$"
_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in DeploymentStatus)


class Deployment(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """서비스-환경 연결에 이미지 하나를 올린 기록. 지우지 않는 이력이다.

    소프트 삭제 칸이 없고, DELETE 는 DB 트리거가 막는다. 재시도·되돌리기는 상태를 되감지 않고
    새 행을 만든다. 되돌리기 행은 rollback_of_id 로 원본을 가리키고 원본의 image_repository·
    image_digest·config_snapshot 을 복사한다. config_snapshot 에는 비밀값을 넣지 않고 참조만 담는다.
    """

    __tablename__ = "deployments"
    __table_args__ = (
        # service_environments 의 복합 FK 가 가리키는 대상.
        UniqueConstraint("id", "service_environment_id"),
        CheckConstraint(f"image_digest ~ '{IMAGE_DIGEST_PATTERN}'", name="image_digest_format"),
        CheckConstraint(
            "jsonb_typeof(config_snapshot) = 'object'", name="config_snapshot_is_object"
        ),
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="status_valid"),
        CheckConstraint("rollback_of_id <> id", name="rollback_of_not_self"),
        Index(
            "ix_deployments_service_environment_id_created_at",
            "service_environment_id",
            "created_at",
        ),
    )

    service_environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("service_environments.id", ondelete="RESTRICT"), nullable=False
    )
    image_repository: Mapped[str] = mapped_column(String(512), nullable=False)
    image_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[DeploymentStatus] = mapped_column(
        String(16),
        nullable=False,
        default=DeploymentStatus.PENDING,
        server_default=text(f"'{DeploymentStatus.PENDING.value}'"),
    )
    rollback_of_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("deployments.id", ondelete="RESTRICT"), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    service_environment: Mapped[ServiceEnvironment] = relationship(
        back_populates="deployments", foreign_keys=[service_environment_id]
    )
    rollback_of: Mapped[Deployment | None] = relationship(
        remote_side="Deployment.id", foreign_keys=[rollback_of_id]
    )
