"""CVE 감시(SPEC sec-6-cve-watch): 공개 취약점 피드를 서비스 의존성과 맞춰 보안 증거를 남긴다.

흐름
1. 대상: 조직의 살아 있는 연결 중 현재 배포가 있는 것(서비스 × 환경 × 지금 도는 이미지 digest).
2. 의존성: 그 서비스·digest 의 가장 최근 의존성 목록(`service_dependency_snapshots`).
   없으면 건너뛴다
   (`targetsWithoutDependencies`). 목록은 빌드 보안 검사 원문에서 뽑는다(`record_snapshot`).
3. 피드: 실제 조회(OSV, `AdvisoryFeed`) 또는 가짜 권고 한 건(`SimulatedAdvisory`, inject).
4. 거르기: HIGH·CRITICAL 만. 실제 조회는 빌드 검사(Trivy)가 그 digest 에 이미 보고한 취약점 id 를
   새 것으로 치지 않는다.
5. 기록: C-IMG-1 증거 한 건 = (서비스, 환경, digest, 권고, 패키지, 설치 버전).
   고친 버전이 있으면 FAIL,
   없으면 WARN(빌드 검사 C-IMG-1 과 같은 기준). `remediation{kind: DEPENDENCY_BUMP, …}` 를 붙인다.
   같은 열쇠의 만료 전 증거가 있으면 새로 만들지 않는다(중복 제거, 조직 단위 잠금).
6. 감사: 즉시 조회·주입은 사람(USER) 줄 하나,
   주기 감시는 새 증거가 있을 때 SYSTEM(cve-watch) 줄 하나.

가짜 권고 증거는 `mode = SIMULATED` 라 판정(매트릭스·게이트)에 쓰이지 않는다.
판정 조회는 SIMULATED 를 상한(LIMIT) 전에 뺀다
(`FindingRepository.search_unexpired_by_organization`).
주입은 조직마다 살아 있는 가짜 증거를 `SIMULATED_ACTIVE_LIMIT` 건까지만 남긴다(넘으면 오래된 것부터
지운다).
"""

import hashlib
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.actions import AuditAction, AuditComponent, AuditTargetType
from app.audit.recorder import AuditActor, AuditRecorder, AuditTarget
from app.clients.osv import Advisory, AdvisoryFeed, AdvisoryMatch, canonical_advisory_id
from app.core.clock import Clock
from app.core.exceptions import (
    FindingNotFoundError,
    NotImplementedYetError,
    RemediationNotAvailableError,
)
from app.enums import (
    DependencySnapshotSource,
    RemediationKind,
    SecurityControlId,
    SecurityFindingMode,
    SecurityFindingReason,
    SecurityFindingSource,
    SecurityResult,
)
from app.models import Organization, ServiceDependencySnapshot
from app.repositories.cve_watch_repository import CveWatchRepository
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.services.cve_watch_packages import DependencyPackage
from app.services.cve_watch_versions import fixed_version_for, is_affected, scheme_for
from app.services.security_finding_service import to_finding_model
from app.services.security_normalizers import FindingItem, NormalizedFinding
from app.services.security_rules import POLICY_VERSION, SecurityFinding, SecurityScope, get_rule

logger = logging.getLogger(__name__)

WATCHED_SEVERITIES = frozenset({"CRITICAL", "HIGH"})
OSV_TOOL_VERSION = "osv.dev v1"
SIMULATED_TOOL_VERSION = "simulated"
OSV_SOURCE_REF = "cve-watch:osv"
SIMULATED_SOURCE_REF_PREFIX = "cve-watch:simulated:"
AUDIT_FINDING_IDS_LIMIT = 20
# 조직마다 살아 있는(만료 전) 가짜 CVE 증거 상한. 넘으면 이번 주입이 쓰지 않은 오래된 것부터 지운다.
SIMULATED_ACTIVE_LIMIT = 20
DIAGNOSIS_ISSUE_URL = "https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/98"


