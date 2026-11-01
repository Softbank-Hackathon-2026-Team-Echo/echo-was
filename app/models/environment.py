from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, Index, String, event, text
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

ENVIRONMENT_KIND_MAX_LENGTH = 32


class Environment(UuidPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """서비스를 올릴 수 있는 곳 하나(AWS·GCP·온프레 클러스터).

    kind 별 고유 설정은 config(jsonb) 한 칸에 두고, 형식은 app.adapters 의 설정 양식이 저장 전에
    검사한다. 접속 자격 증명은 저장하지 않는다. secret_ref 에 비밀 저장소의 보관 위치만 둔다.

    config 는 변경 추적 dict 가 아니다. 바꿀 때는 새 dict 를 다시 대입한다
    (`env.config = {**env.config, "region": ...}`). 제자리 수정은 저장되지 않는다.
    """

    __tablename__ = "environments"
    __table_args__ = (
        resource_name_check(),
        soft_delete_consistency_check(),
        CheckConstraint("jsonb_typeof(config) = 'object'", name="config_is_object"),
        Index(
            "uq_environments_name_live",
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
    secret_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)

    service_environments: Mapped[list[ServiceEnvironment]] = relationship(
        back_populates="environment"
    )

    def parsed_config(self) -> EnvironmentConfig:
        """저장된 config 를 이 환경 종류의 설정 양식 객체로 되돌린다."""
        return validate_environment_config(self.kind, self.config)


def _validate_and_normalize_config(target: Environment) -> None:
    parsed = validate_environment_config(target.kind, target.config)
    target.config = parsed.model_dump(mode="json")


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
