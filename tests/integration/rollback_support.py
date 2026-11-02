"""되돌리기·로그 통합 시험 도구."""

import uuid
from typing import Any

from httpx import Response
from sqlalchemy import select

from app.models import DeployRequestLog
from tests.integration.deploy_harness import DeployHarness

SUFFIX = "a1b2c3d4"


async def deploy_and_finish(
    harness: DeployHarness, service_id: str, environment_id: uuid.UUID | str, **ref: Any
) -> dict[str, Any]:
    accepted = await harness.deploy(service_id, environment_id, **ref)
    await harness.run_until_idle()
    return await harness.get_request(accepted["id"])


async def request_rollback(harness: DeployHarness, request_id: str, **body: Any) -> Response:
    """되돌리기 접수. body 가 없으면 본문 없이 보낸다."""
    url = f"{harness.deploy_requests}/{request_id}/rollback"
    response = await harness.client.post(url, json=body) if body else await harness.client.post(url)
    harness.responses.append(response.text)
    return response


async def rollback(harness: DeployHarness, request_id: str, **body: Any) -> dict[str, Any]:
    response = await request_rollback(harness, request_id, **body)
    assert response.status_code == 202, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def get_logs(harness: DeployHarness, request_id: str, **params: Any) -> Response:
    response = await harness.client.get(
        f"{harness.deploy_requests}/{request_id}/logs", params=params
    )
    harness.responses.append(response.text)
    return response


async def read_all_logs(
    harness: DeployHarness, request_id: str, source: str, limit: int = 200
) -> list[dict[str, Any]]:
    """cursor 로 끝까지 이어 읽은 줄들."""
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(1000):
        params: dict[str, Any] = {"source": source, "limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        response = await get_logs(harness, request_id, **params)
        assert response.status_code == 200, response.text
        page = response.json()["data"]
        items += page["items"]
        cursor = page["nextCursor"]
        if not page["hasMore"]:
            return items
    raise AssertionError("log paging did not end")


async def log_rows(harness: DeployHarness, request_id: str) -> list[DeployRequestLog]:
    async with harness.session_factory() as session:
        return list(
            (
                await session.scalars(
                    select(DeployRequestLog)
                    .where(DeployRequestLog.deploy_request_id == uuid.UUID(request_id))
                    .order_by(DeployRequestLog.seq)
                )
            ).all()
        )


async def log_codes(harness: DeployHarness, request_id: str) -> list[str]:
    return [row.code for row in await log_rows(harness, request_id)]


async def create_rollback_fixture(harness: DeployHarness) -> dict[str, Any]:
    """aws 에 v1·v2 를 성공시키고 v1 로 되돌리기를 접수한다(QUEUED 상태로 돌려준다)."""
    harness.host_suffixes.queue(SUFFIX)
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    v1 = await deploy_and_finish(harness, service["id"], aws.id, branch="main")
    await deploy_and_finish(harness, service["id"], aws.id, branch="release")
    return await rollback(harness, v1["id"])
