"""원터치 배포 통합 시험 도구: 주입 시계, 가짜 실행기 묶음, 일꾼, API 호출 도우미.

시험은 실제 sleep·네트워크 없이 시계를 앞으로 돌리고 run_once() 로 일꾼을 한 단계씩 돌린다.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.source_repository import SourceRepositoryClient
from app.core.config import Settings
from app.enums import ConnectionErrorCode, DeployRequestStatus, JobStatus
from app.executors import ExecutorSet
from app.executors.base import HealthOk, HealthProbeResult
from app.executors.fake import (
    FakeBuildExecutor,
    FakeDataSyncChecker,
    FakeDeployExecutor,
    FakeMainRouteExecutor,
    FakeMetricsProvider,
    FakeTeardownExecutor,
    FakeTrafficExecutor,
)
from app.models import DeployRequest, Environment, Job, Organization
from app.services.deploy_support import generate_host_suffix
from app.worker.runner import Worker
from tests.github_user_fake import FakeGithubUserClient
from tests.integration.auth_support import Member, org_path
from tests.integration.factories import ENVIRONMENT_CONFIGS, make_environment, secret_ref_for

SessionFactory = async_sessionmaker[AsyncSession]

START_TIME = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, start: datetime = START_TIME) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class ScriptedHealthChecker:
    """대본대로 결과를 돌려주는 헬스체커. 대본이 끝나면 default 를 돌려준다."""

    def __init__(
        self, script: Sequence[HealthProbeResult] = (), default: HealthProbeResult | None = None
    ) -> None:
        self._script = list(script)
        self._default = default or HealthOk(status_code=200)
        self.calls: list[str] = []

    @property
    def simulated(self) -> bool:
        return True

    async def probe(self, url: str, path: str, timeout_seconds: float) -> HealthProbeResult:
        self.calls.append(f"{url}{path}")
        return self._script.pop(0) if self._script else self._default


class ScriptedHostSuffixGenerator:
    """대본대로 주소 suffix 를 돌려주는 생성기. 대본이 끝나면 실제 무작위 생성기를 쓴다."""

    def __init__(self) -> None:
        self._script: list[str] = []
        self.calls = 0

    def queue(self, *suffixes: str) -> None:
        self._script.extend(suffixes)

    def __call__(self) -> str:
        self.calls += 1
        return self._script.pop(0) if self._script else generate_host_suffix()


@dataclass
class FakeExecutors:
    """시험이 고쳐 끼울 수 있는 가짜 실행기 묶음."""

    build: FakeBuildExecutor
    deploy: FakeDeployExecutor
    main_route: FakeMainRouteExecutor
    health: Any
    teardown: FakeTeardownExecutor
    traffic: Any
    metrics: FakeMetricsProvider
    data_sync: FakeDataSyncChecker

    def as_set(self) -> ExecutorSet:
        return ExecutorSet(
            build=self.build,
            deploy=self.deploy,
            main_route=self.main_route,
            health=self.health,
            teardown=self.teardown,
            traffic=self.traffic,
            metrics=self.metrics,
            data_sync=self.data_sync,
        )


def make_fake_executors(clock: FakeClock) -> FakeExecutors:
    traffic = FakeTrafficExecutor(clock)
    return FakeExecutors(
        build=FakeBuildExecutor(clock, 0),
        deploy=FakeDeployExecutor(clock, 0),
        main_route=FakeMainRouteExecutor(clock),
        health=ScriptedHealthChecker(),
        teardown=FakeTeardownExecutor(clock, 0),
        traffic=traffic,
        metrics=FakeMetricsProvider(clock, traffic.weight_of),
        data_sync=FakeDataSyncChecker(),
    )


@dataclass
class DeployHarness:
    client: AsyncClient
    session_factory: SessionFactory
    clock: FakeClock
    settings: Settings
    source_client: SourceRepositoryClient
    executors: FakeExecutors
    organization: Organization
    admin: Member
    github_users: FakeGithubUserClient
    responses: list[str] = field(default_factory=list)
    host_suffixes: ScriptedHostSuffixGenerator = field(default_factory=ScriptedHostSuffixGenerator)

    @property
    def services(self) -> str:
        """이 조직의 서비스 경로 `/api/v1/orgs/{org_id}/services`."""
        return org_path(self.organization.id, "/services")

    @property
    def environments(self) -> str:
        return org_path(self.organization.id, "/environments")

    @property
    def deploy_requests(self) -> str:
        return org_path(self.organization.id, "/deploy-requests")

    def worker(self, owner: str = "worker-1") -> Worker:
        return Worker(
            session_factory=self.session_factory,
            settings=self.settings,
            clock=self.clock,
            executors=self.executors.as_set(),
            source_client=self.source_client,
            owner=owner,
        )

    def use_settings(self, **changes: Any) -> None:
        self.settings = self.settings.model_copy(update=changes)

    def replace_executors(self, **changes: Any) -> None:
        self.executors = replace(self.executors, **changes)

    async def run_until_idle(self, max_runs: int = 400, idle_step: float = 1.0) -> int:
        """열린 할 일이 없을 때까지 일꾼을 돌린다. 집을 게 없으면 시계를 idle_step 씩 앞으로."""
        worker = self.worker()
        runs = 0
        for _ in range(max_runs):
            if await worker.run_once():
                runs += 1
                continue
            if await self.open_job_count() == 0:
                return runs
            self.clock.advance(idle_step)
        raise AssertionError("worker did not become idle")

    async def run_until(
        self, request_id: str, statuses: set[DeployRequestStatus], max_runs: int = 100
    ) -> list[str]:
        """요청이 statuses 중 하나가 될 때까지 돌리고, 지나온 상태를 바뀐 순서대로 돌려준다."""
        worker = self.worker()
        trace = [await self.request_status(request_id)]
        for _ in range(max_runs):
            if trace[-1] in statuses:
                return trace
            if not await worker.run_once():
                self.clock.advance(1.0)
            status = await self.request_status(request_id)
            if status != trace[-1]:
                trace.append(status)
        raise AssertionError(f"request did not reach {statuses}: {trace}")

    async def open_job_count(self) -> int:
        async with self.session_factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)))
            )
        return count or 0

    async def request_status(self, request_id: str) -> str:
        async with self.session_factory() as session:
            request = await session.get_one(DeployRequest, uuid.UUID(request_id))
            return request.status

    async def add_environment(
        self,
        name: str,
        failed: bool = False,
        deleted: bool = False,
        organization: Organization | None = None,
    ) -> Environment:
        """환경 행을 접속 확인이 끝난 상태로 넣는다(failed=True 면 마지막 확인 실패).

        factories 의 이름(aws-seoul 등)이 아니면 aws 설정으로 만든다. 조직을 주지 않으면 이
        도구의 조직(team-echo)에 넣는다.
        """
        organization = organization or self.organization
        if name in ENVIRONMENT_CONFIGS:
            environment = make_environment(name, organization)
        else:
            kind, config = ENVIRONMENT_CONFIGS["aws-seoul"]
            environment = Environment(
                organization_id=organization.id,
                name=name,
                kind=kind,
                config=dict(config),
                secret_ref=secret_ref_for(name, organization.slug),
            )
        environment.record_connection_check(
            self.clock.now(), ConnectionErrorCode.AUTH_FAILED if failed else None
        )
        if deleted:
            environment.mark_as_deleted(self.clock.now())
        async with self.session_factory() as session, session.begin():
            session.add(environment)
        return environment

    async def create_service(
        self, name: str = "shop-api", repo_url: str | None = None, **extra: Any
    ) -> dict[str, Any]:
        body = {"name": name, "repoUrl": repo_url or f"https://github.com/team-echo/{name}"}
        response = await self.client.post(self.services, json={**body, **extra})
        assert response.status_code == 201, response.text
        return self._data(response.text, response.json())

    async def deploy(
        self, service_id: str, environment_id: uuid.UUID | str, **fields: Any
    ) -> dict[str, Any]:
        """배포 요청 202 를 확인한다. branch·commitSha 가 없으면 branch=main 으로 보낸다."""
        if "branch" not in fields and "commitSha" not in fields:
            fields["branch"] = "main"
        response = await self.request_deploy(service_id, environment_id, **fields)
        assert response.status_code == 202, response.text
        return self._data(response.text, response.json())

    async def request_deploy(
        self, service_id: str, environment_id: uuid.UUID | str, **fields: Any
    ) -> Any:
        """본문을 그대로 보낸다(칸이 하나도 없을 때만 branch=main)."""
        body = {"environmentId": str(environment_id), **(fields or {"branch": "main"})}
        response = await self.client.post(
            f"{self.services}/{service_id}/deploy-requests", json=body
        )
        self.responses.append(response.text)
        return response

    async def get_request(self, request_id: str) -> dict[str, Any]:
        response = await self.client.get(f"{self.deploy_requests}/{request_id}")
        assert response.status_code == 200, response.text
        return self._data(response.text, response.json())

    async def get_service(self, service_id: str) -> dict[str, Any]:
        response = await self.client.get(f"{self.services}/{service_id}")
        assert response.status_code == 200, response.text
        return self._data(response.text, response.json())

    async def list_requests(self, service_id: str, **params: Any) -> list[dict[str, Any]]:
        response = await self.client.get(
            f"{self.services}/{service_id}/deploy-requests", params=params
        )
        assert response.status_code == 200, response.text
        items: list[dict[str, Any]] = self._data(response.text, response.json())["items"]
        return items

    def _data(self, text: str, body: dict[str, Any]) -> dict[str, Any]:
        self.responses.append(text)
        data: dict[str, Any] = body["data"]
        return data


def connection_of(service: dict[str, Any], environment_name: str) -> dict[str, Any]:
    return next(
        item for item in service["environments"] if item["environment"]["name"] == environment_name
    )
