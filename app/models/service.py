from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.enums import ServiceStatus, TeardownErrorCode
from app.models.base import (
    APPENDED_COLUMN_SORT_ORDER,
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

REPO_URL_MAX_LENGTH = 300
HEALTH_CHECK_PATH_MAX_LENGTH = 255
PUBLIC_HOST_MAX_LENGTH = 253
DEFAULT_HEALTH_CHECK_PATH = "/healthz"
DEFAULT_HEALTH_CHECK_TIMEOUT_SECONDS = 300
MIN_HEALTH_CHECK_TIMEOUT_SECONDS = 10
MAX_HEALTH_CHECK_TIMEOUT_SECONDS = 1800
DEFAULT_CONTAINER_PORT = 8080
HOST_SUFFIX_LENGTH = 8
_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in ServiceStatus)


class Service(UuidPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """사용자가 배포하는 앱 하나. 여러 환경에 동시에 있을 수 있다(서비스-환경 연결 N개).

    - repo_url: 정규화한 GitHub 저장소 주소. repo_private 는 등록·수정 때 GitHub 가 알려 준 값이다.
    - host_suffix: 소문자·숫자 8자 무작위 값. 만들 때 한 번 정하고 바꾸지 않는다. 대표·환경별 주소가
      같이 쓴다(지운 서비스와 같은 이름으로 새 서비스를 만들어도 옛 주소를 잇지 못하게).
    - public_host: 대표 주소 `{name}-{host_suffix}.{public_base_domain}`. 만들 때 한 번 정해
      저장한다.
    - status: ACTIVE → DELETING(모든 연결 내리는 중) → 소프트 삭제.
      delete_error 는 DELETING 일 때만 있다.
    - simulated: 가짜 소스 저장소 클라이언트로 등록했는지(저장 값, 응답 때 다시 계산하지 않는다).
    """

    __tablename__ = "services"
    __table_args__ = (
        resource_name_check(),
        soft_delete_consistency_check(),
        CheckConstraint("health_check_path LIKE '/%'", name="health_check_path_format"),
        CheckConstraint(
            "health_check_timeout_seconds BETWEEN "
            f"{MIN_HEALTH_CHECK_TIMEOUT_SECONDS} AND {MAX_HEALTH_CHECK_TIMEOUT_SECONDS}",
            name="health_check_timeout_range",
        ),
        CheckConstraint("container_port BETWEEN 1 AND 65535", name="container_port_range"),
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="status_valid"),
        CheckConstraint(
            f"delete_error IS NULL OR status = '{ServiceStatus.DELETING.value}'",
            name="delete_error_only_when_deleting",
        ),
        CheckConstraint(
            f"host_suffix ~ '^[a-z0-9]{{{HOST_SUFFIX_LENGTH}}}$'", name="host_suffix_format"
        ),
        # 이름은 조직 안에서만 유일하다. 주소 유일성은 host_suffix 와 public_host 인덱스가 맡는다.
        Index(
            "uq_services_org_name_live",
            "organization_id",
            "name",
            unique=True,
            postgresql_where=text(LIVE_ROWS_CONDITION),
        ),
        Index(
            "uq_services_public_host_live",
            "public_host",
            unique=True,
            postgresql_where=text(LIVE_ROWS_CONDITION),
        ),
        Index("ix_services_organization_id", "organization_id"),
    )

    name: Mapped[str] = mapped_column(String(RESOURCE_NAME_MAX_LENGTH), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    repo_url: Mapped[str] = mapped_column(
        String(REPO_URL_MAX_LENGTH), nullable=False, sort_order=APPENDED_COLUMN_SORT_ORDER
    )
    repo_private: Mapped[bool] = mapped_column(
        Boolean, nullable=False, sort_order=APPENDED_COLUMN_SORT_ORDER
    )
    health_check_path: Mapped[str] = mapped_column(
        String(HEALTH_CHECK_PATH_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_HEALTH_CHECK_PATH,
        server_default=text(f"'{DEFAULT_HEALTH_CHECK_PATH}'"),
        sort_order=APPENDED_COLUMN_SORT_ORDER,
    )
    health_check_timeout_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_HEALTH_CHECK_TIMEOUT_SECONDS,
        server_default=text(str(DEFAULT_HEALTH_CHECK_TIMEOUT_SECONDS)),
        sort_order=APPENDED_COLUMN_SORT_ORDER,
    )
    container_port: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_CONTAINER_PORT,
        server_default=text(str(DEFAULT_CONTAINER_PORT)),
        sort_order=APPENDED_COLUMN_SORT_ORDER,
    )
    public_host: Mapped[str] = mapped_column(
        String(PUBLIC_HOST_MAX_LENGTH), nullable=False, sort_order=APPENDED_COLUMN_SORT_ORDER
    )
    status: Mapped[ServiceStatus] = mapped_column(
        String(16),
        nullable=False,
        default=ServiceStatus.ACTIVE,
        server_default=text(f"'{ServiceStatus.ACTIVE.value}'"),
        sort_order=APPENDED_COLUMN_SORT_ORDER,
    )
    delete_error: Mapped[TeardownErrorCode | None] = mapped_column(
        String(32), nullable=True, sort_order=APPENDED_COLUMN_SORT_ORDER
    )
    simulated: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        sort_order=APPENDED_COLUMN_SORT_ORDER,
    )
    # 서비스가 속한 조직. 서비스 이름은 이 조직 안에서 유일하다.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        sort_order=APPENDED_COLUMN_SORT_ORDER + 1,
    )
    host_suffix: Mapped[str] = mapped_column(
        CHAR(HOST_SUFFIX_LENGTH), nullable=False, sort_order=APPENDED_COLUMN_SORT_ORDER + 2
    )

    service_environments: Mapped[list[ServiceEnvironment]] = relationship(back_populates="service")
