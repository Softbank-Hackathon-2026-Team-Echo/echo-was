"""사람 쪽 유스케이스: AI 사전 점검 만들기·조회·이벤트 조회·취소, 입력 사본 만들기.

- 만들기는 서비스 행을 잠근 뒤 만료 → 열린 실행 확인 → INSERT 순서다. 같은 서비스의 열린 실행은
  하나뿐이다(부분 unique). 도착은 아직 연결하지 않은 환경도 받는다.
- 입력 사본(input_snapshot)에는 비밀값·비밀 참조·변수 값을 넣지 않는다. 변수는 이름만 싣는다.
  같은 상태면 같은 inputHash 가 나온다.
- GET 은 쓰지 않는다(만료 전환 없음). 저장된 상태와 leaseUntil 만 보여 준다.
"""

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import InvalidEnvironmentConfigError, UnknownEnvironmentKindError
from app.audit.actions import AuditAction
from app.audit.recorder import AuditActor, AuditRecorder
from app.core.clock import Clock
from app.core.exceptions import (
    PrecheckInProgressError,
    PrecheckNotFoundError,
    RequestFieldInvalidError,
    RunNotActiveError,
    ServiceDeletingError,
)
from app.core.log_masking import mask_secrets_in_json
from app.enums import AgentRunKind, AgentRunStep, LogLevel, ServiceStatus
from app.models import AgentRun, AgentRunEvent, Environment, Service, ServiceEnvironment
from app.models.agent_run import OPEN_RUN_UNIQUE_INDEX, OPEN_STATUSES
from app.repositories.agent_run_event_repository import AgentRunEventRepository
from app.repositories.agent_run_repository import AgentRunRepository
from app.repositories.deployment_repository import DeploymentRepository
from app.repositories.environment_repository import EnvironmentRepository
from app.repositories.finding_repository import FindingRepository
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.repositories.service_environment_variable_repository import (
    ServiceEnvironmentVariableRepository,
)
from app.repositories.service_repository import ServiceRepository
from app.services.agent_run_service import RUN_DEADLINE, expire_overdue_runs, run_target
from app.services.integrity import violated_constraint_name
from app.services.migration_plan_support import single_source

logger = logging.getLogger(__name__)

INPUT_SCHEMA_VERSION = 1
INPUT_FINDING_LIMIT = 50
SAME_ENVIRONMENT_REASON = "SAME_ENVIRONMENT"
_LEVEL_RANK = {LogLevel.INFO: 0, LogLevel.WARN: 1, LogLevel.ERROR: 2}
_SECRET_REF_KEYS = frozenset({"secretRef", "secret_ref"})


@dataclass(frozen=True)
class PrecheckStep:
    """이벤트에서 계산한 단계 하나. level 은 그 단계의 가장 나쁜 수준이다."""

    name: AgentRunStep
    first_at: datetime
    last_at: datetime
    level: LogLevel


@dataclass(frozen=True)
class PrecheckView:
    run: AgentRun
    steps: list[PrecheckStep]


@dataclass(frozen=True)
class PrecheckLogPage:
    items: list[AgentRunEvent]
    next_after: int


def compute_input_hash(snapshot: dict[str, Any]) -> str:
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


