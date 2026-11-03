"""add findings

보안 증거 표 findings(보안·거버넌스 2)를 새로 만든다. 표를 더하기만 하고 기존 표는 바꾸지 않는다.
AI 결과는 PASS 를 가질 수 없고(ai_result_not_pass), 사람 점검 기록은 점검자·근거 링크가
있어야 한다(manual_review_has_basis). downgrade 는 표를 지운다(쌓인 증거도 사라진다).

Revision ID: eee7a6c8229f
Revises: a83f2c1d9e47
Create Date: 2026-11-04 00:13:25.436835

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "eee7a6c8229f"
down_revision: str | Sequence[str] | None = "a83f2c1d9e47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "findings",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=True),
        sa.Column("environment_id", sa.Uuid(), nullable=True),
        sa.Column("image_digest", sa.String(length=71), nullable=True),
        sa.Column("config_commit", sa.String(length=40), nullable=True),
        sa.Column("was_version", sa.String(length=64), nullable=True),
        sa.Column("control_id", sa.String(length=16), nullable=False),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=32), nullable=False),
        sa.Column("tool_version", sa.String(length=32), nullable=True),
        sa.Column("source_ref", sa.String(length=500), nullable=True),
        sa.Column(
            "items",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("total_items", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("reviewer_user_id", sa.Uuid(), nullable=True),
        sa.Column("basis_url", sa.Text(), nullable=True),
        sa.Column("confirmation", sa.String(length=16), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
            "(source = 'AI_REVIEW') = (confirmation IS NOT NULL)",
            name=op.f("ck_findings_ai_confirmation_consistency"),
        ),
        sa.CheckConstraint(
            "confirmation IS NULL OR confirmation IN ('UNCONFIRMED', 'ESCALATED', 'DISMISSED')",
            name=op.f("ck_findings_confirmation_valid"),
        ),
        sa.CheckConstraint(
            "control_id IN ('C-IMG-1', 'C-IMG-2', 'C-IMG-3', 'C-CFG-1', 'C-TLS-1', 'C-TLS-2', "
            "'C-NET-1', 'C-NET-2', 'C-IAM-1', 'C-IAM-2', 'C-SEC-1', 'C-AUD-1', 'C-AUD-2', "
            "'C-CI-1', 'C-AI-1', 'C-DB-1', 'C-DST-1', 'C-POST-1')",
            name=op.f("ck_findings_control_id_valid"),
        ),
        sa.CheckConstraint(
            "image_digest IS NULL OR image_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_findings_image_digest_format"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(items) = 'array'", name=op.f("ck_findings_items_is_array")
        ),
        sa.CheckConstraint(
            "mode IN ('REAL', 'CACHE', 'SIMULATED')", name=op.f("ck_findings_mode_valid")
        ),
        sa.CheckConstraint(
            "result IN ('PASS', 'WARN', 'FAIL', 'UNKNOWN', 'NA')",
            name=op.f("ck_findings_result_valid"),
        ),
        sa.CheckConstraint(
            "source <> 'AI_REVIEW' OR result IN ('WARN', 'FAIL', 'UNKNOWN')",
            name=op.f("ck_findings_ai_result_not_pass"),
        ),
        sa.CheckConstraint(
            "source <> 'MANUAL_REVIEW' OR (reviewer_user_id IS NOT NULL AND basis_url IS NOT NULL)",
            name=op.f("ck_findings_manual_review_has_basis"),
        ),
        sa.CheckConstraint(
            "source IN ('TRIVY_IMAGE', 'TRIVY_CONFIG', 'TRIVY_SECRET', 'GITLEAKS', 'TLS_PROBE', "
            "'MANUAL_REVIEW', 'WAS_TEST', 'WAS_STATE', 'AUDIT_LOG', 'PRECHECK', 'ENTRYPOINT', "
            "'DATA_ENGINE', 'CLOUD_API', 'KUBERNETES_API', 'GITHUB_API', 'AI_REVIEW')",
            name=op.f("ck_findings_source_valid"),
        ),
        sa.CheckConstraint(
            "expires_at > collected_at", name=op.f("ck_findings_expires_after_collected")
        ),
        sa.CheckConstraint("total_items >= 0", name=op.f("ck_findings_total_items_non_negative")),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_findings_environment_id_environments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_findings_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"],
            ["users.id"],
            name=op.f("fk_findings_reviewer_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_findings_service_id_services"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_findings")),
    )
    op.create_index(
        "ix_findings_organization_id_control_id_collected_at",
        "findings",
        ["organization_id", "control_id", "collected_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_findings_organization_id_control_id_collected_at", table_name="findings")
    op.drop_table("findings")
