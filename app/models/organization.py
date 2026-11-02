import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import OrganizationRole
from app.models.base import (
    APPENDED_COLUMN_SORT_ORDER,
    Base,
    TimestampMixin,
    UuidPrimaryKeyMixin,
)

# 1~39자, 소문자·숫자·하이픈. 하이픈은 처음·끝·연속 금지(조직 열쇠 키에 `__` 가 생기지 않게).
ORGANIZATION_SLUG_PATTERN = "^[a-z0-9](-?[a-z0-9]){0,38}$"
ORGANIZATION_SLUG_MAX_LENGTH = 39
ORGANIZATION_NAME_MAX_LENGTH = 100
ROLE_MAX_LENGTH = 16
ROLE_VALUES = ", ".join(f"'{role.value}'" for role in OrganizationRole)


class Organization(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """고객 회사 하나(개인 조직 포함). 서비스·환경은 조직에 속하고 다른 조직에는 보이지 않는다.

    slug 는 전체에서 유일하고 만든 뒤 바꾸지 않는다(env 비밀 참조의 조직 키가 slug 에서 나온다).
    personal 은 처음 로그인할 때 자동으로 만든 개인 조직이다.
    allow_self_approval 은 이전 계획을 만든 사람이 스스로 승인·거절할 수 있는지다(기본 막음,
    어드민만 바꾼다).
    """

    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint(f"slug ~ '{ORGANIZATION_SLUG_PATTERN}'", name="slug_format"),
        CheckConstraint("char_length(name) >= 1", name="name_not_empty"),
    )

    slug: Mapped[str] = mapped_column(
        String(ORGANIZATION_SLUG_MAX_LENGTH), nullable=False, unique=True
    )
    name: Mapped[str] = mapped_column(String(ORGANIZATION_NAME_MAX_LENGTH), nullable=False)
    personal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    allow_self_approval: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        sort_order=APPENDED_COLUMN_SORT_ORDER,
    )


class OrganizationMember(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """사람 × 조직 × 역할. 한 사람은 조직마다 역할 하나다. 내보내기·탈퇴는 행을 지운다."""

    __tablename__ = "organization_members"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id"),
        CheckConstraint(f"role IN ({ROLE_VALUES})", name="role_valid"),
        Index("ix_organization_members_user_id", "user_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[OrganizationRole] = mapped_column(String(ROLE_MAX_LENGTH), nullable=False)
