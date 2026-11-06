"""사람 쪽 유스케이스: AI 사전 점검 패치 승인·거절.

- 결정은 부모 실행 행을 FOR UPDATE 로 잠근 한 트랜잭션에서 한다. 패치 원문은 부모 실행의 결과 JSON
  그대로이고, 결정은 agent_patch_approvals 에 패치마다 한 행만 남는다.
- 거절 순서: 실행 → 패치 → 부모 성공 → BLOCKED(승인만) → 이미 결정 → 낡음 → 자기 승인(승인만)
  → 열린 실행(스위치 켜짐 승인만). 같은 결정을 다시 보내면 뒤쪽 검사 없이 200 이고 줄을 쓰지 않는다.
- 스위치가 꺼져 있으면(기본) 결정 행만 남긴다. 켜져 있으면 승인이 반영 실행(APPLY_PATCH)도 만든다.
"""

import hashlib
import logging
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.actions import AuditAction
from app.audit.recorder import AuditActor, AuditRecorder
from app.core.clock import Clock
from app.core.exceptions import (
    OrganizationNotFoundError,
    PatchAlreadyDecidedError,
    PatchBlockedError,
    PatchNotFoundError,
    PatchStaleError,
    PrecheckInProgressError,
    PrecheckNotFoundError,
    RunNotSucceededError,
    SelfApprovalForbiddenError,
)
from app.core.log_masking import mask_secrets_in_json
from app.enums import AgentPatchDecision, AgentPatchStatus, AgentRunKind, AgentRunStatus
from app.models import AgentPatchApproval, AgentRun
from app.repositories.agent_patch_approval_repository import AgentPatchApprovalRepository
from app.repositories.agent_run_repository import AgentRunRepository
from app.repositories.organization_repository import OrganizationRepository
from app.services.agent_run_service import RUN_DEADLINE, expire_overdue_runs, run_target
from app.services.integrity import violated_constraint_name
from app.services.precheck_service import (
    PrecheckPatchView,
    compute_checks_digest,
    compute_input_hash,
    insert_agent_run,
    load_patch_decisions,
)
from app.services.run_result import patch_origin

logger = logging.getLogger(__name__)

PATCH_POLICY_VERSION = "patch-policy-2026.11.05"
PATCH_DECISION_WINDOW = timedelta(hours=24)
APPLY_INPUT_SCHEMA_VERSION = 1
APPLY_REPOSITORY = "Softbank-Hackathon-2026-Team-Echo/echo-ai-agent"
_DECISION_UNIQUE_CONSTRAINT = "uq_agent_patch_approvals_run_id_patch_id"
_AUDIT_ACTION_BY_DECISION = {
    AgentPatchDecision.APPROVED: AuditAction.AGENT_PATCH_APPROVED,
    AgentPatchDecision.REJECTED: AuditAction.AGENT_PATCH_REJECTED,
}


