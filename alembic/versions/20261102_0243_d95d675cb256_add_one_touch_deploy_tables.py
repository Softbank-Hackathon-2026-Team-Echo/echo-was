"""add one touch deploy tables

원터치 배포(API 서버 3)에 필요한 칸과 표를 더한다.

- services: 저장소·헬스체크·포트·대표 주소·상태(ACTIVE·DELETING)·삭제 실패 이유·simulated
  칸과 CHECK.
- service_environments: 환경별 주소, 내리기(teardown) 기록 칸.
- builds: 서비스·저장소·커밋으로 이미지 하나를 만드는 일. 실패하지 않은 빌드는 커밋마다 하나.
- deploy_requests: 원터치 배포 한 번. 같은 연결에 실행 중 1 + 대기 1(부분 unique 2개).
  지우지 않는 이력이라 DELETE 를 트리거로 막는다.
- jobs: 일꾼이 집어 갈 일(임대 칸 포함).

repo_url·repo_private·public_host 는 NOT NULL 이고 기본값이 없다. 이 revision 은 services·
service_environments 에 행이 없다고 가정한다(생성 API 가 이번에 처음 생긴다). 행이 있으면 칸을
더하기 전에 분명한 메시지로 실패한다.

autogenerate 가 만들지 못하는 것(기존 표 services 의 CHECK, DELETE 금지 트리거)은 직접 적었다.
DELETE 금지 함수는 표 이름(TG_TABLE_NAME)을 메시지에 넣는 공용 함수로 새로 만든다. 기존
prevent_deployment_delete() 는 바꾸지 않는다.

Revision ID: d95d675cb256
Revises: 6f6d92350314
Create Date: 2026-11-02 02:43:56.368825

"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d95d675cb256"
down_revision: str | Sequence[str] | None = "6f6d92350314"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 마이그레이션은 app.models 를 import 하지 않는다. 그 시점의 값을 여기에 적는다.
LIVE_ROWS = sa.text("is_deleted = false")
COMMIT_SHA_CHECK = "commit_sha ~ '^[0-9a-f]{40}$'"
IMAGE_DIGEST_CHECK = "image_digest ~ '^sha256:[0-9a-f]{64}$'"
PREVENT_DELETE_FUNCTION = "prevent_append_only_delete"
PREVENT_DELETE_TRIGGER = "trg_deploy_requests_prevent_delete"

SERVICE_CHECKS = {
    "ck_services_health_check_path_format": "health_check_path LIKE '/%'",
    "ck_services_health_check_timeout_range": "health_check_timeout_seconds BETWEEN 10 AND 1800",
    "ck_services_container_port_range": "container_port BETWEEN 1 AND 65535",
    "ck_services_status_valid": "status IN ('ACTIVE', 'DELETING')",
    "ck_services_delete_error_only_when_deleting": ("delete_error IS NULL OR status = 'DELETING'"),
}


class ExistingRowsError(RuntimeError):
    pass


def _id_column() -> sa.Column[Any]:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _timestamp_columns() -> list[sa.Column[Any]]:
    return [
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
    ]


def _require_empty(table: str) -> None:
    count = op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {table}"))
    if count:
        raise ExistingRowsError(
            f"{table} has {count} rows: this revision adds NOT NULL columns without defaults "
            f"(repo_url, repo_private, public_host) and expects {table} to be empty. "
            "Remove the rows or backfill them in a separate revision first."
        )


def upgrade() -> None:
    """Upgrade schema."""
    _require_empty("services")
    _require_empty("service_environments")

    op.add_column("services", sa.Column("repo_url", sa.String(length=300), nullable=False))
    op.add_column("services", sa.Column("repo_private", sa.Boolean(), nullable=False))
    op.add_column(
        "services",
        sa.Column(
            "health_check_path",
            sa.String(length=255),
            server_default=sa.text("'/healthz'"),
            nullable=False,
        ),
    )
    op.add_column(
        "services",
        sa.Column(
            "health_check_timeout_seconds",
            sa.Integer(),
            server_default=sa.text("300"),
            nullable=False,
        ),
    )
    op.add_column(
        "services",
        sa.Column("container_port", sa.Integer(), server_default=sa.text("8080"), nullable=False),
    )
    op.add_column("services", sa.Column("public_host", sa.String(length=253), nullable=False))
    op.add_column(
        "services",
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'ACTIVE'"), nullable=False
        ),
    )
    op.add_column("services", sa.Column("delete_error", sa.String(length=32), nullable=True))
    op.add_column(
        "services",
        sa.Column("simulated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    for name, condition in SERVICE_CHECKS.items():
        op.create_check_constraint(op.f(name), "services", condition)
    op.create_index(
        "uq_services_public_host_live",
        "services",
        ["public_host"],
        unique=True,
        postgresql_where=LIVE_ROWS,
    )

    op.add_column(
        "service_environments", sa.Column("public_host", sa.String(length=253), nullable=False)
    )
    op.add_column(
        "service_environments",
        sa.Column("teardown_attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "service_environments",
        sa.Column("teardown_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "service_environments",
        sa.Column("teardown_external_ref", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "service_environments", sa.Column("teardown_error", sa.String(length=32), nullable=True)
    )
    op.create_index(
        "uq_service_environments_public_host_live",
        "service_environments",
        ["public_host"],
        unique=True,
        postgresql_where=LIVE_ROWS,
    )

    op.create_table(
        "builds",
        _id_column(),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("repo_url", sa.String(length=300), nullable=False),
        sa.Column("commit_sha", sa.CHAR(length=40), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'QUEUED'"), nullable=False
        ),
        sa.Column("image_repository", sa.String(length=512), nullable=True),
        sa.Column("image_digest", sa.String(length=71), nullable=True),
        sa.Column("start_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("external_ref", sa.String(length=255), nullable=True),
        sa.Column("error_code", sa.String(length=32), nullable=True),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')",
            name=op.f("ck_builds_status_valid"),
        ),
        sa.CheckConstraint(COMMIT_SHA_CHECK, name=op.f("ck_builds_commit_sha_format")),
        sa.CheckConstraint(IMAGE_DIGEST_CHECK, name=op.f("ck_builds_image_digest_format")),
        sa.CheckConstraint(
            "(status = 'SUCCEEDED') = (image_digest IS NOT NULL AND image_repository IS NOT NULL)",
            name=op.f("ck_builds_image_consistency"),
        ),
        sa.CheckConstraint(
            "(status = 'FAILED') = (error_code IS NOT NULL)",
            name=op.f("ck_builds_error_code_consistency"),
        ),
        sa.CheckConstraint(
            "(status IN ('SUCCEEDED', 'FAILED')) = (finished_at IS NOT NULL)",
            name=op.f("ck_builds_finished_at_consistency"),
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_builds_service_id_services"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_builds")),
    )
    op.create_index(
        "uq_builds_service_id_repo_url_commit_sha_not_failed",
        "builds",
        ["service_id", "repo_url", "commit_sha"],
        unique=True,
        postgresql_where=sa.text("status <> 'FAILED'"),
    )

    op.create_table(
        "deploy_requests",
        _id_column(),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("service_environment_id", sa.Uuid(), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("source_request_id", sa.Uuid(), nullable=True),
        sa.Column("ref_type", sa.String(length=8), nullable=False),
        sa.Column("ref_value", sa.String(length=255), nullable=False),
        sa.Column("repo_url", sa.String(length=300), nullable=False),
        sa.Column("commit_sha", sa.CHAR(length=40), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'QUEUED'"), nullable=False
        ),
        sa.Column("cancel_reason", sa.String(length=16), nullable=True),
        sa.Column("superseded_by_id", sa.Uuid(), nullable=True),
        sa.Column("error_code", sa.String(length=32), nullable=True),
        sa.Column("build_id", sa.Uuid(), nullable=True),
        sa.Column("build_reused", sa.Boolean(), nullable=True),
        sa.Column("deployment_id", sa.Uuid(), nullable=True),
        sa.Column("deploy_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deploy_external_ref", sa.String(length=255), nullable=True),
        sa.Column("revert_status", sa.String(length=16), nullable=True),
        sa.Column("revert_external_ref", sa.String(length=255), nullable=True),
        sa.Column("last_health_result", sa.String(length=16), nullable=True),
        sa.Column("config_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("simulate", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("building_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deploying_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verifying_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'BUILDING', 'DEPLOYING', 'VERIFYING', 'SUCCEEDED', 'FAILED', "
            "'CANCELLED')",
            name=op.f("ck_deploy_requests_status_valid"),
        ),
        sa.CheckConstraint(
            "trigger IN ('MANUAL', 'STANDBY_SYNC')", name=op.f("ck_deploy_requests_trigger_valid")
        ),
        sa.CheckConstraint(
            "ref_type IN ('BRANCH', 'COMMIT')", name=op.f("ck_deploy_requests_ref_type_valid")
        ),
        sa.CheckConstraint(
            "cancel_reason IN ('SUPERSEDED', 'SERVICE_DELETED')",
            name=op.f("ck_deploy_requests_cancel_reason_valid"),
        ),
        sa.CheckConstraint(
            "revert_status IN ('SUCCEEDED', 'FAILED')",
            name=op.f("ck_deploy_requests_revert_status_valid"),
        ),
        sa.CheckConstraint(
            "last_health_result IN ('OK', 'HTTP_ERROR', 'UNREACHABLE')",
            name=op.f("ck_deploy_requests_last_health_result_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'CANCELLED') = (cancel_reason IS NOT NULL)",
            name=op.f("ck_deploy_requests_cancel_reason_consistency"),
        ),
        sa.CheckConstraint(
            "(status = 'FAILED') = (error_code IS NOT NULL)",
            name=op.f("ck_deploy_requests_error_code_consistency"),
        ),
        sa.CheckConstraint(
            "(status IN ('SUCCEEDED', 'FAILED', 'CANCELLED')) = (finished_at IS NOT NULL)",
            name=op.f("ck_deploy_requests_finished_at_consistency"),
        ),
        sa.CheckConstraint(
            "(trigger = 'STANDBY_SYNC') = (source_request_id IS NOT NULL)",
            name=op.f("ck_deploy_requests_source_request_consistency"),
        ),
        sa.CheckConstraint(
            "(build_id IS NULL) = (build_reused IS NULL)",
            name=op.f("ck_deploy_requests_build_reused_consistency"),
        ),
        sa.CheckConstraint(COMMIT_SHA_CHECK, name=op.f("ck_deploy_requests_commit_sha_format")),
        sa.CheckConstraint(
            "jsonb_typeof(config_snapshot) = 'object'",
            name=op.f("ck_deploy_requests_config_snapshot_is_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(simulate) = 'object'", name=op.f("ck_deploy_requests_simulate_is_object")
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_deploy_requests_service_id_services"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_deploy_requests_environment_id_environments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["service_environment_id"],
            ["service_environments.id"],
            name=op.f("fk_deploy_requests_service_environment_id_service_environments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_request_id"],
            ["deploy_requests.id"],
            name=op.f("fk_deploy_requests_source_request_id_deploy_requests"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_id"],
            ["deploy_requests.id"],
            name=op.f("fk_deploy_requests_superseded_by_id_deploy_requests"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["build_id"],
            ["builds.id"],
            name=op.f("fk_deploy_requests_build_id_builds"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["deployments.id"],
            name=op.f("fk_deploy_requests_deployment_id_deployments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deploy_requests")),
    )
    op.create_index(
        "ix_deploy_requests_service_id_created_at",
        "deploy_requests",
        ["service_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_deploy_requests_service_environment_id_created_at",
        "deploy_requests",
        ["service_environment_id", "created_at"],
        unique=False,
    )
    op.create_index("ix_deploy_requests_build_id", "deploy_requests", ["build_id"], unique=False)
    op.create_index(
        "uq_deploy_requests_service_environment_id_active",
        "deploy_requests",
        ["service_environment_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('BUILDING', 'DEPLOYING', 'VERIFYING')"),
    )
    op.create_index(
        "uq_deploy_requests_service_environment_id_queued",
        "deploy_requests",
        ["service_environment_id"],
        unique=True,
        postgresql_where=sa.text("status = 'QUEUED'"),
    )

    op.create_table(
        "jobs",
        _id_column(),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'QUEUED'"), nullable=False
        ),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_losses", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error_code", sa.String(length=32), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "kind IN ('DEPLOY_REQUEST', 'SERVICE_TEARDOWN')", name=op.f("ck_jobs_kind_valid")
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'DONE', 'FAILED', 'CANCELLED')",
            name=op.f("ck_jobs_status_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'RUNNING') = (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name=op.f("ck_jobs_lease_consistency"),
        ),
        sa.CheckConstraint("lease_losses >= 0", name=op.f("ck_jobs_lease_losses_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index("ix_jobs_status_run_after", "jobs", ["status", "run_after"], unique=False)
    op.create_index(
        "uq_jobs_subject_id_open",
        "jobs",
        ["subject_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING')"),
    )

    # 배포 요청은 지우지 않는 이력이다. 23001(restrict_violation)이라 드라이버가 IntegrityError 로
    # 올린다. 표 이름은 TG_TABLE_NAME 으로 메시지에 넣는다(다른 이력 표도 같은 함수를 쓸 수 있다).
    op.execute(
        f"""
        CREATE FUNCTION {PREVENT_DELETE_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% rows are append-only and cannot be deleted', TG_TABLE_NAME
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {PREVENT_DELETE_TRIGGER}
        BEFORE DELETE ON deploy_requests
        FOR EACH ROW EXECUTE FUNCTION {PREVENT_DELETE_FUNCTION}()
        """
    )


def downgrade() -> None:
    """Downgrade schema. 새 표의 행(배포 요청·빌드·할 일 기록)과 새 칸 값은 함께 지워진다."""
    op.execute(f"DROP TRIGGER {PREVENT_DELETE_TRIGGER} ON deploy_requests")
    op.execute(f"DROP FUNCTION {PREVENT_DELETE_FUNCTION}()")

    op.drop_index("uq_jobs_subject_id_open", table_name="jobs")
    op.drop_index("ix_jobs_status_run_after", table_name="jobs")
    op.drop_table("jobs")

    op.drop_index("uq_deploy_requests_service_environment_id_queued", table_name="deploy_requests")
    op.drop_index("uq_deploy_requests_service_environment_id_active", table_name="deploy_requests")
    op.drop_index("ix_deploy_requests_build_id", table_name="deploy_requests")
    op.drop_index(
        "ix_deploy_requests_service_environment_id_created_at", table_name="deploy_requests"
    )
    op.drop_index("ix_deploy_requests_service_id_created_at", table_name="deploy_requests")
    op.drop_table("deploy_requests")

    op.drop_index("uq_builds_service_id_repo_url_commit_sha_not_failed", table_name="builds")
    op.drop_table("builds")

    op.drop_index("uq_service_environments_public_host_live", table_name="service_environments")
    op.drop_column("service_environments", "teardown_error")
    op.drop_column("service_environments", "teardown_external_ref")
    op.drop_column("service_environments", "teardown_requested_at")
    op.drop_column("service_environments", "teardown_attempt")
    op.drop_column("service_environments", "public_host")

    op.drop_index("uq_services_public_host_live", table_name="services")
    for name in reversed(SERVICE_CHECKS):
        op.drop_constraint(op.f(name), "services", type_="check")
    op.drop_column("services", "simulated")
    op.drop_column("services", "delete_error")
    op.drop_column("services", "status")
    op.drop_column("services", "public_host")
    op.drop_column("services", "container_port")
    op.drop_column("services", "health_check_timeout_seconds")
    op.drop_column("services", "health_check_path")
    op.drop_column("services", "repo_private")
    op.drop_column("services", "repo_url")
