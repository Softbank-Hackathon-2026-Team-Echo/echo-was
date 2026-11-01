from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import JobKind, JobStatus
from app.models.base import Base, TimestampMixin, UuidPrimaryKeyMixin

LEASE_OWNER_MAX_LENGTH = 128
# 같은 대상(요청·서비스)의 열린(QUEUED·RUNNING) 할 일은 하나뿐이다.
OPEN_JOB_UNIQUE_INDEX = "uq_jobs_subject_id_open"
_KIND_VALUES = ", ".join(f"'{kind.value}'" for kind in JobKind)
_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in JobStatus)


class Job(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """일꾼이 집어 갈 일 하나. 대상은 kind 에 따라 배포 요청(id) 또는 서비스(id)다.

    일꾼은 `FOR UPDATE SKIP LOCKED` 로 집고 RUNNING + 새 lease_token + 임대 만료 시각을 적는다.
    임대가 끝난 RUNNING 할 일은 다른 일꾼이 집고 lease_losses 를 1 늘린다. 결과는 같은
    lease_token 일 때만 반영한다(조건부 UPDATE).
    """

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(f"kind IN ({_KIND_VALUES})", name="kind_valid"),
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="status_valid"),
        CheckConstraint(
            "(status = 'RUNNING') = (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="lease_consistency",
        ),
        CheckConstraint("lease_losses >= 0", name="lease_losses_non_negative"),
        Index("ix_jobs_status_run_after", "status", "run_after"),
        Index(
            OPEN_JOB_UNIQUE_INDEX,
            "subject_id",
            unique=True,
            postgresql_where=text("status IN ('QUEUED', 'RUNNING')"),
        ),
    )

    kind: Mapped[JobKind] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        String(16),
        nullable=False,
        default=JobStatus.QUEUED,
        server_default=text(f"'{JobStatus.QUEUED.value}'"),
    )
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(LEASE_OWNER_MAX_LENGTH), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_losses: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
