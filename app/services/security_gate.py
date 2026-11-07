"""보안 게이트(보안·거버넌스 5): 이전 승인 때와 일꾼 실행 시작 때 같은 판정을 한다.

판정 한 번의 순서. 네트워크(TLS 접속 시험)는 트랜잭션 밖에서 한다.
1. 짧은 읽기 트랜잭션: 계획·서비스·조직·출발/도착 연결·두 현재 배포, 조직의 유효한 증거,
   기한부 예외(승인됐고 철회되지 않은 것), 24시간 안의 AI 사전 점검.
2. 트랜잭션 밖: 실행 직전 증거를 만든다.
   - C-IMG-3: 두 현재 배포의 digest 비교(WAS 상태).
   - C-SEC-1: 출발 연결의 앱 환경 변수 키가 도착 연결에 모두 있는가(WAS 상태, 키만. 1번에서 읽음).
   - C-TLS-1: 대표 주소·도착 환경 주소 TLS 접속 시험(Echo 가 만든 공개 주소만, 통제 시간 예산 안).
3. 새 증거를 findings 에 남기고(짧은 쓰기 트랜잭션) 규칙표로 실행(EXECUTION) 판정을 한다.
   C-IMG-3·C-TLS-1·C-SEC-1(WAS 상태) 은 방금 만든 증거만 쓴다(이전 판정의 결과가 남아 다시 막지
   않게). C-SEC-1 의 사람 점검 기록은 저장된 것을 그대로 쓴다.

이 모듈은 계획 상태를 바꾸지 않는다. 부르는 쪽(승인·일꾼 시작)이 서비스를 잠근 트랜잭션 안에서
판정을 실행 로그 줄(`gate_log_data`)로 남기고, 막혔으면 승인은 409, 실행 시작은 FAILED 로 끝낸다.
계획을 만든 뒤 바뀐 상태(현재 배포·환경 설정)는 지문이 잡는다.

- C-AI-1 은 같은 조직·서비스·출발·도착의 24시간 안에 성공한 실제 사전 점검 중 최신 실행을
  쓴다(증거 ID = 실행 ID). 실제 실행이 없을 때 모의 실행은 차단 사유 설명에만 쓴다.
- 게이트 설정이 꺼져 있으면 이 모듈을 만들지 않는다(판정·기록·네트워크 없음).
"""

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
from app.services.security_exception_service import to_security_exception
from app.services.security_finding_service import SecurityFindingService, to_security_finding
from app.services.security_normalizers import (
    normalize_image_match,
    normalize_secret_delivery,
    normalize_tls_probes,
)
from app.services.security_rules import (
    EXCEPTION_MAX_DURATION,
    POLICY_VERSION,
    SecurityDecision,
    SecurityException,
    SecurityFinding,
    SecurityScope,
    SecurityTarget,
    evaluate_security,
    get_rule,
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
    """판정에 필요한 값(읽기 트랜잭션에서 한 번에 읽은 것)."""

    plan_id: uuid.UUID
    plan_mode: MigrationPlanMode
    organization_id: uuid.UUID
    allow_self_approval: bool
    service_id: uuid.UUID
    target_environment_id: uuid.UUID | None
    target_environment_kind: str
    source_digest: str | None
    target_digest: str | None
    # 출발·도착 연결의 살아 있는 앱 환경 변수 키(값 없음).
    source_secret_keys: frozenset[str]
    target_secret_keys: frozenset[str]
    hosts: tuple[str, ...]
    findings: list[SecurityFinding] = field(default_factory=list)
    exceptions: list[SecurityException] = field(default_factory=list)


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
        """계획 하나의 실행 판정. 열린 트랜잭션 밖에서 부른다(안에서 트랜잭션을 연다)."""
        snapshot = await self._read_snapshot(plan_id)
        probes = await self._tls_checker.check_hosts(snapshot.hosts)
        now = self._clock.now()
        image_match = normalize_image_match(
            snapshot.source_digest,
            snapshot.target_digest,
            scope=SecurityScope(snapshot.organization_id, service_id=snapshot.service_id),
            collected_at=now,
        )
        target_scope = SecurityScope(
            snapshot.organization_id,
            service_id=snapshot.service_id,
            environment_id=snapshot.target_environment_id,
        )
        tls = normalize_tls_probes(probes, scope=target_scope, collected_at=now)
        secret_delivery = normalize_secret_delivery(
            snapshot.source_secret_keys,
            snapshot.target_secret_keys,
            scope=target_scope,
            collected_at=now,
        )
        await self._findings.record_all(
            [image_match, tls, secret_delivery], source_ref=f"migration-plan:{plan_id}"
        )
        target = SecurityTarget(
            scope=SecurityScope(
                snapshot.organization_id,
                service_id=snapshot.service_id,
                environment_id=snapshot.target_environment_id,
                image_digest=snapshot.target_digest,
                was_version=self._was_version,
            ),
            environment_kind=snapshot.target_environment_kind,
            allow_self_approval=snapshot.allow_self_approval,
        )
        decision = evaluate_security(
            target,
            SecurityCheckPhase.EXECUTION,
            [
                *(f for f in snapshot.findings if not is_rechecked(f)),
                image_match.finding,
                tls.finding,
                secret_delivery.finding,
            ],
            snapshot.exceptions,
            now,
            plan_mode=snapshot.plan_mode,
        )
        logger.info(
            "security gate evaluated",
            extra={
                "action": "evaluate_security_gate",
                "migration_plan_id": str(plan_id),
                "is_blocked": decision.is_blocked,
                "policy_version": decision.policy_version,
                "blocking_control_ids": [r.control_id.value for r in decision.blocking_reasons],
            },
        )
        return decision

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
            deployments = DeploymentRepository(self._session)
            source_deployment = (
                await deployments.find_by_id(source.current_deployment_id)
                if source is not None and source.current_deployment_id is not None
                else None
            )
            target_deployment = (
                await deployments.find_by_id(target.current_deployment_id)
                if target is not None and target.current_deployment_id is not None
                else None
            )
            secret_keys = await ServiceEnvironmentVariableRepository(
                self._session
            ).search_live_keys_by_connections(
                connection.id for connection in (source, target) if connection is not None
            )
            now = self._clock.now()
            finding_models = await FindingRepository(
                self._session
            ).search_unexpired_by_organization(service.organization_id, now)
            exception_models = await SecurityExceptionRepository(
                self._session
            ).search_for_judgement(service.organization_id, now - EXCEPTION_MAX_DURATION)
            precheck = (
                await AgentRunRepository(self._session).find_latest_succeeded_precheck(
                    service.id,
                    target.environment_id,
                    now - get_rule(SecurityControlId.AI_1).max_age,
                    organization_id=service.organization_id,
                    source_environment_id=source.environment_id,
                )
                if source is not None and target is not None
                else None
            )
            findings = [to_security_finding(model) for model in finding_models]
            if precheck is not None and precheck.finished_at is not None:
                findings.append(
                    _precheck_finding(precheck, precheck.finished_at, service.organization_id)
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
                source_digest=source_deployment.image_digest if source_deployment else None,
                target_digest=target_deployment.image_digest if target_deployment else None,
                source_secret_keys=(
                    secret_keys.get(source.id, frozenset()) if source is not None else frozenset()
                ),
                target_secret_keys=(
                    secret_keys.get(target.id, frozenset()) if target is not None else frozenset()
                ),
                hosts=tuple(
                    host
                    for host in (service.public_host, target.public_host if target else None)
                    if host
                ),
                findings=findings,
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
