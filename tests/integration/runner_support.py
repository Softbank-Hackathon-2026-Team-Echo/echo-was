"""AI 러너 통합 시험 도구: 러너 행·토큰, 러너·실행 토큰 클라이언트, 사전 점검 만들기·결과 제출.

migration 픽스처(shop-api: aws-seoul 서비스 중 + gcp-seoul 예비, 개발자 A·B) 위에서 쓴다.
"""

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from httpx import AsyncClient, Response
from sqlalchemy import select

from app.auth.tokens import hash_token, new_runner_token
from app.models import AgentRun, AgentRunner, AuditLog
from tests.integration.auth_support import SessionFactory, make_client, org_path
from tests.integration.migration_support import Migration

RUNNER_NAME = "echo-sandbox-runner"
CLAIM_PATH = "/api/v1/runner/runs:claim"


@dataclass(frozen=True)
class RunnerFixture:
    runner: AgentRunner
    token: str

    def client(self) -> AsyncClient:
        return make_client(agent_token=self.token, origin=None)


async def add_runner(
    session_factory: SessionFactory, name: str = RUNNER_NAME, revoked_at: datetime | None = None
) -> RunnerFixture:
    token = new_runner_token().get_secret_value()
    runner = AgentRunner(name=name, token_hash=hash_token(token), revoked_at=revoked_at)
    async with session_factory() as session, session.begin():
        session.add(runner)
    return RunnerFixture(runner, token)


def prechecks_path(migration: Migration, suffix: str = "") -> str:
    return org_path(migration.harness.organization.id, f"/prechecks{suffix}")


async def request_precheck(
    migration: Migration, client: AsyncClient | None = None, **changes: Any
) -> Response:
    body = {"serviceId": migration.service_id, "targetEnvironmentId": str(migration.gcp.id)}
    response = await (client or migration.author_client).post(
        prechecks_path(migration), json=body | changes
    )
    migration.harness.responses.append(response.text)
    return response


async def create_precheck(migration: Migration) -> dict[str, Any]:
    response = await request_precheck(migration)
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def claim(
    runner: RunnerFixture, capabilities: list[str] | None = None, runner_id: str | None = None
) -> Response:
    async with runner.client() as client:
        return await client.post(
            CLAIM_PATH,
            json={
                "runnerId": runner_id if runner_id is not None else runner.runner.name,
                "capabilities": ["PRECHECK"] if capabilities is None else capabilities,
            },
        )


@dataclass(frozen=True)
class ClaimedRunFixture:
    run_id: str
    run_token: str
    run: dict[str, Any]

    def path(self, action: str, run_id: str | None = None) -> str:
        return f"/api/v1/runner/runs/{run_id or self.run_id}/{action}"

    @asynccontextmanager
    async def client(self, token: str | None = None) -> AsyncIterator[AsyncClient]:
        async with make_client(agent_token=token or self.run_token, origin=None) as client:
            yield client

    async def post(self, action: str, body: Any | None = None) -> Response:
        async with self.client() as client:
            return await client.post(self.path(action), json={} if body is None else body)

    async def post_raw(self, action: str, content: bytes) -> Response:
        async with self.client() as client:
            return await client.post(
                self.path(action),
                content=content,
                headers={"Content-Type": "application/json"},
            )


async def claim_run(runner: RunnerFixture) -> ClaimedRunFixture:
    response = await claim(runner)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["run"] is not None
    return ClaimedRunFixture(data["run"]["id"], data["runToken"], data["run"])


def event(seq: int, step: str = "SANDBOX_CREATE", **changes: Any) -> dict[str, Any]:
    body = {
        "seq": seq,
        "at": "2026-11-05T00:00:00Z",
        "step": step,
        "level": "INFO",
        "message": f"{step.lower()} done",
    }
    return body | changes


def run_result(migration: Migration, **changes: Any) -> dict[str, Any]:
    """규칙 비교만 한 결과: 모델 없음, 설정 차이 1건 + 경고 1건."""
    body: dict[str, Any] = {
        "status": "SUCCEEDED",
        "failureCode": None,
        "simulated": False,
        "sandbox": {
            "id": "echo-ws-1",
            "startedAt": "2026-11-05T00:00:00Z",
            "finishedAt": "2026-11-05T00:01:00Z",
            "network": "none",
            "deletedAt": "2026-11-05T00:01:01Z",
        },
        "model": None,
        "configDiff": [
            {
                "key": "region",
                "source": "ap-northeast-2",
                "target": "asia-northeast3",
                "origin": "RULE",
                "severity": "INFO",
            }
        ],
        "featureGaps": [],
        "findings": [
            {
                "control": "config-diff",
                "environmentId": str(migration.gcp.id),
                "result": "WARN",
                "title": "environment config differs",
                "reason": "region differs between source and target",
                "origin": "RULE",
                "suggestedFix": None,
            }
        ],
        "patches": [],
        "blockedAttempts": [],
    }
    return body | changes


PASSED_PATCH_CHECK = {"name": "TESTS", "result": "PASS", "summary": "19 passed"}


def run_patch(patch_id: str = "p1", **changes: Any) -> dict[str, Any]:
    """검사를 통과한 패치(PROPOSED). patchHash 는 id 로 정해지고, origin 은 주지 않으면 빠진다."""
    body: dict[str, Any] = {
        "id": patch_id,
        "patchHash": f"sha256:{hashlib.sha256(patch_id.encode()).hexdigest()}",
        "files": ["scenarios/demo-app/Dockerfile"],
        "diff": "--- a/Dockerfile\n+++ b/Dockerfile\n@@ -1 +1 @@\n-FROM a\n+FROM b\n",
        "reason": "base image differs",
        "touchesProtectedFiles": False,
        "checks": [PASSED_PATCH_CHECK],
    }
    return body | changes


async def run_row(migration: Migration, run_id: str) -> AgentRun:
    async with migration.harness.session_factory() as session:
        row = await session.get(AgentRun, run_id)
    assert row is not None
    return row


async def run_audit_rows(migration: Migration, run_id: str) -> list[AuditLog]:
    async with migration.harness.session_factory() as session:
        rows = await session.scalars(
            select(AuditLog).where(AuditLog.target_id == run_id).order_by(AuditLog.seq)
        )
        return list(rows.all())


async def finish_precheck(
    migration: Migration, runner: RunnerFixture, **result: Any
) -> dict[str, Any]:
    """만들기 → 가져가기 → 이벤트 → 결과. 만든 실행의 id 를 담은 상세를 돌려준다."""
    created = await create_precheck(migration)
    claimed = await claim_run(runner)
    assert claimed.run_id == created["id"]
    response = await claimed.post(
        "events",
        {"events": [event(1, "SANDBOX_CREATE"), event(2, "INPUT_LOAD"), event(3, "CONFIG_DIFF")]},
    )
    assert response.status_code == 200, response.text
    response = await claimed.post("result", run_result(migration, **result))
    assert response.status_code == 200, response.text
    detail = await migration.author_client.get(prechecks_path(migration, f"/{created['id']}"))
    assert detail.status_code == 200, detail.text
    data: dict[str, Any] = detail.json()["data"]
    return data
