"""빌드가 남긴 보안 검사 원문을 증거(findings)로 남긴다(보안·거버넌스 2·11).

- 배포 일꾼이 빌드를 SUCCEEDED 로 바꾼 직후 한 번 부른다. 재사용 빌드는 다시 모으지 않는다.
- 원문은 실제 빌드 실행기만 준다(`BuildSecurityScanReader`). 가짜 실행기면 아무것도 남기지 않는다.
- 원문의 build_id·digest 가 그 빌드와 다르면 버린다.
- 보고서가 없으면(검사 실패) UNKNOWN 증거를 남긴다.
- 두 비밀 검사(이미지 Trivy secret·소스 gitleaks)는 C-IMG-2 와 함께 C-SEC-1(비밀 전달 경로) 증거도
  만든다(빌드 하나에 최대 5건). C-SEC-1 은 원문을 읽었을 때만 남긴다.
- 실패해도 예외를 올리지 않는다. 배포는 계속하고, 증거가 없으면 판정이 UNKNOWN 으로 본다.
- 빌드 반영과 수집 사이에 일꾼이 죽으면 그 빌드는 다시 모으지 않는다(다시 빌드하거나 주기 재검사).
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.enums import BuildStatus
from app.executors.base import BuildSecurityScan, BuildSecurityScanReader
from app.repositories.build_repository import BuildRepository
from app.repositories.service_repository import ServiceRepository
from app.services.security_finding_service import SecurityFindingService
from app.services.security_normalizers import (
    NormalizedFinding,
    derive_delivery_path_finding,
    normalize_gitleaks,
    normalize_trivy_image,
    normalize_trivy_image_secrets,
)
from app.services.security_rules import SecurityScope

logger = logging.getLogger(__name__)

# 원문의 검사 시각이 우리 시계보다 이만큼 넘게 앞서면 믿지 않고 지금 시각을 쓴다.
_CLOCK_SKEW = timedelta(minutes=5)


@dataclass(frozen=True)
class _ScanTarget:
    build_id: uuid.UUID
    external_ref: str
    image_digest: str
    organization_id: uuid.UUID
    service_id: uuid.UUID


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
        # 원문 읽기·확인·정규화의 어떤 실패도 배포를 멈추지 않는다(증거가 없으면 판정이 UNKNOWN).
        # 원문 내용이 로그에 섞이지 않게 예외 종류만 남긴다.
        # CancelledError(BaseException)는 그대로 나간다.
        try:
            scan = await reader.read_security_scan(target.external_ref)
            if scan is None:
                return 0
            if scan.build_id != str(target.build_id) or scan.image_digest != target.image_digest:
                logger.warning("security scan does not match build", extra=_extra(build_id))
                return 0
            findings = self._normalize(scan, target)
        except Exception as exc:
            logger.warning(
                "security scan not collected",
                extra={**_extra(build_id), "error_type": type(exc).__name__},
            )
            return 0
        try:
            await SecurityFindingService(self._session).record_all(findings, scan.source_ref)
        except SQLAlchemyError:
            logger.exception("security scan not recorded", extra=_extra(build_id))
            return 0
        return len(findings)

    async def _find_target(self, build_id: uuid.UUID) -> _ScanTarget | None:
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
        if service is None:
            return None
        return _ScanTarget(
            build_id=build.id,
            external_ref=build.external_ref,
            image_digest=build.image_digest,
            organization_id=service.organization_id,
            service_id=service.id,
        )

    def _normalize(self, scan: BuildSecurityScan, target: _ScanTarget) -> list[NormalizedFinding]:
        scope = SecurityScope(
            organization_id=target.organization_id,
            service_id=target.service_id,
            image_digest=scan.image_digest,
        )
        collected_at = self._collected_at(scan.scanned_at)
        trivy = scan.trivy_report or {}
        secret_scans = [
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
        # 같은 비밀 검사 원문 → C-SEC-1(조직 범위). 원문을 못 읽었으면 만들지 않는다.
        delivery_paths = [
            derived
            for secret in secret_scans
            if (derived := derive_delivery_path_finding(secret)) is not None
        ]
        return [
            normalize_trivy_image(
                trivy, scope=scope, collected_at=collected_at, tool_version=scan.trivy_version
            ),
            *secret_scans,
            *delivery_paths,
        ]

    def _collected_at(self, scanned_at: datetime | None) -> datetime:
        """수집 시각 = 원문의 검사 시각. 없거나 지금보다 너무 앞서면 지금."""
        now = self._clock.now()
        if scanned_at is None or scanned_at > now + _CLOCK_SKEW:
            return now
        return scanned_at


def _extra(build_id: uuid.UUID) -> dict[str, str]:
    return {"action": "collect_build_security_scan", "build_id": str(build_id)}
