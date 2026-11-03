"""이전 실행 로그 기록기. API(만들기·승인·거절·취소·중단)와 일꾼이 함께 쓴다.

규칙
- 줄은 그 줄을 일으킨 상태 변경과 같은 트랜잭션에서, 변경이 실제로 반영됐을 때만 쓴다(조건부
  UPDATE 가 1행일 때). 그래서 일꾼이 다시 시작해도 같은 줄이 두 번 쓰이지 않는다.
- 부르는 쪽은 그 계획의 서비스 행을 잠근 트랜잭션 안에 있어야 한다. seq 는 그 잠금 안에서
  max(seq) + 1 로 정한다.
- 계획당 MAX_MIGRATION_LOG_LINES 줄까지 쓴다. 마지막 자리에는 LOG_TRUNCATED 를 쓰고 그 뒤 줄은
  버린다. 실행은 로그 상한과 상관없이 계속된다.
- data 에는 비율·지표 숫자(낡은 계획이면 달라진 항목 이름)만 넣는다.
"""

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import MigrationErrorCode, MigrationLogCode
from app.models import MigrationPlan, MigrationPlanLog
from app.repositories.migration_plan_log_repository import MigrationPlanLogRepository
from app.services.migration_log_messages import MIGRATION_LOG_ENTRIES, migration_log_message

MAX_MIGRATION_LOG_LINES = 500


class MigrationPlanLogWriter:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._logs = MigrationPlanLogRepository(session)

    async def append(
        self,
        plan: MigrationPlan,
        code: MigrationLogCode,
        now: datetime,
        *,
        step: int | None = None,
        data: dict[str, Any] | None = None,
        error_code: MigrationErrorCode | None = None,
    ) -> None:
        last_seq = await self._logs.find_last_seq(plan.id)
        if last_seq >= MAX_MIGRATION_LOG_LINES:
            return
        seq = last_seq + 1
        if seq == MAX_MIGRATION_LOG_LINES:
            truncated = MIGRATION_LOG_ENTRIES[MigrationLogCode.LOG_TRUNCATED]
            code, level, message, data = (
                MigrationLogCode.LOG_TRUNCATED,
                truncated.level,
                truncated.message,
                None,
            )
        else:
            level = MIGRATION_LOG_ENTRIES[code].level
            message = migration_log_message(code, error_code, data)
        self._logs.add(
            MigrationPlanLog(
                migration_plan_id=plan.id,
                seq=seq,
                step=step,
                logged_at=now,
                level=level,
                code=code,
                message=message,
                data=data,
                simulated=plan.simulated,
                created_at=now,
            )
        )
        await self._session.flush()
