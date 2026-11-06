"""배포 실패 AI 진단(CHANGE_PROPOSAL) 만들기·가짜 진단·승인 뒤 재배포 잇기(SPEC api-98).

흐름
- 사람이 실패한 배포 요청에서 진단을 만든다(POST .../deploy-requests/{id}/diagnosis). 배포 요청
  하나에 진단은 하나뿐이고(부분 unique), 이미 있으면 그 실행을 그대로 돌려준다(200).
- 진단 실행은 사전 점검과 같은 agent_runs 행이다. 러너는 capabilities 에 CHANGE_PROPOSAL 을
  밝혀야 가져가고, 입력 사본에는 #97 실패 상세(failure, 비밀값을 가린 값)를 싣는다.
- simulate 를 주면(스위치 AI_DIAGNOSIS_SIMULATE_ENABLED) 러너 없이 WAS 가 가짜 진단을 바로
  성공(simulated = true)으로 끝낸다. 그 패치 승인도 러너 없이 가짜 반영으로 끝난다.
  러너 스위치 AI_DIAGNOSIS_RUNNER_ENABLED 가 꺼져 있으면(기본) 본문 없는 요청도 가짜 진단이다.
- 반영 실행이 성공하면(러너 결과 또는 가짜 반영) 같은 환경에 재배포 요청을 기존 배포 접수
  규칙으로 만든다. 러너가 고친 커밋을 push 하지 않으므로 지금은 원래 실패 커밋을 다시
  배포한다(pullRequestUrl 은 늘 null). 접수가 거절되면(이전 중 409 등) 그 오류 코드를 남긴다.
  반영 실행 행을 FOR UPDATE 로 잡고 확인·접수·연결을 한 트랜잭션으로 해 재배포는 한 번뿐이다.
- 진단 실행은 C-AI-1 증거가 아니다(증거는 kind = PRECHECK 만 고른다). AI 레지던시 요약도
  진단 실행을 세지 않는다(ai_residency.RESIDENCY_EXCLUDED_KINDS).
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.actions import AuditAction
from app.audit.recorder import AuditActor, AuditRecorder
from app.core.clock import Clock
from app.core.exceptions import (
    AppError,
    DeployNotFailedError,
    DeployRequestNotFoundError,
    PrecheckInProgressError,
    ServiceDeletingError,
    SimulateNotAllowedError,
)
from app.core.log_masking import mask_secrets_in_json
from app.core.repo_url import InvalidRepoUrlError, parse_repo_url
from app.enums import (
    AgentRunKind,
    AgentRunStatus,
    AgentRunStep,
    DeployRequestStatus,
    DiagnosisCategory,
    DiagnosisEvidenceSource,
    DiagnosisResult,
    LogLevel,
    RefType,
    ServiceStatus,
)
from app.models import AgentRun, DeployRequest, Environment, Service
from app.models.agent_run import DIAGNOSIS_UNIQUE_INDEX, OPEN_RUN_UNIQUE_INDEX
from app.repositories.agent_run_event_repository import AgentRunEventRepository
from app.repositories.agent_run_repository import AgentRunRepository
from app.repositories.deploy_request_log_repository import DeployRequestLogRepository
from app.repositories.deploy_request_repository import DeployRequestRepository
from app.repositories.environment_repository import EnvironmentRepository
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.repositories.service_environment_variable_repository import (
    ServiceEnvironmentVariableRepository,
)
from app.repositories.service_repository import ServiceRepository
from app.services.agent_run_service import RUN_DEADLINE, expire_overdue_runs, run_target
from app.services.apply_commit_service import ApplyCommitService
from app.services.container_diagnostics import (
    FAILURE_LOG_CODES,
    FailureDetail,
    build_failure_detail,
)
from app.services.deploy_request_service import CreateDeployCommand, DeployRequestService
from app.services.integrity import violated_constraint_name
from app.services.precheck_service import (
    PrecheckView,
    compute_input_hash,
    load_precheck_view,
)
from app.services.run_result import compute_patch_hash, normalize_diagnosis_result

logger = logging.getLogger(__name__)

DIAGNOSIS_INPUT_SCHEMA_VERSION = 1
# 반영 실행 result 에 WAS 가 덧붙이는 칸(러너 결과 칸과 겹치지 않는다).
REDEPLOY_REQUEST_ID_KEY = "redeployRequestId"
REDEPLOY_REJECTED_CODE_KEY = "redeployRejectedCode"
REDEPLOY_INTERNAL_ERROR_CODE = "INTERNAL_ERROR"


@dataclass(frozen=True)
class DiagnosisSimulate:
    """가짜 진단 모양. DIAGNOSED 면 원인 + 패치 1개, 그 밖이면 패치 없이 근거 부족 사유."""

    result: DiagnosisResult = DiagnosisResult.DIAGNOSED


@dataclass(frozen=True)
class DiagnosisCreated:
    view: PrecheckView
    is_created: bool


class DiagnosisService:
    def __init__(
        self,
        session: AsyncSession,
        clock: Clock,
        deploy_requests: DeployRequestService,
        *,
        simulate_enabled: bool,
        runner_enabled: bool = False,
        apply_commit: ApplyCommitService | None = None,
    ) -> None:
        self._session = session
        self._clock = clock
        self._deploy_request_service = deploy_requests
        self._simulate_enabled = simulate_enabled
        self._runner_enabled = runner_enabled
        self._apply_commit = apply_commit
        self._runs = AgentRunRepository(session)
        self._events = AgentRunEventRepository(session)
        self._deploy_requests = DeployRequestRepository(session)
        self._deploy_logs = DeployRequestLogRepository(session)
        self._services = ServiceRepository(session)
        self._environments = EnvironmentRepository(session)
        self._connections = ServiceEnvironmentRepository(session)
        self._variables = ServiceEnvironmentVariableRepository(session)
        self._audit = AuditRecorder(session, clock)

    async def create_diagnosis(
        self,
        organization_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        deploy_request_id: uuid.UUID,
        simulate: DiagnosisSimulate | None,
    ) -> DiagnosisCreated:
        """진단을 만든다(201). 이미 있으면 그 실행(200).

        거절 순서: simulate 스위치(422) → 배포 요청(404) → 이미 있음(200) → 실패 아님(409
        DEPLOY_NOT_FAILED) → 서비스·환경(404·409 SERVICE_DELETING) → 열린 실행(409, 러너 실행만).
        러너 스위치(AI_DIAGNOSIS_RUNNER_ENABLED)가 꺼져 있으면 본문이 없어도 가짜 진단이다.
        """
        if simulate is not None and not self._simulate_enabled:
            raise SimulateNotAllowedError("diagnosis simulate is disabled")
        if simulate is None and not self._runner_enabled:
            simulate = DiagnosisSimulate()
        now = self._clock.now()
        async with self._session.begin():
            request = await self._deploy_requests.find_in_organization(
                organization_id, deploy_request_id
            )
            if request is None:
                raise DeployRequestNotFoundError(
                    "deploy request not found", deploy_request_id=str(deploy_request_id)
                )
            existing = await self._runs.find_diagnosis(request.id)
            if existing is not None:
                return DiagnosisCreated(await load_precheck_view(self._session, existing), False)
            if request.status != DeployRequestStatus.FAILED:
                raise DeployNotFailedError(str(request.id), request.status)
            service = await self._services.get_live_by_id(
                organization_id, request.service_id, lock=True
            )
            if service.status == ServiceStatus.DELETING:
                raise ServiceDeletingError("service is being deleted", service_id=str(service.id))
            environment = await self._environments.get_live_by_id(
                organization_id, request.environment_id
            )
            await expire_overdue_runs(self._runs, self._audit, now, service_id=service.id)
            open_run = await self._runs.find_open_for_service(service.id)
            if open_run is not None and simulate is None:
                raise PrecheckInProgressError(str(open_run.id))
            snapshot = await self._build_input_snapshot(service, environment, request)
            run = AgentRun(
                organization_id=organization_id,
                service_id=service.id,
                kind=AgentRunKind.CHANGE_PROPOSAL,
                source_environment_id=None,
                target_environment_id=environment.id,
                deploy_request_id=request.id,
                input_snapshot=snapshot,
                input_hash=compute_input_hash(snapshot),
                requested_by_user_id=actor_user_id,
                deadline_at=now + RUN_DEADLINE,
                created_at=now,
                updated_at=now,
            )
            if simulate is not None:
                _finish_simulated(run, simulate, now)
            winner = await self._insert(run)
            if winner is not None:
                return DiagnosisCreated(await load_precheck_view(self._session, winner), False)
            if simulate is not None:
                await self._events.add_ignoring_duplicates(_simulated_events(run.id, now))
            self._audit.record(
                organization_id=organization_id,
                actor=AuditActor.user(actor_user_id),
                action=AuditAction.DIAGNOSIS_REQUESTED,
                target=run_target(run),
                details={
                    "serviceId": str(service.id),
                    "deployRequestId": str(request.id),
                    "environmentId": str(environment.id),
                    "simulated": run.simulated,
                },
            )
            view = await load_precheck_view(self._session, run)
        logger.info(
            "diagnosis requested",
            extra={
                "action": "create_diagnosis",
                "run_id": str(run.id),
                "deploy_request_id": str(deploy_request_id),
                "simulated": run.simulated,
            },
        )
        return DiagnosisCreated(view, True)

    async def redeploy_after_apply(self, apply_run_id: uuid.UUID) -> AgentRun | None:
        """성공한 진단 패치 반영 뒤 같은 환경에 재배포 요청을 만들고 반영 실행에 연결한다.

        진단에서 온 반영이 아니거나, 성공이 아니거나, 이미 연결(또는 거절 기록)이 있으면 하지
        않는다(멱등). 바뀐(또는 이미 연결된) 반영 실행을 돌려준다(진단 반영이 아니면 None).

        한 트랜잭션: 반영 실행 행 FOR UPDATE → 연결 확인 → 접수 → 연결 기록. 같은 반영을 두
        요청이 함께 이어도(동시 승인·러너 결과와 승인 경합) 뒤 요청은 앞 요청이 커밋할 때까지
        기다렸다가 이미 연결된 값을 읽으므로 재배포는 정확히 하나다.

        접수 거절(AppError, 예: 이전 중 409·서비스 삭제 중)은 savepoint 로 접수 쓰기만 되돌리고
        그 코드를 redeployRejectedCode 로 남긴다 — 반영 결과·승인 응답은 성공 그대로다.
        """
        # 재배포 커밋(SPEC api-19): AI_APPLY_COMMIT_ENABLED 가 켜져 있으면 행 잠금 트랜잭션 밖에서
        # (GitHub 호출) 서비스 저장소에 커밋·PR 을 만들고 그 커밋을 쓴다. 실패면 재배포 없음.
        repo_commit_sha: str | None = None
        if self._apply_commit is not None:
            committed = await self._apply_commit.prepare_redeploy_commit(apply_run_id)
            if committed.stopped:
                return committed.run
            repo_commit_sha = committed.ref
        rejected_code: str | None = None
        redeploy_id: uuid.UUID | None = None
        async with self._session.begin():
            apply_run = await self._runs.find_by_id(apply_run_id, lock=True)
            if (
                apply_run is None
                or apply_run.kind != AgentRunKind.APPLY_PATCH
                or apply_run.parent_run_id is None
            ):
                return None
            if apply_run.status != AgentRunStatus.SUCCEEDED:
                return None
            if REDEPLOY_REQUEST_ID_KEY in (apply_run.result or {}):
                return apply_run
            parent = await self._runs.find_by_id(apply_run.parent_run_id)
            if parent is None or parent.deploy_request_id is None:
                return None
            failed = await self._deploy_requests.find_by_id(parent.deploy_request_id)
            if failed is None:
                return None
            # savepoint 를 되돌리면 그 안에서 바꾼 객체가 만료될 수 있으므로 필요한 값을 먼저 뺀다.
            organization_id = apply_run.organization_id
            actor_user_id = apply_run.requested_by_user_id
            base_result = dict(apply_run.result or {})
            service_id = failed.service_id
            commit_sha = repo_commit_sha or failed.commit_sha
            command = CreateDeployCommand(
                environment_id=failed.environment_id,
                ref_type=RefType.COMMIT,
                ref_value=commit_sha,
                simulate_fail_at=None,
                diagnosis_apply_run_id=apply_run_id,
            )
            try:
                async with self._session.begin_nested():
                    view = await self._deploy_request_service.create_redeploy_in_transaction(
                        organization_id, service_id, actor_user_id, command, commit_sha
                    )
                    redeploy_id = view.deploy_request.id
            except AppError as exc:
                rejected_code = exc.code
            except Exception:
                # DB 일시 오류 등. 반영 결과는 이미 커밋됐으므로 응답을 500 으로 만들지 않고
                # 거절 코드로 남긴다(사람이 다시 배포한다).
                logger.exception(
                    "diagnosis redeploy failed",
                    extra={"action": "redeploy_after_apply", "apply_run_id": str(apply_run_id)},
                )
                rejected_code = REDEPLOY_INTERNAL_ERROR_CODE
            await self._runs.merge_result(
                apply_run_id,
                self._clock.now(),
                {
                    **base_result,
                    REDEPLOY_REQUEST_ID_KEY: str(redeploy_id) if redeploy_id else None,
                    REDEPLOY_REJECTED_CODE_KEY: rejected_code,
                },
            )
            updated = await self._runs.find_by_id(apply_run_id)
        logger.info(
            "diagnosis redeploy linked",
            extra={
                "action": "redeploy_after_apply",
                "apply_run_id": str(apply_run_id),
                "redeploy_request_id": str(redeploy_id) if redeploy_id else None,
                "rejected_code": rejected_code,
            },
        )
        return updated

    async def _insert(self, run: AgentRun) -> AgentRun | None:
        """넣는다. 같은 배포 요청의 진단이 먼저 들어갔으면(동시 요청) 그 실행을 돌려준다."""
        try:
            async with self._session.begin_nested():
                self._runs.add(run)
                await self._session.flush()
        except IntegrityError as exc:
            name = violated_constraint_name(exc)
            if name == DIAGNOSIS_UNIQUE_INDEX and run.deploy_request_id is not None:
                return await self._runs.find_diagnosis(run.deploy_request_id)
            if name == OPEN_RUN_UNIQUE_INDEX:
                existing = await self._runs.find_open_for_service(run.service_id)
                raise PrecheckInProgressError(str(existing.id) if existing else None) from None
            raise
        return None

    async def _build_input_snapshot(
        self, service: Service, environment: Environment, request: DeployRequest
    ) -> dict[str, Any]:
        """러너에게 주는 입력. 비밀값·비밀 참조·변수 값은 싣지 않는다(변수는 이름만).

        failure 는 #97 실패 상세(배포 상세 failure 칸과 같은 값, 비밀값을 가린 로그 꼬리)다.
        """
        rows = await self._deploy_logs.search_by_codes(request.id, FAILURE_LOG_CODES)
        failure = build_failure_detail(
            request.status, request.error_code, [(row.code, row.message) for row in rows]
        )
        connections = await self._connections.search_live_by_service(service.id)
        connection = next(
            (item for item in connections if item.id == request.service_environment_id), None
        )
        variable_keys: list[str] = []
        if connection is not None:
            variables = await self._variables.search_live_by_connection(connection.id)
            variable_keys = sorted(variable.key for variable in variables)
        snapshot = {
            "schemaVersion": DIAGNOSIS_INPUT_SCHEMA_VERSION,
            "service": {
                "id": str(service.id),
                "name": service.name,
                "containerPort": service.container_port,
                "healthCheckPath": service.health_check_path,
            },
            "deployRequest": {
                "id": str(request.id),
                "environmentId": str(request.environment_id),
                "trigger": request.trigger,
                "repo": _repo_full_name(request.repo_url),
                "refType": request.ref_type,
                "refValue": request.ref_value,
                "commitSha": request.commit_sha,
                "errorCode": request.error_code,
                "failedAt": request.finished_at.isoformat() if request.finished_at else None,
            },
            "environment": {"id": str(environment.id), "kind": environment.kind},
            "variableKeys": variable_keys,
            "failure": _failure_snapshot(failure),
        }
        masked: dict[str, Any] = mask_secrets_in_json(snapshot)
        return masked


def _repo_full_name(repo_url: str) -> str | None:
    try:
        return parse_repo_url(repo_url).full_name
    except InvalidRepoUrlError:
        return None


def _failure_snapshot(failure: FailureDetail | None) -> dict[str, Any] | None:
    """배포 상세 failure 칸과 같은 camelCase 값."""
    if failure is None:
        return None
    return {
        "code": failure.code.value,
        "message": failure.message,
        "container": failure.container,
        "containerReason": failure.container_reason,
        "terminationReason": failure.termination_reason,
        "exitCode": failure.exit_code,
        "restartCount": failure.restart_count,
        "logTail": list(failure.log_tail),
        "logTruncated": failure.log_truncated,
        "previousVersionRunning": failure.previous_version_running,
    }


# --- 가짜 진단(simulate) ---

SIMULATED_FILE = "app/static/index.html"
SIMULATED_LINE = 120
SIMULATED_CAUSE = "app/static/index.html 120번째 줄의 괄호가 닫히지 않았습니다."
SIMULATED_UNFIXABLE_REASON = "실패 로그에 원인을 가리킬 근거가 부족합니다(simulated)."
SIMULATED_DIFF = (
    "--- a/app/static/index.html\n"
    "+++ b/app/static/index.html\n"
    "@@ -119,3 +119,3 @@\n"
    "     <script>\n"
    "-      renderStatus(data;\n"
    "+      renderStatus(data);\n"
    "     </script>\n"
)
SIMULATED_CHECKS = (
    {"name": "BUILD", "result": "PASS", "summary": "simulated: image build passed"},
    {"name": "TRIVY", "result": "PASS", "summary": "simulated: no HIGH/CRITICAL"},
    {"name": "HEALTHZ", "result": "PASS", "summary": "simulated: /healthz 200"},
)
_SIMULATED_EVENTS = (
    (AgentRunStep.INPUT_LOAD, "failure context loaded (simulated)"),
    (AgentRunStep.ANALYZE, "failure diagnosed (simulated)"),
    (AgentRunStep.PATCH_PROPOSE, "patch proposed (simulated)"),
    (AgentRunStep.PATCH_VERIFY, "patch verified in sandbox (simulated)"),
)
_SIMULATED_EVENT_GAP = timedelta(seconds=1)


def simulated_runner_payload(simulate: DiagnosisSimulate) -> dict[str, Any]:
    """러너 결과 본문과 같은 모양의 가짜 진단. 저장은 러너 결과와 같은 정규화를 거친다."""
    payload: dict[str, Any] = {
        "status": AgentRunStatus.SUCCEEDED.value,
        "failureCode": None,
        "simulated": True,
        "sandbox": None,
        "model": None,
        "processing": None,
        "configDiff": [],
        "featureGaps": [],
        "findings": [],
        "patches": [],
        "blockedAttempts": [],
    }
    if simulate.result is not DiagnosisResult.DIAGNOSED:
        payload["diagnosis"] = {
            "result": DiagnosisResult.INSUFFICIENT_EVIDENCE.value,
            "category": None,
            "cause": None,
            "fixable": False,
            "unfixableReason": SIMULATED_UNFIXABLE_REASON,
            "evidence": [],
        }
        return payload
    payload["diagnosis"] = {
        "result": DiagnosisResult.DIAGNOSED.value,
        "category": DiagnosisCategory.CODE.value,
        "cause": SIMULATED_CAUSE,
        "fixable": True,
        "unfixableReason": None,
        "evidence": [
            {
                "source": DiagnosisEvidenceSource.LOG.value,
                "line": 42,
                "path": None,
                "text": "SyntaxError: Unexpected token ';'",
            },
            {
                "source": DiagnosisEvidenceSource.FILE.value,
                "line": SIMULATED_LINE,
                "path": SIMULATED_FILE,
                "text": "      renderStatus(data;",
            },
        ],
    }
    payload["patches"] = [
        {
            "id": "fix-index-html-paren",
            "patchHash": compute_patch_hash(SIMULATED_DIFF),
            "files": [SIMULATED_FILE],
            "diff": SIMULATED_DIFF,
            "reason": "닫히지 않은 괄호를 닫는다(simulated).",
            "touchesProtectedFiles": False,
            "checks": [dict(check) for check in SIMULATED_CHECKS],
            "origin": "AI",
        }
    ]
    return payload


def _finish_simulated(run: AgentRun, simulate: DiagnosisSimulate, now: datetime) -> None:
    """가짜 진단을 성공으로 끝낸 실행 값. 러너·모델을 거치지 않았다(model = null)."""
    normalized = normalize_diagnosis_result(simulated_runner_payload(simulate))
    run.status = AgentRunStatus.SUCCEEDED
    run.finished_at = now
    run.simulated = True
    run.result = normalized.result


def _simulated_events(run_id: uuid.UUID, now: datetime) -> list[dict[str, Any]]:
    """진행 단계(steps)가 화면에 보이도록 가짜 이벤트 몇 줄."""
    return [
        {
            "id": uuid.uuid4(),
            "run_id": run_id,
            "seq": seq,
            "at": now - _SIMULATED_EVENT_GAP * (len(_SIMULATED_EVENTS) - seq),
            "step": step.value,
            "level": LogLevel.INFO.value,
            "message": message,
            "data": {"simulated": True},
            "created_at": now,
        }
        for seq, (step, message) in enumerate(_SIMULATED_EVENTS, start=1)
    ]