@dataclass(frozen=True)
class SimulatedAdvisory:
    """시연용 가짜 권고 한 건. 기본값은 React2Shell(CVE-2025-55182, Node.js RCE)을 본뜬 것이다."""

    advisory_id: str = "SIM-NODE2SHELL-0001"
    summary: str = "[시연용 가짜 권고] React Server Components 원격 코드 실행(React2Shell 모양)"
    severity: str = "CRITICAL"
    ecosystem: str = "npm"
    package: str = "react-server-dom-webpack"
    introduced: str = "19.0.0"
    fixed: str | None = "19.0.1"
    aliases: tuple[str, ...] = ()

    def events(self) -> list[dict[str, str]]:
        events = [{"introduced": self.introduced}]
        if self.fixed:
            events.append({"fixed": self.fixed})
        return events

    def match(self, packages: Sequence[DependencyPackage]) -> list[AdvisoryMatch]:
        wanted = DependencyPackage(self.ecosystem, self.package, "0").key
        advisory = Advisory(
            advisory_id=self.advisory_id,
            aliases=self.aliases,
            summary=self.summary,
            severity=self.severity,
            url=None,
        )
        ranges = [{"events": self.events()}]
        scheme = scheme_for(self.ecosystem)
        return [
            AdvisoryMatch(
                package, advisory, fixed_version_for(package.version, ranges, self.ecosystem)
            )
            for package in packages
            if package.key == wanted and is_affected(package.version, self.events(), scheme)
        ]


@dataclass(frozen=True)
class CveWatchMatchView:
    finding_id: uuid.UUID
    created: bool
    service_id: uuid.UUID
    environment_id: uuid.UUID
    image_digest: str
    advisory_id: str
    aliases: tuple[str, ...]
    severity: str
    summary: str | None
    ecosystem: str
    package: str
    installed_version: str
    fixed_version: str | None
    result: SecurityResult


@dataclass(frozen=True)
class CveWatchRunView:
    simulated: bool
    checked_at: datetime
    targets: int
    targets_without_dependencies: int
    packages: int
    matches: list[CveWatchMatchView] = field(default_factory=list)
    skipped_low_severity: int = 0
    skipped_known: int = 0
    # 상세 상한 때문에 이번에 못 보고 다음 조회로 미룬 권고 수.
    deferred_advisories: int = 0
    # 피드 응답 안쪽 모양이 틀려 건너뛴 결과·상세 건수.
    feed_errors: int = 0
    # 가짜 증거 상한을 넘어 지운 옛 가짜 증거 수(주입만).
    retired_simulated: int = 0

    @property
    def created_count(self) -> int:
        return sum(1 for match in self.matches if match.created)

    @property
    def duplicate_count(self) -> int:
        return sum(1 for match in self.matches if not match.created)


@dataclass(frozen=True)
class _Target:
    service_id: uuid.UUID
    environment_id: uuid.UUID
    image_digest: str


