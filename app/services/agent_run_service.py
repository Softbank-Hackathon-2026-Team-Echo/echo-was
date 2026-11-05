"""러너 쪽 유스케이스: 실행 가져가기·하트비트·이벤트·결과 제출, 부를 때 만료.

- 상태는 조건부 UPDATE 로만 바꾼다. 일꾼 루프가 없어 임대·마감이 지난 실행은 누가 부를 때
  TIMED_OUT 으로 바꾼다. 만료 전환·감사 줄을 커밋한 뒤 블록 밖에서 409 를 올린다(안에서 올리면
  함께 되돌려진다).
- 러너 행동의 감사 줄은 SYSTEM + component = ai-runner 다. 하트비트·이벤트는 줄을 쓰지 않는다.
- 실행 토큰 원문은 가져가기 응답에 한 번만 준다. 저장·로그에 넣지 않는다.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.actions import AuditAction, AuditComponent, AuditTargetType
from app.audit.recorder import AuditActor, AuditRecorder, AuditTarget
from app.auth.tokens import hash_token, new_run_token
from app.core.clock import Clock
from app.core.exceptions import (
    RequestFieldInvalidError,
    ResultAlreadySubmittedError,
    RunNotActiveError,
)
from app.core.log_masking import mask_secrets, mask_secrets_in_json
from app.core.secrets import SecretValue
from app.enums import (
    AgentRunFailureCode,
    AgentRunKind,
    AgentRunStatus,
    AgentRunStep,
    AuditResult,
    LogLevel,
)
from app.models import AgentRun, AgentRunner
from app.models.agent_run import OPEN_STATUSES
from app.repositories.agent_run_event_repository import AgentRunEventRepository
from app.repositories.agent_run_repository import AgentRunRepository
from app.services.run_result import NormalizedRunResult

logger = logging.getLogger(__name__)

RUN_LEASE = timedelta(seconds=60)
RUN_DEADLINE = timedelta(minutes=15)
EXPIRE_BATCH_SIZE = 100
RUNNER_ID_MISMATCH_REASON = "RUNNER_ID_MISMATCH"


@dataclass(frozen=True)
class ClaimedRun:
    run: AgentRun
    run_token: SecretValue


@dataclass(frozen=True)
class RunEventCommand:
    seq: int
    at: datetime
    step: AgentRunStep
    level: LogLevel
    message: str
    data: dict[str, Any] | None


def next_lease(now: datetime, deadline_at: datetime) -> datetime:
    """임대는 지금부터 60초, 마감을 넘지 않는다."""
    return min(now + RUN_LEASE, deadline_at)


def run_target(run: AgentRun) -> AuditTarget:
    """감사 대상. 이름표는 입력 사본의 서비스 이름이다."""
    service = run.input_snapshot.get("service") or {}
    return AuditTarget(AuditTargetType.AGENT_RUN, run.id, service.get("name"))


async def expire_overdue_runs(
    runs: AgentRunRepository,
    audit: AuditRecorder,
    now: datetime,
    *,
    service_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
) -> list[AgentRun]:
    """임대·마감이 지난 열린 실행을 TIMED_OUT 으로 바꾸고 SYSTEM 줄을 쓴다. 바꾼 실행을 돌려준다."""
    overdue = await runs.lock_overdue(now, EXPIRE_BATCH_SIZE, service_id=service_id, run_id=run_id)
    expired = []
    for run in overdue:
        code = (
            AgentRunFailureCode.DEADLINE_EXCEEDED
            if run.deadline_at <= now
            else AgentRunFailureCode.LEASE_EXPIRED
        )
        if await runs.expire(run.id, now, code):
            audit.record(
                organization_id=run.organization_id,
                actor=AuditActor.system(AuditComponent.AI_RUNNER),
                action=AuditAction.AGENT_RUN_TIMED_OUT,
                result=AuditResult.FAILED,
                target=run_target(run),
                details={"failureCode": code.value},
            )
            expired.append(run)
            logger.info(
                "agent run timed out",
                extra={
                    "action": "expire_overdue_runs",
                    "run_id": str(run.id),
                    "failure_code": code.value,
                },
            )
    return expired


class AgentRunService:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock
        self._runs = AgentRunRepository(session)
        self._events = AgentRunEventRepository(session)
        self._audit = AuditRecorder(session, clock)

    async def claim_run(
        self, runner: AgentRunner, runner_id: str, capabilities: Sequence[str]
    ) -> ClaimedRun | None:
        """가져갈 사전 점검 하나를 준다. 없으면 None. 만료 전환은 어느 쪽이든 커밋한다."""
        if runner_id != runner.name:
            raise RequestFieldInvalidError("runnerId", RUNNER_ID_MISMATCH_REASON)
        now = self._clock.now()
        async with self._session.begin():
            await expire_overdue_runs(self._runs, self._audit, now)
            if AgentRunKind.PRECHECK not in capabilities:
                return None
            run = await self._runs.lock_next_queued(now)
            if run is None:
                return None
            token = new_run_token()
            is_claimed = await self._runs.claim(
                run.id,
                runner.id,
                hash_token(token.get_secret_value()),
                now,
                next_lease(now, run.deadline_at),
            )
            if not is_claimed:
                return None
            self._audit.record(
                organization_id=run.organization_id,
                actor=AuditActor.system(AuditComponent.AI_RUNNER),
                action=AuditAction.AGENT_RUN_CLAIMED,
                target=run_target(run),
                details={"runnerName": runner.name},
            )
            fresh = await self._runs.find_by_id(run.id)
        if fresh is None:
            return None
        logger.info(
            "agent run claimed",
            extra={"action": "claim_run", "run_id": str(fresh.id), "runner_id": str(runner.id)},
        )
        return ClaimedRun(fresh, token)

    async def heartbeat(self, run_id: uuid.UUID) -> AgentRun:
        """임대를 늘린다(첫 호출이면 RUNNING). 끝났거나 임대·마감이 지났으면 409."""
        now = self._clock.now()
        async with self._session.begin():
            run = await self._extend_lease(run_id, now)
        if run is None:
            raise RunNotActiveError(str(run_id))
        return run

    async def add_events(self, run_id: uuid.UUID, events: Sequence[RunEventCommand]) -> int:
        """이벤트를 넣고 그 실행의 가장 큰 seq 를 돌려준다. 같은 seq 는 무시한다. 임대도 늘린다."""
        now = self._clock.now()
        accepted_up_to = 0
        async with self._session.begin():
            run = await self._extend_lease(run_id, now)
            if run is not None:
                await self._events.add_ignoring_duplicates(
                    [_event_row(run_id, event, now) for event in events]
                )
                accepted_up_to = await self._events.find_last_seq(run_id)
        if run is None:
            raise RunNotActiveError(str(run_id))
        return accepted_up_to

    async def submit_result(
        self, run_id: uuid.UUID, normalized: NormalizedRunResult
    ) -> AgentRunStatus:
        """결과를 저장한다. 끝난 실행과 임대·마감이 지난 실행(이때 만료 전환)은 409."""
        now = self._clock.now()
        async with self._session.begin():
            run = await self._runs.find_by_id(run_id)
            if run is None or run.status not in OPEN_STATUSES:
                raise ResultAlreadySubmittedError(str(run_id))
            is_finished = await self._runs.finish(
                run_id,
                now,
                {
                    "status": normalized.status,
                    "failure_code": normalized.failure_code,
                    "result": normalized.result,
                    "model_id": normalized.model_id,
                    "prompt_version": normalized.prompt_version,
                    "simulated": normalized.simulated,
                    "processing": normalized.processing,
                },
            )
            if is_finished:
                self._record_finished(run, normalized)
            else:
                # 조건부 UPDATE 사이에 다른 호출이 끝냈으면 저장할 것 없이 끝난 실행으로 답한다.
                fresh = await self._runs.find_by_id(run_id)
                if fresh is None or fresh.status not in OPEN_STATUSES:
                    raise ResultAlreadySubmittedError(str(run_id))
                await expire_overdue_runs(self._runs, self._audit, now, run_id=run_id)
        if not is_finished:
            raise RunNotActiveError(str(run_id))
        logger.info(
            "agent run finished",
            extra={
                "action": "submit_result",
                "run_id": str(run_id),
                "status": normalized.status,
                "failure_code": normalized.failure_code,
            },
        )
        return normalized.status

    async def _extend_lease(self, run_id: uuid.UUID, now: datetime) -> AgentRun | None:
        """임대를 늘린 실행. 끝났거나 임대·마감이 지났으면(이때 만료 전환) None."""
        run = await self._runs.find_by_id(run_id)
        if run is None or run.status not in OPEN_STATUSES:
            return None
        if await self._runs.extend_lease(run_id, now, next_lease(now, run.deadline_at)):
            return await self._runs.find_by_id(run_id)
        await expire_overdue_runs(self._runs, self._audit, now, run_id=run_id)
        return None

    def _record_finished(self, run: AgentRun, normalized: NormalizedRunResult) -> None:
        details: dict[str, Any] = {
            "status": normalized.status.value,
            "simulated": normalized.simulated,
            "aiReviewed": normalized.is_ai_reviewed,
        }
        if normalized.failure_code is not None:
            details["failureCode"] = normalized.failure_code
        if normalized.processing is not None:
            details["processing"] = normalized.processing
        self._audit.record(
            organization_id=run.organization_id,
            actor=AuditActor.system(AuditComponent.AI_RUNNER),
            action=AuditAction.AGENT_RUN_FINISHED,
            result=(
                AuditResult.SUCCEEDED
                if normalized.status is AgentRunStatus.SUCCEEDED
                else AuditResult.FAILED
            ),
            target=run_target(run),
            details=details,
        )


def _event_row(run_id: uuid.UUID, event: RunEventCommand, now: datetime) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "run_id": run_id,
        "seq": event.seq,
        "at": event.at,
        "step": event.step.value,
        "level": event.level.value,
        "message": mask_secrets(event.message),
        "data": mask_secrets_in_json(event.data) if event.data is not None else None,
        "created_at": now,
    }
