"""add rollback and deploy request logs

배포 이력 · 되돌리기 · 로그(API 서버 4)에 필요한 칸과 표를 더한다.

- deploy_requests.trigger 에 ROLLBACK, cancel_reason 에 BASE_CHANGED 를 더한다(CHECK 를 지우고
  다시 만든다).
- deploy_requests.rollback_of_request_id: 되돌리기 대상(이력에서 고른 성공 요청). 자기 FK.
- deploy_requests.rollback_base_deployment_id: 접수 순간 연결의 현재 배포. 시작할 때 다르면
  CANCELLED(BASE_CHANGED).
- 새 CHECK: trigger = ROLLBACK 이면 두 칸이 모두 있고, 아니면 모두 없다. 자기 자신을 가리키지
  않는다.
- deploy_request_logs: 배포 작업 로그 한 줄(요청마다 seq 1 부터, (deploy_request_id, seq) unique).
  지우지 않는 기록이라 DELETE 를 트리거로 막는다(API 서버 3 의 공용 함수
  prevent_append_only_delete() 를 그대로 쓴다. 이 revision 은 함수를 만들거나 지우지 않는다).

autogenerate 가 만들지 못하는 것(CHECK 변경, DELETE 금지 트리거, downgrade 전 행 확인)은
직접 적었다.

Revision ID: 33ad047a493c
Revises: 1d1601c0111a
Create Date: 2026-11-02 17:50:07.258422

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "33ad047a493c"
down_revision: str | Sequence[str] | None = "1d1601c0111a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 마이그레이션은 app.models 를 import 하지 않는다. 그 시점의 값을 여기에 적는다.
PREVENT_DELETE_FUNCTION = "prevent_append_only_delete"
LOG_PREVENT_DELETE_TRIGGER = "trg_deploy_request_logs_prevent_delete"
TRIGGER_CHECK = "ck_deploy_requests_trigger_valid"
CANCEL_REASON_CHECK = "ck_deploy_requests_cancel_reason_valid"
OLD_TRIGGER_VALUES = "trigger IN ('MANUAL', 'STANDBY_SYNC')"
NEW_TRIGGER_VALUES = "trigger IN ('MANUAL', 'STANDBY_SYNC', 'ROLLBACK')"
OLD_CANCEL_REASON_VALUES = "cancel_reason IN ('SUPERSEDED', 'SERVICE_DELETED')"
NEW_CANCEL_REASON_VALUES = "cancel_reason IN ('SUPERSEDED', 'SERVICE_DELETED', 'BASE_CHANGED')"
ROLLBACK_CHECKS = {
    "ck_deploy_requests_rollback_of_request_consistency": (
        "(trigger = 'ROLLBACK') = (rollback_of_request_id IS NOT NULL)"
    ),
    "ck_deploy_requests_rollback_base_consistency": (
        "(trigger = 'ROLLBACK') = (rollback_base_deployment_id IS NOT NULL)"
    ),
    "ck_deploy_requests_rollback_of_request_not_self": "rollback_of_request_id <> id",
}


class RollbackRowsExistError(RuntimeError):
    pass


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("deploy_requests", sa.Column("rollback_of_request_id", sa.Uuid(), nullable=True))
    op.add_column(
        "deploy_requests", sa.Column("rollback_base_deployment_id", sa.Uuid(), nullable=True)
    )
    op.create_index(
        "ix_deploy_requests_rollback_of_request_id",
        "deploy_requests",
        ["rollback_of_request_id"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_deploy_requests_rollback_of_request_id_deploy_requests"),
        "deploy_requests",
        "deploy_requests",
        ["rollback_of_request_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_deploy_requests_rollback_base_deployment_id_deployments"),
        "deploy_requests",
        "deployments",
        ["rollback_base_deployment_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(op.f(TRIGGER_CHECK), "deploy_requests", type_="check")
    op.create_check_constraint(op.f(TRIGGER_CHECK), "deploy_requests", NEW_TRIGGER_VALUES)
    op.drop_constraint(op.f(CANCEL_REASON_CHECK), "deploy_requests", type_="check")
    op.create_check_constraint(
        op.f(CANCEL_REASON_CHECK), "deploy_requests", NEW_CANCEL_REASON_VALUES
    )
    for name, condition in ROLLBACK_CHECKS.items():
        op.create_check_constraint(op.f(name), "deploy_requests", condition)

    op.create_table(
        "deploy_request_logs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("deploy_request_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("logged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.String(length=8), nullable=False),
        sa.Column("stage", sa.String(length=16), nullable=False),
        sa.Column("code", sa.String(length=48), nullable=False),
        sa.Column("message", sa.String(length=500), nullable=False),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "level IN ('INFO', 'WARN', 'ERROR')", name=op.f("ck_deploy_request_logs_level_valid")
        ),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_deploy_request_logs_seq_positive")),
        sa.ForeignKeyConstraint(
            ["deploy_request_id"],
            ["deploy_requests.id"],
            name=op.f("fk_deploy_request_logs_deploy_request_id_deploy_requests"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deploy_request_logs")),
        sa.UniqueConstraint(
            "deploy_request_id",
            "seq",
            name=op.f("uq_deploy_request_logs_deploy_request_id_seq"),
        ),
    )
    op.execute(
        f"""
        CREATE TRIGGER {LOG_PREVENT_DELETE_TRIGGER}
        BEFORE DELETE ON deploy_request_logs
        FOR EACH ROW EXECUTE FUNCTION {PREVENT_DELETE_FUNCTION}()
        """
    )


def downgrade() -> None:
    """Downgrade schema.

    되돌리기 요청(trigger = ROLLBACK) 이나 BASE_CHANGED 취소가 하나라도 있으면 아무것도 바꾸지 않고
    실패한다(요청 이력을 지우지 않는다). 배포 작업 로그 표의 행은 표와 함께 사라진다.
    prevent_append_only_delete() 함수는 API 서버 3 소유라 지우지 않는다.
    """
    count = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM deploy_requests "
            "WHERE trigger = 'ROLLBACK' OR cancel_reason = 'BASE_CHANGED'"
        )
    )
    if count:
        raise RollbackRowsExistError(
            f"deploy_requests has {count} rollback rows (trigger = 'ROLLBACK' or "
            "cancel_reason = 'BASE_CHANGED'): this downgrade would make them invalid and "
            "it does not delete request history. Keep this revision or remove them by hand."
        )

    op.execute(f"DROP TRIGGER {LOG_PREVENT_DELETE_TRIGGER} ON deploy_request_logs")
    op.drop_table("deploy_request_logs")

    for name in ROLLBACK_CHECKS:
        op.drop_constraint(op.f(name), "deploy_requests", type_="check")
    op.drop_constraint(op.f(CANCEL_REASON_CHECK), "deploy_requests", type_="check")
    op.create_check_constraint(
        op.f(CANCEL_REASON_CHECK), "deploy_requests", OLD_CANCEL_REASON_VALUES
    )
    op.drop_constraint(op.f(TRIGGER_CHECK), "deploy_requests", type_="check")
    op.create_check_constraint(op.f(TRIGGER_CHECK), "deploy_requests", OLD_TRIGGER_VALUES)
    op.drop_constraint(
        op.f("fk_deploy_requests_rollback_base_deployment_id_deployments"),
        "deploy_requests",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_deploy_requests_rollback_of_request_id_deploy_requests"),
        "deploy_requests",
        type_="foreignkey",
    )
    op.drop_index("ix_deploy_requests_rollback_of_request_id", table_name="deploy_requests")
    op.drop_column("deploy_requests", "rollback_base_deployment_id")
    op.drop_column("deploy_requests", "rollback_of_request_id")