class CveWatchService:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock
        self._repository = CveWatchRepository(session)

    # --- 의존성 목록 ---

    async def record_snapshot(
        self,
        *,
        organization_id: uuid.UUID,
        service_id: uuid.UUID,
        image_digest: str,
        source: DependencySnapshotSource,
        packages: Sequence[DependencyPackage],
        source_ref: str | None,
        collected_at: datetime,
    ) -> uuid.UUID | None:
        """의존성 목록 하나를 남긴다. 빈 목록은 남기지 않는다(None)."""
        if not packages:
            return None
        snapshot = ServiceDependencySnapshot(
            id=uuid.uuid4(),
            organization_id=organization_id,
            service_id=service_id,
            image_digest=image_digest,
            source=source,
            source_ref=source_ref[:500] if source_ref else None,
            packages=[package.to_json() for package in packages],
            package_count=len(packages),
            collected_at=collected_at,
        )
        async with self._session.begin():
            self._repository.add_snapshot(snapshot)
        return snapshot.id

    # --- 조회·주입 ---

    async def check(
        self,
        organization_id: uuid.UUID,
        feed: AdvisoryFeed,
        *,
        actor_user_id: uuid.UUID | None,
    ) -> CveWatchRunView:
        """실제 피드(OSV)로 조회한다. actor_user_id 가 None 이면 주기 감시(SYSTEM)다."""
        view = await self._run(
            organization_id, feed.find, simulated=False, service_id=None, advisory=None
        )
        view = replace(
            view,
            deferred_advisories=int(getattr(feed, "last_deferred", 0) or 0),
            feed_errors=int(getattr(feed, "last_errors", 0) or 0),
        )
        if view.deferred_advisories or view.feed_errors:
            logger.warning(
                "cve watch incomplete",
                extra={
                    "action": "check_cve_watch",
                    "organization_id": str(organization_id),
                    "deferred_advisories": view.deferred_advisories,
                    "feed_errors": view.feed_errors,
                },
            )
        await self._audit(organization_id, view, actor_user_id, advisory=None)
        return view

    async def inject(
        self,
        organization_id: uuid.UUID,
        advisory: SimulatedAdvisory,
        *,
        actor_user_id: uuid.UUID,
        service_id: uuid.UUID | None,
    ) -> CveWatchRunView:
        """가짜 권고 한 건을 같은 길로 맞춰 본다. 증거는 SIMULATED 다."""

        async def match(packages: Sequence[DependencyPackage]) -> list[AdvisoryMatch]:
            return advisory.match(packages)

        view = await self._run(
            organization_id, match, simulated=True, service_id=service_id, advisory=advisory
        )
        await self._audit(organization_id, view, actor_user_id, advisory=advisory)
        return view

    async def request_diagnosis(self, organization_id: uuid.UUID, finding_id: uuid.UUID) -> None:
        """증거의 고칠 방법으로 AI 진단 실행(CHANGE_PROPOSAL)을 만든다 — 지금은 계약만(501).

        이슈 #98(SPEC api-98)의 CHANGE_PROPOSAL 은 실패한 배포 요청(deploy_request_id)이
        있어야 한다.
        CVE 증거에서 시작하려면 agent_runs 에 finding_id 를 더하는 후속이 필요하다(SPEC 2-d).
        """
        async with self._session.begin():
            finding = await self._repository.find_finding(organization_id, finding_id)
        if finding is None:
            raise FindingNotFoundError("finding not found", finding_id=str(finding_id))
        remediation = finding.remediation or {}
        if remediation.get("kind") != RemediationKind.DEPENDENCY_BUMP or not remediation.get(
            "toVersion"
        ):
            raise RemediationNotAvailableError(
                "finding has no dependency bump remediation", finding_id=str(finding_id)
            )
        raise NotImplementedYetError(
            "diagnosis run for CVE findings waits for CHANGE_PROPOSAL (issue #98)",
            issue=DIAGNOSIS_ISSUE_URL,
        )

    # --- 내부 ---

    async def _targets(
        self, organization_id: uuid.UUID, service_id: uuid.UUID | None
    ) -> list[_Target]:
        async with self._session.begin():
            connections = await ServiceEnvironmentRepository(
                self._session
            ).search_live_current_by_organization(organization_id)
        return [
            _Target(
                service_id=connection.service_id,
                environment_id=connection.environment_id,
                image_digest=connection.current_deployment.image_digest,
            )
            for connection in connections
            if connection.current_deployment is not None
            and (service_id is None or connection.service_id == service_id)
        ]

    async def _run(
        self,
        organization_id: uuid.UUID,
        matcher: Callable[[Sequence[DependencyPackage]], Any],
        *,
        simulated: bool,
        service_id: uuid.UUID | None,
        advisory: SimulatedAdvisory | None,
    ) -> CveWatchRunView:
        now = self._clock.now()
        targets = await self._targets(organization_id, service_id)
        packages_by_image: dict[tuple[uuid.UUID, str], list[DependencyPackage]] = {}
        async with self._session.begin():
            for target in targets:
                image = (target.service_id, target.image_digest)
                if image in packages_by_image:
                    continue
                snapshot = await self._repository.find_latest_snapshot(*image)
                packages_by_image[image] = (
                    [
                        package
                        for raw in snapshot.packages
                        if isinstance(raw, dict)
                        and (package := DependencyPackage.from_json(raw)) is not None
                    ]
                    if snapshot is not None
                    else []
                )
        without = sum(1 for t in targets if not packages_by_image[(t.service_id, t.image_digest)])
        unique_packages = sorted({p for ps in packages_by_image.values() for p in ps})
        matches: list[AdvisoryMatch] = await matcher(unique_packages) if unique_packages else []
        by_package: dict[DependencyPackage, list[AdvisoryMatch]] = {}
        for match in matches:
            by_package.setdefault(match.package, []).append(match)

        mode = SecurityFindingMode.SIMULATED if simulated else SecurityFindingMode.REAL
        rule = get_rule(SecurityControlId.IMG_1)
        low = known_skipped = 0
        candidates: list[tuple[_Target, AdvisoryMatch, str]] = []
        async with self._session.begin():
            known = (
                {}
                if simulated
                else await self._repository.search_known_vulnerability_ids(
                    organization_id, {t.image_digest for t in targets}, now
                )
            )
            for target in targets:
                for package in packages_by_image[(target.service_id, target.image_digest)]:
                    for match in by_package.get(package, []):
                        if match.advisory.severity not in WATCHED_SEVERITIES:
                            low += 1
                            continue
                        ids = {match.advisory.advisory_id, *match.advisory.aliases}
                        if ids & known.get(target.image_digest, set()):
                            known_skipped += 1
                            continue
                        key = dedupe_key(organization_id, target, match, mode)
                        candidates.append((target, match, key))

            await self._repository.lock_organization(organization_id)
            existing = await self._repository.search_live_dedupe_keys(
                organization_id, {key for _, _, key in candidates}, now
            )
            views: list[CveWatchMatchView] = []
            seen: dict[str, uuid.UUID] = {}
            for target, match, key in candidates:
                found = existing.get(key) or seen.get(key)
                created = found is None
                if found is None:
                    model = to_finding_model(
                        _normalized(organization_id, target, match, mode, now, rule.max_age),
                        _source_ref(match, simulated),
                    )
                    model.remediation = _remediation(match)
                    model.dedupe_key = key
                    self._session.add(model)
                    found = model.id
                    seen[key] = found
                views.append(_view(found, created, target, match))
            retired = 0
            if simulated:
                retired = await self._repository.retire_simulated_overflow(
                    organization_id,
                    keep_ids={view.finding_id for view in views},
                    limit=SIMULATED_ACTIVE_LIMIT,
                    now=now,
                )
        view = CveWatchRunView(
            simulated=simulated,
            checked_at=now,
            targets=len(targets),
            targets_without_dependencies=without,
            packages=len(unique_packages),
            matches=views,
            skipped_low_severity=low,
            skipped_known=known_skipped,
            retired_simulated=retired,
        )
        logger.info(
            "cve watch checked",
            extra={
                "action": "check_cve_watch",
                "organization_id": str(organization_id),
                "simulated": simulated,
                "targets": view.targets,
                "packages": view.packages,
                "created_count": view.created_count,
                "duplicate_count": view.duplicate_count,
                "advisory_id": advisory.advisory_id if advisory else None,
            },
        )
        return view

    async def _audit(
        self,
        organization_id: uuid.UUID,
        view: CveWatchRunView,
        actor_user_id: uuid.UUID | None,
        *,
        advisory: SimulatedAdvisory | None,
    ) -> None:
        if actor_user_id is None and view.created_count == 0:
            return
        created_ids = [str(m.finding_id) for m in view.matches if m.created]
        details: dict[str, Any] = {
            "simulated": view.simulated,
            "targets": view.targets,
            "created": view.created_count,
            "duplicates": view.duplicate_count,
            "findingIds": created_ids[:AUDIT_FINDING_IDS_LIMIT],
        }
        if view.deferred_advisories:
            details["deferredAdvisories"] = view.deferred_advisories
        if view.feed_errors:
            details["feedErrors"] = view.feed_errors
        if view.retired_simulated:
            details["retiredSimulated"] = view.retired_simulated
        if advisory is not None:
            details["advisoryId"] = advisory.advisory_id
            details["package"] = f"{advisory.ecosystem}/{advisory.package}"
        if actor_user_id is None:
            actor = AuditActor.system(AuditComponent.CVE_WATCH)
            action = AuditAction.CVE_WATCH_FINDINGS_RECORDED
        else:
            actor = AuditActor.user(actor_user_id)
            action = (
                AuditAction.CVE_WATCH_INJECTED if view.simulated else AuditAction.CVE_WATCH_CHECKED
            )
        async with self._session.begin():
            organization = await self._session.get(Organization, organization_id)
            AuditRecorder(self._session, self._clock).record(
                organization_id=organization_id,
                actor=actor,
                action=action,
                target=AuditTarget(
                    AuditTargetType.ORGANIZATION,
                    organization_id,
                    organization.name if organization is not None else None,
                ),
                details=details,
            )


