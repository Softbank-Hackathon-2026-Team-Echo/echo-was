"""add migration plans

이전 계획 생성 · 승인 · 실행(API 서버 8)에 필요한 표와 칸을 더한다.

- migration_plans: 이전 계획 하나(출발·도착 연결, 고정 5단계, 롤백 조건, 지문, 진행 위치, 사람 칸).
  서비스당 끝나지 않은 계획은 하나다(부분 unique uq_migration_plans_service_id_open). 지우지 않는
  기록이라 DELETE 를 트리거로 막는다.
- migration_plan_logs: 이전 실행 로그 한 줄(계획마다 seq 1 부터, (migration_plan_id, seq) unique).
  DELETE 금지 트리거.
- organizations.allow_self_approval: 만든 사람의 자기 승인을 허용하는 조직 스위치(기본 false).
  기존 조직 행은 false 로 채워진다.
- jobs.kind CHECK 에 MIGRATION_PLAN 을 더한다(CHECK 를 지우고 다시 만든다).

DELETE 금지 트리거는 API 서버 3 의 공용 함수 prevent_append_only_delete() 를 그대로 쓴다(이
revision 은 함수를 만들거나 지우지 않는다). autogenerate 가 만들지 못하는 것(jobs CHECK 변경,
DELETE 금지 트리거, downgrade 전 행 확인)은 직접 적었다.

Revision ID: 4cea1ec99f1b
Revises: 33ad047a493c
Create Date: 2026-11-02 22:22:38.286924

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "4cea1ec99f1b"
down_revision: str | Sequence[str] | None = "33ad047a493c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 마이그레이션은 app.models 를 import 하지 않는다. 그 시점의 값을 여기에 적는다.
PREVENT_DELETE_FUNCTION = "prevent_append_only_delete"
PLAN_PREVENT_DELETE_TRIGGER = "trg_migration_plans_prevent_delete"
LOG_PREVENT_DELETE_TRIGGER = "trg_migration_plan_logs_prevent_delete"
JOB_KIND_CHECK = "ck_jobs_kind_valid"
OLD_JOB_KIND_VALUES = "kind IN ('DEPLOY_REQUEST', 'SERVICE_TEARDOWN')"
NEW_JOB_KIND_VALUES = "kind IN ('DEPLOY_REQUEST', 'SERVICE_TEARDOWN', 'MIGRATION_PLAN')"


STATUS_VALUES = (
    "status IN ('PENDING_APPROVAL', 'APPROVED', 'RUNNING', 'ROLLING_BACK', 'SUCCEEDED', "
    "'ROLLED_BACK', 'FAILED', 'REJECTED', 'CANCELLED', 'EXPIRED', 'STALE')"
)
FINISHED_AT_CONSISTENCY = (
    "(status IN ('SUCCEEDED', 'ROLLED_BACK', 'FAILED', 'REJECTED', 'CANCELLED', 'EXPIRED', "
    "'STALE')) = (finished_at IS NOT NULL)"
)
ERROR_CODE_VALUES = (
    "error_code IN ('PREFLIGHT_FAILED', 'TRAFFIC_APPLY_FAILED', 'HEALTH_CHECK_FAILED', "
    "'METRICS_UNAVAILABLE', 'THRESHOLD_EXCEEDED', 'DATA_LAG_EXCEEDED', 'ABORTED', "
    "'WORKER_LOST', 'INTERNAL_ERROR')"
)


class MigrationPlanJobsExistError(RuntimeError):
    pass


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "migration_plans",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("source_service_environment_id", sa.Uuid(), nullable=False),
        sa.Column("target_service_environment_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("steps", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("step_results", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rollback_conditions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("observe_seconds", sa.Integer(), nullable=False),
        sa.Column("expected_seconds", sa.Integer(), nullable=False),
        sa.Column("fingerprint", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "simulate", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("current_step", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("step_phase", sa.String(length=8), nullable=True),
        sa.Column("phase_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observe_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("traffic_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rollback_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "rollback_attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("error_code", sa.String(length=32), nullable=True),
        sa.Column("rollback_status", sa.String(length=16), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("approved_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("rejected_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("cancelled_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("aborted_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("self_approved", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("abort_requested_at", sa.DateTime(timezone=True), nullable=True),
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
            "(status IN ('ROLLING_BACK', 'ROLLED_BACK', 'FAILED')) = (error_code IS NOT NULL)",
            name=op.f("ck_migration_plans_error_code_consistency"),
        ),
        sa.CheckConstraint(
            FINISHED_AT_CONSISTENCY,
            name=op.f("ck_migration_plans_finished_at_consistency"),
        ),
        sa.CheckConstraint(
            ERROR_CODE_VALUES,
            name=op.f("ck_migration_plans_error_code_valid"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(fingerprint) = 'object'",
            name=op.f("ck_migration_plans_fingerprint_is_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(rollback_conditions) = 'object'",
            name=op.f("ck_migration_plans_rollback_conditions_is_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(simulate) = 'object'", name=op.f("ck_migration_plans_simulate_is_object")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(step_results) = 'array'",
            name=op.f("ck_migration_plans_step_results_is_array"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(steps) = 'array'", name=op.f("ck_migration_plans_steps_is_array")
        ),
        sa.CheckConstraint(
            "rollback_status IN ('SUCCEEDED', 'FAILED', 'NOT_NEEDED')",
            name=op.f("ck_migration_plans_rollback_status_valid"),
        ),
        sa.CheckConstraint(
            STATUS_VALUES,
            name=op.f("ck_migration_plans_status_valid"),
        ),
        sa.CheckConstraint(
            "step_phase IN ('APPLY', 'OBSERVE', 'JUDGE')",
            name=op.f("ck_migration_plans_step_phase_valid"),
        ),
        sa.CheckConstraint(
            "(approved_by_user_id IS NULL) = (approved_at IS NULL)",
            name=op.f("ck_migration_plans_approved_consistency"),
        ),
        sa.CheckConstraint(
            "current_step BETWEEN 0 AND 4", name=op.f("ck_migration_plans_current_step_range")
        ),
        sa.CheckConstraint(
            "expected_seconds >= 0", name=op.f("ck_migration_plans_expected_seconds_non_negative")
        ),
        sa.CheckConstraint(
            "observe_seconds BETWEEN 1 AND 600",
            name=op.f("ck_migration_plans_observe_seconds_range"),
        ),
        sa.CheckConstraint(
            "rollback_attempts >= 0", name=op.f("ck_migration_plans_rollback_attempts_non_negative")
        ),
        sa.CheckConstraint(
            "target_service_environment_id <> source_service_environment_id",
            name=op.f("ck_migration_plans_target_not_source"),
        ),
        sa.ForeignKeyConstraint(
            ["aborted_by_user_id"],
            ["users.id"],
            name=op.f("fk_migration_plans_aborted_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"],
            ["users.id"],
            name=op.f("fk_migration_plans_approved_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["cancelled_by_user_id"],
            ["users.id"],
            name=op.f("fk_migration_plans_cancelled_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_migration_plans_created_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rejected_by_user_id"],
            ["users.id"],
            name=op.f("fk_migration_plans_rejected_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_migration_plans_service_id_services"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_service_environment_id"],
            ["service_environments.id"],
            name=op.f("fk_migration_plans_source_service_environment_id_service_environments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_service_environment_id"],
            ["service_environments.id"],
            name=op.f("fk_migration_plans_target_service_environment_id_service_environments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_migration_plans")),
    )
    op.create_index(
        "ix_migration_plans_service_id_created_at",
        "migration_plans",
        ["service_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_migration_plans_service_id_open",
        "migration_plans",
        ["service_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('PENDING_APPROVAL', 'APPROVED', 'RUNNING', 'ROLLING_BACK')"
        ),
    )
    op.create_table(
        "migration_plan_logs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("migration_plan_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("step", sa.SmallInteger(), nullable=True),
        sa.Column("logged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.String(length=8), nullable=False),
        sa.Column("code", sa.String(length=48), nullable=False),
        sa.Column("message", sa.String(length=500), nullable=False),
        sa.Column(
            "data", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(data) = 'object'", name=op.f("ck_migration_plan_logs_data_is_object")
        ),
        sa.CheckConstraint(
            "level IN ('INFO', 'WARN', 'ERROR')", name=op.f("ck_migration_plan_logs_level_valid")
        ),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_migration_plan_logs_seq_positive")),
        sa.CheckConstraint("step BETWEEN 0 AND 4", name=op.f("ck_migration_plan_logs_step_range")),
        sa.ForeignKeyConstraint(
            ["migration_plan_id"],
            ["migration_plans.id"],
            name=op.f("fk_migration_plan_logs_migration_plan_id_migration_plans"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_migration_plan_logs")),
        sa.UniqueConstraint(
            "migration_plan_id", "seq", name=op.f("uq_migration_plan_logs_migration_plan_id_seq")
        ),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "allow_self_approval", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    for table, trigger in (
        ("migration_plans", PLAN_PREVENT_DELETE_TRIGGER),
        ("migration_plan_logs", LOG_PREVENT_DELETE_TRIGGER),
    ):
        op.execute(
            f"""
            CREATE TRIGGER {trigger}
            BEFORE DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION {PREVENT_DELETE_FUNCTION}()
            """
        )
    op.drop_constraint(op.f(JOB_KIND_CHECK), "jobs", type_="check")
    op.create_check_constraint(op.f(JOB_KIND_CHECK), "jobs", NEW_JOB_KIND_VALUES)


def downgrade() -> None:
    """Downgrade schema.

    MIGRATION_PLAN 할 일 행이 하나라도 있으면 아무것도 바꾸지 않고 실패한다(옛 jobs CHECK 에 맞지
    않고, 이 revision 은 할 일 이력을 지우지 않는다). 이전 계획·로그 표의 행은 표와 함께 사라진다.
    prevent_append_only_delete() 함수는 API 서버 3 소유라 지우지 않는다.
    """
    count = op.get_bind().scalar(sa.text("SELECT count(*) FROM jobs WHERE kind = 'MIGRATION_PLAN'"))
    if count:
        raise MigrationPlanJobsExistError(
            f"jobs has {count} MIGRATION_PLAN rows: this downgrade would make them invalid and "
            "it does not delete job history. Keep this revision or remove them by hand."
        )

    op.drop_constraint(op.f(JOB_KIND_CHECK), "jobs", type_="check")
    op.create_check_constraint(op.f(JOB_KIND_CHECK), "jobs", OLD_JOB_KIND_VALUES)
    op.execute(f"DROP TRIGGER {LOG_PREVENT_DELETE_TRIGGER} ON migration_plan_logs")
    op.execute(f"DROP TRIGGER {PLAN_PREVENT_DELETE_TRIGGER} ON migration_plans")
    op.drop_column("organizations", "allow_self_approval")
    op.drop_table("migration_plan_logs")
    op.drop_index(
        "uq_migration_plans_service_id_open",
        table_name="migration_plans",
        postgresql_where=sa.text(
            "status IN ('PENDING_APPROVAL', 'APPROVED', 'RUNNING', 'ROLLING_BACK')"
        ),
    )
    op.drop_index("ix_migration_plans_service_id_created_at", table_name="migration_plans")
    op.drop_table("migration_plans")
