"""add target not receiving migration error code

이전 판정을 "요청 N건 처리 확인"으로 바꾸며(API 서버 8-1) 새 원인 코드 TARGET_NOT_RECEIVING(도착에
갔어야 할 만큼 출발에 요청이 왔는데 도착이 거의 받지 못함)을 migration_plans.error_code CHECK 에
더한다. CHECK 를 넓히기만 한다(지우고 다시 만든다). 다른 칸은 바꾸지 않는다(센 값·진행
숫자·필요 건수·최대 대기는 steps·step_results JSONB 안에 있고, observe_seconds 칸에는 단계 최대
대기를 적는다).

autogenerate 는 CHECK 정의 변경을 감지하지 못해 직접 적었다.

Revision ID: 6d89c52e39a4
Revises: 4cea1ec99f1b
Create Date: 2026-11-03 12:55:15.701805

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6d89c52e39a4"
down_revision: str | Sequence[str] | None = "4cea1ec99f1b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 마이그레이션은 app.models 를 import 하지 않는다. 그 시점의 값을 여기에 적는다.
ERROR_CODE_CHECK = "ck_migration_plans_error_code_valid"
NEW_ERROR_CODE = "TARGET_NOT_RECEIVING"
OLD_ERROR_CODE_VALUES = (
    "error_code IN ('PREFLIGHT_FAILED', 'TRAFFIC_APPLY_FAILED', 'HEALTH_CHECK_FAILED', "
    "'METRICS_UNAVAILABLE', 'THRESHOLD_EXCEEDED', 'DATA_LAG_EXCEEDED', 'ABORTED', "
    "'WORKER_LOST', 'INTERNAL_ERROR')"
)
NEW_ERROR_CODE_VALUES = (
    "error_code IN ('PREFLIGHT_FAILED', 'TRAFFIC_APPLY_FAILED', 'HEALTH_CHECK_FAILED', "
    "'METRICS_UNAVAILABLE', 'THRESHOLD_EXCEEDED', 'DATA_LAG_EXCEEDED', 'ABORTED', "
    "'WORKER_LOST', 'INTERNAL_ERROR', 'TARGET_NOT_RECEIVING')"
)


class TargetNotReceivingPlansExistError(RuntimeError):
    pass


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(op.f(ERROR_CODE_CHECK), "migration_plans", type_="check")
    op.create_check_constraint(op.f(ERROR_CODE_CHECK), "migration_plans", NEW_ERROR_CODE_VALUES)


def downgrade() -> None:
    """Downgrade schema.

    error_code = TARGET_NOT_RECEIVING 인 계획이 하나라도 있으면 아무것도 바꾸지 않고 실패한다(옛
    CHECK 에 맞지 않고, 이전 계획은 지우지도 고치지도 않는 기록이다).
    """
    count = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM migration_plans WHERE error_code = :code"),
        {"code": NEW_ERROR_CODE},
    )
    if count:
        raise TargetNotReceivingPlansExistError(
            f"migration_plans has {count} {NEW_ERROR_CODE} rows: this downgrade would make them "
            "invalid and it does not rewrite migration plan history. Keep this revision."
        )
    op.drop_constraint(op.f(ERROR_CODE_CHECK), "migration_plans", type_="check")
    op.create_check_constraint(op.f(ERROR_CODE_CHECK), "migration_plans", OLD_ERROR_CODE_VALUES)
