"""Bounded CycloneDX collection bound to the immutable PRECHECK input."""

import json
import re
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import AgentRunKind, AgentRunStatus, DependencySnapshotSource
from app.models import AgentRun
from app.models.service_dependency_snapshot import ServiceDependencySnapshot
from app.services.cve_watch_packages import MAX_PACKAGES, packages_from_cyclonedx
from app.services.run_result import NormalizedRunResult

MAX_SBOM_BYTES = 512 * 1024


def validate_sbom(value: dict[str, Any]) -> None:
    if len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_SBOM_BYTES:
        raise ValueError("SBOM_TOO_LARGE")
    if value.get("bomFormat") != "CycloneDX" or not isinstance(value.get("specVersion"), str):
        raise ValueError("SBOM_INVALID_FORMAT")
    components = value.get("components")
    if not isinstance(components, list) or not components:
        raise ValueError("SBOM_EMPTY")
    stack = list(components)
    count = 0
    while stack:
        item = stack.pop()
        count += 1
        if count > MAX_PACKAGES or not isinstance(item, dict):
            raise ValueError("SBOM_COMPONENT_LIMIT")
        purl = item.get("purl")
        os_metadata = item.get("type") == "operating-system" and purl is None
        if not os_metadata and (
            not isinstance(purl, str)
            or not re.fullmatch(r"pkg:[a-zA-Z0-9.+-]+/[^\s]+@[^\s]+", purl)
        ):
            raise ValueError("SBOM_PURL_REQUIRED")
        children = item.get("components", [])
        if not isinstance(children, list):
            raise ValueError("SBOM_INVALID_COMPONENTS")
        stack.extend(children)


def store_runner_sbom(
    session: AsyncSession, run: AgentRun, normalized: NormalizedRunResult, now: datetime
) -> None:
    sbom = normalized.result.get("sbom")
    if (
        run.kind != AgentRunKind.PRECHECK
        or normalized.simulated
        or normalized.status != AgentRunStatus.SUCCEEDED
        or not sbom
    ):
        return
    source = run.input_snapshot.get("source") or {}
    digest = source.get("imageDigest")
    ref = source.get("imageRef")
    if not isinstance(digest, str) or not isinstance(ref, str) or not ref.endswith("@" + digest):
        return
    validate_sbom(sbom)
    packages = packages_from_cyclonedx(sbom)
    if not packages:
        return
    session.add(
        ServiceDependencySnapshot(
            organization_id=run.organization_id,
            service_id=run.service_id,
            image_digest=digest,
            source=DependencySnapshotSource.RUNNER_SBOM,
            source_ref=str(run.id),
            packages=[package.to_json() for package in packages],
            package_count=len(packages),
            collected_at=now,
        )
    )
