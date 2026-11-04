"""add service environment variables

고객 앱 환경 변수 표 service_environment_variables(API 서버 10)를 새로 만든다. 표·인덱스·CHECK 를
더하기만 하고 기존 표는 바꾸지 않는다. 값 칸은 없다(값은 보관함에만 있다). 옛 파드는 이 표를 읽지
않는다. downgrade 는 표를 지운다(이름·버전 기록이 사라지고 보관함 비밀은 남는다).

Revision ID: 48b2126a9c37
Revises: 201cf5419f4d
Create Date: 2026-11-04 21:37:24.896944

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "48b2126a9c37"
down_revision: str | Sequence[str] | None = "201cf5419f4d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_environment_variables",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("service_environment_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("secret_name", sa.String(length=255), nullable=False),
        sa.Column("latest_version", sa.Integer(), nullable=False),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "key ~ '^[A-Z][A-Z0-9_]{0,63}$'",
            name=op.f("ck_service_environment_variables_key_format"),
        ),
        sa.CheckConstraint(
            "is_deleted = (deleted_at IS NOT NULL)",
            name=op.f("ck_service_environment_variables_soft_delete_consistency"),
        ),
        sa.CheckConstraint(
            "latest_version > 0",
            name=op.f("ck_service_environment_variables_latest_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["service_environment_id"],
            ["service_environments.id"],
            name="fk_service_environment_variables_connection",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name=op.f("fk_service_environment_variables_updated_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_environment_variables")),
    )
    op.create_index(
        "ix_service_environment_variables_connection_key",
        "service_environment_variables",
        ["service_environment_id", "key"],
        unique=False,
    )
    op.create_index(
        "uq_service_environment_variables_connection_key_live",
        "service_environment_variables",
        ["service_environment_id", "key"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_service_environment_variables_connection_key_live",
        table_name="service_environment_variables",
        postgresql_where=sa.text("is_deleted = false"),
    )
    op.drop_index(
        "ix_service_environment_variables_connection_key",
        table_name="service_environment_variables",
    )
    op.drop_table("service_environment_variables")
