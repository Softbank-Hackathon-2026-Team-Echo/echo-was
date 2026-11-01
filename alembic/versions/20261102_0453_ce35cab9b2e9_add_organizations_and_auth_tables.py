"""add organizations and auth tables

GitHub 로그인 + 조직·역할 권한(API 서버 6)에 필요한 표와 칸을 더한다.

- users: GitHub 로 로그인한 사람(github_id 유일).
- organizations: 고객 조직(slug 형식 CHECK·전체 유일). organization_members: 사람 × 조직 × 역할.
- invitations: GitHub 번호 × 역할 대기. 같은 조직·번호의 PENDING 은 하나(부분 unique).
- sessions: 쿠키 값의 SHA-256 만 둔다. agent_tokens: 원문 대신 SHA-256 과 끝 4자리.
- audit_logs: 거부(403) 한 건. 지우지 않는 기록이라 DELETE 를 트리거로 막는다
  (API 서버 3 이 만든 공용 함수 prevent_append_only_delete() 를 그대로 쓴다).
- services·environments: organization_id NOT NULL FK. 환경 이름 유일성은 조직 안으로 좁힌다
  (uq_environments_name_live → uq_environments_org_name_live). 서비스 이름은 전체 유일 그대로다.

organization_id 는 NOT NULL 이고 기본값이 없다. 이 revision 은 services·environments 에 행이
없다고 가정하고, 행이 있으면 칸을 더하기 전에 분명한 메시지로 실패한다.

autogenerate 가 만들지 못하는 것(빈 표 확인, DELETE 금지 트리거)은 직접 적었다.

Revision ID: ce35cab9b2e9
Revises: d95d675cb256
Create Date: 2026-11-02 04:53:57.687358

"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ce35cab9b2e9"
down_revision: str | Sequence[str] | None = "d95d675cb256"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 마이그레이션은 app.models 를 import 하지 않는다. 그 시점의 값을 여기에 적는다.
LIVE_ROWS = sa.text("is_deleted = false")
ROLE_CHECK = "role IN ('ADMIN', 'DEVELOPER', 'VIEWER')"
PREVENT_DELETE_FUNCTION = "prevent_append_only_delete"
AUDIT_PREVENT_DELETE_TRIGGER = "trg_audit_logs_prevent_delete"


class ExistingRowsError(RuntimeError):
    pass


def _id_column() -> sa.Column[Any]:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _created_at_column() -> sa.Column[Any]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _timestamp_columns() -> list[sa.Column[Any]]:
    return [
        _created_at_column(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def _restrict_fk(column: str, target: str, table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column],
        [f"{target}.id"],
        name=op.f(f"fk_{table}_{column}_{target}"),
        ondelete="RESTRICT",
    )


def _require_empty(table: str) -> None:
    count = op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {table}"))
    if count:
        raise ExistingRowsError(
            f"{table} has {count} rows: this revision adds organization_id NOT NULL without a "
            f"default and expects {table} to be empty. "
            "Remove the rows or backfill them in a separate revision first."
        )


def upgrade() -> None:
    """Upgrade schema."""
    _require_empty("services")
    _require_empty("environments")

    op.create_table(
        "users",
        _id_column(),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("github_login", sa.String(length=39), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("avatar_url", sa.String(length=500), nullable=True),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("github_id", name=op.f("uq_users_github_id")),
    )
    op.create_table(
        "organizations",
        _id_column(),
        sa.Column("slug", sa.String(length=39), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("personal", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "slug ~ '^[a-z0-9](-?[a-z0-9]){0,38}$'", name=op.f("ck_organizations_slug_format")
        ),
        sa.CheckConstraint("char_length(name) >= 1", name=op.f("ck_organizations_name_not_empty")),
        _restrict_fk("created_by_user_id", "users", "organizations"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organizations")),
        sa.UniqueConstraint("slug", name=op.f("uq_organizations_slug")),
    )
    op.create_table(
        "sessions",
        _id_column(),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        _created_at_column(),
        _restrict_fk("user_id", "users", "sessions"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_sessions_token_hash")),
    )
    op.create_table(
        "agent_tokens",
        _id_column(),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("token_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("last4", sa.CHAR(length=4), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        _created_at_column(),
        _restrict_fk("created_by_user_id", "users", "agent_tokens"),
        _restrict_fk("organization_id", "organizations", "agent_tokens"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_agent_tokens_token_hash")),
    )
    op.create_index(
        "ix_agent_tokens_organization_id_created_at",
        "agent_tokens",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "invitations",
        _id_column(),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("github_login", sa.String(length=39), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("invited_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "(status = 'ACCEPTED') = (accepted_at IS NOT NULL)",
            name=op.f("ck_invitations_accepted_at_consistency"),
        ),
        sa.CheckConstraint(ROLE_CHECK, name=op.f("ck_invitations_role_valid")),
        sa.CheckConstraint(
            "status IN ('PENDING', 'ACCEPTED', 'EXPIRED', 'REVOKED')",
            name=op.f("ck_invitations_status_valid"),
        ),
        _restrict_fk("invited_by_user_id", "users", "invitations"),
        _restrict_fk("organization_id", "organizations", "invitations"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invitations")),
    )
    op.create_index("ix_invitations_github_id", "invitations", ["github_id"], unique=False)
    op.create_index(
        "uq_invitations_organization_id_github_id_pending",
        "invitations",
        ["organization_id", "github_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_table(
        "organization_members",
        _id_column(),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        *_timestamp_columns(),
        sa.CheckConstraint(ROLE_CHECK, name=op.f("ck_organization_members_role_valid")),
        _restrict_fk("organization_id", "organizations", "organization_members"),
        _restrict_fk("user_id", "users", "organization_members"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_members")),
        sa.UniqueConstraint(
            "organization_id",
            "user_id",
            name=op.f("uq_organization_members_organization_id_user_id"),
        ),
    )
    op.create_index(
        "ix_organization_members_user_id", "organization_members", ["user_id"], unique=False
    )
    op.create_table(
        "audit_logs",
        _id_column(),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("actor_agent_token_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=300), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column(
            "result", sa.String(length=16), server_default=sa.text("'DENIED'"), nullable=False
        ),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        _created_at_column(),
        sa.CheckConstraint(
            "(actor_type = 'USER' AND actor_user_id IS NOT NULL AND actor_agent_token_id IS NULL) "
            "OR (actor_type = 'AGENT' AND actor_agent_token_id IS NOT NULL "
            "AND actor_user_id IS NULL)",
            name=op.f("ck_audit_logs_actor_consistency"),
        ),
        sa.CheckConstraint(
            "actor_type IN ('USER', 'AGENT')", name=op.f("ck_audit_logs_actor_type_valid")
        ),
        sa.CheckConstraint("result IN ('DENIED')", name=op.f("ck_audit_logs_result_valid")),
        _restrict_fk("actor_agent_token_id", "agent_tokens", "audit_logs"),
        _restrict_fk("actor_user_id", "users", "audit_logs"),
        _restrict_fk("organization_id", "organizations", "audit_logs"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
    )
    op.create_index(
        "ix_audit_logs_organization_id_created_at",
        "audit_logs",
        ["organization_id", "created_at"],
        unique=False,
    )
    # 거부 기록은 지우지 않는다. API 서버 3 의 공용 함수(표 이름을 메시지에 넣음)를 그대로 쓴다.
    op.execute(
        f"""
        CREATE TRIGGER {AUDIT_PREVENT_DELETE_TRIGGER}
        BEFORE DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION {PREVENT_DELETE_FUNCTION}()
        """
    )

    op.add_column("environments", sa.Column("organization_id", sa.Uuid(), nullable=False))
    op.drop_index("uq_environments_name_live", table_name="environments")
    op.create_index(
        "uq_environments_org_name_live",
        "environments",
        ["organization_id", "name"],
        unique=True,
        postgresql_where=LIVE_ROWS,
    )
    op.create_foreign_key(
        op.f("fk_environments_organization_id_organizations"),
        "environments",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column("services", sa.Column("organization_id", sa.Uuid(), nullable=False))
    op.create_index("ix_services_organization_id", "services", ["organization_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_services_organization_id_organizations"),
        "services",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    """Downgrade schema. 새 표의 행(사용자·조직·세션·토큰·거부 기록)과 조직 칸 값은 함께 지워진다.

    전역 환경 이름 유일성(uq_environments_name_live)을 다시 만들므로, 서로 다른 조직에 같은
    이름의 살아 있는 환경이 있으면 실패한다. prevent_append_only_delete() 함수는 API 서버 3 소유라
    지우지 않고 audit_logs 트리거만 지운다.
    """
    op.drop_constraint(
        op.f("fk_services_organization_id_organizations"), "services", type_="foreignkey"
    )
    op.drop_index("ix_services_organization_id", table_name="services")
    op.drop_column("services", "organization_id")
    op.drop_constraint(
        op.f("fk_environments_organization_id_organizations"), "environments", type_="foreignkey"
    )
    op.drop_index("uq_environments_org_name_live", table_name="environments")
    op.create_index(
        "uq_environments_name_live",
        "environments",
        ["name"],
        unique=True,
        postgresql_where=LIVE_ROWS,
    )
    op.drop_column("environments", "organization_id")

    op.execute(f"DROP TRIGGER {AUDIT_PREVENT_DELETE_TRIGGER} ON audit_logs")
    op.drop_index("ix_audit_logs_organization_id_created_at", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index("ix_organization_members_user_id", table_name="organization_members")
    op.drop_table("organization_members")
    op.drop_index("uq_invitations_organization_id_github_id_pending", table_name="invitations")
    op.drop_index("ix_invitations_github_id", table_name="invitations")
    op.drop_table("invitations")
    op.drop_index("ix_agent_tokens_organization_id_created_at", table_name="agent_tokens")
    op.drop_table("agent_tokens")
    op.drop_table("sessions")
    op.drop_table("organizations")
    op.drop_table("users")
