"""플랫폼 자기 시험(보안 5-3): C-IAM-2·C-AUD-1 준비 증거를 운영에서 직접 만든다.

보안 게이트가 켜진 일꾼 프로세스가 `SECURITY_PLATFORM_SELF_TEST_INTERVAL_SECONDS`(기본 6시간)마다,
그리고 시작하자마자 한 번 돈다. 살아 있는 환경이 있는 조직마다:

1. **C-IAM-2 에이전트 권한 경계(`WAS_TEST`)** — 그 조직의 일회용 에이전트 토큰(원문은 메모리에만,
   이름 `security-self-test`, 유효 1일)을 발급해 앱의 쓰기 경로 전부(로그아웃·러너 경로 제외,
   승인 경로 3개 포함)를 같은 프로세스 안의 앱(ASGI)으로 호출한다. 모두 403 `AGENT_READ_ONLY` 면
   PASS, 하나라도 다른 응답이면 FAIL. 토큰은 끝나면 바로 폐기한다(발급·폐기는 SYSTEM
   `security-self-test` 감사 줄).
   경로 변수는 임의 uuid 라 검사기가 뚫려도 리소스를 찾지 못한다(본문도 빈 객체).
2. **C-AUD-1 플랫폼 감사 기록(`AUDIT_LOG`)** — (a) 쓰기 경로가 모두 감사 행동 이름표에 있는가
   (`test_every_write_route_has_an_action_name` 과 같은 계산), (b) 최근 24시간에 일어난 승인(이전
   계획·AI 패치·기한부 예외)·게이트 판정(승인된 계획의 `SECURITY_GATE_PASSED` 실행 로그)·AI 실행
   (가져가기·끝·시간 초과)·예외(요청·철회)가 그 실행 ID(대상 id)로 감사 기록에 있는가, (c) 1번의
   거부 요청이 모두 에이전트 토큰 id 와 함께 DENIED 줄로 남았는가. 빠진 것이 없으면 PASS.
   그 시간에 일이 없던 종류는 "0건"으로 보이고 결과를 바꾸지 않는다.

- 증거 범위는 조직 + WAS 버전(`WAS_VERSION`)이다. 환경 칸이 없으므로 매트릭스의 모든 환경 행이 같은
  증거를 쓴다. WAS 버전이 비어 있으면 판정에 쓸 수 없어 돌지 않는다.
- 증거 만료는 수집 + 24시간(규칙 유효기간 7일보다 짧다). 한 번의 FAIL 이 일주일 동안 남지 않게 하고,
  일꾼이 멈추면 하루 뒤 UNKNOWN 으로 돌아가게 하려는 것이다.
- 시험 자체가 실패(예외)하면 증거를 남기지 않고 로그만 남긴다. 다음 주기에 다시 한다.
- 기한부 예외는 건드리지 않는다. 규칙표는 PASS 에 예외를 적용하지 않으므로 증거가 PASS 면 예외 없이
  PASS 로 보이고, 예외는 그대로 만료된다.
"""

import asyncio
import logging
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp

from app.audit.actions import AuditAction, AuditComponent, AuditTargetType
from app.audit.recorder import AuditActor, AuditRecorder, AuditTarget
from app.audit.route_coverage import Route, agent_probe_routes, unnamed_write_routes
from app.core.clock import Clock
from app.core.config import Settings
from app.enums import (
    ActorType,
    AgentPatchDecision,
    AgentRunStatus,
    AuditResult,
    MigrationLogCode,
    SecurityControlId,
    SecurityFindingMode,
    SecurityFindingReason,
    SecurityFindingSource,
    SecurityResult,
)
from app.models import (
    AgentPatchApproval,
    AgentRun,
    AgentToken,
    AuditLog,
    Environment,
    MigrationPlan,
    MigrationPlanLog,
    Organization,
    SecurityException,
    Service,
)
from app.services.agent_token_service import issue_agent_token
from app.services.security_finding_service import SecurityFindingService
from app.services.security_normalizers import FindingItem, NormalizedFinding
from app.services.security_rules import POLICY_VERSION, SecurityFinding, SecurityScope

logger = logging.getLogger(__name__)

