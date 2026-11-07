"""expand migration canary steps

Revision ID: 1d5d0b3b9598
Revises: 9d7fe177bcaf
Create Date: 2026-11-07 20:00:39.939651

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1d5d0b3b9598"
down_revision: str | Sequence[str] | None = "9d7fe177bcaf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """계획·로그 단계 범위만 넓힌다. 저장된 계획은 바꾸지 않는다."""
    _replace_step_checks(8)


def downgrade() -> None:
    """옛 범위로 되돌린다. 단계 6~8을 쓰는 행이 있으면 CHECK가 막는다."""
    _replace_step_checks(5)


def _replace_step_checks(last_step: int) -> None:
    for table, column, name in (
        ("migration_plans", "current_step", "ck_migration_plans_current_step_range"),
        ("migration_plan_logs", "step", "ck_migration_plan_logs_step_range"),
    ):
        op.drop_constraint(op.f(name), table, type_="check")
        op.create_check_constraint(op.f(name), table, f"{column} BETWEEN 0 AND {last_step}")
