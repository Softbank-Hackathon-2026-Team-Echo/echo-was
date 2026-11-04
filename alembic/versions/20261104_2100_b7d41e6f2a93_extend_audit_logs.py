"""extend audit_logs

감사 로그(API 서버 7): 거부만 담던 audit_logs 를 행동 기록 표로 넓힌다. 더하기·넓히기만 한다.
롤링 배포 중 옛 파드의 거부 기록 INSERT(옛 칸만, result 는 서버 기본값)는 계속 통과한다.

- 칸 더하기: seq(identity, 기존 행도 채움), component, route, target_type, target_id,
  target_label, details.
- 넓히기: path·reason NULL 허용, result 에 SUCCEEDED·FAILED, actor_type 에 SYSTEM.
- 새 규칙: target_consistency, 색인 (organization_id, target_id).
- 고치기 금지: BEFORE UPDATE 트리거 + 함수 prevent_append_only_update()(이 revision 소유).
  지우기 금지 트리거는 그대로다.

downgrade 는 빈 표나 옛 모양 행만 있을 때의 왕복 시험용이다. 새 행동 줄이 쌓인 곳은 줄을 고치거나
지울 수 없어(트리거) 되돌릴 수 없으므로 실패로 멈춘다. 운영은 forward-only 다: 코드만 되돌리고
revision 은 남긴다. 옛 코드는 넓어진 스키마에서 거부 줄만 쓰므로 그대로 동작한다.

Revision ID: b7d41e6f2a93
Revises: 48b2126a9c37
Create Date: 2026-11-04 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b7d41e6f2a93"
down_revision: str | Sequence[str] | None = "48b2126a9c37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "audit_logs"
PREVENT_UPDATE_FUNCTION = "prevent_append_only_update"
PREVENT_UPDATE_TRIGGER = "trg_audit_logs_prevent_update"
TARGET_INDEX = "ix_audit_logs_organization_id_target_id"

OLD_ACTOR_CONSISTENCY = (
    "(actor_type = 'USER' AND actor_user_id IS NOT NULL AND actor_agent_token_id IS NULL) OR "
    "(actor_type = 'AGENT' AND actor_agent_token_id IS NOT NULL AND actor_user_id IS NULL)"
)
NEW_ACTOR_CONSISTENCY = (
    "(actor_type = 'USER' AND actor_user_id IS NOT NULL "
    "AND actor_agent_token_id IS NULL AND component IS NULL) OR "
    "(actor_type = 'AGENT' AND actor_agent_token_id IS NOT NULL "
    "AND actor_user_id IS NULL AND component IS NULL) OR "
    "(actor_type = 'SYSTEM' AND actor_user_id IS NULL "
    "AND actor_agent_token_id IS NULL AND component IS NOT NULL)"
)
TARGET_CONSISTENCY = (
    "((target_type IS NULL) = (target_id IS NULL)) "
    "AND (target_label IS NULL OR target_type IS NOT NULL)"
)


def _replace_check(name: str, condition: str) -> None:
    op.drop_constraint(op.f(f"ck_audit_logs_{name}"), TABLE, type_="check")
    op.create_check_constraint(op.f(f"ck_audit_logs_{name}"), TABLE, condition)


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
    )
    op.add_column(TABLE, sa.Column("component", sa.String(length=32), nullable=True))
    op.add_column(TABLE, sa.Column("route", sa.String(length=300), nullable=True))
    op.add_column(TABLE, sa.Column("target_type", sa.String(length=32), nullable=True))
    op.add_column(TABLE, sa.Column("target_id", sa.Uuid(), nullable=True))
    op.add_column(TABLE, sa.Column("target_label", sa.String(length=200), nullable=True))
    op.add_column(
        TABLE,
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.alter_column(TABLE, "path", existing_type=sa.Text(), nullable=True)
    op.alter_column(TABLE, "reason", existing_type=sa.String(length=64), nullable=True)
    _replace_check("result_valid", "result IN ('SUCCEEDED', 'FAILED', 'DENIED')")
    _replace_check("actor_type_valid", "actor_type IN ('USER', 'AGENT', 'SYSTEM')")
    _replace_check("actor_consistency", NEW_ACTOR_CONSISTENCY)
    op.create_check_constraint(op.f("ck_audit_logs_target_consistency"), TABLE, TARGET_CONSISTENCY)
    op.create_index(op.f(TARGET_INDEX), TABLE, ["organization_id", "target_id"], unique=False)
    op.execute(
        f"""
        CREATE FUNCTION {PREVENT_UPDATE_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% rows are append-only and cannot be updated', TG_TABLE_NAME
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {PREVENT_UPDATE_TRIGGER}
        BEFORE UPDATE ON {TABLE}
        FOR EACH ROW EXECUTE FUNCTION {PREVENT_UPDATE_FUNCTION}()
        """
    )


def downgrade() -> None:
    """새 행동 줄이 있으면 멈춘다(옛 규칙을 어기는 줄을 고치거나 지울 수 없다)."""
    has_new_rows = op.get_bind().scalar(
        sa.text(f"SELECT EXISTS (SELECT 1 FROM {TABLE} WHERE result <> 'DENIED')")
    )
    if has_new_rows:
        raise RuntimeError(
            "audit_logs has action rows and is append-only: this revision is forward-only"
        )
    op.execute(f"DROP TRIGGER {PREVENT_UPDATE_TRIGGER} ON {TABLE}")
    op.execute(f"DROP FUNCTION {PREVENT_UPDATE_FUNCTION}()")
    op.drop_index(op.f(TARGET_INDEX), table_name=TABLE)
    op.drop_constraint(op.f("ck_audit_logs_target_consistency"), TABLE, type_="check")
    _replace_check("actor_consistency", OLD_ACTOR_CONSISTENCY)
    _replace_check("actor_type_valid", "actor_type IN ('USER', 'AGENT')")
    _replace_check("result_valid", "result IN ('DENIED')")
    op.alter_column(TABLE, "reason", existing_type=sa.String(length=64), nullable=False)
    op.alter_column(TABLE, "path", existing_type=sa.Text(), nullable=False)
    op.drop_column(TABLE, "details")
    op.drop_column(TABLE, "target_label")
    op.drop_column(TABLE, "target_id")
    op.drop_column(TABLE, "target_type")
    op.drop_column(TABLE, "route")
    op.drop_column(TABLE, "component")
    op.drop_column(TABLE, "seq")
