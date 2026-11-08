"""환경별 자동 검사를 실행하고 같은 수집 흐름의 증거만 갱신한다."""

import asyncio
import logging
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.core.config import Settings
from app.enums import (
    SecurityControlId,
    SecurityFindingMode,
    SecurityFindingReason,
    SecurityFindingSource,
    SecurityResult,
)
from app.executors.base import ClusterEnvironment
from app.repositories.environment_evidence_repository import EnvironmentEvidenceRepository
from app.repositories.finding_repository import FindingRepository
from app.security_environment.types import (
    COLLECTOR_VERSION,
    EVIDENCE_TTL,
    EnvironmentSecurityProfile,
    EvidenceCheck,
    unknown,
)
from app.services.security_finding_service import to_finding_model
from app.services.security_normalizers import MAX_ITEMS, FindingItem, NormalizedFinding
from app.services.security_rules import POLICY_VERSION, SecurityFinding, SecurityScope

logger = logging.getLogger(__name__)
COLLECTION_KEY = COLLECTOR_VERSION
_CONTROL_SOURCES = {
    SecurityControlId.IAM_1: (
        SecurityFindingSource.KUBERNETES_API,
        SecurityFindingSource.CLOUD_API,
    ),
    SecurityControlId.TLS_2: (SecurityFindingSource.TLS_PROBE, SecurityFindingSource.DATA_ENGINE),
}
_RANK = {
    SecurityResult.PASS: 0,
    SecurityResult.WARN: 1,
    SecurityResult.UNKNOWN: 2,
    SecurityResult.FAIL: 3,
}


class EnvironmentSecurityReader(Protocol):
    async def inspect(
        self, profile: EnvironmentSecurityProfile, environment: ClusterEnvironment
    ) -> dict[SecurityControlId, list[EvidenceCheck]]: ...
    async def aclose(self) -> None: ...


def normalize_environment_checks(
    checks: dict[SecurityControlId, list[EvidenceCheck]],
    profile: EnvironmentSecurityProfile,
    revision: str,
    now: datetime,
) -> list[NormalizedFinding]:
    findings: list[NormalizedFinding] = []
    for control, sources in _CONTROL_SOURCES.items():
        values = list(checks.get(control, []))
        expected = (
            [(s.id, sources[0] if s.kind == "kubernetes" else sources[1]) for s in profile.subjects]
            if control == SecurityControlId.IAM_1
            else [
                (t.id, sources[1] if t.kind == "database" else sources[0])
                for t in profile.transports
            ]
        )
        for item_id, source in expected:
            if not any(v.id == item_id and v.source == source for v in values):
                values.append(unknown(item_id, "EVIDENCE_INCOMPLETE", now, source))
        if not values:
            values = [unknown("collector", "EVIDENCE_INCOMPLETE", now, sources[0])]
        if any(v.source not in sources for v in values):
            values = [unknown("collector", "SOURCE_INVALID", now, sources[0])]
        for source in sources:
            group = [v for v in values if v.source == source]
            if not group:
                continue
            # 오래된 snapshot을 새로 수집한 것으로 바꿔 TTL을 연장하지 않는다.
            if any(v.observed_at > now or now - v.observed_at >= EVIDENCE_TTL for v in group):
                group = [unknown("collector", "OBSERVATION_EXPIRED", now, source)]
            collected_at = min(v.observed_at for v in group)
            result = max((v.result for v in group), key=_RANK.__getitem__)
            reason = (
                SecurityFindingReason.NO_ISSUES
                if result == SecurityResult.PASS
                else SecurityFindingReason.MISCONFIGURATION_WARNING
                if result == SecurityResult.WARN
                else SecurityFindingReason.MISCONFIGURATION_HIGH
                if result == SecurityResult.FAIL
                else SecurityFindingReason.REPORT_INVALID
            )
            finding = SecurityFinding(
                finding_id=uuid.uuid4(),
                control_id=control,
                result=result,
                mode=SecurityFindingMode.REAL,
                source=source,
                scope=SecurityScope(profile.organization_id, environment_id=profile.environment_id),
                collected_at=collected_at,
                expires_at=collected_at + EVIDENCE_TTL,
                policy_version=POLICY_VERSION,
                environment_revision=revision,
                collection_key=COLLECTION_KEY,
            )
            items = tuple(
                FindingItem(item_id=v.reason, target=v.id, severity=v.result.value)
                for v in sorted(group, key=lambda v: (-_RANK[v.result], v.id, v.reason))
            )
            findings.append(
                NormalizedFinding(finding, reason, items[:MAX_ITEMS], len(items), COLLECTOR_VERSION)
            )
    return findings


