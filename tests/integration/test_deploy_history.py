"""배포 이력 확장 통합 시험(SPEC api-4 AC-7): 필터·isCurrent·rollbackable·쿼리 수."""

from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.deploy_harness import DeployHarness
from tests.integration.rollback_support import SUFFIX, deploy_and_finish, rollback


async def _history(harness: DeployHarness) -> dict[str, Any]:
    """aws: v1·v2 성공, 실패 1, v1 로 되돌리기 성공, 취소 1·대기 1. gcp: 예비 따라가기."""
    harness.host_suffixes.queue(SUFFIX)
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    gcp = await harness.add_environment("gcp-seoul")
    v1 = await deploy_and_finish(harness, service["id"], aws.id, branch="main")
    gcp_v1 = await deploy_and_finish(harness, service["id"], gcp.id, branch="main")
    v2 = await deploy_and_finish(harness, service["id"], aws.id, branch="release")
    failed = await deploy_and_finish(
        harness, service["id"], aws.id, branch="bad", simulate={"failAt": "VERIFYING"}
    )
    back = await rollback(harness, v1["id"])
    await harness.run_until_idle()
    cancelled = await harness.deploy(service["id"], aws.id, branch="later")
    queued = await harness.deploy(service["id"], aws.id, branch="latest")
    return {
        "service": service,
        "aws": aws,
        "gcp": gcp,
        "v1": v1,
        "gcp_v1": gcp_v1,
        "v2": v2,
        "failed": failed,
        "rollback": back,
        "cancelled": cancelled,
        "queued": queued,
    }


async def test_history_lists_every_attempt_newest_first_with_current_and_rollbackable(
    harness: DeployHarness,
) -> None:
    h = await _history(harness)

    items = await harness.list_requests(h["service"]["id"], size=100)
    by_id = {item["id"]: item for item in items}
    flags = {
        name: (by_id[h[name]["id"]]["isCurrent"], by_id[h[name]["id"]]["rollbackable"])
        for name in ("v1", "v2", "failed", "rollback", "cancelled", "queued")
    }

    assert len(items) == 9
    assert {item["trigger"] for item in items} == {"MANUAL", "STANDBY_SYNC", "ROLLBACK"}
    assert {item["status"] for item in items} == {"SUCCEEDED", "FAILED", "CANCELLED", "QUEUED"}
    created = [(item["stageTimes"]["queuedAt"], item["id"]) for item in items]
    assert created == sorted(created, reverse=True)
    assert by_id[h["rollback"]["id"]]["rollbackOfRequestId"] == h["v1"]["id"]
    assert by_id[h["v1"]["id"]]["rollbackOfRequestId"] is None
    assert flags == {
        "v1": (False, True),
        "v2": (False, True),
        "failed": (False, False),
        "rollback": (True, False),
        "cancelled": (False, False),
        "queued": (False, False),
    }
    syncs = {item["sourceRequestId"]: item for item in items if item["trigger"] == "STANDBY_SYNC"}
    following_rollback = syncs[h["rollback"]["id"]]
    following_v2 = syncs[h["v2"]["id"]]
    assert (following_rollback["isCurrent"], following_rollback["rollbackable"]) == (True, False)
    assert (following_v2["isCurrent"], following_v2["rollbackable"]) == (False, True)
    assert (by_id[h["gcp_v1"]["id"]]["isCurrent"], by_id[h["gcp_v1"]["id"]]["rollbackable"]) == (
        False,
        True,
    )


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        # 예비 따라가기는 v2 성공·되돌리기 성공 때 한 번씩 두 건이다.
        ({"trigger": "ROLLBACK"}, ["rollback"]),
        ({"trigger": ["ROLLBACK", "STANDBY_SYNC"]}, ["rollback", "sync", "sync"]),
        ({"status": "FAILED"}, ["failed"]),
        ({"status": ["CANCELLED", "QUEUED"]}, ["cancelled", "queued"]),
        ({"trigger": "MANUAL", "status": "SUCCEEDED", "env": "aws"}, ["v1", "v2"]),
        ({"status": "SUCCEEDED", "env": "gcp"}, ["gcp_v1", "sync", "sync"]),
    ],
)
async def test_history_filters_match_items_and_total(
    harness: DeployHarness, params: dict[str, Any], expected: list[str]
) -> None:
    h = await _history(harness)
    everything = await harness.list_requests(h["service"]["id"], size=100)
    names = {item["id"]: "sync" for item in everything if item["trigger"] == "STANDBY_SYNC"} | {
        h[name]["id"]: name
        for name in ("v1", "gcp_v1", "v2", "failed", "rollback", "cancelled", "queued")
    }
    query = dict(params)
    if "env" in query:
        query["environmentId"] = str(h[query.pop("env")].id)

    response = await harness.client.get(
        f"{harness.services}/{h['service']['id']}/deploy-requests",
        params={**query, "size": 1},
    )
    items = await harness.list_requests(h["service"]["id"], size=100, **query)

    assert response.status_code == 200
    assert response.json()["data"]["total"] == len(expected)
    assert sorted(names[item["id"]] for item in items) == sorted(expected)


@pytest.mark.parametrize("params", [{"trigger": "DEPLOY"}, {"status": "DONE"}])
async def test_history_unknown_filter_value_returns_422(
    harness: DeployHarness, params: dict[str, str]
) -> None:
    service = await harness.create_service("shop-api")

    response = await harness.client.get(
        f"{harness.services}/{service['id']}/deploy-requests", params=params
    )

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


async def test_history_of_deleting_and_deleted_service_is_not_rollbackable(
    harness: DeployHarness,
) -> None:
    h = await _history(harness)
    deleting = await harness.client.delete(f"{harness.services}/{h['service']['id']}")
    assert deleting.status_code == 202

    while_deleting = await harness.get_request(h["v1"]["id"])
    listed = await harness.list_requests(h["service"]["id"], size=100)
    await harness.run_until_idle()
    after_delete = await harness.get_request(h["v1"]["id"])

    assert while_deleting["rollbackable"] is False
    assert not any(item["rollbackable"] for item in listed)
    assert after_delete["rollbackable"] is False
    assert after_delete["status"] == "SUCCEEDED"


async def _count_statements(engine: AsyncEngine, harness: DeployHarness, url: str) -> int:
    statements: list[str] = []

    def record(*args: Any) -> None:
        statements.append(str(args[2]))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        response = await harness.client.get(url)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    assert response.status_code == 200, response.text
    return len(statements)


async def test_history_page_uses_constant_number_of_queries(harness: DeployHarness) -> None:
    h = await _history(harness)
    engine = harness.session_factory.kw["bind"]
    url = f"{harness.services}/{h['service']['id']}/deploy-requests"

    small = await _count_statements(engine, harness, f"{url}?size=2")
    large = await _count_statements(engine, harness, f"{url}?size=100")
    filtered = await _count_statements(engine, harness, f"{url}?size=100&status=SUCCEEDED")

    assert small == large == filtered
    assert large <= 12
