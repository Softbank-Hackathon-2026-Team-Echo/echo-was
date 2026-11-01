"""add connection status to environments

환경 표에 마지막 접속 확인 결과 칸 3개와 CHECK 3개를 더한다.

- connection_status: UNCHECKED·CONNECTED·FAILED. 기존 행은 기본값 UNCHECKED 가 된다.
- connection_checked_at: UNCHECKED 일 때만 NULL.
- connection_error: FAILED 일 때만 값이 있다. 이유 코드 값은 앱이 지키고 DB CHECK 는 걸지 않는다.

CHECK 는 autogenerate 가 만들지 못해 직접 적었다. downgrade 는 CHECK 를 먼저 지우고 칸을 지운다.

Revision ID: 6f6d92350314
Revises: b17994eaa7aa
Create Date: 2026-11-01 23:48:41.734498

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6f6d92350314"
down_revision: str | Sequence[str] | None = "b17994eaa7aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 마이그레이션은 app.models 를 import 하지 않는다. 그 시점의 값을 여기에 적는다.
CHECKS = {
    "ck_environments_connection_status_valid": (
        "connection_status IN ('UNCHECKED', 'CONNECTED', 'FAILED')"
    ),
    "ck_environments_connection_checked_at_consistency": (
        "(connection_status = 'UNCHECKED') = (connection_checked_at IS NULL)"
    ),
    "ck_environments_connection_error_consistency": (
        "(connection_status = 'FAILED') = (connection_error IS NOT NULL)"
    ),
}


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "environments",
        sa.Column(
            "connection_status",
            sa.String(length=16),
            server_default=sa.text("'UNCHECKED'"),
            nullable=False,
        ),
    )
    op.add_column(
        "environments",
        sa.Column("connection_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "environments", sa.Column("connection_error", sa.String(length=32), nullable=True)
    )
    for name, condition in CHECKS.items():
        op.create_check_constraint(op.f(name), "environments", condition)


def downgrade() -> None:
    """Downgrade schema."""
    for name in reversed(CHECKS):
        op.drop_constraint(op.f(name), "environments", type_="check")
    op.drop_column("environments", "connection_error")
    op.drop_column("environments", "connection_checked_at")
    op.drop_column("environments", "connection_status")
