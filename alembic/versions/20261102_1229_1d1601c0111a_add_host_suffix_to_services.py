"""add host suffix to services

고객 앱 주소를 "이름 + 무작위 8자"로 바꾼다(API 서버 3-1).

- services.host_suffix: 소문자·숫자 8자(char(8), NOT NULL, 형식 CHECK). 대표 주소
  `{name}-{suffix}.{도메인}` 과 환경별 주소 `{name}-{environment}-{suffix}.{도메인}` 이 같이 쓴다.
- 서비스 이름 유일성을 조직 안으로 좁힌다(uq_services_name_live → uq_services_org_name_live).
  주소 유일성은 public_host 의 전체 unique 인덱스(그대로)가 맡는다.

host_suffix 는 NOT NULL 이고 기본값이 없다(만들 때 서버가 무작위로 정한다). 이 revision 은
services·service_environments 에 행이 없다고 가정하고, 행이 있으면 칸을 더하기 전에 분명한
메시지로 실패한다(옛 형식 주소를 새 형식으로 백필하지 않는다).

autogenerate 가 만들지 못하는 것(빈 표 확인, host_suffix 형식 CHECK)은 직접 적었다.

Revision ID: 1d1601c0111a
Revises: ce35cab9b2e9
Create Date: 2026-11-02 12:29:33.142992

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1d1601c0111a"
down_revision: str | Sequence[str] | None = "ce35cab9b2e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 마이그레이션은 app.models 를 import 하지 않는다. 그 시점의 값을 여기에 적는다.
LIVE_ROWS = sa.text("is_deleted = false")
HOST_SUFFIX_CHECK = "host_suffix ~ '^[a-z0-9]{8}$'"


class ExistingRowsError(RuntimeError):
    pass


def _require_empty(table: str) -> None:
    count = op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {table}"))
    if count:
        raise ExistingRowsError(
            f"{table} has {count} rows: this revision adds services.host_suffix NOT NULL "
            f"without a default and changes the public host format, so it expects {table} "
            "to be empty. Remove the rows or backfill them in a separate revision first."
        )


def upgrade() -> None:
    """Upgrade schema."""
    _require_empty("services")
    _require_empty("service_environments")

    op.add_column("services", sa.Column("host_suffix", sa.CHAR(length=8), nullable=False))
    op.create_check_constraint(
        op.f("ck_services_host_suffix_format"), "services", HOST_SUFFIX_CHECK
    )
    op.drop_index("uq_services_name_live", table_name="services", postgresql_where=LIVE_ROWS)
    op.create_index(
        "uq_services_org_name_live",
        "services",
        ["organization_id", "name"],
        unique=True,
        postgresql_where=LIVE_ROWS,
    )


def downgrade() -> None:
    """Downgrade schema.

    전역 서비스 이름 유일성(uq_services_name_live)을 다시 만들므로, 서로 다른 조직에 같은 이름의
    살아 있는 서비스가 있으면 인덱스를 만들지 못해 실패한다.
    """
    op.drop_index("uq_services_org_name_live", table_name="services", postgresql_where=LIVE_ROWS)
    op.create_index(
        "uq_services_name_live",
        "services",
        ["name"],
        unique=True,
        postgresql_where=LIVE_ROWS,
    )
    op.drop_constraint(op.f("ck_services_host_suffix_format"), "services", type_="check")
    op.drop_column("services", "host_suffix")