SELF_TEST_SOURCE_REF = "self-test:platform"
SELF_TEST_TOKEN_NAME = "security-self-test"
SELF_TEST_TOKEN_DAYS = 1
AGENT_READ_ONLY_CODE = "AGENT_READ_ONLY"
# 감사 기록을 대조하는 최근 시간.
AUDIT_WINDOW = timedelta(hours=24)
# 증거 만료(수집 시각 기준).
EVIDENCE_TTL = timedelta(hours=24)
# 근거 줄 상한(나머지는 total_items 로 센다).
ITEM_LIMIT = 50
# 같은 트랜잭션의 줄이라도 시각이 조금 다를 수 있어 감사 줄을 읽는 범위를 조금 넓힌다.
_AUDIT_SLACK = timedelta(minutes=5)
# 경로 변수 중 uuid 가 아닌 것(앱 환경 변수 키).
_STRING_PATH_PARAMS = {"key": "SELF_TEST_KEY"}
_PATH_PARAM = re.compile(r"{([^}:]+)(?::[^}]*)?}")
_SELF_TEST_BASE_URL = "http://echo-self-test.internal"


@dataclass(frozen=True)
class RouteProbe:
    """쓰기 경로 하나를 에이전트 토큰으로 부른 결과."""

    method: str
    route: str
    status_code: int
    code: str | None

    @property
    def label(self) -> str:
        return f"{self.method} {self.route}"

    @property
    def is_denied(self) -> bool:
        return self.status_code == 403 and self.code == AGENT_READ_ONLY_CODE


@dataclass(frozen=True)
class AuditCheck:
    """감사 기록 대조 한 종류: 있어야 할 대상 id 와 그중 감사 줄이 없는 것."""

    key: str
    expected: tuple[str, ...]
    missing: tuple[str, ...]


def fill_route(route: str, organization_id: uuid.UUID) -> str:
    """경로 틀의 변수를 채운다: 조직은 그 조직, 문자열 변수는 고정 값, 나머지는 임의 uuid."""

    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        if name == "org_id":
            return str(organization_id)
        return _STRING_PATH_PARAMS.get(name, str(uuid.uuid4()))

    return _PATH_PARAM.sub(value, route)


def judge_agent_writes(
    probes: Sequence[RouteProbe], scope: SecurityScope, collected_at: datetime
) -> NormalizedFinding:
    """C-IAM-2: 모두 403 AGENT_READ_ONLY 면 PASS. 부른 경로가 없으면 확인하지 못한 것(UNKNOWN)."""
    failed = [probe for probe in probes if not probe.is_denied]
    if not probes:
        result, reason = SecurityResult.UNKNOWN, SecurityFindingReason.REPORT_INVALID
    elif failed:
        result, reason = SecurityResult.FAIL, SecurityFindingReason.AGENT_WRITE_NOT_DENIED
    else:
        result, reason = SecurityResult.PASS, SecurityFindingReason.AGENT_WRITES_DENIED
    # 실패한 경로를 먼저 싣는다.
    ordered = [*failed, *(probe for probe in probes if probe.is_denied)]
    items = tuple(
        FindingItem(
            item_id=probe.label,
            target=f"{probe.status_code} {probe.code or '-'}",
            reason=(
                SecurityFindingReason.AGENT_WRITES_DENIED
                if probe.is_denied
                else SecurityFindingReason.AGENT_WRITE_NOT_DENIED
            ),
        )
        for probe in ordered[:ITEM_LIMIT]
    )
    return _normalized(
        SecurityControlId.IAM_2,
        SecurityFindingSource.WAS_TEST,
        result,
        reason,
        scope,
        collected_at,
        items,
        len(probes),
    )


def judge_audit_trail(
    unnamed_routes: Sequence[Route],
    checks: Sequence[AuditCheck],
    scope: SecurityScope,
    collected_at: datetime,
) -> NormalizedFinding:
    """C-AUD-1: 이름표에 없는 쓰기 경로도, 빠진 감사 줄도 없으면 PASS."""
    missing_items = [
        FindingItem(
            item_id=f"{method} {route}",
            target="route",
            reason=SecurityFindingReason.AUDIT_ROUTE_UNNAMED,
        )
        for method, route in unnamed_routes
    ] + [
        FindingItem(
            item_id=check.key,
            target=target_id,
            reason=SecurityFindingReason.AUDIT_RECORD_MISSING,
        )
        for check in checks
        for target_id in check.missing
    ]
    summary_items = [
        FindingItem(
            item_id=check.key,
            target=f"{len(check.expected) - len(check.missing)}/{len(check.expected)}",
            reason=(
                SecurityFindingReason.AUDIT_RECORD_MISSING
                if check.missing
                else SecurityFindingReason.AUDIT_TRAIL_COMPLETE
            ),
        )
        for check in checks
    ]
    if unnamed_routes:
        result, reason = SecurityResult.FAIL, SecurityFindingReason.AUDIT_ROUTE_UNNAMED
    elif missing_items:
        result, reason = SecurityResult.FAIL, SecurityFindingReason.AUDIT_RECORD_MISSING
    else:
        result, reason = SecurityResult.PASS, SecurityFindingReason.AUDIT_TRAIL_COMPLETE
    items = [*missing_items, *summary_items]
    return _normalized(
        SecurityControlId.AUD_1,
        SecurityFindingSource.AUDIT_LOG,
        result,
        reason,
        scope,
        collected_at,
        tuple(items[:ITEM_LIMIT]),
        len(items),
    )


