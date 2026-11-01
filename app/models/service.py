from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    LIVE_ROWS_CONDITION,
    RESOURCE_NAME_MAX_LENGTH,
    Base,
    SoftDeleteMixin,
    TimestampMixin,
    UuidPrimaryKeyMixin,
    resource_name_check,
    soft_delete_consistency_check,
)

if TYPE_CHECKING:
    from app.models.service_environment import ServiceEnvironment


class Service(UuidPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """사용자가 배포하는 앱 하나. 여러 환경에 동시에 있을 수 있다(서비스-환경 연결 N개)."""

    __tablename__ = "services"
    __table_args__ = (
        resource_name_check(),
        soft_delete_consistency_check(),
        Index(
            "uq_services_name_live",
            "name",
            unique=True,
            postgresql_where=text(LIVE_ROWS_CONDITION),
        ),
    )

    name: Mapped[str] = mapped_column(String(RESOURCE_NAME_MAX_LENGTH), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    service_environments: Mapped[list[ServiceEnvironment]] = relationship(back_populates="service")
