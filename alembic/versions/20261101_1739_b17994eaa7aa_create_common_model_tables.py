"""create common model tables

환경·서비스·서비스-환경 연결·배포 표 4개를 만든다.

- service_environments 의 current 복합 FK 는 두 표가 서로 가리키므로 표를 다 만든 뒤 추가하고,
  downgrade 에서 가장 먼저 지운다.
- deployments 는 지우지 않는 이력이다. BEFORE DELETE 행 트리거가 DELETE 를 막는다.

Revision ID: b17994eaa7aa
Revises:
Create Date: 2026-11-01 17:39:37.679291

"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b17994eaa7aa"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 마이그레이션은 app.models 를 import 하지 않는다. 그 시점의 값을 여기에 적는다.
RESOURCE_NAME_CHECK = "name ~ '^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$'"
SOFT_DELETE_CHECK = "is_deleted = (deleted_at IS NOT NULL)"
LIVE_ROWS = sa.text("is_deleted = false")
CURRENT_DEPLOYMENT_FK = "fk_service_environments_current_deployment_id_id_deployments"
PREVENT_DELETE_FUNCTION = "prevent_deployment_delete"
PREVENT_DELETE_TRIGGER = "trg_deployments_prevent_delete"


def _id_column() -> sa.Column[Any]:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _timestamp_columns() -> list[sa.Column[Any]]:
    return [
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
    ]


def _soft_delete_columns() -> list[sa.Column[Any]]:
    return [
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "services",
        _id_column(),
        sa.Column("name", sa.String(length=63), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        *_timestamp_columns(),
        *_soft_delete_columns(),
        sa.CheckConstraint(RESOURCE_NAME_CHECK, name=op.f("ck_services_name_format")),
        sa.CheckConstraint(SOFT_DELETE_CHECK, name=op.f("ck_services_soft_delete_consistency")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_services")),
    )
    op.create_index(
        "uq_services_name_live", "services", ["name"], unique=True, postgresql_where=LIVE_ROWS
    )

    op.create_table(
        "environments",
        _id_column(),
        sa.Column("name", sa.String(length=63), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("secret_ref", sa.String(length=255), nullable=True),
        *_timestamp_columns(),
        *_soft_delete_columns(),
        sa.CheckConstraint(RESOURCE_NAME_CHECK, name=op.f("ck_environments_name_format")),
        sa.CheckConstraint(SOFT_DELETE_CHECK, name=op.f("ck_environments_soft_delete_consistency")),
        sa.CheckConstraint(
            "jsonb_typeof(config) = 'object'", name=op.f("ck_environments_config_is_object")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_environments")),
    )
    op.create_index(
        "uq_environments_name_live",
        "environments",
        ["name"],
        unique=True,
        postgresql_where=LIVE_ROWS,
    )

    op.create_table(
        "service_environments",
        _id_column(),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("current_deployment_id", sa.Uuid(), nullable=True),
        sa.Column("traffic_weight", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("traffic_applied_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
        *_soft_delete_columns(),
        sa.CheckConstraint(
            SOFT_DELETE_CHECK, name=op.f("ck_service_environments_soft_delete_consistency")
        ),
        sa.CheckConstraint(
            "traffic_weight BETWEEN 0 AND 100",
            name=op.f("ck_service_environments_traffic_weight_range"),
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_service_environments_service_id_services"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_service_environments_environment_id_environments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_environments")),
    )
    op.create_index(
        "uq_service_environments_service_id_environment_id_live",
        "service_environments",
        ["service_id", "environment_id"],
        unique=True,
        postgresql_where=LIVE_ROWS,
    )

    op.create_table(
        "deployments",
        _id_column(),
        sa.Column("service_environment_id", sa.Uuid(), nullable=False),
        sa.Column("image_repository", sa.String(length=512), nullable=False),
        sa.Column("image_digest", sa.String(length=71), nullable=False),
        sa.Column(
            "config_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'PENDING'"), nullable=False
        ),
        sa.Column("rollback_of_id", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "image_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_deployments_image_digest_format"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(config_snapshot) = 'object'",
            name=op.f("ck_deployments_config_snapshot_is_object"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'IN_PROGRESS', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name=op.f("ck_deployments_status_valid"),
        ),
        sa.CheckConstraint(
            "rollback_of_id <> id", name=op.f("ck_deployments_rollback_of_not_self")
        ),
        sa.ForeignKeyConstraint(
            ["service_environment_id"],
            ["service_environments.id"],
            name=op.f("fk_deployments_service_environment_id_service_environments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rollback_of_id"],
            ["deployments.id"],
            name=op.f("fk_deployments_rollback_of_id_deployments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployments")),
        sa.UniqueConstraint(
            "id",
            "service_environment_id",
            name=op.f("uq_deployments_id_service_environment_id"),
        ),
    )
    op.create_index(
        "ix_deployments_service_environment_id_created_at",
        "deployments",
        ["service_environment_id", "created_at"],
        unique=False,
    )

    # 현재 배포는 같은 연결의 배포만 가리킨다.
    # (current_deployment_id, id) → deployments(id, service_environment_id)
    op.create_foreign_key(
        CURRENT_DEPLOYMENT_FK,
        "service_environments",
        "deployments",
        ["current_deployment_id", "id"],
        ["id", "service_environment_id"],
        ondelete="RESTRICT",
    )

    # 배포 행은 지우지 않는다. 23001(restrict_violation)이라 드라이버가 IntegrityError 로 올린다.
    op.execute(
        f"""
        CREATE FUNCTION {PREVENT_DELETE_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'deployments rows are append-only and cannot be deleted'
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {PREVENT_DELETE_TRIGGER}
        BEFORE DELETE ON deployments
        FOR EACH ROW EXECUTE FUNCTION {PREVENT_DELETE_FUNCTION}()
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(f"DROP TRIGGER {PREVENT_DELETE_TRIGGER} ON deployments")
    op.execute(f"DROP FUNCTION {PREVENT_DELETE_FUNCTION}()")
    op.drop_constraint(CURRENT_DEPLOYMENT_FK, "service_environments", type_="foreignkey")

    op.drop_index("ix_deployments_service_environment_id_created_at", table_name="deployments")
    op.drop_table("deployments")
    op.drop_index(
        "uq_service_environments_service_id_environment_id_live",
        table_name="service_environments",
    )
    op.drop_table("service_environments")
    op.drop_index("uq_environments_name_live", table_name="environments")
    op.drop_table("environments")
    op.drop_index("uq_services_name_live", table_name="services")
    op.drop_table("services")
