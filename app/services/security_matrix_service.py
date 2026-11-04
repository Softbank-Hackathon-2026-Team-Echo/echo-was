"""보안 매트릭스·증거 조회(보안·거버넌스 4).

- 행 = 조직의 살아 있는 환경, 칸 = 권한·암호화·감사 기록·정책 4개. 준비 시점(PREPARATION) 판정이다.
- 환경 단위 통제(범위가 환경·WAS 버전·없음)는 환경마다 한 번 판정한다.
- 서비스 단위 통제(범위에 서비스·이미지·설정 커밋)는 그 환경에 현재 배포가 있는 서비스마다
  지금 도는 이미지 digest 로 판정한다. 서비스가 없는 환경은 이 통제를 세지 않는다.
- 칸 결과는 그 칸 통제들의 가장 나쁜 값이다(규칙표 `summarize_cell`). 그 칸에 셀 통제가 없으면 NA.
- 미수집·만료·범위 불일치는 PASS 가 아니라 UNKNOWN 이다(빈칸 없음).
- 기한부 예외 표는 아직 없어 예외 없이 판정한다.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.enums import (
    SecurityCheckPhase,
    SecurityControlId,
    SecurityFindingMode,
    SecurityMatrixCell,
    SecurityReasonCode,
    SecurityResult,
)
from app.models import Environment, Finding, ServiceEnvironment
from app.repositories.environment_repository import EnvironmentRepository
from app.repositories.finding_repository import FindingConditions, FindingRepository
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.services.security_finding_service import to_security_finding
from app.services.security_rules import (
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
class MatrixControlView:
    control_id: SecurityControlId
    title: str
    # 화면에 보일 값(예외 반영 뒤). 미수집은 UNKNOWN.
    result: SecurityResult
    is_mandatory: bool
    reasons: tuple[SecurityReasonCode, ...]
    finding_ids: tuple[uuid.UUID, ...]
    mode: SecurityFindingMode | None
    checked_at: datetime | None
    expires_at: datetime | None
    # 서비스 단위 통제면 그 서비스, 환경 단위면 None.
    service_id: uuid.UUID | None


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


class SecurityMatrixService:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock
        self._environments = EnvironmentRepository(session)
        self._connections = ServiceEnvironmentRepository(session)
        self._findings = FindingRepository(session)

    async def get_matrix(self, organization_id: uuid.UUID) -> SecurityMatrixView:
        """조직의 환경 × 4칸. 칸마다 판정 근거(통제·사유·증거 id·모드·시각)를 함께 준다."""
        now = self._clock.now()
        async with self._session.begin():
            environments = await self._environments.search_live(
                organization_id, kind=None, offset=0, limit=MATRIX_ENVIRONMENT_LIMIT
            )
            connections = await self._connections.search_live_current_by_organization(
                organization_id
            )
            models = await self._findings.search_unexpired_by_organization(organization_id, now)
        findings = [to_security_finding(model) for model in models]
        rows = tuple(
            _evaluate_row(
                organization_id,
                environment,
                [c for c in connections if c.environment_id == environment.id],
                findings,
                now,
            )
            for environment in environments
        )
        return SecurityMatrixView(POLICY_VERSION, MATRIX_PHASE, now, rows)

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


def _evaluate_row(
    organization_id: uuid.UUID,
    environment: Environment,
    connections: list[ServiceEnvironment],
    findings: Sequence[SecurityFinding],
    now: datetime,
) -> MatrixRowView:
    environment_target = SecurityTarget(
        scope=SecurityScope(organization_id=organization_id, environment_id=environment.id),
        environment_kind=environment.kind,
    )
    service_targets = [
        (
            connection.service_id,
            SecurityTarget(
                scope=SecurityScope(
                    organization_id=organization_id,
                    service_id=connection.service_id,
                    environment_id=environment.id,
                    image_digest=_image_digest(connection),
                ),
                environment_kind=environment.kind,
            ),
        )
        for connection in connections
    ]
    decisions: list[tuple[SecurityControlDecision, uuid.UUID | None]] = []
    for rule in RULES:
        if rule.cell is None or not is_evaluated_in(rule, MATRIX_PHASE):
            continue
        if rule.scope_keys & SERVICE_SCOPE_KEYS:
            for service_id, target in service_targets:
                decision = evaluate_control(rule, target, MATRIX_PHASE, findings, (), now)
                decisions.append((decision, service_id))
        else:
            decision = evaluate_control(rule, environment_target, MATRIX_PHASE, findings, (), now)
            decisions.append((decision, None))
    return MatrixRowView(
        environment_id=environment.id,
        environment_name=environment.name,
        environment_kind=environment.kind,
        services=tuple(
            MatrixServiceView(c.service_id, c.service.name, _image_digest(c) or "")
            for c in connections
        ),
        cells=tuple(_cell_view(cell, decisions) for cell in CELL_ORDER),
    )


def _cell_view(
    cell: SecurityMatrixCell, decisions: list[tuple[SecurityControlDecision, uuid.UUID | None]]
) -> MatrixCellView:
    in_cell = [
        (decision, service_id) for decision, service_id in decisions if decision.cell == cell
    ]
    summary = summarize_cell(cell, (decision for decision, _ in in_cell))
    return MatrixCellView(
        cell=cell,
        result=summary.result,
        finding_count=len({fid for decision, _ in in_cell for fid in decision.finding_ids}),
        has_exception=summary.has_exception,
        mode=summary.mode,
        checked_at=summary.checked_at,
        expires_at=summary.expires_at,
        controls=tuple(
            MatrixControlView(
                control_id=decision.control_id,
                title=get_rule(decision.control_id).title,
                result=decision.effective_result,
                is_mandatory=decision.is_mandatory,
                reasons=decision.reasons,
                finding_ids=decision.finding_ids,
                mode=decision.mode,
                checked_at=decision.checked_at,
                expires_at=decision.expires_at,
                service_id=service_id,
            )
            for decision, service_id in in_cell
        ),
    )


def _image_digest(connection: ServiceEnvironment) -> str | None:
    deployment = connection.current_deployment
    return deployment.image_digest if deployment is not None else None
