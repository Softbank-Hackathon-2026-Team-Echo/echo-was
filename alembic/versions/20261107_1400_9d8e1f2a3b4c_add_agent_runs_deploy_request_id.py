"""add agent runs deploy request id

배포 실패 AI 진단(CHANGE_PROPOSAL, SPEC api-98)이 진단하는 배포 요청을 가리키는 칸
agent_runs.deploy_request_id(NULL 허용, FK RESTRICT)와 CHECK 하나, 부분 unique 인덱스 하나를 더한다.
더하기만 하고 기존 칸·CHECK·인덱스는 바꾸지 않는다. 기존 실행은 모두 NULL 이고, 지금까지
CHANGE_PROPOSAL 을 만드는 곳이 없었으므로 새 CHECK 를 어기는 행이 없다. 롤링 배포 중 옛 파드는
이 칸을 모른 채 동작한다(옛 파드에는 진단 경로가 없다).

downgrade 는 더한 것만 지운다. 진단 실행과 배포 요청의 연결이 함께 사라진다.

Revision ID: 9d8e1f2a3b4c
Revises: 31bc0563b24b
Create Date: 2026-11-07 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9d8e1f2a3b4c"
down_revision: str | Sequence[str] | None = "31bc0563b24b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("agent_runs", sa.Column("deploy_request_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_agent_runs_deploy_request_id_deploy_requests"),
        "agent_runs",
        "deploy_requests",
        ["deploy_request_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_agent_runs_diagnosis_has_deploy_request"),
        "agent_runs",
        "kind <> 'CHANGE_PROPOSAL' OR "
        "(deploy_request_id IS NOT NULL AND target_environment_id IS NOT NULL)",
    )
    op.create_index(
        "uq_agent_runs_deploy_request_id_diagnosis",
        "agent_runs",
        ["deploy_request_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'CHANGE_PROPOSAL'"),
    )


def downgrade() -> None:
    """Downgrade schema. 진단 실행과 배포 요청의 연결이 사라진다(모듈 설명 참고)."""
    op.drop_index(
        "uq_agent_runs_deploy_request_id_diagnosis",
        table_name="agent_runs",
        postgresql_where=sa.text("kind = 'CHANGE_PROPOSAL'"),
    )
    op.drop_constraint(
        op.f("ck_agent_runs_diagnosis_has_deploy_request"), "agent_runs", type_="check"
    )
    op.drop_constraint(
        op.f("fk_agent_runs_deploy_request_id_deploy_requests"),
        "agent_runs",
        type_="foreignkey",
    )
    op.drop_column("agent_runs", "deploy_request_id")
