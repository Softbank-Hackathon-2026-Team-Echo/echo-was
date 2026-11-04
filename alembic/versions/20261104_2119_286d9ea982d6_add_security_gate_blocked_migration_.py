"""add security gate blocked migration error code

보안 게이트(보안·거버넌스 5)가 실행 시작 때 막은 계획을 FAILED 로 끝낼 원인 코드
SECURITY_GATE_BLOCKED 를 migration_plans.error_code CHECK 에 더한다. CHECK 를 넓히기만 한다
(지우고 다시 만든다). 다른 칸은 바꾸지 않는다(게이트 판정은 실행 로그 줄 data 에 남는다).

처음에는 201cf5419f4d 뒤에 만들었으나, 먼저 병합된 48b2126a9c37(앱 환경 변수 표, API 서버 10) 뒤로
옮겼다(head 하나). 두 변경은 서로 다른 표라 순서와 상관없다.

autogenerate 는 CHECK 정의 변경을 감지하지 못해 직접 적었다.

Revision ID: 286d9ea982d6
Revises: 48b2126a9c37
Create Date: 2026-11-04 21:19:43.604135

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "286d9ea982d6"
down_revision: str | Sequence[str] | None = "48b2126a9c37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 마이그레이션은 app.models 를 import 하지 않는다. 그 시점의 값을 여기에 적는다.
ERROR_CODE_CHECK = "ck_migration_plans_error_code_valid"
NEW_ERROR_CODE = "SECURITY_GATE_BLOCKED"
OLD_ERROR_CODE_VALUES = (
    "error_code IN ('PREFLIGHT_FAILED', 'TRAFFIC_APPLY_FAILED', 'HEALTH_CHECK_FAILED', "
    "'METRICS_UNAVAILABLE', 'THRESHOLD_EXCEEDED', 'DATA_LAG_EXCEEDED', 'ABORTED', "
    "'WORKER_LOST', 'INTERNAL_ERROR', 'TARGET_NOT_RECEIVING')"
)
NEW_ERROR_CODE_VALUES = (
    "error_code IN ('PREFLIGHT_FAILED', 'TRAFFIC_APPLY_FAILED', 'HEALTH_CHECK_FAILED', "
    "'METRICS_UNAVAILABLE', 'THRESHOLD_EXCEEDED', 'DATA_LAG_EXCEEDED', 'ABORTED', "
    "'WORKER_LOST', 'INTERNAL_ERROR', 'TARGET_NOT_RECEIVING', 'SECURITY_GATE_BLOCKED')"
)


class SecurityGateBlockedPlansExistError(RuntimeError):
    pass


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(op.f(ERROR_CODE_CHECK), "migration_plans", type_="check")
    op.create_check_constraint(op.f(ERROR_CODE_CHECK), "migration_plans", NEW_ERROR_CODE_VALUES)


def downgrade() -> None:
    """Downgrade schema.

    error_code = SECURITY_GATE_BLOCKED 인 계획이 하나라도 있으면 아무것도 바꾸지 않고 실패한다(옛
    CHECK 에 맞지 않고, 이전 계획은 지우지도 고치지도 않는 기록이다).
    """
    count = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM migration_plans WHERE error_code = :code"),
        {"code": NEW_ERROR_CODE},
    )
    if count:
        raise SecurityGateBlockedPlansExistError(
            f"migration_plans has {count} {NEW_ERROR_CODE} rows: this downgrade would make them "
            "invalid and it does not rewrite migration plan history. Keep this revision."
        )
    op.drop_constraint(op.f(ERROR_CODE_CHECK), "migration_plans", type_="check")
    op.create_check_constraint(op.f(ERROR_CODE_CHECK), "migration_plans", OLD_ERROR_CODE_VALUES)
