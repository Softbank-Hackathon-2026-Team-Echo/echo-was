"""실행 중(APPROVED·RUNNING·ROLLING_BACK)인 이전 계획이 있는 서비스의 접수를 막는다.

배포 요청·되돌리기·서비스 삭제 접수가 서비스 행을 잠근 트랜잭션 안에서 부른다. 승인 대기
(PENDING_APPROVAL) 계획은 막지 않는다(그 접수가 계획을 낡게 만들어 승인 때 STALE 이 된다).
일꾼이 만드는 예비 따라가기(STANDBY_SYNC)는 이 검사를 받지 않는다.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import MigrationInProgressError
from app.repositories.migration_plan_repository import MigrationPlanRepository


async def check_no_migration_in_progress(session: AsyncSession, service_id: uuid.UUID) -> None:
    plan = await MigrationPlanRepository(session).find_in_progress_for_service(service_id)
    if plan is not None:
        raise MigrationInProgressError(str(plan.id))
