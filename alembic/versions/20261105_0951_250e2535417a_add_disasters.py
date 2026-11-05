"""add disasters

재난 이벤트 표 disasters(API 서버 9)와 이전 계획 칸 migration_plans.disaster_id(NULL 허용)를 더한다.
더하기만 하고 기존 칸·CHECK·인덱스는 바꾸지 않는다. 롤링 배포 중 옛 파드는 이 칸을 모른 채 동작한다
(옛 파드가 만든 계획은 disaster_id 가 NULL 이다).

DELETE 금지 트리거는 API 서버 3 의 공용 함수 prevent_append_only_delete() 를 그대로 쓴다(이
revision 은 함수를 만들거나 지우지 않는다).

downgrade 는 더한 것만 지운다. 재난 기록과 계획의 재난 연결 정보가 함께 사라진다(계획 행 자체와
mode = EMERGENCY 값은 남는다).

Revision ID: 250e2535417a
Revises: 286d9ea982d6
Create Date: 2026-11-05 09:51:19.922086

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "250e2535417a"
down_revision: str | Sequence[str] | None = "286d9ea982d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PREVENT_DELETE_FUNCTION = "prevent_append_only_delete"
PREVENT_DELETE_TRIGGER = "trg_disasters_prevent_delete"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "disasters",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), server_default="SIMULATED", nullable=False),
        sa.Column("magnitude", sa.Numeric(precision=3, scale=1), nullable=False),
        sa.Column("epicenter_lat", sa.Double(), nullable=False),
        sa.Column("epicenter_lng", sa.Double(), nullable=False),
        sa.Column("radius_km", sa.Integer(), nullable=False),
        sa.Column("meets_threshold", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "affected_environment_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("safe_environment_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "unknown_environment_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("ended_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "(status = 'ENDED') = (ended_at IS NOT NULL)",
            name=op.f("ck_disasters_ended_at_consistency"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(affected_environment_ids) = 'array'",
            name=op.f("ck_disasters_affected_environment_ids_is_array"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(safe_environment_ids) = 'array'",
            name=op.f("ck_disasters_safe_environment_ids_is_array"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(unknown_environment_ids) = 'array'",
            name=op.f("ck_disasters_unknown_environment_ids_is_array"),
        ),
        sa.CheckConstraint("source IN ('SIMULATED')", name=op.f("ck_disasters_source_valid")),
        sa.CheckConstraint("status IN ('ACTIVE', 'ENDED')", name=op.f("ck_disasters_status_valid")),
        sa.CheckConstraint(
            "type IN ('EARTHQUAKE', 'TYPHOON')", name=op.f("ck_disasters_type_valid")
        ),
        sa.CheckConstraint(
            "epicenter_lat BETWEEN -90 AND 90", name=op.f("ck_disasters_epicenter_lat_range")
        ),
        sa.CheckConstraint(
            "epicenter_lng BETWEEN -180 AND 180", name=op.f("ck_disasters_epicenter_lng_range")
        ),
        sa.CheckConstraint("magnitude BETWEEN 0 AND 10", name=op.f("ck_disasters_magnitude_range")),
        sa.CheckConstraint("radius_km >= 0", name=op.f("ck_disasters_radius_km_non_negative")),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_disasters_created_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ended_by_user_id"],
            ["users.id"],
            name=op.f("fk_disasters_ended_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_disasters_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_disasters")),
    )
    op.create_index(
        "ix_disasters_organization_id_status_created_at",
        "disasters",
        ["organization_id", "status", "created_at"],
        unique=False,
    )
    op.execute(
        f"""
        CREATE TRIGGER {PREVENT_DELETE_TRIGGER}
        BEFORE DELETE ON disasters
        FOR EACH ROW EXECUTE FUNCTION {PREVENT_DELETE_FUNCTION}()
        """
    )
    op.add_column("migration_plans", sa.Column("disaster_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_migration_plans_disaster_id", "migration_plans", ["disaster_id"], unique=False
    )
    op.create_foreign_key(
        op.f("fk_migration_plans_disaster_id_disasters"),
        "migration_plans",
        "disasters",
        ["disaster_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    """Downgrade schema. 재난 기록과 계획의 재난 연결 정보가 사라진다(모듈 설명 참고)."""
    op.drop_constraint(
        op.f("fk_migration_plans_disaster_id_disasters"), "migration_plans", type_="foreignkey"
    )
    op.drop_index("ix_migration_plans_disaster_id", table_name="migration_plans")
    op.drop_column("migration_plans", "disaster_id")
    op.execute(f"DROP TRIGGER {PREVENT_DELETE_TRIGGER} ON disasters")
    op.drop_index("ix_disasters_organization_id_status_created_at", table_name="disasters")
    op.drop_table("disasters")