class PrecheckPatchService:
    def __init__(self, session: AsyncSession, clock: Clock, *, patch_apply_enabled: bool) -> None:
        self._session = session
        self._clock = clock
        self._patch_apply_enabled = patch_apply_enabled
        self._runs = AgentRunRepository(session)
        self._approvals = AgentPatchApprovalRepository(session)
        self._organizations = OrganizationRepository(session)
        self._audit = AuditRecorder(session, clock)

    async def approve_patch(
        self,
        organization_id: uuid.UUID,
        precheck_id: uuid.UUID,
        patch_id: str,
        actor_user_id: uuid.UUID,
        patch_hash: str,
        reason: str | None,
    ) -> PrecheckPatchView:
        return await self._decide(
            organization_id,
            precheck_id,
            patch_id,
            actor_user_id,
            AgentPatchDecision.APPROVED,
            patch_hash,
            reason,
        )

    async def reject_patch(
        self,
        organization_id: uuid.UUID,
        precheck_id: uuid.UUID,
        patch_id: str,
        actor_user_id: uuid.UUID,
        patch_hash: str,
        reason: str | None,
    ) -> PrecheckPatchView:
        return await self._decide(
            organization_id,
            precheck_id,
            patch_id,
            actor_user_id,
            AgentPatchDecision.REJECTED,
            patch_hash,
            reason,
        )

    async def _decide(
        self,
        organization_id: uuid.UUID,
        precheck_id: uuid.UUID,
        patch_id: str,
        actor_user_id: uuid.UUID,
        decision: AgentPatchDecision,
        patch_hash: str,
        reason: str | None,
    ) -> PrecheckPatchView:
        now = self._clock.now()
        is_approval = decision == AgentPatchDecision.APPROVED
        async with self._session.begin():
            run = await self._runs.find_patch_parent(organization_id, precheck_id, lock=True)
            if run is None:
                raise PrecheckNotFoundError(str(precheck_id))
            stored = _find_stored_patch(run, patch_id)
            if stored is None:
                raise PatchNotFoundError(patch_id)
            if run.status != AgentRunStatus.SUCCEEDED:
                raise RunNotSucceededError(str(run.id))
            if is_approval and stored["status"] == AgentPatchStatus.BLOCKED:
                raise PatchBlockedError(stored.get("blockReason"))
            approval = await self._approvals.find_by_run_and_patch(run.id, patch_id)
            is_new = False
            if approval is None:
                _check_not_stale(run, stored, patch_hash, now)
                if is_approval:
                    await self._check_self_approval(run, actor_user_id)
                    await self._check_no_open_run(run, now)
                approval, is_new = await self._insert_decision(
                    run, stored, actor_user_id, decision, reason, now
                )
            if approval.decision != decision:
                raise PatchAlreadyDecidedError(approval.decision)
            if is_new:
                self._record_decision(run, stored, approval, actor_user_id, decision)
            decisions = await load_patch_decisions(self._approvals, self._runs, run.id)
            view = decisions.patch_view(stored)
        if is_new:
            logger.info(
                "precheck patch decided",
                extra={
                    "action": "decide_patch",
                    "run_id": str(precheck_id),
                    "patch_id": patch_id,
                    "decision": decision.value,
                    "apply_run_id": str(approval.apply_run_id) if approval.apply_run_id else None,
                },
            )
        return view

    async def _check_self_approval(self, run: AgentRun, actor_user_id: uuid.UUID) -> None:
        if run.requested_by_user_id != actor_user_id:
            return
        organization = await self._organizations.find_by_id(run.organization_id)
        if organization is None:
            raise OrganizationNotFoundError
        if not organization.allow_self_approval:
            raise SelfApprovalForbiddenError

    async def _check_no_open_run(self, run: AgentRun, now: datetime) -> None:
        """스위치가 켜져 있을 때만 본다(가짜 진단의 반영은 끝난 채로 넣어 열린 실행이 안 생긴다).

        임대·마감이 지난 실행은 먼저 끝낸다.
        """
        if not self._patch_apply_enabled or is_simulated_diagnosis(run):
            return
        await expire_overdue_runs(self._runs, self._audit, now, service_id=run.service_id)
        open_run = await self._runs.find_open_for_service(run.service_id)
        if open_run is not None:
            raise PrecheckInProgressError(str(open_run.id))

    async def _insert_decision(
        self,
        run: AgentRun,
        stored: Mapping[str, Any],
        actor_user_id: uuid.UUID,
        decision: AgentPatchDecision,
        reason: str | None,
        now: datetime,
    ) -> tuple[AgentPatchApproval, bool]:
        """결정 행(승인 + 스위치 켜짐이면 반영 실행도)을 넣는다. (행, 새로 넣었나)를 돌려준다.

        부모 잠금이 동시 요청을 줄 세우지만, 잠금 밖에서 먼저 들어간 행이 있으면 그 행을 돌려준다.
        반영 실행도 같은 savepoint 안이라 함께 되돌려져 짝 없는 실행이 남지 않는다.
        """
        patch_id = stored["id"]
        try:
            async with self._session.begin_nested():
                apply_run_id = None
                if decision == AgentPatchDecision.APPROVED and (
                    self._patch_apply_enabled or is_simulated_diagnosis(run)
                ):
                    apply_run = _build_apply_run(run, stored, actor_user_id, now)
                    await insert_agent_run(self._session, self._runs, apply_run)
                    apply_run_id = apply_run.id
                approval = AgentPatchApproval(
                    run_id=run.id,
                    patch_id=patch_id,
                    patch_hash=stored["patchHash"],
                    checks_digest=compute_checks_digest(stored["checks"]),
                    policy_version=PATCH_POLICY_VERSION,
                    decision=decision,
                    decided_by_user_id=actor_user_id,
                    reason=reason,
                    decided_at=now,
                    apply_run_id=apply_run_id,
                    created_at=now,
                )
                self._approvals.add(approval)
                await self._session.flush()
        except IntegrityError as exc:
            if violated_constraint_name(exc) != _DECISION_UNIQUE_CONSTRAINT:
                raise
            existing = await self._approvals.find_by_run_and_patch(run.id, patch_id)
            if existing is None:
                raise
            return existing, False
        return approval, True

    def _record_decision(
        self,
        run: AgentRun,
        stored: Mapping[str, Any],
        approval: AgentPatchApproval,
        actor_user_id: uuid.UUID,
        decision: AgentPatchDecision,
    ) -> None:
        details: dict[str, Any] = {"patchId": stored["id"], "patchHash": stored["patchHash"]}
        if decision == AgentPatchDecision.APPROVED:
            details["applyRunId"] = str(approval.apply_run_id) if approval.apply_run_id else None
        self._audit.record(
            organization_id=run.organization_id,
            actor=AuditActor.user(actor_user_id),
            action=_AUDIT_ACTION_BY_DECISION[decision],
            target=run_target(run),
            details=details,
        )


