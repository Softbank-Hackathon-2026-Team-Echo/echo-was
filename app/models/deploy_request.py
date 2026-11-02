from __future__ import annotations

import uuid
from datetime import datetime
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

from app.enums import (
    DeployRequestCancelReason,
    DeployRequestErrorCode,
    DeployRequestStatus,
    DeployRequestTrigger,
    HealthCheckResult,
    RefType,
    RevertStatus,
)
from app.models.base import (
    APPENDED_COLUMN_SORT_ORDER,
    Base,
    TimestampMixin,
    UuidPrimaryKeyMixin,
)
from app.models.build import COMMIT_SHA_PATTERN, EXTERNAL_REF_MAX_LENGTH, REPO_URL_MAX_LENGTH

REF_VALUE_MAX_LENGTH = 255
# 같은 연결에는 실행 중 요청 1개 + 대기 요청 1개까지만 둔다(DB 보호망).
ACTIVE_REQUEST_UNIQUE_INDEX = "uq_deploy_requests_service_environment_id_active"
QUEUED_REQUEST_UNIQUE_INDEX = "uq_deploy_requests_service_environment_id_queued"
PREVENT_DELETE_TRIGGER = "trg_deploy_requests_prevent_delete"


def _values(enum: type[Any]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum)


class DeployRequest(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """원터치 배포 한 번(서비스 + 환경 1개 + 브랜치·커밋). 지우지 않는 이력이다(DELETE 트리거).

    - 접수 순간 commit_sha·repo_url·config_snapshot 을 고정한다. 뒤에 서비스가 바뀌어도
      이 값을 쓴다.
    - 상태 QUEUED → BUILDING → DEPLOYING → VERIFYING → SUCCEEDED, 실패는 FAILED, 취소는 QUEUED
      에서만 CANCELLED. 상태는 조건부 UPDATE 로만 바꾼다.
    - build_id 는 이 요청이 쓰는 빌드(재사용이면 build_reused = true), deployment_id 는 빌드 성공
      뒤 만든 배포 기록이다. 배포 기록의 id 가 배포 실행기의 멱등 키다.
    - config_snapshot 에는 비밀값을 넣지 않는다.
    - 되돌리기(trigger = ROLLBACK)는 rollback_of_request_id 로 이력에서 고른 성공 요청을,
      rollback_base_deployment_id 로 접수 순간 연결의 현재 배포를 가리킨다. 시작할 때 현재 배포가
      이 값과 다르면 CANCELLED(BASE_CHANGED) 로 끝낸다.
    """

    __tablename__ = "deploy_requests"
    __table_args__ = (
        CheckConstraint(f"status IN ({_values(DeployRequestStatus)})", name="status_valid"),
        CheckConstraint(f"trigger IN ({_values(DeployRequestTrigger)})", name="trigger_valid"),
        CheckConstraint(f"ref_type IN ({_values(RefType)})", name="ref_type_valid"),
        CheckConstraint(
            f"cancel_reason IN ({_values(DeployRequestCancelReason)})",
            name="cancel_reason_valid",
        ),
        CheckConstraint(f"revert_status IN ({_values(RevertStatus)})", name="revert_status_valid"),
        CheckConstraint(
            f"last_health_result IN ({_values(HealthCheckResult)})",
            name="last_health_result_valid",
        ),
        CheckConstraint(
            "(status = 'CANCELLED') = (cancel_reason IS NOT NULL)",
            name="cancel_reason_consistency",
        ),
        CheckConstraint(
            "(status = 'FAILED') = (error_code IS NOT NULL)", name="error_code_consistency"
        ),
        CheckConstraint(
            "(status IN ('SUCCEEDED', 'FAILED', 'CANCELLED')) = (finished_at IS NOT NULL)",
            name="finished_at_consistency",
        ),
        CheckConstraint(
            "(trigger = 'STANDBY_SYNC') = (source_request_id IS NOT NULL)",
            name="source_request_consistency",
        ),
        CheckConstraint(
            "(build_id IS NULL) = (build_reused IS NULL)", name="build_reused_consistency"
        ),
        CheckConstraint(
            "(trigger = 'ROLLBACK') = (rollback_of_request_id IS NOT NULL)",
            name="rollback_of_request_consistency",
        ),
        CheckConstraint(
            "(trigger = 'ROLLBACK') = (rollback_base_deployment_id IS NOT NULL)",
            name="rollback_base_consistency",
        ),
        CheckConstraint("rollback_of_request_id <> id", name="rollback_of_request_not_self"),
        CheckConstraint(f"commit_sha ~ '{COMMIT_SHA_PATTERN}'", name="commit_sha_format"),
        CheckConstraint(
            "jsonb_typeof(config_snapshot) = 'object'", name="config_snapshot_is_object"
        ),
        CheckConstraint("jsonb_typeof(simulate) = 'object'", name="simulate_is_object"),
        Index("ix_deploy_requests_service_id_created_at", "service_id", "created_at"),
        Index(
            "ix_deploy_requests_service_environment_id_created_at",
            "service_environment_id",
            "created_at",
        ),
        Index("ix_deploy_requests_build_id", "build_id"),
        Index("ix_deploy_requests_rollback_of_request_id", "rollback_of_request_id"),
        Index(
            ACTIVE_REQUEST_UNIQUE_INDEX,
            "service_environment_id",
            unique=True,
            postgresql_where=text("status IN ('BUILDING', 'DEPLOYING', 'VERIFYING')"),
        ),
        Index(
            QUEUED_REQUEST_UNIQUE_INDEX,
            "service_environment_id",
            unique=True,
            postgresql_where=text("status = 'QUEUED'"),
        ),
    )

    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="RESTRICT"), nullable=False
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="RESTRICT"), nullable=False
    )
    service_environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("service_environments.id", ondelete="RESTRICT"), nullable=False
    )
    trigger: Mapped[DeployRequestTrigger] = mapped_column(String(16), nullable=False)
    source_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("deploy_requests.id", ondelete="RESTRICT"), nullable=True
    )
    ref_type: Mapped[RefType] = mapped_column(String(8), nullable=False)
    ref_value: Mapped[str] = mapped_column(String(REF_VALUE_MAX_LENGTH), nullable=False)
    repo_url: Mapped[str] = mapped_column(String(REPO_URL_MAX_LENGTH), nullable=False)
    commit_sha: Mapped[str] = mapped_column(CHAR(40), nullable=False)
    status: Mapped[DeployRequestStatus] = mapped_column(
        String(16),
        nullable=False,
        default=DeployRequestStatus.QUEUED,
        server_default=text(f"'{DeployRequestStatus.QUEUED.value}'"),
    )
    cancel_reason: Mapped[DeployRequestCancelReason | None] = mapped_column(
        String(16), nullable=True
    )
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("deploy_requests.id", ondelete="RESTRICT"), nullable=True
    )
    error_code: Mapped[DeployRequestErrorCode | None] = mapped_column(String(32), nullable=True)
    build_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("builds.id", ondelete="RESTRICT"), nullable=True
    )
    build_reused: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("deployments.id", ondelete="RESTRICT"), nullable=True
    )
    deploy_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deploy_external_ref: Mapped[str | None] = mapped_column(
        String(EXTERNAL_REF_MAX_LENGTH), nullable=True
    )
    revert_status: Mapped[RevertStatus | None] = mapped_column(String(16), nullable=True)
    revert_external_ref: Mapped[str | None] = mapped_column(
        String(EXTERNAL_REF_MAX_LENGTH), nullable=True
    )
    last_health_result: Mapped[HealthCheckResult | None] = mapped_column(String(16), nullable=True)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # None 은 JSON null 이 아니라 SQL NULL 로 저장한다(simulate 가 없는 요청).
    simulate: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    building_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deploying_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verifying_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rollback_of_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("deploy_requests.id", ondelete="RESTRICT"),
        nullable=True,
        sort_order=APPENDED_COLUMN_SORT_ORDER,
    )
    rollback_base_deployment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("deployments.id", ondelete="RESTRICT"),
        nullable=True,
        sort_order=APPENDED_COLUMN_SORT_ORDER,
    )
