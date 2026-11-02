from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    ColumnElement,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    and_,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.enums import TeardownErrorCode
from app.models.base import (
    APPENDED_COLUMN_SORT_ORDER,
    LIVE_ROWS_CONDITION,
    Base,
    SoftDeleteMixin,
    TimestampMixin,
    UuidPrimaryKeyMixin,
    soft_delete_consistency_check,
)

if TYPE_CHECKING:
    from app.models.deployment import Deployment
    from app.models.environment import Environment
    from app.models.service import Service

# 마이그레이션이 두 표를 만든 뒤 따로 추가하는 순환 FK 이름.
CURRENT_DEPLOYMENT_FK_NAME = "fk_service_environments_current_deployment_id_id_deployments"
PUBLIC_HOST_UNIQUE_INDEX = "uq_service_environments_public_host_live"
PUBLIC_HOST_MAX_LENGTH = 253
# 내리기 실행기가 돌려준 작업 번호의 최대 길이.
EXTERNAL_REF_MAX_LENGTH = 255


class ServiceEnvironment(UuidPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """서비스-환경 연결. 서비스 하나가 여러 환경에 동시에 있으면 연결 행이 여러 개다.

    - current_deployment_id: 지금 이 연결에서 돌고 있는 배포. 같은 연결의 배포만 가리킨다(복합 FK).
    - traffic_weight: 마지막으로 앞단 프록시에 적용한 트래픽 비율(0~100). 실제 분배는 프록시가 한다.
      0 보다 크면 서비스 중, 0 이고 현재 배포가 있으면 예비다(역할은 저장하지 않고 계산한다).
    - public_host: 환경별 주소 `{서비스}-{환경}-{서비스의 host_suffix}.{도메인}`. 만들 때 한 번 정해
      저장한다(환경 이름이 바뀌어도 그대로다).
    - teardown_*: 서비스 삭제 때 이 연결을 내린 기록. 다시 삭제하면 teardown_attempt 가 1 늘고
      멱등 키(연결 id + attempt)가 바뀐다.
    """

    __tablename__ = "service_environments"
    __table_args__ = (
        soft_delete_consistency_check(),
        CheckConstraint("traffic_weight BETWEEN 0 AND 100", name="traffic_weight_range"),
        # (current_deployment_id, id) → deployments(id, service_environment_id).
        # 두 표가 서로 가리키므로 use_alter 로 두 표를 만든 뒤 추가한다.
        ForeignKeyConstraint(
            ["current_deployment_id", "id"],
            ["deployments.id", "deployments.service_environment_id"],
            name=CURRENT_DEPLOYMENT_FK_NAME,
            use_alter=True,
            ondelete="RESTRICT",
        ),
        Index(
            "uq_service_environments_service_id_environment_id_live",
            "service_id",
            "environment_id",
            unique=True,
            postgresql_where=text(LIVE_ROWS_CONDITION),
        ),
        Index(
            PUBLIC_HOST_UNIQUE_INDEX,
            "public_host",
            unique=True,
            postgresql_where=text(LIVE_ROWS_CONDITION),
        ),
    )

    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="RESTRICT"), nullable=False
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="RESTRICT"), nullable=False
    )
    current_deployment_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    traffic_weight: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default=text("0")
    )
    traffic_applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    public_host: Mapped[str] = mapped_column(
        String(PUBLIC_HOST_MAX_LENGTH), nullable=False, sort_order=APPENDED_COLUMN_SORT_ORDER
    )
    teardown_attempt: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        sort_order=APPENDED_COLUMN_SORT_ORDER,
    )
    teardown_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, sort_order=APPENDED_COLUMN_SORT_ORDER
    )
    teardown_external_ref: Mapped[str | None] = mapped_column(
        String(EXTERNAL_REF_MAX_LENGTH), nullable=True, sort_order=APPENDED_COLUMN_SORT_ORDER
    )
    teardown_error: Mapped[TeardownErrorCode | None] = mapped_column(
        String(32), nullable=True, sort_order=APPENDED_COLUMN_SORT_ORDER
    )

    service: Mapped[Service] = relationship(back_populates="service_environments")
    environment: Mapped[Environment] = relationship(back_populates="service_environments")
    deployments: Mapped[list[Deployment]] = relationship(
        back_populates="service_environment",
        foreign_keys="Deployment.service_environment_id",
        order_by="Deployment.created_at",
    )
    # 읽기 전용. 복합 FK 에 id(PK)가 들어 있어 ORM 이 쓰기 관계로 다루면 PK 를 덮어쓸 수 있다.
    # 현재 배포를 바꿀 때는 current_deployment_id 값을 직접 넣는다.
    current_deployment: Mapped[Deployment | None] = relationship(
        primaryjoin=lambda: _current_deployment_join(),
        foreign_keys="[ServiceEnvironment.current_deployment_id, ServiceEnvironment.id]",
        viewonly=True,
    )


def _current_deployment_join() -> ColumnElement[bool]:
    from app.models.deployment import Deployment

    return and_(
        ServiceEnvironment.current_deployment_id == Deployment.id,
        ServiceEnvironment.id == Deployment.service_environment_id,
    )
