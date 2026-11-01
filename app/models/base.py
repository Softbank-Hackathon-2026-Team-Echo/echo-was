import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, MetaData, Uuid, func, text
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 제약 이름을 고정해야 autogenerate 가 제약 변경을 감지한다.
# uq·fk 는 복합 키도 이름이 겹치지 않도록 모든 컬럼 이름을 잇는다(column_0_N_name).
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Kubernetes 이름 규칙(RFC 1123 label): 소문자·숫자·하이픈 1~63자, 처음과 끝은 소문자·숫자.
RESOURCE_NAME_PATTERN = "^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"
RESOURCE_NAME_MAX_LENGTH = 63

# 공통 칸의 표 안 위치: id 를 맨 앞에, 시각·삭제 칸을 맨 뒤에 둔다.
_FIRST_COLUMN = -100
_TRAILING_COLUMN = 100
# 이미 있는 표에 op.add_column 으로 더한 칸은 DB 에서 맨 뒤(믹스인 칸 뒤)에 붙는다.
# 모델 메타데이터의 칸 순서도 같게 하려고 믹스인 칸보다 큰 값을 준다.
APPENDED_COLUMN_SORT_ORDER = 200


class Base(AsyncAttrs, DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UuidPrimaryKeyMixin:
    """PK 는 uuid. ORM 은 Python 에서 만들고(flush 전에 id 를 알 수 있게),
    SQL 직접 입력은 DB 가 만든다.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
        sort_order=_FIRST_COLUMN,
    )


class TimestampMixin:
    """created_at·updated_at(timestamptz, UTC).

    updated_at 은 ORM 으로 행을 수정할 때만 now() 로 바뀐다. Core·raw SQL UPDATE 는 갱신하지 않는다.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        sort_order=_TRAILING_COLUMN,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        sort_order=_TRAILING_COLUMN,
    )

    # 서버가 만든 값(created_at·updated_at 등)을 INSERT·UPDATE 의 RETURNING 으로 바로 읽는다.
    # async 세션에서 만료된 속성을 지연 로드하면 오류가 나기 때문이다.
    __mapper_args__ = {"eager_defaults": True}


class SoftDeleteMixin:
    """소프트 삭제. 쓰는 표는 __table_args__ 에 soft_delete_consistency_check() 를 넣는다.

    살아 있는 행의 유일성은 `WHERE is_deleted = false` 부분 unique index 로 건다.
    """

    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        sort_order=_TRAILING_COLUMN,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, sort_order=_TRAILING_COLUMN
    )

    def mark_as_deleted(self, deleted_at: datetime) -> None:
        self.is_deleted = True
        self.deleted_at = deleted_at


LIVE_ROWS_CONDITION = "is_deleted = false"


def soft_delete_consistency_check() -> CheckConstraint:
    """is_deleted 와 deleted_at 이 항상 함께 움직이게 한다."""
    return CheckConstraint("is_deleted = (deleted_at IS NOT NULL)", name="soft_delete_consistency")


def resource_name_check() -> CheckConstraint:
    return CheckConstraint(f"name ~ '{RESOURCE_NAME_PATTERN}'", name="name_format")
