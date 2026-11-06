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
    ErrorField,
    RequestFieldInvalidError,
    ResultAlreadySubmittedError,
    ResultInvalidError,
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
from app.models import AgentPatchApproval, AgentRun, AgentRunner
from app.models.agent_run import OPEN_STATUSES
from app.repositories.agent_patch_approval_repository import AgentPatchApprovalRepository
from app.repositories.agent_run_event_repository import AgentRunEventRepository
from app.repositories.agent_run_repository import AgentRunRepository
from app.services.run_result import NormalizedApplyResult, NormalizedRunResult, compute_patch_hash

logger = logging.getLogger(__name__)

RUN_LEASE = timedelta(seconds=60)
RUN_DEADLINE = timedelta(minutes=15)
EXPIRE_BATCH_SIZE = 100
RUNNER_ID_MISMATCH_REASON = "RUNNER_ID_MISMATCH"
# 러너가 가져갈 수 있는 종류(PD-8). capabilities 에 그 종류를 밝힌 러너에게만 준다.
# CHANGE_PROPOSAL(배포 실패 진단, SPEC api-98)은 진단을 구현한 러너(echo-ai-agent #22)만 밝힌다.
CLAIMABLE_KINDS = (AgentRunKind.PRECHECK, AgentRunKind.APPLY_PATCH, AgentRunKind.CHANGE_PROPOSAL)
PATCH_HASH_MISMATCH_REASON = "PATCH_HASH_MISMATCH"
APPROVAL_NOT_FOUND_REASON = "APPROVAL_NOT_FOUND"
BRANCH_MISMATCH_REASON = "BRANCH_MISMATCH"


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


