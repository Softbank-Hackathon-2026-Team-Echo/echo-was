import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import DependencySnapshotSource
from app.models.base import Base, TimestampMixin, UuidPrimaryKeyMixin
from app.models.deployment import IMAGE_DIGEST_PATTERN

_SOURCE_VALUES = ", ".join(f"'{source.value}'" for source in DependencySnapshotSource)
DEPENDENCY_SNAPSHOT_SOURCE_REF_MAX_LENGTH = 500


class ServiceDependencySnapshot(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """서비스 이미지 하나에 든 의존성(패키지 이름·버전) 목록(SPEC sec-6-cve-watch).

    - 이미지 digest 단위다. 같은 digest 가 여러 환경에 떠 있으면 한 목록을 같이 쓴다.
    - packages: [{ecosystem, name, version, purl?}] — OSV 생태계 이름(PyPI·npm·Debian:12 …).
      비밀값은 없다(패키지 이름·버전·purl 만).
    - 같은 서비스·digest 를 다시 받으면 새 행이다. CVE 감시는 가장 최근 행을 쓴다.
    """

    __tablename__ = "service_dependency_snapshots"
    __table_args__ = (
        CheckConstraint(f"source IN ({_SOURCE_VALUES})", name="source_valid"),
        CheckConstraint(f"image_digest ~ '{IMAGE_DIGEST_PATTERN}'", name="image_digest_format"),
        CheckConstraint("jsonb_typeof(packages) = 'array'", name="packages_is_array"),
        CheckConstraint("package_count >= 0", name="package_count_non_negative"),
        Index(
            "ix_service_dependency_snapshots_service_digest_collected",
            "service_id",
            "image_digest",
            "collected_at",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="RESTRICT"), nullable=False
    )
    image_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    source: Mapped[DependencySnapshotSource] = mapped_column(String(16), nullable=False)
    # 원문 위치(검사 워크플로 실행·러너 실행 id). 외부 시스템 표기 그대로.
    source_ref: Mapped[str | None] = mapped_column(
        String(DEPENDENCY_SNAPSHOT_SOURCE_REF_MAX_LENGTH), nullable=True
    )
    packages: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    package_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
