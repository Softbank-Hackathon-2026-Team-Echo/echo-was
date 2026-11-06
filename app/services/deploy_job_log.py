"""배포 작업 로그 기록기. 접수·일꾼·서비스 삭제가 함께 쓴다.

규칙
- 줄은 그 줄을 일으킨 상태 전환(또는 요청 삽입)과 같은 트랜잭션에서, 전환이 실제로 반영됐을 때만
  쓴다(조건부 UPDATE 가 1행일 때). 그래서 일꾼이 다시 시작해도 같은 줄이 두 번 쓰이지 않는다.
- 부르는 쪽은 그 요청의 서비스 행을 잠근 트랜잭션 안에 있어야 한다. seq 는 그 잠금 안에서
  max(seq) + 1 로 정하므로 경쟁이 없다. (요청, seq) unique 위반은 버그라 그대로 올린다.
- 요청당 MAX_DEPLOY_LOG_LINES 줄까지 쓴다. 마지막 자리에는 LOG_TRUNCATED 를 쓰고 그 뒤 줄은
  버린다. 상태 진행은 로그 상한과 상관없이 계속된다.
"""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import DeployLogCode, DeployRequestErrorCode
from app.models import DeployRequest, DeployRequestLog
from app.models.deploy_request_log import DEPLOY_LOG_MESSAGE_MAX_LENGTH
from app.repositories.deploy_request_log_repository import DeployRequestLogRepository
from app.services.deploy_log_messages import (
    DEPLOY_LOG_ENTRIES,
    deploy_log_message,
    env_vars_applied_message,
)

MAX_DEPLOY_LOG_LINES = 500


class DeployJobLog:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._logs = DeployRequestLogRepository(session)

    async def append(
        self,
        request: DeployRequest,
        code: DeployLogCode,
        now: datetime,
        *,
        error_code: DeployRequestErrorCode | None = None,
        env_keys: Sequence[str] = (),
        message: str | None = None,
    ) -> None:
        """message 를 주면 고정 문장 대신 쓴다(실패 진단 줄, 이미 가린 값만).

        길이 상한에서 자른다.
        """
        last_seq = await self._logs.find_last_seq(request.id)
        if last_seq >= MAX_DEPLOY_LOG_LINES:
            return
        seq = last_seq + 1
        entry = DEPLOY_LOG_ENTRIES[code]
        if seq == MAX_DEPLOY_LOG_LINES:
            truncated = DEPLOY_LOG_ENTRIES[DeployLogCode.LOG_TRUNCATED]
            code, level, text = DeployLogCode.LOG_TRUNCATED, truncated.level, truncated.message
        else:
            level, text = entry.level, deploy_log_message(code, error_code)
            if code is DeployLogCode.ENV_VARS_APPLIED:
                text = env_vars_applied_message(env_keys, DEPLOY_LOG_MESSAGE_MAX_LENGTH)
            elif message is not None:
                text = message[:DEPLOY_LOG_MESSAGE_MAX_LENGTH]
        self._logs.add(
            DeployRequestLog(
                deploy_request_id=request.id,
                seq=seq,
                logged_at=now,
                level=level,
                stage=entry.stage,
                code=code,
                message=text,
                simulated=request.simulated,
                created_at=now,
            )
        )
        await self._session.flush()
