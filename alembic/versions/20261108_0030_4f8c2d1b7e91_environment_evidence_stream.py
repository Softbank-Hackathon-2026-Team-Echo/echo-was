"""환경 자동 증거의 설정 지문과 수집 이력 대체.

Revision ID: 4f8c2d1b7e91
Revises: 1d5d0b3b9598
"""

import sqlalchemy as sa
from alembic import op

revision = "4f8c2d1b7e91"
down_revision = "1d5d0b3b9598"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("findings", sa.Column("collection_key", sa.String(64), nullable=True))
    op.add_column(
        "findings", sa.Column("collection_started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("findings", sa.Column("environment_revision", sa.String(64), nullable=True))
    op.add_column("findings", sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for name in (
        "superseded_at",
        "environment_revision",
        "collection_started_at",
        "collection_key",
    ):
        op.drop_column("findings", name)