def dedupe_key(
    organization_id: uuid.UUID,
    target: _Target,
    match: AdvisoryMatch,
    mode: SecurityFindingMode,
) -> str:
    """같은 조직·서비스·환경·이미지·권고·패키지·설치 버전·모드면 같은 열쇠다(sha256).

    권고는 별칭까지 본 대표 id(CVE 우선)로 센다 — OSV 가 같은 취약점을 GHSA·CVE 어느 id 로 돌려줘도
    같은 열쇠다.
    """
    raw = "|".join(
        (
            str(organization_id),
            str(target.service_id),
            str(target.environment_id),
            target.image_digest,
            canonical_advisory_id(match.advisory),
            match.package.ecosystem,
            match.package.name,
            match.package.version,
            mode.value,
        )
    )
    return "cve:" + hashlib.sha256(raw.encode()).hexdigest()


def _normalized(
    organization_id: uuid.UUID,
    target: _Target,
    match: AdvisoryMatch,
    mode: SecurityFindingMode,
    now: datetime,
    max_age: Any,
) -> NormalizedFinding:
    fixable = match.fixed_version is not None
    finding = SecurityFinding(
        finding_id=uuid.uuid4(),
        control_id=SecurityControlId.IMG_1,
        result=SecurityResult.FAIL if fixable else SecurityResult.WARN,
        mode=mode,
        source=SecurityFindingSource.CVE_WATCH,
        scope=SecurityScope(
            organization_id,
            service_id=target.service_id,
            environment_id=target.environment_id,
            image_digest=target.image_digest,
        ),
        collected_at=now,
        expires_at=now + max_age,
        policy_version=POLICY_VERSION,
    )
    item = FindingItem(
        item_id=match.advisory.advisory_id,
        severity=match.advisory.severity,
        target=match.package.name,
        installed_version=match.package.version,
        fixed_version=match.fixed_version,
    )
    reason = (
        SecurityFindingReason.FIXABLE_HIGH_VULNERABILITY
        if fixable
        else SecurityFindingReason.VULNERABILITY_WARNING
    )
    tool = SIMULATED_TOOL_VERSION if mode == SecurityFindingMode.SIMULATED else OSV_TOOL_VERSION
    return NormalizedFinding(finding, reason, (item,), 1, tool)


