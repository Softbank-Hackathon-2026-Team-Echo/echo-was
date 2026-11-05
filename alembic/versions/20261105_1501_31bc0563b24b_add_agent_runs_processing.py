"""add agent runs processing

AI 실행의 데이터 레지던시 기록 칸 agent_runs.processing(NULL 허용)과 CHECK 하나를 더한다(AI 3).
더하기만 하고 기존 칸·CHECK·인덱스는 바꾸지 않는다. 기존 실행은 NULL("기록 없음")이다. 롤링 배포
중 옛 파드는 이 칸을 모른 채 동작한다(옛 파드가 받은 processing 본문은 모르는 칸이라 422 다. 러너는
이 리비전 배포 뒤에 칸을 보낸다).

downgrade 는 더한 것만 지운다. 저장된 레지던시 기록이 함께 사라진다.

Revision ID: 31bc0563b24b
Revises: 250e2535417a
Create Date: 2026-11-05 15:01:19.559375

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "31bc0563b24b"
down_revision: str | Sequence[str] | None = "250e2535417a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "agent_runs",
        sa.Column("processing", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_agent_runs_processing_only_when_reported"),
        "agent_runs",
        "processing IS NULL OR (status IN ('SUCCEEDED', 'FAILED') "
        "AND jsonb_typeof(processing) = 'object')",
    )


def downgrade() -> None:
    """Downgrade schema. 저장된 레지던시 기록이 사라진다(모듈 설명 참고)."""
    op.drop_constraint(
        op.f("ck_agent_runs_processing_only_when_reported"), "agent_runs", type_="check"
    )
    op.drop_column("agent_runs", "processing")
