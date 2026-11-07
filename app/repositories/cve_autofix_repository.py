"""CVE 수정 상태는 원 finding의 remediation JSON에만 저장한다. 새 migration 없음."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Finding, Service
from app.repositories.cve_watch_repository import CveWatchRepository
from app.repositories.service_environment_repository import ServiceEnvironmentRepository


class CveAutofixRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_finding(
        self, organization_id: uuid.UUID, finding_id: uuid.UUID
    ) -> Finding | None:
        return await self._session.scalar(
            select(Finding)
            .where(Finding.organization_id == organization_id, Finding.id == finding_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )

    async def find_context(self, finding: Finding) -> tuple[str, list[dict[str, Any]], str | None]:
        service = await self._session.scalar(
            select(Service).where(
                Service.organization_id == finding.organization_id,
                Service.id == finding.service_id,
                Service.is_deleted.is_(False),
            )
        )
        connections = await ServiceEnvironmentRepository(
            self._session
        ).search_live_current_by_organization(finding.organization_id)
        digests = [
            c.current_deployment.image_digest
            for c in connections
            if c.service_id == finding.service_id
            and c.environment_id == finding.environment_id
            and c.current_deployment is not None
        ]
        if (
            service is None
            or service.status != "ACTIVE"
            or len(digests) != 1
            or finding.service_id is None
        ):
            return "", [], None
        snapshot = await CveWatchRepository(self._session).find_latest_snapshot(
            finding.service_id, digests[0]
        )
        packages = (
            snapshot.packages
            if snapshot and snapshot.organization_id == finding.organization_id
            else []
        )
        return service.repo_url, packages, digests[0]
