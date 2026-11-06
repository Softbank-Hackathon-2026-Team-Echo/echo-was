"""add live disaster feed

실제 재난 피드(API 서버 18)를 위한 바뀜. 롤링 배포 중 옛 파드가 새 스키마로 돌아도 되게 했다.

- 새 표 disaster_feed_states(피드 조회 상태 + 이미 본 지진 id 기억).
- disasters: feed_event_id·feed_event 칸(NULL 허용) + 같은 조직·같은 피드 지진 하나만
  (부분 유니크 인덱스).
  created_by_user_id 를 NULL 허용으로 푼다(피드 재난은 만든 사람이 없다). source CHECK 에 LIVE_FEED·
  FEED_INJECTED 를 더하고, "버튼 재난만 만든 사람이 있고 피드 원본이 없다" CHECK 2개를 더한다.
  기존 행은 모두 SIMULATED + 만든 사람 있음 + 피드 칸 NULL 이라 새 CHECK 를 만족한다.
- organizations: 피드 조직 기준 live_feed_min_magnitude(기본 6.0)·live_feed_radius_km(기본 300) +
  범위 CHECK. 기존 조직은 기본값을 받는다.

옛 파드는 새 칸을 모르고 SIMULATED 재난만 만든다(새 CHECK 를 만족).
스위치(LIVE_DISASTER_FEED_ENABLED)가 기본 꺼짐이라 배포만으로 피드 재난이 생기지 않는다.

downgrade 는 피드 재난(SIMULATED 가 아닌 행)이 있으면 실패한다(만든 사람 NOT NULL·source CHECK 를
되돌릴 수 없다). 그 행은 지울 수 없으므로(DELETE 금지 트리거) 그런 DB 는 되돌리지 않는다.

Revision ID: 8748e995387a
Revises: 9d8e1f2a3b4c
Create Date: 2026-11-07 02:49:24.754363

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "8748e995387a"
down_revision: str | Sequence[str] | None = "9d8e1f2a3b4c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE_VALID = "ck_disasters_source_valid"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "disaster_feed_states",
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_feed_count", sa.Integer(), nullable=False),
        sa.Column("last_new_event_count", sa.Integer(), nullable=False),
        sa.Column("last_skipped_count", sa.Integer(), nullable=False),
        sa.Column("seen_event_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
            "jsonb_typeof(seen_event_ids) = 'array'",
            name=op.f("ck_disaster_feed_states_seen_event_ids_is_array"),
        ),
        sa.CheckConstraint(
            "last_feed_count >= 0 AND last_new_event_count >= 0 AND last_skipped_count >= 0",
            name=op.f("ck_disaster_feed_states_counts_non_negative"),
        ),
        sa.PrimaryKeyConstraint("provider", name=op.f("pk_disaster_feed_states")),
    )
    op.add_column("disasters", sa.Column("feed_event_id", sa.String(length=128), nullable=True))
    op.add_column(
        "disasters",
        sa.Column("feed_event", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.alter_column("disasters", "created_by_user_id", existing_type=sa.UUID(), nullable=True)
    op.drop_constraint(op.f(SOURCE_VALID), "disasters", type_="check")
    op.create_check_constraint(
        op.f(SOURCE_VALID), "disasters", "source IN ('SIMULATED', 'LIVE_FEED', 'FEED_INJECTED')"
    )
    op.create_check_constraint(
        op.f("ck_disasters_creator_matches_source"),
        "disasters",
        "(source = 'SIMULATED') = (created_by_user_id IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_disasters_feed_event_matches_source"),
        "disasters",
        "(source = 'SIMULATED') = (feed_event_id IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_disasters_feed_event_is_object"),
        "disasters",
        "feed_event IS NULL OR jsonb_typeof(feed_event) = 'object'",
    )
    op.create_index(
        "uq_disasters_organization_id_feed_event_id",
        "disasters",
        ["organization_id", "feed_event_id"],
        unique=True,
        postgresql_where=sa.text("feed_event_id IS NOT NULL"),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "live_feed_min_magnitude",
            sa.Numeric(precision=3, scale=1),
            server_default=sa.text("6.0"),
            nullable=False,
        ),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "live_feed_radius_km", sa.Integer(), server_default=sa.text("300"), nullable=False
        ),
    )
    op.create_check_constraint(
        op.f("ck_organizations_live_feed_min_magnitude_range"),
        "organizations",
        "live_feed_min_magnitude BETWEEN 0 AND 10",
    )
    op.create_check_constraint(
        op.f("ck_organizations_live_feed_radius_km_range"),
        "organizations",
        "live_feed_radius_km BETWEEN 1 AND 2000",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        op.f("ck_organizations_live_feed_radius_km_range"), "organizations", type_="check"
    )
    op.drop_constraint(
        op.f("ck_organizations_live_feed_min_magnitude_range"), "organizations", type_="check"
    )
    op.drop_column("organizations", "live_feed_radius_km")
    op.drop_column("organizations", "live_feed_min_magnitude")
    op.drop_index(
        "uq_disasters_organization_id_feed_event_id",
        table_name="disasters",
        postgresql_where=sa.text("feed_event_id IS NOT NULL"),
    )
    op.drop_constraint(op.f("ck_disasters_feed_event_is_object"), "disasters", type_="check")
    op.drop_constraint(op.f("ck_disasters_feed_event_matches_source"), "disasters", type_="check")
    op.drop_constraint(op.f("ck_disasters_creator_matches_source"), "disasters", type_="check")
    op.drop_constraint(op.f(SOURCE_VALID), "disasters", type_="check")
    op.create_check_constraint(op.f(SOURCE_VALID), "disasters", "source IN ('SIMULATED')")
    op.alter_column("disasters", "created_by_user_id", existing_type=sa.UUID(), nullable=False)
    op.drop_column("disasters", "feed_event")
    op.drop_column("disasters", "feed_event_id")
    op.drop_table("disaster_feed_states")