class EnvironmentEvidenceService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_snapshot(
        self,
        profile: EnvironmentSecurityProfile,
        revision: str,
        normalized: Sequence[NormalizedFinding],
        started_at: datetime,
    ) -> int:
        if not normalized or {n.finding.control_id for n in normalized} != set(_CONTROL_SOURCES):
            raise ValueError("complete control snapshot required")
        for value in normalized:
            f = value.finding
            if (
                f.collection_key != COLLECTION_KEY
                or f.environment_revision != revision
                or f.scope.organization_id != profile.organization_id
                or f.scope.environment_id != profile.environment_id
                or f.source not in _CONTROL_SOURCES[f.control_id]
                or f.policy_version != POLICY_VERSION
                or f.mode != SecurityFindingMode.REAL
            ):
                raise ValueError("invalid environment evidence snapshot")
        async with self._session.begin():
            repository = EnvironmentEvidenceRepository(self._session)
            environment = await repository.find_environment(
                profile.organization_id, profile.environment_id, lock=True
            )
            if (
                environment is None
                or profile.revision(environment.config, environment.secret_ref) != revision
            ):
                return 0
            if not await repository.retire_previous(
                profile.organization_id,
                profile.environment_id,
                COLLECTION_KEY,
                POLICY_VERSION,
                started_at,
            ):
                return 0
            for value in normalized:
                model = to_finding_model(value, source_ref=f"{COLLECTION_KEY}:{revision}")
                model.collection_started_at = started_at
                FindingRepository(self._session).add(model)
        logger.info(
            "environment security snapshot recorded",
            extra={
                "action": "collect_environment_security",
                "organization_id": str(profile.organization_id),
                "environment_id": str(profile.environment_id),
                "finding_count": len(normalized),
            },
        )
        return len(normalized)


class EnvironmentSecurityCollector:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        settings: Settings,
        reader: EnvironmentSecurityReader,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._settings = settings
        self._reader = reader

    async def run_once(self) -> int:
        total = 0
        for profile in self._settings.security_environment_profiles:
            started_at = self._clock.now()
            async with self._session_factory() as session, session.begin():
                row = await EnvironmentEvidenceRepository(session).find_environment(
                    profile.organization_id, profile.environment_id
                )
                if row is None or row.is_deleted or row.organization_id != profile.organization_id:
                    continue
                revision = profile.revision(row.config, row.secret_ref)
            try:
                environment = ClusterEnvironment(
                    row.id, row.kind, row.name, row.parsed_config(), row.secret_ref
                )
                async with asyncio.timeout(60):
                    checks = await self._reader.inspect(profile, environment)
            except Exception as exc:
                logger.warning(
                    "environment security inspection failed",
                    extra={
                        "action": "collect_environment_security",
                        "environment_id": str(profile.environment_id),
                        "error_type": type(exc).__name__,
                    },
                )
                checks = {
                    control: [
                        unknown("collector", "COLLECTION_FAILED", self._clock.now(), sources[0])
                    ]
                    for control, sources in _CONTROL_SOURCES.items()
                }
            findings = normalize_environment_checks(checks, profile, revision, self._clock.now())
            async with self._session_factory() as session:
                total += await EnvironmentEvidenceService(session).record_snapshot(
                    profile, revision, findings, started_at
                )
        return total

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    await self.run_once()
                except Exception as exc:
                    logger.warning(
                        "environment security cycle failed",
                        extra={
                            "action": "collect_environment_security",
                            "error_type": type(exc).__name__,
                        },
                    )
                try:
                    await asyncio.wait_for(
                        stop.wait(), self._settings.security_environment_interval_seconds
                    )
                except TimeoutError:
                    continue
        finally:
            await self._reader.aclose()
