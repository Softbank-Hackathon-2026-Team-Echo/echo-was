from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, event, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection
from sqlalchemy.orm import (
    Mapped,
    Mapper,
    ORMExecuteState,
    Session,
    attributes,
    mapped_column,
    relationship,
)

from app.adapters.base import EnvironmentConfig
from app.adapters.registry import validate_environment_config
from app.core.exceptions import InvalidInputError
from app.enums import ConnectionErrorCode, EnvironmentConnectionStatus
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

ENVIRONMENT_KIND_MAX_LENGTH = 32
SECRET_REF_MAX_LENGTH = 255
_CONNECTION_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in EnvironmentConnectionStatus)


class Environment(UuidPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """서비스를 올릴 수 있는 곳 하나(AWS·GCP·온프레 클러스터).

    kind 별 고유 설정은 config(jsonb) 한 칸에 두고, 형식은 app.adapters 의 설정 양식이 저장 전에
    검사한다. 접속 자격 증명은 저장하지 않는다. secret_ref 에 비밀 저장소의 보관 위치만 둔다.

    config 는 변경 추적 dict 가 아니다. 바꿀 때는 새 dict 를 다시 대입한다
    (`env.config = {**env.config, "region": ...}`). 제자리 수정은 저장되지 않는다.

    접속 상태 세 칸(connection_*)은 마지막 접속 확인 결과다. UNCHECKED 면 시각·이유가 없고,
    FAILED 일 때만 이유가 있다(DB CHECK). 상태는 아래 동사 메서드로만 바꾼다.
    """

    __tablename__ = "environments"
    __table_args__ = (
        resource_name_check(),
        soft_delete_consistency_check(),
        CheckConstraint("jsonb_typeof(config) = 'object'", name="config_is_object"),
        CheckConstraint(
            f"connection_status IN ({_CONNECTION_STATUS_VALUES})", name="connection_status_valid"
        ),
        CheckConstraint(
            "(connection_status = 'UNCHECKED') = (connection_checked_at IS NULL)",
            name="connection_checked_at_consistency",
        ),
        CheckConstraint(
            "(connection_status = 'FAILED') = (connection_error IS NOT NULL)",
            name="connection_error_consistency",
        ),
        Index(
            "uq_environments_org_name_live",
            "organization_id",
            "name",
            unique=True,
            postgresql_where=text(LIVE_ROWS_CONDITION),
        ),
    )

    name: Mapped[str] = mapped_column(String(RESOURCE_NAME_MAX_LENGTH), nullable=False)
    kind: Mapped[str] = mapped_column(String(ENVIRONMENT_KIND_MAX_LENGTH), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    secret_ref: Mapped[str | None] = mapped_column(String(SECRET_REF_MAX_LENGTH), nullable=True)
    connection_status: Mapped[EnvironmentConnectionStatus] = mapped_column(
        String(16),
        nullable=False,
        default=EnvironmentConnectionStatus.UNCHECKED,
        server_default=text(f"'{EnvironmentConnectionStatus.UNCHECKED.value}'"),
        sort_order=APPENDED_COLUMN_SORT_ORDER,
    )
    connection_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, sort_order=APPENDED_COLUMN_SORT_ORDER
    )
    connection_error: Mapped[ConnectionErrorCode | None] = mapped_column(
        String(32), nullable=True, sort_order=APPENDED_COLUMN_SORT_ORDER
    )
    # 환경이 속한 조직. 이름은 조직 안에서만 유일하다.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        sort_order=APPENDED_COLUMN_SORT_ORDER + 1,
    )

    service_environments: Mapped[list[ServiceEnvironment]] = relationship(
        back_populates="environment"
    )

    def parsed_config(self) -> EnvironmentConfig:
        """저장된 config 를 이 환경 종류의 설정 양식 객체로 되돌린다."""
        return validate_environment_config(self.kind, self.config)

    def mark_connection_unchecked(self) -> None:
        """config·secret_ref 가 바뀌어 이전 확인 결과가 더는 맞지 않는다."""
        self.connection_status = EnvironmentConnectionStatus.UNCHECKED
        self.connection_checked_at = None
        self.connection_error = None

    def record_connection_check(
        self, checked_at: datetime, error: ConnectionErrorCode | None
    ) -> None:
        """접속 확인 결과를 적는다. error 가 없으면 CONNECTED, 있으면 FAILED."""
        self.connection_status = (
            EnvironmentConnectionStatus.CONNECTED
            if error is None
            else EnvironmentConnectionStatus.FAILED
        )
        self.connection_checked_at = checked_at
        self.connection_error = error


def _validate_and_normalize_config(target: Environment) -> None:
    parsed = validate_environment_config(target.kind, target.config)
    # 값이 없는 선택 칸은 키로 남기지 않는다(새 칸을 쓰지 않는 행은 예전과 글자까지 같다).
    target.config = parsed.model_dump(mode="json", exclude_none=True)


@event.listens_for(Environment, "before_insert")
def _check_config_before_insert(
    _mapper: Mapper[Environment], _connection: Connection, target: Environment
) -> None:
    _validate_and_normalize_config(target)


@event.listens_for(Environment, "before_update")
def _check_config_before_update(
    _mapper: Mapper[Environment], _connection: Connection, target: Environment
) -> None:
    state = attributes.instance_state(target)
    if state.attrs.kind.history.has_changes() or state.attrs.config.history.has_changes():
        _validate_and_normalize_config(target)


@event.listens_for(Session, "do_orm_execute")
def _reject_environment_statements(orm_execute_state: ORMExecuteState) -> None:
    """insert(Environment)·update(Environment) 문장 실행을 거부한다.

    이 경로는 매퍼 이벤트를 거치지 않아 설정 검사를 건너뛴다. 환경 저장은 Session 객체 경로만 쓴다.
    """
    if not (orm_execute_state.is_insert or orm_execute_state.is_update):
        return
    mapper = orm_execute_state.bind_mapper
    if mapper is not None and mapper.class_ is Environment:
        raise InvalidInputError(
            "environments must be written through Session objects", action="write_environment"
        )
