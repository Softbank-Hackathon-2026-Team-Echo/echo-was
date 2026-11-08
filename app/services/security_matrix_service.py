"""보안 매트릭스·증거 조회(보안·거버넌스 4).

- 행 = 조직의 살아 있는 환경, 칸 = 권한·암호화·감사 기록·정책 4개. 준비 시점(PREPARATION) 판정이다.
- 환경 단위 통제(범위가 환경·WAS 버전·없음)는 환경마다 한 번 판정한다.
- 서비스 단위 통제(범위에 서비스·이미지·설정 커밋)는 그 환경에 현재 배포가 있는 서비스마다
  지금 도는 이미지 digest 로 판정한다. 서비스가 없는 환경은 이 통제를 세지 않는다.
- 칸 결과는 그 칸 통제들의 **예외 반영 전 증거 값** 중 가장 나쁜 값이다(규칙표 `summarize_cell`).
  그 칸에 셀 통제가 없으면 NA.
- 미수집·만료·범위 불일치는 PASS 가 아니라 UNKNOWN 이다(빈칸 없음).
- 예외는 게이트와 같은 입력(승인·미철회 예외, 서버 WAS 버전, 조직 자기 승인 스위치)으로 규칙표
  `evaluate_control` 이 판정한다. 게이트가 막는지는 `gate_status`(BLOCKED·WAIVED·PASS)로 따로 준다.
- 실행 때만 보는 통제(C-DB-1·C-DST-1·C-AI-1·C-IMG-3)는 줄이 없어 그 예외도 나타나지 않는다.
- C-SEC-1(비밀 전달 경로)은 조회 때 WAS 상태로 계산한다: 서비스마다 서비스 중 연결(비율이 가장 큰
  연결)을 기준으로, 각 연결에 기준의 앱 환경 변수 키가 모두 있는가(키만, 값은 읽지 않는다). 대기
  환경에 비밀이 빠졌는지 이전 전에 보이게 하려는 것이다. 이 증거는 저장하지 않아 증거 목록에 없고,
  저장된 WAS 상태 C-SEC-1(게이트 기록)은 쓰지 않는다. 사람 점검 기록은 그대로 쓴다.
- 이전 전후 비교(보안 5-2)는 `get_rows` 로 고른 환경의 행을 같은 계산으로 받는다.
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.enums import (
    SecurityCheckPhase,
    SecurityControlId,
    SecurityFindingMode,
    SecurityFindingSource,
    SecurityGateStatus,
    SecurityMatrixCell,
    SecurityReasonCode,
    SecurityResult,
)
from app.models import Environment, Finding, SecurityException, ServiceEnvironment, User
from app.repositories.environment_repository import EnvironmentRepository
from app.repositories.finding_repository import FindingConditions, FindingRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.security_exception_repository import SecurityExceptionRepository
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.repositories.service_environment_variable_repository import (
    ServiceEnvironmentVariableRepository,
)
from app.repositories.service_repository import ServiceRepository
from app.repositories.user_repository import UserRepository
from app.security_environment.revision import current_revision
from app.services.security_exception_service import to_security_exception
from app.services.security_finding_service import to_security_finding
from app.services.security_normalizers import normalize_secret_delivery
from app.services.security_rules import (
    EXCEPTION_MAX_DURATION,
    POLICY_VERSION,
    RULES,
    ScopeKey,
    SecurityControlDecision,
    SecurityFinding,
    SecurityScope,
    SecurityTarget,
    evaluate_control,
    get_rule,
    is_evaluated_in,
    summarize_cell,
)
from app.services.security_rules import SecurityException as SecurityExceptionInput

MATRIX_PHASE = SecurityCheckPhase.PREPARATION
CELL_ORDER = (
    SecurityMatrixCell.IAM,
    SecurityMatrixCell.ENCRYPTION,
    SecurityMatrixCell.AUDIT_LOG,
    SecurityMatrixCell.POLICY,
)
# 이 범위 항목이 있는 통제는 서비스마다 판정한다.
SERVICE_SCOPE_KEYS = frozenset({ScopeKey.SERVICE, ScopeKey.IMAGE_DIGEST, ScopeKey.CONFIG_COMMIT})
# 매트릭스 한 번에 읽는 환경 수 상한.
MATRIX_ENVIRONMENT_LIMIT = 200
# 증거 목록에서 id 로 한 번에 고를 수 있는 수.
FINDING_IDS_LIMIT = 100


@dataclass(frozen=True)
class MatrixExceptionView:
    """칸·통제에 적용된 기한부 예외 요약."""

    exception_id: uuid.UUID
    control_id: SecurityControlId
    reason: str
    expires_at: datetime
    # 사용자 행을 못 찾으면 None.
    approved_by: User | None


@dataclass(frozen=True)
class MatrixControlView:
    control_id: SecurityControlId
    title: str
    # 예외 반영 전 증거 값. 미수집은 UNKNOWN. 게이트가 막는지는 gate_status 로 본다.
    result: SecurityResult
    is_mandatory: bool
    reasons: tuple[SecurityReasonCode, ...]
    finding_ids: tuple[uuid.UUID, ...]
    mode: SecurityFindingMode | None
    checked_at: datetime | None
    expires_at: datetime | None
    # 서비스 단위 통제면 그 서비스, 환경 단위면 None.
    service_id: uuid.UUID | None
    gate_status: SecurityGateStatus
    exception: MatrixExceptionView | None


@dataclass(frozen=True)
class MatrixCellView:
    cell: SecurityMatrixCell
    result: SecurityResult
    finding_count: int
    has_exception: bool
    mode: SecurityFindingMode | None
    checked_at: datetime | None
    expires_at: datetime | None
    controls: tuple[MatrixControlView, ...]
    gate_status: SecurityGateStatus
    # 칸에 적용된 예외 중 가장 먼저 만료되는 것.
    exception: MatrixExceptionView | None


@dataclass(frozen=True)
class MatrixServiceView:
    service_id: uuid.UUID
    service_name: str
    image_digest: str


@dataclass(frozen=True)
class MatrixRowView:
    environment_id: uuid.UUID
    environment_name: str
    environment_kind: str
    services: tuple[MatrixServiceView, ...]
    cells: tuple[MatrixCellView, ...]


@dataclass(frozen=True)
class SecurityMatrixView:
    policy_version: str
    phase: SecurityCheckPhase
    checked_at: datetime
    rows: tuple[MatrixRowView, ...]


@dataclass(frozen=True)
class FindingPage:
    items: list[Finding]
    total: int


@dataclass(frozen=True)
class _JudgementInput:
    """환경마다 같은 판정 입력(읽기 트랜잭션에서 한 번에 읽은 것). 게이트와 같은 값이다."""

    organization_id: uuid.UUID
    was_version: str | None
    allow_self_approval: bool
    findings: Sequence[SecurityFinding]
    exceptions: Sequence[SecurityExceptionInput]
    exception_views: dict[uuid.UUID, MatrixExceptionView]
    now: datetime


class SecurityMatrixService:
    def __init__(self, session: AsyncSession, clock: Clock, was_version: str | None) -> None:
        self._session = session
        self._clock = clock
        self._was_version = was_version
        self._environments = EnvironmentRepository(session)
        self._connections = ServiceEnvironmentRepository(session)
        self._findings = FindingRepository(session)
        self._exceptions = SecurityExceptionRepository(session)
        self._organizations = OrganizationRepository(session)
        self._users = UserRepository(session)
        self._services = ServiceRepository(session)
        self._variables = ServiceEnvironmentVariableRepository(session)

    async def get_matrix(self, organization_id: uuid.UUID) -> SecurityMatrixView:
        """조직의 환경 × 4칸. 칸마다 판정 근거(통제·사유·증거 id·모드·시각)를 함께 준다."""
        now = self._clock.now()
        async with self._session.begin():
            environments = await self._environments.search_live(
                organization_id, kind=None, offset=0, limit=MATRIX_ENVIRONMENT_LIMIT
            )
            connections, judgement = await self._read_judgement(organization_id, now)
        rows = _evaluate_rows(environments, connections, judgement)
        return SecurityMatrixView(POLICY_VERSION, MATRIX_PHASE, now, rows)

    async def get_rows(
        self,
        organization_id: uuid.UUID,
        environment_ids: Sequence[uuid.UUID],
        *,
        service_id: uuid.UUID | None = None,
    ) -> SecurityMatrixView:
        """고른 환경들의 매트릭스 행(준 순서대로). 칸 계산은 `get_matrix` 와 같다.

        환경·서비스는 이 조직의 살아 있는 것만(아니면 404). `service_id` 를 주면 서비스 단위 통제는
        그 서비스의 현재 배포만 센다(그 환경에 배포가 없으면 세지 않는다).
        """
        now = self._clock.now()
        async with self._session.begin():
            environments = [
                await self._environments.get_live_by_id(organization_id, environment_id)
                for environment_id in environment_ids
            ]
            if service_id is not None:
                await self._services.get_live_by_id(organization_id, service_id)
            connections, judgement = await self._read_judgement(organization_id, now)
        if service_id is not None:
            connections = [c for c in connections if c.service_id == service_id]
        rows = _evaluate_rows(environments, connections, judgement)
        return SecurityMatrixView(POLICY_VERSION, MATRIX_PHASE, now, rows)

    async def _read_judgement(
        self, organization_id: uuid.UUID, now: datetime
    ) -> tuple[list[ServiceEnvironment], _JudgementInput]:
        """열린 읽기 트랜잭션 안에서: 현재 배포가 있는 연결과 판정 입력(게이트와 같은 값)."""
        connections = await self._connections.search_live_current_by_organization(organization_id)
        models = await self._findings.search_unexpired_by_organization(organization_id, now)
        organization = await self._organizations.find_by_id(organization_id)
        exception_models = await self._exceptions.search_for_judgement(
            organization_id, now - EXCEPTION_MAX_DURATION
        )
        approvers = await self._users.search_by_ids(
            model.approved_by_user_id
            for model in exception_models
            if model.approved_by_user_id is not None
        )
        secret_keys = await self._variables.search_live_keys_by_connections(
            connection.id for connection in connections
        )
        judgement = _JudgementInput(
            organization_id=organization_id,
            was_version=self._was_version,
            allow_self_approval=(
                organization.allow_self_approval if organization is not None else False
            ),
            findings=[
                *(
                    finding
                    for finding in (to_security_finding(model) for model in models)
                    if not _is_live_secret_delivery(finding)
                ),
                *_secret_delivery_findings(organization_id, connections, secret_keys, now),
            ],
            exceptions=[to_security_exception(model) for model in exception_models],
            exception_views=_exception_views(exception_models, approvers),
            now=now,
        )
        return list(connections), judgement

    async def search_findings(
        self,
        organization_id: uuid.UUID,
        *,
        environment_id: uuid.UUID | None,
        service_id: uuid.UUID | None,
        control_id: SecurityControlId | None,
        finding_ids: Sequence[uuid.UUID],
        include_expired: bool,
        page: int,
        size: int,
    ) -> FindingPage:
        """증거 목록(최신순, 조직 안). 기본은 만료되지 않은 것만."""
        conditions = FindingConditions(
            unexpired_at=None if include_expired else self._clock.now(),
            environment_id=environment_id,
            service_id=service_id,
            control_id=control_id.value if control_id is not None else None,
            finding_ids=tuple(finding_ids),
        )
        async with self._session.begin():
            items = await self._findings.search_by_organization(
                organization_id, conditions, offset=page * size, limit=size
            )
            total = await self._findings.count_by_organization(organization_id, conditions)
        return FindingPage(items=items, total=total)


def _is_live_secret_delivery(finding: SecurityFinding) -> bool:
    return (
        finding.control_id == SecurityControlId.SEC_1
        and finding.source == SecurityFindingSource.WAS_STATE
    )


def _secret_delivery_findings(
    organization_id: uuid.UUID,
    connections: Sequence[ServiceEnvironment],
    secret_keys: dict[uuid.UUID, frozenset[str]],
    now: datetime,
) -> list[SecurityFinding]:
    """연결마다 C-SEC-1: 그 서비스의 서비스 중 연결(비율이 가장 큰, 같으면 id 가 작은) 키 기준.

    증거 id 는 (연결, 기준 연결)로 정한다. 저장하지 않는 값이라 조회마다 바뀌지 않게 한다.
    """
    reference_by_service: dict[uuid.UUID, ServiceEnvironment] = {}
    for connection in connections:
        reference = reference_by_service.get(connection.service_id)
        if reference is None or (-connection.traffic_weight, connection.id) < (
            -reference.traffic_weight,
            reference.id,
        ):
            reference_by_service[connection.service_id] = connection
    findings: list[SecurityFinding] = []
    for connection in connections:
        reference = reference_by_service[connection.service_id]
        finding = normalize_secret_delivery(
            secret_keys.get(reference.id, frozenset()),
            secret_keys.get(connection.id, frozenset()),
            scope=SecurityScope(
                organization_id,
                service_id=connection.service_id,
                environment_id=connection.environment_id,
            ),
            collected_at=now,
        ).finding
        finding_id = uuid.uuid5(connection.id, f"{finding.control_id}:{reference.id}")
        findings.append(replace(finding, finding_id=finding_id))
    return findings


def _evaluate_rows(
    environments: Sequence[Environment],
    connections: Sequence[ServiceEnvironment],
    judgement: _JudgementInput,
) -> tuple[MatrixRowView, ...]:
    return tuple(
        _evaluate_row(
            environment,
            [c for c in connections if c.environment_id == environment.id],
            judgement,
        )
        for environment in environments
    )


def _exception_views(
    models: Sequence[SecurityException], approvers: Sequence[User]
) -> dict[uuid.UUID, MatrixExceptionView]:
    approver_by_id = {user.id: user for user in approvers}
    return {
        model.id: MatrixExceptionView(
            exception_id=model.id,
            control_id=SecurityControlId(model.control_id),
            reason=model.reason,
            expires_at=model.expires_at,
            approved_by=(
                approver_by_id.get(model.approved_by_user_id)
                if model.approved_by_user_id is not None
                else None
            ),
        )
        for model in models
    }


def _evaluate_row(
    environment: Environment,
    connections: list[ServiceEnvironment],
    judgement: _JudgementInput,
) -> MatrixRowView:
    environment_target = SecurityTarget(
        scope=SecurityScope(
            organization_id=judgement.organization_id,
            environment_id=environment.id,
            was_version=judgement.was_version,
        ),
        environment_kind=environment.kind,
        environment_revision=current_revision(
            judgement.organization_id, environment.id, environment.config, environment.secret_ref
        ),
        allow_self_approval=judgement.allow_self_approval,
    )
    service_targets: list[tuple[uuid.UUID | None, SecurityTarget]] = [
        (
            connection.service_id,
            SecurityTarget(
                scope=SecurityScope(
                    organization_id=judgement.organization_id,
                    service_id=connection.service_id,
                    environment_id=environment.id,
                    image_digest=_image_digest(connection),
                    was_version=judgement.was_version,
                ),
                environment_kind=environment.kind,
                allow_self_approval=judgement.allow_self_approval,
            ),
        )
        for connection in connections
    ]
    decisions: list[tuple[SecurityControlDecision, uuid.UUID | None]] = []
    for rule in RULES:
        if rule.cell is None or not is_evaluated_in(rule, MATRIX_PHASE):
            continue
        targets = (
            service_targets
            if rule.scope_keys & SERVICE_SCOPE_KEYS
            else [(None, environment_target)]
        )
        for service_id, target in targets:
            decision = evaluate_control(
                rule, target, MATRIX_PHASE, judgement.findings, judgement.exceptions, judgement.now
            )
            decisions.append((decision, service_id))
    return MatrixRowView(
        environment_id=environment.id,
        environment_name=environment.name,
        environment_kind=environment.kind,
        services=tuple(
            MatrixServiceView(c.service_id, c.service.name, _image_digest(c) or "")
            for c in connections
        ),
        cells=tuple(_cell_view(cell, decisions, judgement.exception_views) for cell in CELL_ORDER),
    )


def _cell_view(
    cell: SecurityMatrixCell,
    decisions: list[tuple[SecurityControlDecision, uuid.UUID | None]],
    exception_views: dict[uuid.UUID, MatrixExceptionView],
) -> MatrixCellView:
    in_cell = [
        (decision, service_id) for decision, service_id in decisions if decision.cell == cell
    ]
    # 칸 결과는 예외 반영 전 증거 값이다. 같은 순서 규칙을 쓰려고 증거 값을 넘긴다.
    summary = summarize_cell(
        cell, (replace(decision, effective_result=decision.result) for decision, _ in in_cell)
    )
    controls = tuple(
        MatrixControlView(
            control_id=decision.control_id,
            title=get_rule(decision.control_id).title,
            result=decision.result,
            is_mandatory=decision.is_mandatory,
            reasons=decision.reasons,
            finding_ids=decision.finding_ids,
            mode=decision.mode,
            checked_at=decision.checked_at,
            expires_at=decision.expires_at,
            service_id=service_id,
            gate_status=_control_gate_status(decision),
            exception=(
                exception_views.get(decision.exception_id)
                if decision.exception_id is not None
                else None
            ),
        )
        for decision, service_id in in_cell
    )
    return MatrixCellView(
        cell=cell,
        result=summary.result,
        finding_count=len({fid for decision, _ in in_cell for fid in decision.finding_ids}),
        has_exception=summary.has_exception,
        mode=summary.mode,
        checked_at=summary.checked_at,
        expires_at=summary.expires_at,
        controls=controls,
        gate_status=_cell_gate_status(control.gate_status for control in controls),
        exception=min(
            (control.exception for control in controls if control.exception is not None),
            key=lambda exception: exception.expires_at,
            default=None,
        ),
    )


def build_cell_view(
    cell: SecurityMatrixCell,
    decisions: Iterable[tuple[SecurityControlDecision, uuid.UUID | None]],
    exception_views: dict[uuid.UUID, MatrixExceptionView] | None = None,
) -> MatrixCellView:
    """판정(통제 판정, 서비스 id) → 매트릭스 칸 하나. 행 계산과 같은 함수다(비교 시험이 쓴다)."""
    return _cell_view(cell, list(decisions), exception_views or {})


def _control_gate_status(decision: SecurityControlDecision) -> SecurityGateStatus:
    if decision.is_blocking:
        return SecurityGateStatus.BLOCKED
    if decision.exception_id is not None:
        return SecurityGateStatus.WAIVED
    return SecurityGateStatus.PASS


def _cell_gate_status(statuses: Iterable[SecurityGateStatus]) -> SecurityGateStatus:
    seen = set(statuses)
    if SecurityGateStatus.BLOCKED in seen:
        return SecurityGateStatus.BLOCKED
    if SecurityGateStatus.WAIVED in seen:
        return SecurityGateStatus.WAIVED
    return SecurityGateStatus.PASS


def _image_digest(connection: ServiceEnvironment) -> str | None:
    deployment = connection.current_deployment
    return deployment.image_digest if deployment is not None else None
