"""add security exceptions and agent runs

보안·AI 기반 스키마. 표를 새로 만들기만 한다(기존 표 변경 없음, 기존 데이터 영향 없음).

- security_exceptions: 기한부 예외(사람 요청·승인, 최대 7일, 범위 칸, 철회)
- agent_runners: 샌드박스 AI 러너 신원(토큰 SHA-256 해시)
- agent_runs: AI 실행(사전 점검·패치 반영), 서비스당 열린 실행 하나(부분 unique)
- agent_run_events: 러너 진행 이벤트(실행마다 seq unique)
- agent_patch_approvals: AI 패치에 대한 사람의 결정(패치마다 한 번)

원본 계약: echo-ai-agent specs/ai-1-appendix-a-was-runner-contract.md(v1, 11/4 승인).

Revision ID: 201cf5419f4d
Revises: eee7a6c8229f
Create Date: 2026-11-04 20:46:11.056550

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "201cf5419f4d"
down_revision: str | Sequence[str] | None = "eee7a6c8229f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """표 5개를 만든다(FK 순서: 러너 → 실행 → 예외 → 승인 → 이벤트)."""
    op.create_table(
        "agent_runners",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=63), nullable=False),
        sa.Column("token_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "name ~ '^[a-z0-9]([-a-z0-9]*[a-z0-9])?$'", name=op.f("ck_agent_runners_name_format")
        ),
        sa.CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_agent_runners_token_hash_format")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_runners")),
        sa.UniqueConstraint("name", name=op.f("uq_agent_runners_name")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_agent_runners_token_hash")),
    )
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'QUEUED'"), nullable=False
        ),
        sa.Column("source_environment_id", sa.Uuid(), nullable=True),
        sa.Column("target_environment_id", sa.Uuid(), nullable=True),
        sa.Column("parent_run_id", sa.Uuid(), nullable=True),
        sa.Column("scenario_ref", sa.String(length=200), nullable=True),
        sa.Column(
            "input_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("input_hash", sa.String(length=71), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("claimed_by_runner_id", sa.Uuid(), nullable=True),
        sa.Column("run_token_hash", sa.CHAR(length=64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=32), nullable=True),
        sa.Column(
            "result", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column("model_id", sa.String(length=64), nullable=True),
        sa.Column("prompt_version", sa.String(length=32), nullable=True),
        sa.Column("simulated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
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
            "(status IN ('SUCCEEDED', 'FAILED', 'TIMED_OUT', 'CANCELLED')) "
            "= (finished_at IS NOT NULL)",
            name=op.f("ck_agent_runs_finished_consistency"),
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR status IN ('FAILED', 'TIMED_OUT')",
            name=op.f("ck_agent_runs_failure_code_only_when_failed"),
        ),
        sa.CheckConstraint(
            "input_hash ~ '^sha256:[0-9a-f]{64}$'", name=op.f("ck_agent_runs_input_hash_format")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(input_snapshot) = 'object'", name=op.f("ck_agent_runs_input_is_object")
        ),
        sa.CheckConstraint(
            "kind <> 'APPLY_PATCH' OR parent_run_id IS NOT NULL",
            name=op.f("ck_agent_runs_apply_patch_has_parent"),
        ),
        sa.CheckConstraint(
            "kind <> 'PRECHECK' OR target_environment_id IS NOT NULL",
            name=op.f("ck_agent_runs_precheck_has_target"),
        ),
        sa.CheckConstraint(
            "kind IN ('PRECHECK', 'APPLY_PATCH', 'CHANGE_PROPOSAL')",
            name=op.f("ck_agent_runs_kind_valid"),
        ),
        sa.CheckConstraint(
            "result IS NULL OR (status IN ('SUCCEEDED', 'FAILED') "
            "AND jsonb_typeof(result) = 'object')",
            name=op.f("ck_agent_runs_result_only_when_reported"),
        ),
        sa.CheckConstraint(
            "run_token_hash IS NULL OR run_token_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_agent_runs_run_token_hash_format"),
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'CLAIMED', 'RUNNING', 'SUCCEEDED', 'FAILED', "
            "'TIMED_OUT', 'CANCELLED')",
            name=op.f("ck_agent_runs_status_valid"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('CLAIMED', 'RUNNING') OR (claimed_by_runner_id IS NOT NULL "
            "AND run_token_hash IS NOT NULL AND lease_until IS NOT NULL)",
            name=op.f("ck_agent_runs_claim_consistency"),
        ),
        sa.CheckConstraint(
            "deadline_at > created_at", name=op.f("ck_agent_runs_deadline_after_created")
        ),
        sa.ForeignKeyConstraint(
            ["claimed_by_runner_id"],
            ["agent_runners.id"],
            name=op.f("fk_agent_runs_claimed_by_runner_id_agent_runners"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_agent_runs_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_run_id"],
            ["agent_runs.id"],
            name=op.f("fk_agent_runs_parent_run_id_agent_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["users.id"],
            name=op.f("fk_agent_runs_requested_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_agent_runs_service_id_services"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_environment_id"],
            ["environments.id"],
            name=op.f("fk_agent_runs_source_environment_id_environments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_environment_id"],
            ["environments.id"],
            name=op.f("fk_agent_runs_target_environment_id_environments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_runs")),
    )
    op.create_index(
        "ix_agent_runs_organization_id_created_at",
        "agent_runs",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_agent_runs_queued_created_at",
        "agent_runs",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'QUEUED'"),
    )
    op.create_index(
        "uq_agent_runs_run_token_hash",
        "agent_runs",
        ["run_token_hash"],
        unique=True,
        postgresql_where=sa.text("run_token_hash IS NOT NULL"),
    )
    op.create_index(
        "uq_agent_runs_service_id_open",
        "agent_runs",
        ["service_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'CLAIMED', 'RUNNING')"),
    )
    op.create_table(
        "security_exceptions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("control_id", sa.String(length=16), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=True),
        sa.Column("environment_id", sa.Uuid(), nullable=True),
        sa.Column("image_digest", sa.String(length=71), nullable=True),
        sa.Column("config_commit", sa.String(length=40), nullable=True),
        sa.Column("was_version", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("approved_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
            "control_id IN ('C-IMG-1', 'C-IMG-2', 'C-IMG-3', 'C-CFG-1', 'C-TLS-1', "
            "'C-TLS-2', 'C-NET-1', 'C-NET-2', 'C-IAM-1', 'C-IAM-2', 'C-SEC-1', 'C-AUD-1', "
            "'C-AUD-2', 'C-CI-1', 'C-AI-1', 'C-DB-1', 'C-DST-1', 'C-POST-1')",
            name=op.f("ck_security_exceptions_control_id_valid"),
        ),
        sa.CheckConstraint(
            "expires_at <= created_at + interval '7 days'",
            name=op.f("ck_security_exceptions_max_duration"),
        ),
        sa.CheckConstraint(
            "image_digest IS NULL OR image_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_security_exceptions_image_digest_format"),
        ),
        sa.CheckConstraint(
            "(approved_by_user_id IS NULL) = (approved_at IS NULL)",
            name=op.f("ck_security_exceptions_approval_consistency"),
        ),
        sa.CheckConstraint(
            "(revoked_by_user_id IS NULL) = (revoked_at IS NULL)",
            name=op.f("ck_security_exceptions_revocation_consistency"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(reason)) BETWEEN 1 AND 1000",
            name=op.f("ck_security_exceptions_reason_present"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_security_exceptions_expires_after_created")
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"],
            ["users.id"],
            name=op.f("fk_security_exceptions_approved_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_security_exceptions_environment_id_environments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_security_exceptions_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["users.id"],
            name=op.f("fk_security_exceptions_requested_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by_user_id"],
            ["users.id"],
            name=op.f("fk_security_exceptions_revoked_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_security_exceptions_service_id_services"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_security_exceptions")),
    )
    op.create_index(
        "ix_security_exceptions_organization_id_control_id_expires_at",
        "security_exceptions",
        ["organization_id", "control_id", "expires_at"],
        unique=False,
    )
    op.create_table(
        "agent_patch_approvals",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("patch_id", sa.String(length=64), nullable=False),
        sa.Column("patch_hash", sa.String(length=71), nullable=False),
        sa.Column("checks_digest", sa.String(length=71), nullable=False),
        sa.Column("policy_version", sa.String(length=32), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("decided_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("apply_run_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "checks_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_agent_patch_approvals_checks_digest_format"),
        ),
        sa.CheckConstraint(
            "decision = 'APPROVED' OR apply_run_id IS NULL",
            name=op.f("ck_agent_patch_approvals_apply_run_only_when_approved"),
        ),
        sa.CheckConstraint(
            "decision IN ('APPROVED', 'REJECTED')",
            name=op.f("ck_agent_patch_approvals_decision_valid"),
        ),
        sa.CheckConstraint(
            "patch_hash ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_agent_patch_approvals_patch_hash_format"),
        ),
        sa.ForeignKeyConstraint(
            ["apply_run_id"],
            ["agent_runs.id"],
            name=op.f("fk_agent_patch_approvals_apply_run_id_agent_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["users.id"],
            name=op.f("fk_agent_patch_approvals_decided_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_runs.id"],
            name=op.f("fk_agent_patch_approvals_run_id_agent_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_patch_approvals")),
        sa.UniqueConstraint(
            "run_id", "patch_id", name=op.f("uq_agent_patch_approvals_run_id_patch_id")
        ),
    )
    op.create_table(
        "agent_run_events",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("step", sa.String(length=16), nullable=False),
        sa.Column("level", sa.String(length=8), nullable=False),
        sa.Column("message", sa.String(length=200), nullable=False),
        sa.Column(
            "data", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "data IS NULL OR jsonb_typeof(data) = 'object'",
            name=op.f("ck_agent_run_events_data_is_object"),
        ),
        sa.CheckConstraint(
            "level IN ('INFO', 'WARN', 'ERROR')", name=op.f("ck_agent_run_events_level_valid")
        ),
        sa.CheckConstraint(
            "step IN ('SANDBOX_CREATE', 'INPUT_LOAD', 'CONFIG_DIFF', 'ANALYZE', "
            "'PATCH_PROPOSE', 'PATCH_VERIFY', 'SANDBOX_DELETE')",
            name=op.f("ck_agent_run_events_step_valid"),
        ),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_agent_run_events_seq_positive")),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_runs.id"],
            name=op.f("fk_agent_run_events_run_id_agent_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_run_events")),
        sa.UniqueConstraint("run_id", "seq", name=op.f("uq_agent_run_events_run_id_seq")),
    )


def downgrade() -> None:
    """만든 역순으로 지운다."""
    op.drop_table("agent_run_events")
    op.drop_table("agent_patch_approvals")
    op.drop_index(
        "ix_security_exceptions_organization_id_control_id_expires_at",
        table_name="security_exceptions",
    )
    op.drop_table("security_exceptions")
    op.drop_index(
        "uq_agent_runs_service_id_open",
        table_name="agent_runs",
        postgresql_where=sa.text("status IN ('QUEUED', 'CLAIMED', 'RUNNING')"),
    )
    op.drop_index(
        "uq_agent_runs_run_token_hash",
        table_name="agent_runs",
        postgresql_where=sa.text("run_token_hash IS NOT NULL"),
    )
    op.drop_index(
        "ix_agent_runs_queued_created_at",
        table_name="agent_runs",
        postgresql_where=sa.text("status = 'QUEUED'"),
    )
    op.drop_index("ix_agent_runs_organization_id_created_at", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_table("agent_runners")