def _remediation(match: AdvisoryMatch) -> dict[str, Any]:
    return {
        "kind": RemediationKind.DEPENDENCY_BUMP.value,
        "ecosystem": match.package.ecosystem,
        "package": match.package.name,
        "fromVersion": match.package.version,
        "toVersion": match.fixed_version,
        "advisoryId": match.advisory.advisory_id,
        "aliases": list(match.advisory.aliases),
        "severity": match.advisory.severity,
        "summary": match.advisory.summary,
    }


def _source_ref(match: AdvisoryMatch, simulated: bool) -> str:
    if simulated:
        return SIMULATED_SOURCE_REF_PREFIX + match.advisory.advisory_id
    return match.advisory.url or OSV_SOURCE_REF


def _view(
    finding_id: uuid.UUID, created: bool, target: _Target, match: AdvisoryMatch
) -> CveWatchMatchView:
    return CveWatchMatchView(
        finding_id=finding_id,
        created=created,
        service_id=target.service_id,
        environment_id=target.environment_id,
        image_digest=target.image_digest,
        advisory_id=match.advisory.advisory_id,
        aliases=match.advisory.aliases,
        severity=match.advisory.severity,
        summary=match.advisory.summary,
        ecosystem=match.package.ecosystem,
        package=match.package.name,
        installed_version=match.package.version,
        fixed_version=match.fixed_version,
        result=SecurityResult.FAIL if match.fixed_version else SecurityResult.WARN,
    )