class PrecheckService:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock
        self._runs = AgentRunRepository(session)
        self._events = AgentRunEventRepository(session)
        self._services = ServiceRepository(session)
        self._environments = EnvironmentRepository(session)
        self._connections = ServiceEnvironmentRepository(session)
        self._deployments = DeploymentRepository(session)
        self._variables = ServiceEnvironmentVariableRepository(session)
        self._findings = FindingRepository(session)
        self._audit = AuditRecorder(session, clock)

    async def create_precheck(
        self,
        organization_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        service_id: uuid.UUID,
        target_environment_id: uuid.UUID,
    ) -> PrecheckView:
        """QUEUED 실행을 만든다. 거절 순서: 서비스·환경 → 삭제 중 → 출발 → 같은 환경 → 열린 실행."""
        now = self._clock.now()
        async with self._session.begin():
            service = await self._services.get_live_by_id(organization_id, service_id, lock=True)
            target = await self._environments.get_live_by_id(organization_id, target_environment_id)
            if service.status == ServiceStatus.DELETING:
                raise ServiceDeletingError("service is being deleted", service_id=str(service.id))
            connections = await self._connections.search_live_by_service(service.id)
            source = single_source(connections)
            if source.environment_id == target.id:
                raise RequestFieldInvalidError("targetEnvironmentId", SAME_ENVIRONMENT_REASON)
            await expire_overdue_runs(self._runs, self._audit, now, service_id=service.id)
            open_run = await self._runs.find_open_for_service(service.id)
            if open_run is not None:
                raise PrecheckInProgressError(str(open_run.id))
            target_connection = next(
                (item for item in connections if item.environment_id == target.id), None
            )
            snapshot = await self._build_input_snapshot(
                service, source, target, target_connection, now
            )
            run = AgentRun(
                organization_id=organization_id,
                service_id=service.id,
                kind=AgentRunKind.PRECHECK,
                source_environment_id=source.environment_id,
                target_environment_id=target.id,
                input_snapshot=snapshot,
                input_hash=compute_input_hash(snapshot),
                requested_by_user_id=actor_user_id,
                deadline_at=now + RUN_DEADLINE,
                created_at=now,
                updated_at=now,
            )
            await self._insert(run)
            self._record_user_action(AuditAction.PRECHECK_REQUESTED, run, actor_user_id)
            view = await self._view(run)
        _log_run("precheck requested", "create_precheck", run)
        return view

    async def get_precheck(
        self, organization_id: uuid.UUID, precheck_id: uuid.UUID
    ) -> PrecheckView:
        async with self._session.begin():
            return await self._view(await self._get(organization_id, precheck_id))

    async def search_logs(
        self, organization_id: uuid.UUID, precheck_id: uuid.UUID, after: int, size: int
    ) -> PrecheckLogPage:
        async with self._session.begin():
            run = await self._get(organization_id, precheck_id)
            items = await self._events.search_after(run.id, after, size)
        return PrecheckLogPage(items, items[-1].seq if items else after)

    async def cancel_precheck(
        self, organization_id: uuid.UUID, precheck_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> PrecheckView:
        """열린 실행을 취소한다. 임대·마감이 지났으면 TIMED_OUT 을 저장하고 409."""
        now = self._clock.now()
        async with self._session.begin():
            run = await self._get(organization_id, precheck_id)
            expired = await expire_overdue_runs(self._runs, self._audit, now, run_id=run.id)
            view = None
            if not expired:
                if run.status not in OPEN_STATUSES or not await self._runs.cancel(run.id, now):
                    raise RunNotActiveError(str(run.id))
                self._record_user_action(AuditAction.PRECHECK_CANCELLED, run, actor_user_id)
                view = await self._view(await self._get(organization_id, run.id))
        if view is None:
            raise RunNotActiveError(str(precheck_id))
        _log_run("precheck cancelled", "cancel_precheck", view.run)
        return view

    async def _get(self, organization_id: uuid.UUID, precheck_id: uuid.UUID) -> AgentRun:
        run = await self._runs.find_precheck(organization_id, precheck_id)
        if run is None:
            raise PrecheckNotFoundError(str(precheck_id))
        return run

    async def _insert(self, run: AgentRun) -> None:
        """부분 unique 위반(동시 생성)이면 먼저 들어간 실행을 담아 409."""
        try:
            async with self._session.begin_nested():
                self._runs.add(run)
                await self._session.flush()
        except IntegrityError as exc:
            if violated_constraint_name(exc) != OPEN_RUN_UNIQUE_INDEX:
                raise
            existing = await self._runs.find_open_for_service(run.service_id)
            raise PrecheckInProgressError(str(existing.id) if existing else None) from None

    async def _view(self, run: AgentRun) -> PrecheckView:
        return PrecheckView(run, await self._steps(run.id))

    async def _steps(self, run_id: uuid.UUID) -> list[PrecheckStep]:
        steps: dict[AgentRunStep, PrecheckStep] = {}
        for row in await self._events.search_step_summaries(run_id):
            seen = steps.get(row.step)
            steps[row.step] = (
                PrecheckStep(row.step, row.first_at, row.last_at, row.level)
                if seen is None
                else PrecheckStep(
                    row.step,
                    min(seen.first_at, row.first_at),
                    max(seen.last_at, row.last_at),
                    max(seen.level, row.level, key=_LEVEL_RANK.__getitem__),
                )
            )
        return sorted(steps.values(), key=lambda step: (step.first_at, step.last_at, step.name))

    async def _build_input_snapshot(
        self,
        service: Service,
        source: ServiceEnvironment,
        target: Environment,
        target_connection: ServiceEnvironment | None,
        now: datetime,
    ) -> dict[str, Any]:
        source_side = await self._snapshot_side(source.environment, source)
        target_side = await self._snapshot_side(target, target_connection)
        digests = [
            digest
            for digest in (source_side["imageDigest"], target_side["imageDigest"])
            if digest is not None
        ]
        findings = await self._findings.search_recent_for_precheck(
            service.organization_id,
            service.id,
            [source.environment_id, target.id],
            digests,
            now,
            INPUT_FINDING_LIMIT,
        )
        snapshot = {
            "schemaVersion": INPUT_SCHEMA_VERSION,
            "service": {
                "id": str(service.id),
                "name": service.name,
                "containerPort": service.container_port,
                "healthCheckPath": service.health_check_path,
            },
            "source": source_side,
            "target": target_side,
            "findings": [
                {
                    "controlId": finding.control_id,
                    "result": finding.result,
                    "source": finding.source,
                    "environmentId": (
                        str(finding.environment_id) if finding.environment_id else None
                    ),
                    "collectedAt": finding.collected_at.isoformat(),
                }
                for finding in findings
            ],
        }
        masked: dict[str, Any] = mask_secrets_in_json(snapshot)
        return masked

    async def _snapshot_side(
        self, environment: Environment, connection: ServiceEnvironment | None
    ) -> dict[str, Any]:
        image_digest = None
        variable_keys: list[str] = []
        if connection is not None:
            if connection.current_deployment_id is not None:
                deployment = await self._deployments.find_by_id(connection.current_deployment_id)
                image_digest = deployment.image_digest if deployment is not None else None
            variables = await self._variables.search_live_by_connection(connection.id)
            variable_keys = sorted(variable.key for variable in variables)
        return {
            "environmentId": str(environment.id),
            "kind": environment.kind,
            "config": _snapshot_config(environment),
            "imageDigest": image_digest,
            "variableKeys": variable_keys,
        }

    def _record_user_action(
        self, action: AuditAction, run: AgentRun, actor_user_id: uuid.UUID
    ) -> None:
        self._audit.record(
            organization_id=run.organization_id,
            actor=AuditActor.user(actor_user_id),
            action=action,
            target=run_target(run),
            details={
                "serviceId": str(run.service_id),
                "targetEnvironmentId": str(run.target_environment_id),
            },
        )


def _snapshot_config(environment: Environment) -> dict[str, Any]:
    """API 와 같은 camelCase 설정. 지금 양식으로 읽지 못하면 비운다. 비밀 참조는 싣지 않는다."""
    try:
        config = environment.parsed_config().model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
    except (InvalidEnvironmentConfigError, UnknownEnvironmentKindError):
        return {}
    return {key: value for key, value in config.items() if key not in _SECRET_REF_KEYS}


def _log_run(message: str, action: str, run: AgentRun) -> None:
    logger.info(
        message,
        extra={
            "action": action,
            "run_id": str(run.id),
            "organization_id": str(run.organization_id),
            "service_id": str(run.service_id),
        },
    )
