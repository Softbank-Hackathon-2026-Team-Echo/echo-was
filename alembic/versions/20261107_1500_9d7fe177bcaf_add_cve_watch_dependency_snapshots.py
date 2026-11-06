"""add cve watch dependency snapshots

CVE 감시(SPEC sec-6-cve-watch)의 저장 칸을 더한다.

- 새 표 service_dependency_snapshots(서비스 이미지 digest 별 의존성 목록) + 인덱스 하나.
- findings 에 remediation(JSONB, NULL 허용) · dedupe_key(NULL 허용) 칸, CHECK
  ck_findings_remediation_is_object, 부분 인덱스 ix_findings_organization_id_dedupe_key.
- ck_findings_source_valid 를 넓힌다(값 CVE_WATCH 추가). 지우고 다시 만든다 — 기존 행은 모두
  옛 목록 안이라 그대로 통과하고, 롤링 배포 중 옛 파드는 CVE_WATCH 를 쓰지 않는다.

나머지는 더하기만 한다. 기존 행은 새 칸이 NULL 이다.

downgrade 는 더한 것을 지운다. CVE_WATCH 증거 행이 있으면 옛 CHECK 를 다시 걸 수 없으므로
먼저 그 행을 지운다(감시 증거·의존성 목록이 함께 사라진다).

Revision ID: 9d7fe177bcaf
Revises: 9d8e1f2a3b4c
Create Date: 2026-11-07 02:55:13.968275 (main #101 병합 뒤 9d8e1f2a3b4c 위로 다시 이음)

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "9d7fe177bcaf"
down_revision: str | Sequence[str] | None = "9d8e1f2a3b4c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_SOURCES = (
    "'TRIVY_IMAGE', 'TRIVY_CONFIG', 'TRIVY_SECRET', 'GITLEAKS', 'TLS_PROBE', 'MANUAL_REVIEW', "
    "'WAS_TEST', 'WAS_STATE', 'AUDIT_LOG', 'PRECHECK', 'ENTRYPOINT', 'DATA_ENGINE', "
    "'CLOUD_API', 'KUBERNETES_API', 'GITHUB_API', 'AI_REVIEW'"
)
_NEW_SOURCES = f"{_OLD_SOURCES}, 'CVE_WATCH'"
_SOURCE_CHECK = "ck_findings_source_valid"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "service_dependency_snapshots",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("image_digest", sa.String(length=71), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("source_ref", sa.String(length=500), nullable=True),
        sa.Column(
            "packages",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("package_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
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
            "image_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_service_dependency_snapshots_image_digest_format"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(packages) = 'array'",
            name=op.f("ck_service_dependency_snapshots_packages_is_array"),
        ),
        sa.CheckConstraint(
            "source IN ('BUILD_SCAN', 'RUNNER_SBOM')",
            name=op.f("ck_service_dependency_snapshots_source_valid"),
        ),
        sa.CheckConstraint(
            "package_count >= 0",
            name=op.f("ck_service_dependency_snapshots_package_count_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_service_dependency_snapshots_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
            name=op.f("fk_service_dependency_snapshots_service_id_services"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_dependency_snapshots")),
    )
    op.create_index(
        "ix_service_dependency_snapshots_service_digest_collected",
        "service_dependency_snapshots",
        ["service_id", "image_digest", "collected_at"],
        unique=False,
    )
    op.add_column(
        "findings",
        sa.Column(
            "remediation", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
    )
    op.add_column("findings", sa.Column("dedupe_key", sa.String(length=400), nullable=True))
    op.create_check_constraint(
        op.f("ck_findings_remediation_is_object"),
        "findings",
        "remediation IS NULL OR jsonb_typeof(remediation) = 'object'",
    )
    op.create_index(
        "ix_findings_organization_id_dedupe_key",
        "findings",
        ["organization_id", "dedupe_key"],
        unique=False,
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
    )
    op.drop_constraint(op.f(_SOURCE_CHECK), "findings", type_="check")
    op.create_check_constraint(op.f(_SOURCE_CHECK), "findings", f"source IN ({_NEW_SOURCES})")


def downgrade() -> None:
    """Downgrade schema. CVE_WATCH 증거와 의존성 목록이 사라진다(모듈 설명 참고)."""
    op.execute("DELETE FROM findings WHERE source = 'CVE_WATCH'")
    op.drop_constraint(op.f(_SOURCE_CHECK), "findings", type_="check")
    op.create_check_constraint(op.f(_SOURCE_CHECK), "findings", f"source IN ({_OLD_SOURCES})")
    op.drop_index(
        "ix_findings_organization_id_dedupe_key",
        table_name="findings",
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
    )
    op.drop_constraint(op.f("ck_findings_remediation_is_object"), "findings", type_="check")
    op.drop_column("findings", "dedupe_key")
    op.drop_column("findings", "remediation")
    op.drop_index(
        "ix_service_dependency_snapshots_service_digest_collected",
        table_name="service_dependency_snapshots",
    )
    op.drop_table("service_dependency_snapshots")