def _normalized(
    control_id: SecurityControlId,
    source: SecurityFindingSource,
    result: SecurityResult,
    reason: SecurityFindingReason,
    scope: SecurityScope,
    collected_at: datetime,
    items: tuple[FindingItem, ...],
    total_items: int,
) -> NormalizedFinding:
    finding = SecurityFinding(
        finding_id=uuid.uuid4(),
        control_id=control_id,
        result=result,
        mode=SecurityFindingMode.REAL,
        source=source,
        scope=scope,
        collected_at=collected_at,
        expires_at=collected_at + EVIDENCE_TTL,
        policy_version=POLICY_VERSION,
    )
    return NormalizedFinding(finding, reason, items, total_items, None)


def _check(key: str, expected: Iterable[uuid.UUID], recorded: set[uuid.UUID]) -> AuditCheck:
    ids = sorted(set(expected), key=str)
    return AuditCheck(
        key=key,
        expected=tuple(str(item) for item in ids),
        missing=tuple(str(item) for item in ids if item not in recorded),
    )


@dataclass(frozen=True)
class _IssuedToken:
    token_id: uuid.UUID
    raw: str


class PlatformSelfTest:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        settings: Settings,
        app: ASGIApp,
        routes: Iterable[Any],
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._was_version = settings.was_version
        self._gate_enabled = settings.security_gate_enabled
        self._interval = settings.security_platform_self_test_interval_seconds
        self._app = app
        routes = list(routes)
        self._probe_routes = agent_probe_routes(routes)
        self._unnamed_routes = unnamed_write_routes(routes)

    async def run_once(self) -> int:
        """한 번 시험한다. 남긴 증거 수(조직마다 2건)."""
        if self._was_version is None:
            logger.warning(
                "platform self test skipped: WAS_VERSION is empty",
                extra={"action": "run_platform_self_test"},
            )
            return 0
        async with self._session_factory() as session, session.begin():
            organizations = list(
                (
                    await session.execute(
                        select(Organization.id, Organization.created_by_user_id)
                        .where(
                            Organization.id.in_(
                                select(Environment.organization_id).where(
                                    Environment.is_deleted.is_(False)
                                )
                            )
                        )
                        .order_by(Organization.id)
                    )
                ).all()
            )
        recorded = 0
        for organization_id, creator_id in organizations:
            try:
                recorded += await self._test_organization(organization_id, creator_id)
            except Exception:
                # 한 조직의 실패가 다른 조직 시험을 막지 않게 한다. 증거는 남기지 않는다.
                logger.exception(
                    "platform self test failed",
                    extra={
                        "action": "run_platform_self_test",
                        "organization_id": str(organization_id),
                    },
                )
        return recorded

    async def _test_organization(self, organization_id: uuid.UUID, creator_id: uuid.UUID) -> int:
        started_at = self._clock.now()
        issued = await self._issue_token(organization_id, creator_id)
        try:
            probes = await self._probe_writes(organization_id, issued.raw)
        finally:
            await self._revoke_token(organization_id, issued.token_id)
        collected_at = self._clock.now()
        scope = SecurityScope(organization_id, was_version=self._was_version)
        checks = await self._audit_checks(
            organization_id, issued.token_id, probes, started_at - AUDIT_WINDOW
        )
        findings = [
            judge_agent_writes(probes, scope, collected_at),
            judge_audit_trail(self._unnamed_routes, checks, scope, collected_at),
        ]
        async with self._session_factory() as session:
            await SecurityFindingService(session).record_all(
                findings, source_ref=SELF_TEST_SOURCE_REF
            )
        logger.info(
            "platform self test recorded",
            extra={
                "action": "run_platform_self_test",
                "organization_id": str(organization_id),
                "iam_result": findings[0].finding.result,
                "audit_result": findings[1].finding.result,
                "probe_count": len(probes),
            },
        )
        return len(findings)

    async def _issue_token(self, organization_id: uuid.UUID, creator_id: uuid.UUID) -> _IssuedToken:
        async with self._session_factory() as session, session.begin():
            issued = await issue_agent_token(
                session,
                organization_id=organization_id,
                created_by_user_id=creator_id,
                name=SELF_TEST_TOKEN_NAME,
                expires_in_days=SELF_TEST_TOKEN_DAYS,
                clock=self._clock,
            )
            AuditRecorder(session, self._clock).record(
                organization_id=organization_id,
                actor=AuditActor.system(AuditComponent.SECURITY_SELF_TEST),
                action=AuditAction.AGENT_TOKEN_ISSUED,
                target=AuditTarget(
                    AuditTargetType.AGENT_TOKEN, issued.token.id, SELF_TEST_TOKEN_NAME
                ),
                details={"name": SELF_TEST_TOKEN_NAME, "purpose": "C-IAM-2 self test"},
            )
            return _IssuedToken(issued.token.id, issued.raw.get_secret_value())

    async def _revoke_token(self, organization_id: uuid.UUID, token_id: uuid.UUID) -> None:
        async with self._session_factory() as session, session.begin():
            token = await session.get(AgentToken, token_id)
            if token is None or token.revoked_at is not None:
                return
            token.revoked_at = self._clock.now()
            AuditRecorder(session, self._clock).record(
                organization_id=organization_id,
                actor=AuditActor.system(AuditComponent.SECURITY_SELF_TEST),
                action=AuditAction.AGENT_TOKEN_REVOKED,
                target=AuditTarget(AuditTargetType.AGENT_TOKEN, token_id, SELF_TEST_TOKEN_NAME),
            )

    async def _probe_writes(self, organization_id: uuid.UUID, raw_token: str) -> list[RouteProbe]:
        transport = httpx.ASGITransport(app=self._app, raise_app_exceptions=False)
        headers = {"Authorization": f"Bearer {raw_token}"}
        probes: list[RouteProbe] = []
        async with httpx.AsyncClient(
            transport=transport, base_url=_SELF_TEST_BASE_URL, headers=headers
        ) as client:
            for method, route in self._probe_routes:
                response = await client.request(method, fill_route(route, organization_id), json={})
                probes.append(RouteProbe(method, route, response.status_code, _code_of(response)))
        return probes

    async def _audit_checks(
        self,
        organization_id: uuid.UUID,
        token_id: uuid.UUID,
        probes: Sequence[RouteProbe],
        since: datetime,
    ) -> list[AuditCheck]:
        async with self._session_factory() as session, session.begin():
            audit_rows = (
                await session.execute(
                    select(
                        AuditLog.action,
                        AuditLog.target_id,
                        AuditLog.route,
                        AuditLog.actor_type,
                        AuditLog.actor_agent_token_id,
                        AuditLog.result,
                        AuditLog.reason,
                    ).where(
                        AuditLog.organization_id == organization_id,
                        AuditLog.created_at >= since - _AUDIT_SLACK,
                    )
                )
            ).all()
            plans = (
                (
                    await session.execute(
                        select(MigrationPlan.id)
                        .join(Service, Service.id == MigrationPlan.service_id)
                        .where(
                            Service.organization_id == organization_id,
                            MigrationPlan.approved_at >= since,
                            MigrationPlan.approved_by_user_id.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            gate_logged = set(
                (
                    await session.execute(
                        select(MigrationPlanLog.migration_plan_id).where(
                            MigrationPlanLog.migration_plan_id.in_(plans),
                            MigrationPlanLog.code == MigrationLogCode.SECURITY_GATE_PASSED,
                        )
                    )
                ).scalars()
            )
            patch_decisions = (
                await session.execute(
                    select(AgentPatchApproval.run_id, AgentPatchApproval.decision)
                    .join(AgentRun, AgentRun.id == AgentPatchApproval.run_id)
                    .where(
                        AgentRun.organization_id == organization_id,
                        AgentPatchApproval.decided_at >= since,
                    )
                )
            ).all()
            runs = (
                await session.execute(
                    select(AgentRun.id, AgentRun.status, AgentRun.claimed_at, AgentRun.finished_at)
                    .where(AgentRun.organization_id == organization_id)
                    .where((AgentRun.claimed_at >= since) | (AgentRun.finished_at >= since))
                )
            ).all()
            exceptions = (
                await session.execute(
                    select(
                        SecurityException.id,
                        SecurityException.created_at,
                        SecurityException.approved_at,
                        SecurityException.revoked_at,
                    ).where(
                        SecurityException.organization_id == organization_id,
                        (SecurityException.created_at >= since)
                        | (SecurityException.approved_at >= since)
                        | (SecurityException.revoked_at >= since),
                    )
                )
            ).all()

        def recorded(action: str) -> set[uuid.UUID]:
            return {
                row.target_id
                for row in audit_rows
                if row.action == action and row.target_id is not None
            }

        denied_routes = {
            row.route
            for row in audit_rows
            if row.actor_type == ActorType.AGENT
            and row.actor_agent_token_id == token_id
            and row.result == AuditResult.DENIED
            and row.reason == AGENT_READ_ONLY_CODE
        }
        denied_probes = [probe for probe in probes if probe.is_denied]
        checks = [
            AuditCheck(
                key="denial:AGENT_READ_ONLY",
                expected=tuple(probe.label for probe in denied_probes),
                missing=tuple(
                    probe.label for probe in denied_probes if probe.label not in denied_routes
                ),
            ),
            _check(
                f"approval:{AuditAction.MIGRATION_PLAN_APPROVED}",
                plans,
                recorded(AuditAction.MIGRATION_PLAN_APPROVED),
            ),
        ]
        if self._gate_enabled:
            checks.append(_check("gate:SECURITY_GATE_PASSED", plans, gate_logged))
        for decision, action in (
            (AgentPatchDecision.APPROVED, AuditAction.AGENT_PATCH_APPROVED),
            (AgentPatchDecision.REJECTED, AuditAction.AGENT_PATCH_REJECTED),
        ):
            checks.append(
                _check(
                    f"approval:{action}",
                    (run_id for run_id, value in patch_decisions if value == decision),
                    recorded(action),
                )
            )
        checks.extend(
            [
                _check(
                    f"approval:{AuditAction.SECURITY_EXCEPTION_APPROVED}",
                    (row.id for row in exceptions if _within(row.approved_at, since)),
                    recorded(AuditAction.SECURITY_EXCEPTION_APPROVED),
                ),
                _check(
                    f"agent:{AuditAction.AGENT_RUN_CLAIMED}",
                    (row.id for row in runs if _within(row.claimed_at, since)),
                    recorded(AuditAction.AGENT_RUN_CLAIMED),
                ),
                _check(
                    f"agent:{AuditAction.AGENT_RUN_FINISHED}",
                    (
                        row.id
                        for row in runs
                        if _within(row.finished_at, since)
                        and row.status in (AgentRunStatus.SUCCEEDED, AgentRunStatus.FAILED)
                    ),
                    recorded(AuditAction.AGENT_RUN_FINISHED),
                ),
                _check(
                    f"agent:{AuditAction.AGENT_RUN_TIMED_OUT}",
                    (
                        row.id
                        for row in runs
                        if _within(row.finished_at, since)
                        and row.status == AgentRunStatus.TIMED_OUT
                    ),
                    recorded(AuditAction.AGENT_RUN_TIMED_OUT),
                ),
                _check(
                    f"exception:{AuditAction.SECURITY_EXCEPTION_REQUESTED}",
                    (row.id for row in exceptions if _within(row.created_at, since)),
                    recorded(AuditAction.SECURITY_EXCEPTION_REQUESTED),
                ),
                _check(
                    f"exception:{AuditAction.SECURITY_EXCEPTION_REVOKED}",
                    (row.id for row in exceptions if _within(row.revoked_at, since)),
                    recorded(AuditAction.SECURITY_EXCEPTION_REVOKED),
                ),
            ]
        )
        return checks

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                # 시험 실패로 일꾼이 멈추지 않게 한다. 다음 주기에 다시 한다.
                logger.exception(
                    "platform self test crashed", extra={"action": "run_platform_self_test"}
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass


def _within(value: datetime | None, since: datetime) -> bool:
    return value is not None and value >= since


def _code_of(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, str) else None