def _find_stored_patch(run: AgentRun, patch_id: str) -> Mapping[str, Any] | None:
    patches = (run.result or {}).get("patches", [])
    return next((patch for patch in patches if patch["id"] == patch_id), None)


def _check_not_stale(
    run: AgentRun, stored: Mapping[str, Any], patch_hash: str, now: datetime
) -> None:
    if stored["patchHash"] != patch_hash:
        raise PatchStaleError("patchHash", PatchStaleError.HASH_MISMATCH_REASON)
    finished_at = run.finished_at
    if finished_at is not None and now - finished_at > PATCH_DECISION_WINDOW:
        raise PatchStaleError("finishedAt", PatchStaleError.EXPIRED_REASON)


def is_simulated_diagnosis(run: AgentRun) -> bool:
    """WAS 가 지어낸 가짜 진단(simulate)인가. 그 패치 승인은 러너 없이 가짜 반영으로 끝낸다."""
    return run.kind == AgentRunKind.CHANGE_PROPOSAL and run.simulated


def simulated_commit_sha(apply_run_id: uuid.UUID, patch_hash: str) -> str:
    """가짜 반영의 appliedCommitSha. 실제 커밋이 아니다(simulated = true 로 함께 보인다)."""
    return hashlib.sha256(f"simulated:{apply_run_id}:{patch_hash}".encode()).hexdigest()[:40]


def _build_apply_run(
    run: AgentRun, stored: Mapping[str, Any], actor_user_id: uuid.UUID, now: datetime
) -> AgentRun:
    """반영 실행. branch 에 실행 id 가 들어가서 id 를 먼저 정한다.

    부모가 배포 실패 진단이면 repo 는 그 서비스 저장소이고 diagnosis 묶음(배포 요청·환경·실패
    커밋)을 더한다(SPEC api-98). 가짜 진단이면 러너에게 주지 않고 성공(simulated)으로 끝낸 채
    넣는다.
    """
    apply_run_id = uuid.uuid4()
    branch = f"applied/{apply_run_id}"
    snapshot: dict[str, Any] = {
        "schemaVersion": APPLY_INPUT_SCHEMA_VERSION,
        "parentRunId": str(run.id),
        "patch": {
            "id": stored["id"],
            "patchHash": stored["patchHash"],
            "files": stored["files"],
            "diff": stored["diff"],
            "origin": patch_origin(stored).value,
        },
        "repo": APPLY_REPOSITORY,
        "branch": branch,
    }
    if run.kind == AgentRunKind.CHANGE_PROPOSAL:
        deploy_request = run.input_snapshot.get("deployRequest") or {}
        snapshot["repo"] = deploy_request.get("repo") or APPLY_REPOSITORY
        snapshot["diagnosis"] = {
            "deployRequestId": deploy_request.get("id"),
            "environmentId": str(run.target_environment_id),
            "sourceCommitSha": deploy_request.get("commitSha"),
        }
    masked: dict[str, Any] = mask_secrets_in_json(snapshot)
    apply_run = AgentRun(
        id=apply_run_id,
        organization_id=run.organization_id,
        service_id=run.service_id,
        kind=AgentRunKind.APPLY_PATCH,
        source_environment_id=run.source_environment_id,
        target_environment_id=run.target_environment_id,
        parent_run_id=run.id,
        input_snapshot=masked,
        input_hash=compute_input_hash(masked),
        requested_by_user_id=actor_user_id,
        deadline_at=now + RUN_DEADLINE,
        created_at=now,
        updated_at=now,
    )
    if is_simulated_diagnosis(run):
        apply_run.status = AgentRunStatus.SUCCEEDED
        apply_run.finished_at = now
        apply_run.simulated = True
        apply_run.result = {
            "appliedCommitSha": simulated_commit_sha(apply_run_id, stored["patchHash"]),
            "branch": branch,
        }
    return apply_run