def record_run_finished(
    audit: AuditRecorder,
    run: AgentRun,
    *,
    actor: AuditActor,
    status: AgentRunStatus,
    simulated: bool,
    ai_reviewed: bool,
    failure_code: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """실행이 SUCCEEDED·FAILED 로 끝났다는 AGENT_RUN_FINISHED 줄을 쓴다.

    러너 결과 제출과 WAS 가 바로 끝내는 가짜 실행(가짜 진단·가짜 반영)이 모두 이 함수를 쓴다.
    C-AUD-1 자기 시험은 끝난 실행마다 이 줄을 요구하므로, 실행을 끝내는 경로는 같은
    트랜잭션에서 이 함수를 불러야 한다.
    """
    details: dict[str, Any] = {
        "kind": AgentRunKind(run.kind).value,
        "status": status.value,
        "simulated": simulated,
        "aiReviewed": ai_reviewed,
    }
    if extra:
        details.update(extra)
    if failure_code is not None:
        details["failureCode"] = failure_code
    audit.record(
        organization_id=run.organization_id,
        actor=actor,
        action=AuditAction.AGENT_RUN_FINISHED,
        result=(
            AuditResult.SUCCEEDED if status is AgentRunStatus.SUCCEEDED else AuditResult.FAILED
        ),
        target=run_target(run),
        details=details,
    )


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
        self._approvals = AgentPatchApprovalRepository(session)
        self._audit = AuditRecorder(session, clock)

    async def claim_run(
        self, runner: AgentRunner, runner_id: str, capabilities: Sequence[str]
    ) -> ClaimedRun | None:
        """capabilities 에 든 종류(PRECHECK·APPLY_PATCH·CHANGE_PROPOSAL) 중 가장 오래된 대기 실행
        하나를 준다(PD-8).

        없으면 None. 만료 전환은 어느 쪽이든 커밋한다.
        """
        if runner_id != runner.name:
            raise RequestFieldInvalidError("runnerId", RUNNER_ID_MISMATCH_REASON)
        now = self._clock.now()
        kinds = [kind for kind in CLAIMABLE_KINDS if kind in capabilities]
        async with self._session.begin():
            await expire_overdue_runs(self._runs, self._audit, now)
            if not kinds:
                return None
            run = await self._runs.lock_next_queued(now, kinds)
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
                details={"runnerName": runner.name, "kind": AgentRunKind(run.kind).value},
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

    async def find_run_kind(self, run_id: uuid.UUID) -> AgentRunKind | None:
        """결과 본문 모델을 고르려고 실행 종류만 읽는다(트랜잭션 없음)."""
        run = await self._runs.find_by_id(run_id)
        kind = AgentRunKind(run.kind) if run is not None else None
        await self._session.rollback()
        return kind

    async def submit_result(
        self, run_id: uuid.UUID, normalized: NormalizedRunResult
    ) -> AgentRunStatus:
        """결과를 저장한다. 끝난 실행과 임대·마감이 지난 실행(이때 만료 전환)은 409."""
        now = self._clock.now()
        async with self._session.begin():
            run = await self._runs.find_by_id(run_id)
            if run is None or run.status not in OPEN_STATUSES:
                raise ResultAlreadySubmittedError(str(run_id))
            is_finished = await self._finish_or_expire(
                run,
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

    async def submit_apply_result(
        self, run_id: uuid.UUID, normalized: NormalizedApplyResult
    ) -> AgentRunStatus:
        """반영 실행 결과를 저장한다(PD-9). 지문이 승인 행과 다르면 422, 그 밖은 결과 저장과 같다.

        성공 결과만 본다(실패는 그대로 받는다):
        - 입력 사본 patch.patchHash 또는 그 diff 로 다시 계산한 해시 ≠ 승인 행 → 422(PD-14).
        - branch 가 입력 사본 값(applied/<runId>)과 다름 → 422 BRANCH_MISMATCH.
        임대·마감이 지났으면 지문보다 먼저 만료 전환하고 409 다(422 로 롤백하면 만료가 안 남는다).
        """
        now = self._clock.now()
        async with self._session.begin():
            run = await self._runs.find_by_id(run_id)
            if run is None or run.status not in OPEN_STATUSES:
                raise ResultAlreadySubmittedError(str(run_id))
            if _is_lease_alive(run, now):
                approval = await self._approvals.find_by_apply_run(run.id)
                _check_apply_fingerprint(run, approval, normalized)
                is_finished = await self._finish_or_expire(
                    run,
                    now,
                    {
                        "status": normalized.status,
                        "failure_code": normalized.failure_code,
                        "result": normalized.result,
                        "simulated": normalized.simulated,
                    },
                )
            else:
                await expire_overdue_runs(self._runs, self._audit, now, run_id=run.id)
                is_finished = False
            if is_finished:
                self._record_apply_finished(run, normalized)
        if not is_finished:
            raise RunNotActiveError(str(run_id))
        logger.info(
            "agent apply run finished",
            extra={
                "action": "submit_apply_result",
                "run_id": str(run_id),
                "status": normalized.status,
                "failure_code": normalized.failure_code,
            },
        )
        return normalized.status

    async def _finish_or_expire(self, run: AgentRun, now: datetime, values: dict[str, Any]) -> bool:
        """결과를 적는다. 못 적었으면(임대·마감 지남) 만료 전환하고 False, 이미 끝났으면 409."""
        if await self._runs.finish(run.id, now, values):
            return True
        # 조건부 UPDATE 사이에 다른 호출이 끝냈으면 저장할 것 없이 끝난 실행으로 답한다.
        fresh = await self._runs.find_by_id(run.id)
        if fresh is None or fresh.status not in OPEN_STATUSES:
            raise ResultAlreadySubmittedError(str(run.id))
        await expire_overdue_runs(self._runs, self._audit, now, run_id=run.id)
        return False

    async def _extend_lease(self, run_id: uuid.UUID, now: datetime) -> AgentRun | None:
        """임대를 늘린 실행. 끝났거나 임대·마감이 지났으면(이때 만료 전환) None."""
        run = await self._runs.find_by_id(run_id)
        if run is None or run.status not in OPEN_STATUSES:
            return None
        if await self._runs.extend_lease(run_id, now, next_lease(now, run.deadline_at)):
            return await self._runs.find_by_id(run_id)
        await expire_overdue_runs(self._runs, self._audit, now, run_id=run_id)
        return None

    def _record_apply_finished(self, run: AgentRun, normalized: NormalizedApplyResult) -> None:
        record_run_finished(
            self._audit,
            run,
            actor=AuditActor.system(AuditComponent.AI_RUNNER),
            status=normalized.status,
            simulated=normalized.simulated,
            ai_reviewed=False,
            failure_code=normalized.failure_code,
            extra={"appliedCommitSha": normalized.applied_commit_sha},
        )

    def _record_finished(self, run: AgentRun, normalized: NormalizedRunResult) -> None:
        record_run_finished(
            self._audit,
            run,
            actor=AuditActor.system(AuditComponent.AI_RUNNER),
            status=normalized.status,
            simulated=normalized.simulated,
            ai_reviewed=normalized.is_ai_reviewed,
            failure_code=normalized.failure_code,
            extra=(
                {"processing": normalized.processing} if normalized.processing is not None else None
            ),
        )


def _is_lease_alive(run: AgentRun, now: datetime) -> bool:
    """임대와 마감이 모두 남아 있다. 저장소의 _is_lease_alive 와 같은 경계다."""
    return run.lease_until is not None and run.lease_until >= now and run.deadline_at > now


def _check_apply_fingerprint(
    run: AgentRun, approval: AgentPatchApproval | None, normalized: NormalizedApplyResult
) -> None:
    """성공 결과가 승인한 그 패치를 반영한 것인지 본다. 다르면 422 RESULT_INVALID(저장 없음).

    결과 본문에는 해시 칸이 없다(계약 2.1). WAS 는 러너에게 준 입력 사본의 patchHash 와 diff 를
    승인 행 해시와 다시 대조한다. 승인 행이 없으면(같은 트랜잭션에서 만들어 정상엔 없다) 거부한다.
    실패 결과는 대조하지 않는다 — 러너가 해시 불일치를 FAILED 로 알리는 길을 막지 않으려고.
    """
    if normalized.status is not AgentRunStatus.SUCCEEDED:
        return
    if approval is None:
        raise ResultInvalidError([ErrorField("status", APPROVAL_NOT_FOUND_REASON)])
    patch = run.input_snapshot.get("patch") or {}
    if patch.get("patchHash") != approval.patch_hash:
        raise ResultInvalidError(
            [ErrorField("inputSnapshot.patch.patchHash", PATCH_HASH_MISMATCH_REASON)]
        )
    if compute_patch_hash(patch.get("diff", "")) != approval.patch_hash:
        raise ResultInvalidError(
            [ErrorField("inputSnapshot.patch.diff", PATCH_HASH_MISMATCH_REASON)]
        )
    if normalized.result["branch"] != run.input_snapshot.get("branch"):
        raise ResultInvalidError([ErrorField("branch", BRANCH_MISMATCH_REASON)])


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
