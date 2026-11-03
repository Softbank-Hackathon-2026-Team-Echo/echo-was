"""add mode to migration plans

이전 계획 종류 migration_plans.mode(PLANNED·EMERGENCY·FAILBACK, API 서버 8-3)를 더한다. 칸과 CHECK
를 더하기만 한다. 기존 행은 서버 기본값 PLANNED 로 채워진다(그 전 계획은 모두 사람이 만든 계획
이고, 만든 때의 판정을 되살리지 않는다).

autogenerate 는 표가 이미 있을 때 새 CHECK 를 만들지 않아 CHECK 는 직접 적었다.

Revision ID: a83f2c1d9e47
Revises: 6d89c52e39a4
Create Date: 2026-11-03 18:23:24.256150

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a83f2c1d9e47"
down_revision: str | Sequence[str] | None = "6d89c52e39a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 마이그레이션은 app.models 를 import 하지 않는다. 그 시점의 값을 여기에 적는다.
MODE_CHECK = "ck_migration_plans_mode_valid"
MODE_VALUES = "mode IN ('PLANNED', 'EMERGENCY', 'FAILBACK')"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "migration_plans",
        sa.Column("mode", sa.String(length=16), server_default="PLANNED", nullable=False),
    )
    op.create_check_constraint(op.f(MODE_CHECK), "migration_plans", MODE_VALUES)


def downgrade() -> None:
    """Downgrade schema.

    CHECK 와 칸을 지운다. 저장된 종류(FAILBACK 등)는 함께 사라진다(계획의 다른 기록은 남는다).
    """
    op.drop_constraint(op.f(MODE_CHECK), "migration_plans", type_="check")
    op.drop_column("migration_plans", "mode")
