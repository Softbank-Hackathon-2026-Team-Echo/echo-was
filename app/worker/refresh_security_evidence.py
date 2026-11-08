"""정책 변경 후 현재 배포의 원본 스캔을 재판정한다. 기본은 대상 조회만 한다.

python -m app.worker.refresh_security_evidence --organization-id UUID [--apply]
기존 검사 시각/digest/repository/commit 검증은 BuildSecurityScanCollector가 그대로 수행한다.
"""

import argparse
import asyncio
import json
import uuid

from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.database import get_engine, get_session_factory
from app.executors import build_executor_set
from app.repositories.environment_evidence_repository import EnvironmentEvidenceRepository
from app.services.executor_environment_lookup import DbEnvironmentLookup
from app.services.security_scan_collector import BuildSecurityScanCollector


async def refresh(organization_id: uuid.UUID, apply: bool) -> None:
    factory = get_session_factory()
    async with factory() as session, session.begin():
        ids = await EnvironmentEvidenceRepository(session).current_builds(organization_id)
    if not apply:
        print(json.dumps({"mode": "plan", "buildIds": [str(value) for value in ids]}))
        await get_engine().dispose()
        return
    clock = SystemClock()
    executors = build_executor_set(
        get_settings(), clock, environment_lookup=DbEnvironmentLookup(factory)
    )
    try:
        for build_id in ids:
            async with factory() as session:
                count = await BuildSecurityScanCollector(session, clock).collect(
                    build_id, executors.build
                )
            print(json.dumps({"buildId": str(build_id), "recorded": count}))
    finally:
        await executors.aclose()
        await get_engine().dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization-id", type=uuid.UUID, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(refresh(args.organization_id, args.apply))
