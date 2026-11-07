"""이전 계획 통합 시험 도구.

serving_and_standby(): shop-api 를 aws-seoul 에 배포(서비스 중 100%)하고 gcp-seoul 에 같은 커밋을
배포(예비 0%, 같은 digest)한 상태를 만든다. 계획 만들기는 개발자 A, 승인은 개발자 B 가 한다.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from httpx import AsyncClient, Response
from sqlalchemy import select, text

from app.enums import JobKind, OrganizationRole
from app.models import Environment, Job, MigrationPlan, MigrationPlanLog, ServiceEnvironment
from app.services.migration_plan_support import initial_step_results
from tests.integration.auth_support import Member, create_member, make_client, org_path
from tests.integration.deploy_harness import DeployHarness
from tests.integration.rollback_support import deploy_and_finish

SUFFIX = "m1g2r3t4"


@dataclass
class Migration:
    harness: DeployHarness
    author: Member
    approver: Member
    author_client: AsyncClient
    approver_client: AsyncClient
    service: dict[str, Any]
    aws: Environment
    gcp: Environment

    @property
    def service_id(self) -> str:
        return str(self.service["id"])

    @property
    def plans_path(self) -> str:
        return f"{self.harness.services}/{self.service_id}/migration-plans"

    def plan_path(self, plan_id: str, suffix: str = "") -> str:
        return org_path(self.harness.organization.id, f"/migration-plans/{plan_id}{suffix}")

    async def request_plan(self, client: AsyncClient | None = None, **body: Any) -> Response:
        """계획 만들기. targetEnvironmentId 를 주지 않으면 gcp-seoul 이다."""
        body.setdefault("targetEnvironmentId", str(self.gcp.id))
        response = await (client or self.author_client).post(self.plans_path, json=body)
        self.harness.responses.append(response.text)
        return response

    async def create_plan(self, **body: Any) -> dict[str, Any]:
        response = await self.request_plan(**body)
        assert response.status_code == 201, response.text
        data: dict[str, Any] = response.json()["data"]
        return data

    async def act(self, plan_id: str, action: str, client: AsyncClient | None = None) -> Response:
        response = await (client or self.approver_client).post(
            self.plan_path(plan_id, f"/{action}")
        )
        self.harness.responses.append(response.text)
        return response

    async def approve(self, plan_id: str) -> dict[str, Any]:
        response = await self.act(plan_id, "approve")
        assert response.status_code == 202, response.text
        data: dict[str, Any] = response.json()["data"]
        return data

    async def get_plan(self, plan_id: str) -> dict[str, Any]:
        response = await self.harness.client.get(self.plan_path(plan_id))
        assert response.status_code == 200, response.text
        data: dict[str, Any] = response.json()["data"]
        return data

    async def logs(self, plan_id: str, **params: Any) -> Response:
        response = await self.harness.client.get(self.plan_path(plan_id, "/logs"), params=params)
        self.harness.responses.append(response.text)
        return response

    async def log_rows(self, plan_id: str) -> list[MigrationPlanLog]:
        async with self.harness.session_factory() as session:
            return list(
                (
                    await session.scalars(
                        select(MigrationPlanLog)
                        .where(MigrationPlanLog.migration_plan_id == uuid.UUID(plan_id))
                        .order_by(MigrationPlanLog.seq)
                    )
                ).all()
            )

    async def log_codes(self, plan_id: str) -> list[str]:
        return [row.code for row in await self.log_rows(plan_id)]

    async def plan_row(self, plan_id: str) -> MigrationPlan:
        async with self.harness.session_factory() as session:
            return await session.get_one(MigrationPlan, uuid.UUID(plan_id))

    async def weights(self) -> tuple[int, int]:
        """(aws, gcp) 의 traffic_weight."""
        async with self.harness.session_factory() as session:
            rows = {
                row.environment_id: row.traffic_weight
                for row in await session.scalars(
                    select(ServiceEnvironment).where(
                        ServiceEnvironment.service_id == uuid.UUID(self.service_id),
                        ServiceEnvironment.is_deleted.is_(False),
                    )
                )
            }
        return rows.get(self.aws.id, 0), rows.get(self.gcp.id, 0)

    async def set_weights(self, aws: int, gcp: int) -> None:
        """시험에서 직접 비율을 바꾼다(지문을 낡게 만들기·서비스 중 둘 만들기)."""
        async with self.harness.session_factory() as session, session.begin():
            for environment, weight in ((self.aws, aws), (self.gcp, gcp)):
                await session.execute(
                    text(
                        "UPDATE service_environments SET traffic_weight = :weight "
                        "WHERE service_id = :service AND environment_id = :environment "
                        "AND is_deleted = false"
                    ),
                    {"weight": weight, "service": self.service_id, "environment": environment.id},
                )

    async def plan_job(self, plan_id: str) -> Job | None:
        async with self.harness.session_factory() as session:
            return await session.scalar(
                select(Job)
                .where(Job.kind == JobKind.MIGRATION_PLAN, Job.subject_id == uuid.UUID(plan_id))
                .order_by(Job.created_at.desc())
            )

    def traffic_keys(self) -> list[str]:
        return [call.idempotency_key for call in self.harness.executors.traffic.calls]


async def serving_and_standby(harness: DeployHarness) -> Migration:
    harness.host_suffixes.queue(SUFFIX)
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    gcp = await harness.add_environment("gcp-seoul")
    await deploy_and_finish(harness, service["id"], aws.id, branch="main")
    await deploy_and_finish(harness, service["id"], gcp.id, branch="main")
    now = harness.clock.now()
    author = await create_member(
        harness.session_factory, harness.organization, OrganizationRole.DEVELOPER, now, "dev-a"
    )
    approver = await create_member(
        harness.session_factory, harness.organization, OrganizationRole.DEVELOPER, now, "dev-b"
    )
    return Migration(
        harness=harness,
        author=author,
        approver=approver,
        author_client=make_client(session_token=author.session_token),
        approver_client=make_client(session_token=approver.session_token),
        service=service,
        aws=aws,
        gcp=gcp,
    )


async def store_legacy_steps(
    migration: Migration, plan_id: str, *, before_counting: bool = False
) -> list[dict[str, Any]]:
    """API-24 이전 저장 계획을 재현한다. 새 단계 상수에 의존하지 않는다."""
    async with migration.harness.session_factory() as session, session.begin():
        plan = await session.get_one(MigrationPlan, uuid.UUID(plan_id))
        selected = (0, 2, 4, 5, 7, 8) if len(plan.steps) == 9 else (0, 2, 4, 5, 7)
        steps = [dict(plan.steps[i], index=index) for index, i in enumerate(selected)]
        for step in steps:
            step.pop("minObserveSeconds", None)
            if step["kind"] == "SHIFT":
                required = {10: 20, 50: 50, 100: 50}[step["targetWeight"]]
                step["requiredRequests"] = required
                step["expectedSeconds"] = 2 + {10: 4, 50: 2, 100: 1}[step["targetWeight"]]
                if before_counting:
                    step.pop("requiredRequests")
                    step.pop("maxWaitSeconds", None)
        plan.steps = steps
        plan.step_results = initial_step_results(steps)
        plan.expected_seconds = sum(step["expectedSeconds"] for step in steps)
        return steps
