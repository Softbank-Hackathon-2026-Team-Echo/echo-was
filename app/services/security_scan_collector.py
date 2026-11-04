"""빌드가 남긴 보안 검사 원문을 증거(findings)로 남긴다(보안·거버넌스 2·11).

- 배포 일꾼이 빌드를 SUCCEEDED 로 바꾼 직후 한 번 부른다. 재사용 빌드는 다시 모으지 않는다.
- 원문은 실제 빌드 실행기만 준다(`BuildSecurityScanReader`). 가짜 실행기면 아무것도 남기지 않는다.
- 원문의 build_id·digest 가 그 빌드와 다르면 버린다.
- 보고서가 없으면(검사 실패) UNKNOWN 증거를 남긴다.
- 실패해도 예외를 올리지 않는다. 배포는 계속하고, 증거가 없으면 판정이 UNKNOWN 으로 본다.
"""

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.enums import BuildStatus
from app.executors.base import BuildSecurityScan, BuildSecurityScanReader
from app.models import Build, Service
from app.repositories.build_repository import BuildRepository
from app.repositories.service_repository import ServiceRepository
from app.services.security_finding_service import SecurityFindingService
from app.services.security_normalizers import (
    NormalizedFinding,
    normalize_gitleaks,
    normalize_trivy_image,
    normalize_trivy_image_secrets,
)
from app.services.security_rules import SecurityScope

logger = logging.getLogger(__name__)

# 원문의 검사 시각이 우리 시계보다 이만큼 넘게 앞서면 믿지 않고 지금 시각을 쓴다.
_CLOCK_SKEW = timedelta(minutes=5)


class BuildSecurityScanCollector:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock
        self._builds = BuildRepository(session)
        self._services = ServiceRepository(session)

    async def collect(self, build_id: uuid.UUID, reader: object) -> int:
        """빌드 하나의 검사 원문을 증거로 남긴다. 남긴 증거 수(0 = 남기지 않음)."""
        if not isinstance(reader, BuildSecurityScanReader):
            return 0
        try:
            target = await self._find_target(build_id)
        except SQLAlchemyError:
            logger.exception("security scan target not read", extra=_extra(build_id))
            return 0
        if target is None:
            return 0
        build, service = target
        assert build.external_ref is not None and build.image_digest is not None
        scan = await reader.read_security_scan(build.external_ref)
        if scan is None:
            return 0
        if scan.build_id != str(build.id) or scan.image_digest != build.image_digest:
            logger.warning("security scan does not match build", extra=_extra(build_id))
            return 0
        findings = self._normalize(scan, service)
        try:
            await SecurityFindingService(self._session).record_all(findings, scan.source_ref)
        except SQLAlchemyError:
            logger.exception("security scan not recorded", extra=_extra(build_id))
            return 0
        return len(findings)

    async def _find_target(self, build_id: uuid.UUID) -> tuple[Build, Service] | None:
        async with self._session.begin():
            build = await self._builds.find_by_id(build_id)
            if (
                build is None
                or build.status != BuildStatus.SUCCEEDED
                or build.external_ref is None
                or build.image_digest is None
            ):
                return None
            service = await self._services.find_by_id(build.service_id)
        return (build, service) if service is not None else None

    def _normalize(self, scan: BuildSecurityScan, service: Service) -> list[NormalizedFinding]:
        scope = SecurityScope(
            organization_id=service.organization_id,
            service_id=service.id,
            image_digest=scan.image_digest,
        )
        collected_at = self._collected_at(scan.scanned_at)
        trivy = scan.trivy_report or {}
        return [
            normalize_trivy_image(
                trivy, scope=scope, collected_at=collected_at, tool_version=scan.trivy_version
            ),
            normalize_trivy_image_secrets(
                trivy, scope=scope, collected_at=collected_at, tool_version=scan.trivy_version
            ),
            normalize_gitleaks(
                scan.gitleaks_report,
                scope=scope,
                collected_at=collected_at,
                tool_version=scan.gitleaks_version,
            ),
        ]

    def _collected_at(self, scanned_at: datetime | None) -> datetime:
        """수집 시각 = 원문의 검사 시각. 없거나 지금보다 너무 앞서면 지금."""
        now = self._clock.now()
        if scanned_at is None or scanned_at > now + _CLOCK_SKEW:
            return now
        return scanned_at


def _extra(build_id: uuid.UUID) -> dict[str, str]:
    return {"action": "collect_build_security_scan", "build_id": str(build_id)}
