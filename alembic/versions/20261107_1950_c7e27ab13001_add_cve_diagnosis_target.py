"""Add CVE diagnosis target; retain failed-deployment diagnosis.

Revision ID: c7e27ab13001
Revises: 9d7fe177bcaf
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7e27ab13001"
down_revision: str | Sequence[str] | None = "9d7fe177bcaf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("finding_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_agent_runs_finding_id_findings",
        "agent_runs",
        "findings",
        ["finding_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        op.f("ck_agent_runs_diagnosis_has_deploy_request"), "agent_runs", type_="check"
    )
    op.create_check_constraint(
        "diagnosis_has_deploy_request",
        "agent_runs",
        "kind <> 'CHANGE_PROPOSAL' OR (target_environment_id IS NOT NULL AND "
        "((deploy_request_id IS NOT NULL AND finding_id IS NULL) OR "
        "(deploy_request_id IS NULL AND finding_id IS NOT NULL)))",
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM agent_runs WHERE finding_id IS NOT NULL LIMIT 1"))
        .first()
    ):
        raise RuntimeError("CVE diagnosis history exists; forward-only downgrade required")
    op.drop_constraint(
        op.f("ck_agent_runs_diagnosis_has_deploy_request"), "agent_runs", type_="check"
    )
    op.create_check_constraint(
        "diagnosis_has_deploy_request",
        "agent_runs",
        "kind <> 'CHANGE_PROPOSAL' OR "
        "(deploy_request_id IS NOT NULL AND target_environment_id IS NOT NULL)",
    )
    op.drop_constraint("fk_agent_runs_finding_id_findings", "agent_runs", type_="foreignkey")
    op.drop_column("agent_runs", "finding_id")
