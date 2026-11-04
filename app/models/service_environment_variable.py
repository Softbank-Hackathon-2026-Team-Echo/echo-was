from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    LIVE_ROWS_CONDITION,
    Base,
    SoftDeleteMixin,
    TimestampMixin,
    UuidPrimaryKeyMixin,
    soft_delete_consistency_check,
)

if TYPE_CHECKING:
    from app.models.service_environment import ServiceEnvironment
    from app.models.user import User

ENV_VAR_KEY_PATTERN = "^[A-Z][A-Z0-9_]{0,63}$"
ENV_VAR_KEY_MAX_LENGTH = 64
ENV_VAR_SECRET_NAME_MAX_LENGTH = 255


class ServiceEnvironmentVariable(UuidPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """고객 앱 환경 변수 하나(서비스 × 환경 × 키). **값 칸은 없다.**

    값은 보관함(GCP Secret Manager)의 비밀 `secret_name` 에 있고 여기에는 위치와 최신 버전 번호만
    둔다. 같은 키를 지운 뒤 다시 넣으면 새 행이 이전 행의 secret_name 을 그대로 쓰고 보관함 버전이
    이어진다. 그래서 옛 배포 기록의 `versions/<n>` 이 다른 값을 가리키지 않는다.
    """

    __tablename__ = "service_environment_variables"
    __table_args__ = (
        soft_delete_consistency_check(),
        CheckConstraint(f"key ~ '{ENV_VAR_KEY_PATTERN}'", name="key_format"),
        CheckConstraint("latest_version > 0", name="latest_version_positive"),
        Index(
            "uq_service_environment_variables_connection_key_live",
            "service_environment_id",
            "key",
            unique=True,
            postgresql_where=text(LIVE_ROWS_CONDITION),
        ),
        # 지운 행까지 찾는 조회(같은 키를 다시 넣을 때 이전 secret_name)에 쓴다.
        Index(
            "ix_service_environment_variables_connection_key",
            "service_environment_id",
            "key",
        ),
    )

    service_environment_id: Mapped[uuid.UUID] = mapped_column(
        # 기본 이름 규칙으로 만들면 PostgreSQL 식별자 한도(63자)를 넘는다.
        ForeignKey(
            "service_environments.id",
            ondelete="RESTRICT",
            name="fk_service_environment_variables_connection",
        ),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String(ENV_VAR_KEY_MAX_LENGTH), nullable=False)
    secret_name: Mapped[str] = mapped_column(String(ENV_VAR_SECRET_NAME_MAX_LENGTH), nullable=False)
    latest_version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    service_environment: Mapped[ServiceEnvironment] = relationship()
    updated_by: Mapped[User] = relationship()
