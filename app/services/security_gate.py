"""이전 승인·실행 시작의 보안 게이트.

공통 계획/연결/예외는 값으로 읽고, 통제별 읽기·실행 검사를 독립 트랜잭션으로 수행한다.
규칙표에 실행 예산이 있는 통제는 조회·변환·검사 전체에 적용한다. 초과 시 해당 읽기를
취소·롤백하고 CONTROL_TIMEOUT/UNKNOWN으로 판정한다. 일부 결과·옛 PASS는 재사용하지 않는다.
TLS 네트워크는 DB 트랜잭션 밖에서 실행한다. 새 증거 저장·계획 상태 변경은 별도 경계다.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.tls_probe import PublicHostTlsChecker, SocketTlsProbeClient, SystemHostResolver
from app.core.clock import Clock
from app.core.config import Settings
from app.core.exceptions import ErrorField, MigrationPlanNotFoundError, SecurityGateBlockedError
from app.enums import (
    MigrationPlanMode,
    SecurityCheckPhase,
    SecurityControlId,
    SecurityFindingMode,
    SecurityFindingSource,
    SecurityReasonCode,
    SecurityResult,
)
from app.models import AgentRun
from app.repositories.agent_run_repository import AgentRunRepository
from app.repositories.deployment_repository import DeploymentRepository
from app.repositories.finding_repository import FindingRepository
from app.repositories.migration_plan_repository import MigrationPlanRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.security_exception_repository import SecurityExceptionRepository
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.repositories.service_environment_variable_repository import (
    ServiceEnvironmentVariableRepository,
)
from app.repositories.service_repository import ServiceRepository
from app.security_environment.revision import current_revision
from app.services.security_exception_service import to_security_exception
from app.services.security_finding_service import SecurityFindingService, to_security_finding
from app.services.security_normalizers import (
    NormalizedFinding,
    normalize_image_match,
    normalize_secret_delivery,
    normalize_tls_probes,
)
from app.services.security_rules import (
    EXCEPTION_MAX_DURATION,
    POLICY_VERSION,
    RULES,
    SecurityDecision,
    SecurityException,
    SecurityFinding,
    SecurityScope,
    SecurityTarget,
    evaluate_security,
    get_rule,
    is_evaluated_in,
)

logger = logging.getLogger(__name__)

# 게이트가 판정마다 새로 확인하는 통제. 저장된 옛 증거 대신 방금 만든 증거만 쓴다.
RECHECKED_CONTROLS = frozenset({SecurityControlId.IMG_3, SecurityControlId.TLS_1})
# 출처까지 같을 때만 새로 확인하는 통제(C-SEC-1 은 WAS 상태만 다시 계산, 사람 점검 기록은 그대로).
RECHECKED_SOURCES = frozenset({(SecurityControlId.SEC_1, SecurityFindingSource.WAS_STATE)})

# 로그 줄 data 키(camelCase, 다른 이전 로그 data 와 같은 표기).
LOG_POLICY_VERSION = "policyVersion"
LOG_PHASE = "phase"
LOG_BLOCKING = "blocking"
LOG_CONTROL_ID = "controlId"
LOG_REASONS = "reasons"
LOG_FINDING_IDS = "findingIds"
LOG_EXCEPTION_IDS = "exceptionIds"


def build_public_tls_checker(settings: Settings) -> PublicHostTlsChecker | None:
    """게이트가 켜져 있을 때만 실행 직전 TLS 시험기를 만든다(꺼져 있으면 None = 게이트 없음)."""
    if not settings.security_gate_enabled:
        return None
    budget = get_rule(SecurityControlId.TLS_1).time_budget
    timeout_seconds = budget.total_seconds() if budget is not None else 5.0
    return PublicHostTlsChecker(
        SocketTlsProbeClient(timeout_seconds=timeout_seconds),
        SystemHostResolver(),
        settings.public_base_domain,
        timeout_seconds=timeout_seconds,
    )


@dataclass(frozen=True)
class _Snapshot:
    """취소·롤백으로 ORM이 만료되어도 쓸 수 있는 계획 값."""

    plan_id: uuid.UUID
    plan_mode: MigrationPlanMode
    organization_id: uuid.UUID
    allow_self_approval: bool
    service_id: uuid.UUID
    source_connection_id: uuid.UUID | None
    target_connection_id: uuid.UUID | None
    source_environment_id: uuid.UUID | None
    target_environment_id: uuid.UUID | None
    target_environment_kind: str
    source_deployment_id: uuid.UUID | None
    target_deployment_id: uuid.UUID | None
    hosts: tuple[str, ...]
    environment_revision: str | None = None
    exceptions: list[SecurityException] = field(default_factory=list)

    @property
    def target_scope(self) -> SecurityScope:
        return SecurityScope(
            self.organization_id,
            service_id=self.service_id,
            environment_id=self.target_environment_id,
        )


@dataclass(frozen=True)
class _ControlCheck:
    findings: list[SecurityFinding] = field(default_factory=list)
    fresh: list[NormalizedFinding] = field(default_factory=list)
    target_digest: str | None = None


class SecurityGate:
    def __init__(
        self,
        session: AsyncSession,
        clock: Clock,
        tls_checker: PublicHostTlsChecker,
        was_version: str | None,
    ) -> None:
        self._session = session
        self._clock = clock
        self._tls_checker = tls_checker
        self._was_version = was_version
        self._findings = SecurityFindingService(session)

    async def evaluate_plan(self, plan_id: uuid.UUID) -> SecurityDecision:
        """계획 하나의 실행 판정. 열린 트랜잭션 밖에서 부른다."""
        snapshot = await self._read_snapshot(plan_id)
        findings: list[SecurityFinding] = []
        fresh: list[NormalizedFinding] = []
        timed_out: set[SecurityControlId] = set()
        target_digest: str | None = None

        # 이미지 범위에 필요한 digest를 먼저 읽는다. 같은 세션의 DB 작업은 병렬로 돌리지 않는다.
        controls = [
            SecurityControlId.IMG_3,
            *(
                rule.control_id
                for rule in RULES
                if rule.control_id != SecurityControlId.IMG_3
                and is_evaluated_in(rule, SecurityCheckPhase.EXECUTION)
            ),
        ]
        for control in controls:
            check = await self._check_with_budget(control, snapshot)
            if check is None:
                timed_out.add(control)
                continue
            findings.extend(check.findings)
            fresh.extend(check.fresh)
            findings.extend(item.finding for item in check.fresh)
            if control == SecurityControlId.IMG_3:
                target_digest = check.target_digest

        await self._findings.record_all(fresh, source_ref=f"migration-plan:{plan_id}")
        target = SecurityTarget(
            scope=SecurityScope(
                snapshot.organization_id,
                service_id=snapshot.service_id,
                environment_id=snapshot.target_environment_id,
                image_digest=target_digest,
                was_version=self._was_version,
            ),
            environment_kind=snapshot.target_environment_kind,
            environment_revision=snapshot.environment_revision,
            allow_self_approval=snapshot.allow_self_approval,
        )
        decision = evaluate_security(
            target,
            SecurityCheckPhase.EXECUTION,
            findings,
            snapshot.exceptions,
            self._clock.now(),
            plan_mode=snapshot.plan_mode,
            timed_out_controls=frozenset(timed_out),
        )
        logger.info(
            "security gate evaluated",
            extra={
                "action": "evaluate_security_gate",
                "migration_plan_id": str(plan_id),
                "is_blocked": decision.is_blocked,
                "policy_version": decision.policy_version,
                "blocking_control_ids": [r.control_id.value for r in decision.blocking_reasons],
                "timed_out_control_ids": sorted(control.value for control in timed_out),
            },
        )
        return decision

    async def _check_with_budget(
        self, control: SecurityControlId, snapshot: _Snapshot
    ) -> _ControlCheck | None:
        budget = get_rule(control).time_budget
        if budget is None:
            return await self._check_control(control, snapshot)
        deadline = asyncio.timeout(budget.total_seconds())
        try:
            async with deadline:
                result = await self._check_control(control, snapshot)
            # 동기 변환 때문에 취소 콜백이 늦어져도 예산을 넘긴 결과는 채택하지 않는다.
            ends_at = deadline.when()
            if ends_at is not None and asyncio.get_running_loop().time() >= ends_at:
                return None
            return result
        except TimeoutError:
            if not deadline.expired():
                raise
            return None

    async def _check_control(
        self, control: SecurityControlId, snapshot: _Snapshot
    ) -> _ControlCheck:
        if control == SecurityControlId.TLS_1:
            probes = await self._tls_checker.check_hosts(snapshot.hosts)
            return _ControlCheck(
                fresh=[
                    normalize_tls_probes(
                        probes, scope=snapshot.target_scope, collected_at=self._clock.now()
                    )
                ]
            )

        # 타임아웃은 이 블록 바깥에서 처리하여 취소된 DB 트랜잭션을 먼저 롤백한다.
        async with self._session.begin():
            now = self._clock.now()
            if control == SecurityControlId.IMG_3:
                deployments = DeploymentRepository(self._session)
                source = (
                    await deployments.find_by_id(snapshot.source_deployment_id)
                    if snapshot.source_deployment_id is not None
                    else None
                )
                target = (
                    await deployments.find_by_id(snapshot.target_deployment_id)
                    if snapshot.target_deployment_id is not None
                    else None
                )
                target_digest = target.image_digest if target is not None else None
                return _ControlCheck(
                    fresh=[
                        normalize_image_match(
                            source.image_digest if source is not None else None,
                            target_digest,
                            scope=SecurityScope(
                                snapshot.organization_id, service_id=snapshot.service_id
                            ),
                            collected_at=now,
                        )
                    ],
                    target_digest=target_digest,
                )

            # 다시 계산할 옛 상태 증거는 읽지 않는다. 상한으로 유효 FAIL을 버리지 않는다.
            models = await FindingRepository(self._session).search_unexpired_by_organization(
                snapshot.organization_id,
                now,
                control_id=control,
                excluded_sources=(
                    (SecurityFindingSource.WAS_STATE,) if control == SecurityControlId.SEC_1 else ()
                ),
            )
            findings = [to_security_finding(model) for model in models]
            if control == SecurityControlId.SEC_1:
                keys = await ServiceEnvironmentVariableRepository(
                    self._session
                ).search_live_keys_by_connections(
                    connection_id
                    for connection_id in (
                        snapshot.source_connection_id,
                        snapshot.target_connection_id,
                    )
                    if connection_id is not None
                )
                return _ControlCheck(
                    findings=findings,
                    fresh=[
                        normalize_secret_delivery(
                            keys.get(snapshot.source_connection_id, frozenset())
                            if snapshot.source_connection_id is not None
                            else frozenset(),
                            keys.get(snapshot.target_connection_id, frozenset())
                            if snapshot.target_connection_id is not None
                            else frozenset(),
                            scope=snapshot.target_scope,
                            collected_at=now,
                        )
                    ],
                )
            if (
                control == SecurityControlId.AI_1
                and snapshot.source_environment_id is not None
                and snapshot.target_environment_id is not None
            ):
                precheck = await AgentRunRepository(self._session).find_latest_succeeded_precheck(
                    snapshot.service_id,
                    snapshot.target_environment_id,
                    now - get_rule(control).max_age,
                    organization_id=snapshot.organization_id,
                    source_environment_id=snapshot.source_environment_id,
                )
                if precheck is not None and precheck.finished_at is not None:
                    findings.append(
                        _precheck_finding(precheck, precheck.finished_at, snapshot.organization_id)
                    )
            return _ControlCheck(findings=findings)

    async def _read_snapshot(self, plan_id: uuid.UUID) -> _Snapshot:
        async with self._session.begin():
            plan = await MigrationPlanRepository(self._session).find_by_id(plan_id)
            if plan is None:
                raise MigrationPlanNotFoundError("migration plan not found", plan_id=str(plan_id))
            service = await ServiceRepository(self._session).find_by_id(plan.service_id)
            if service is None:
                raise MigrationPlanNotFoundError("migration plan not found", plan_id=str(plan_id))
            organization = await OrganizationRepository(self._session).find_by_id(
                service.organization_id
            )
            connections = ServiceEnvironmentRepository(self._session)
            source = await connections.find_by_id(plan.source_service_environment_id)
            target = await connections.find_by_id(plan.target_service_environment_id)
            exception_models = await SecurityExceptionRepository(
                self._session
            ).search_for_judgement(
                service.organization_id, self._clock.now() - EXCEPTION_MAX_DURATION
            )
            return _Snapshot(
                plan_id=plan.id,
                plan_mode=MigrationPlanMode(plan.mode),
                organization_id=service.organization_id,
                allow_self_approval=(
                    organization.allow_self_approval if organization is not None else False
                ),
                service_id=service.id,
                target_environment_id=target.environment_id if target is not None else None,
                target_environment_kind=target.environment.kind if target is not None else "",
                environment_revision=(
                    current_revision(
                        service.organization_id,
                        target.environment_id,
                        target.environment.config,
                        target.environment.secret_ref,
                    )
                    if target is not None
                    else None
                ),
                source_connection_id=source.id if source is not None else None,
                target_connection_id=target.id if target is not None else None,
                source_environment_id=source.environment_id if source is not None else None,
                source_deployment_id=source.current_deployment_id if source is not None else None,
                target_deployment_id=target.current_deployment_id if target is not None else None,
                hosts=tuple(
                    host
                    for host in (service.public_host, target.public_host if target else None)
                    if host
                ),
                exceptions=[to_security_exception(model) for model in exception_models],
            )


def is_rechecked(finding: SecurityFinding) -> bool:
    """판정 때 새로 계산하는 증거인가(저장된 옛 것은 판정에 넣지 않는다)."""
    return (
        finding.control_id in RECHECKED_CONTROLS
        or (finding.control_id, finding.source) in RECHECKED_SOURCES
    )


def _precheck_finding(
    run: AgentRun, finished_at: datetime, organization_id: uuid.UUID
) -> SecurityFinding:
    """성공으로 끝난 AI 사전 점검 실행 → C-AI-1 증거(점검이 그 대상에 대해 끝났다는 WAS 기록)."""
    rule = get_rule(SecurityControlId.AI_1)
    return SecurityFinding(
        finding_id=run.id,
        control_id=SecurityControlId.AI_1,
        result=SecurityResult.PASS,
        mode=SecurityFindingMode.SIMULATED if run.simulated else SecurityFindingMode.REAL,
        source=SecurityFindingSource.PRECHECK,
        scope=SecurityScope(
            organization_id,
            service_id=run.service_id,
            environment_id=run.target_environment_id,
        ),
        collected_at=finished_at,
        expires_at=finished_at + rule.max_age,
        policy_version=POLICY_VERSION,
    )


def gate_log_data(decision: SecurityDecision) -> dict[str, Any]:
    """실행 로그 줄 data: 정책 버전·판정 시점·막은 통제와 사유·쓴 증거·예외 ID(값·원문 없음)."""
    return {
        LOG_POLICY_VERSION: decision.policy_version,
        LOG_PHASE: decision.phase.value,
        LOG_BLOCKING: [
            {
                LOG_CONTROL_ID: reason.control_id.value,
                LOG_REASONS: [code.value for code in reason.reasons],
            }
            for reason in decision.blocking_reasons
        ],
        LOG_FINDING_IDS: [str(finding_id) for finding_id in decision.finding_ids],
        LOG_EXCEPTION_IDS: [str(exception_id) for exception_id in decision.exception_ids],
        "timedOutControlIds": [
            control.control_id.value
            for control in decision.controls
            if SecurityReasonCode.CONTROL_TIMEOUT in control.reasons
        ],
    }


def blocked_error(decision: SecurityDecision) -> SecurityGateBlockedError:
    """409 SECURITY_GATE_BLOCKED. details 한 칸 = 막은 통제 하나의 사유 하나."""
    return SecurityGateBlockedError(
        "a required security control failed or could not be confirmed",
        details=[
            ErrorField(field=reason.control_id.value, reason=code.value)
            for reason in decision.blocking_reasons
            for code in reason.reasons
        ],
    )
