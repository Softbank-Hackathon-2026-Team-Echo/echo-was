"""widen migration plans for db cutover step

API 서버 17(이전 계획에 DB 쓰기 전환 단계 넣기). CHECK 를 넓히기만 한다(지우고 다시 만든다).
- migration_plans.current_step·migration_plan_logs.step: 0~4 → 0~5(6번째 단계 DB_CUTOVER)
- migration_plans.error_code: DB_CUTOVER_FAILED·DB_CUTOVER_TIMEOUT·DB_CUTOVER_PARTIAL·
  DATA_INTEGRITY_FAILED·DB_CUTOVER_UNKNOWN 을 더한다
- migration_plans.rollback_status: SKIPPED(자동으로 되돌리지 않음)를 더한다
칸·인덱스는 바꾸지 않는다. 옛 파드는 새 값을 쓰지 않으므로 롤아웃 동안 안전하다.

down_revision 은 main(#103 병합 뒤)의 head `8748e995387a`(실제 재난 피드: disasters·organizations·
disaster_feed_states)다. 그 리비전은 migration_plans 를 바꾸지 않으므로 이 CHECK
들과 겹치지 않는다. 이 PR 앞에 다른 마이그레이션이 먼저 병합되면 병합할 때 down_revision 을 그
head 로 다시 맞춘다.

autogenerate 는 CHECK 정의 변경을 감지하지 못해 직접 적었다.

Revision ID: 7c2e5a9d4b18
Revises: 8748e995387a
Create Date: 2026-11-07 04:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c2e5a9d4b18"
down_revision: str | Sequence[str] | None = "8748e995387a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 마이그레이션은 app.models 를 import 하지 않는다. 그 시점의 값을 여기에 적는다.
PLANS = "migration_plans"
LOGS = "migration_plan_logs"
CURRENT_STEP_CHECK = "ck_migration_plans_current_step_range"
LOG_STEP_CHECK = "ck_migration_plan_logs_step_range"
ERROR_CODE_CHECK = "ck_migration_plans_error_code_valid"
ROLLBACK_STATUS_CHECK = "ck_migration_plans_rollback_status_valid"

OLD_CURRENT_STEP = "current_step BETWEEN 0 AND 4"
NEW_CURRENT_STEP = "current_step BETWEEN 0 AND 5"
OLD_LOG_STEP = "step BETWEEN 0 AND 4"
NEW_LOG_STEP = "step BETWEEN 0 AND 5"
_OLD_ERROR_CODES = (
    "'PREFLIGHT_FAILED', 'TRAFFIC_APPLY_FAILED', 'HEALTH_CHECK_FAILED', "
    "'METRICS_UNAVAILABLE', 'THRESHOLD_EXCEEDED', 'DATA_LAG_EXCEEDED', 'ABORTED', "
    "'WORKER_LOST', 'INTERNAL_ERROR', 'TARGET_NOT_RECEIVING', 'SECURITY_GATE_BLOCKED'"
)
NEW_ERROR_CODES = (
    "DB_CUTOVER_FAILED",
    "DB_CUTOVER_TIMEOUT",
    "DB_CUTOVER_PARTIAL",
    "DATA_INTEGRITY_FAILED",
    "DB_CUTOVER_UNKNOWN",
)
OLD_ERROR_CODE_VALUES = f"error_code IN ({_OLD_ERROR_CODES})"
NEW_ERROR_CODE_VALUES = (
    f"error_code IN ({_OLD_ERROR_CODES}, "
    + ", ".join(f"'{code}'" for code in NEW_ERROR_CODES)
    + ")"
)
OLD_ROLLBACK_STATUS_VALUES = "rollback_status IN ('SUCCEEDED', 'FAILED', 'NOT_NEEDED')"
NEW_ROLLBACK_STATUS_VALUES = "rollback_status IN ('SUCCEEDED', 'FAILED', 'NOT_NEEDED', 'SKIPPED')"


class DbCutoverPlansExistError(RuntimeError):
    pass


def _replace(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, condition)


def upgrade() -> None:
    """Upgrade schema."""
    _replace(PLANS, CURRENT_STEP_CHECK, NEW_CURRENT_STEP)
    _replace(LOGS, LOG_STEP_CHECK, NEW_LOG_STEP)
    _replace(PLANS, ERROR_CODE_CHECK, NEW_ERROR_CODE_VALUES)
    _replace(PLANS, ROLLBACK_STATUS_CHECK, NEW_ROLLBACK_STATUS_VALUES)


def downgrade() -> None:
    """Downgrade schema.

    6단계 계획(단계 5 줄·위치)이나 새 원인 코드·SKIPPED 가 하나라도 있으면 아무것도 바꾸지 않고
    실패한다(옛 CHECK 에 맞지 않고, 이전 계획은 지우지도 고치지도 않는 기록이다).
    """
    bind = op.get_bind()
    count = bind.scalar(
        sa.text(
            "SELECT (SELECT count(*) FROM migration_plans WHERE current_step = 5 "
            "OR error_code = ANY(:codes) OR rollback_status = 'SKIPPED') + "
            "(SELECT count(*) FROM migration_plan_logs WHERE step = 5)"
        ),
        {"codes": list(NEW_ERROR_CODES)},
    )
    if count:
        raise DbCutoverPlansExistError(
            f"{count} migration plan rows use the DB cutover step: this downgrade would make "
            "them invalid and it does not rewrite migration plan history. Keep this revision."
        )
    _replace(PLANS, ROLLBACK_STATUS_CHECK, OLD_ROLLBACK_STATUS_VALUES)
    _replace(PLANS, ERROR_CODE_CHECK, OLD_ERROR_CODE_VALUES)
    _replace(LOGS, LOG_STEP_CHECK, OLD_LOG_STEP)
    _replace(PLANS, CURRENT_STEP_CHECK, OLD_CURRENT_STEP)
