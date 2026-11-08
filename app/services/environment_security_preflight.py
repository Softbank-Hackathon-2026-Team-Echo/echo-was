"""운영 전환용 읽기 전용 목록. 통과 증거를 만들거나 운영 인수를 대신하지 않는다."""

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.executors.base import ClusterEnvironment
from app.repositories.environment_evidence_repository import EnvironmentEvidenceRepository
from app.security_environment.access import reader_environment
from app.services.security_rules import POLICY_VERSION


async def inspect_configuration(
    factory: async_sessionmaker[AsyncSession], settings: Settings, now: datetime
) -> dict[str, Any]:
    rows: list[dict[str, object]] = []
    async with factory() as session, session.begin():
        await session.execute(text("SET TRANSACTION READ ONLY"))
        for profile in settings.security_environment_profiles:
            env = await EnvironmentEvidenceRepository(session).find_environment(
                profile.organization_id, profile.environment_id
            )
            issue = None
            if env is None:
                issue = "ENVIRONMENT_UNAVAILABLE"
            else:
                try:
                    reader_environment(
                        profile,
                        ClusterEnvironment(
                            env.id, env.kind, env.name, env.parsed_config(), env.secret_ref
                        ),
                        settings.executor_aws_role_arn,
                    )
                except ValueError:
                    issue = "DEDICATED_IDENTITY_REQUIRED"
            # 이전 정책 증거는 검토 목록이다. 재판정·재등록 여부를 자동 결정하지 않는다.
            old = await EnvironmentEvidenceRepository(session).count_active_old_policy(
                profile.organization_id, POLICY_VERSION, now
            )
            rows.append(
                {
                    "environmentId": str(profile.environment_id),
                    "issue": issue,
                    "organizationOldPolicyActiveCount": old,
                }
            )
    return {
        "mode": "read-only",
        "policyVersion": POLICY_VERSION,
        "configurationReady": bool(rows) and all(row["issue"] is None for row in rows),
        "operationalAcceptance": "NOT_VERIFIED",
        "policyTransition": "REVIEW_REQUIRED",
        "environments": rows,
    }
