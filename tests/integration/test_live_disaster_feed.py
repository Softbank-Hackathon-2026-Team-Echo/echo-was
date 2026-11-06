"""실제 재난 피드(API 서버 18): 가짜 HTTP 피드 → 재난 → 대피 제안, 스위치, 시연 주입, 상태 API."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from app.audit.actions import AuditAction, AuditComponent
from app.enums import ActorType, DisasterSource, MigrationPlanMode, MigrationPlanStatus
from app.models import AuditLog, Disaster, DisasterFeedState, MigrationPlan
from app.services.live_disaster_feed import (
    DUPLICATE,
    FEED_HTTP_ERROR,
    LiveDisasterFeedPoller,
    process_feed_event,
)
from app.services.live_disaster_feed_rules import parse_usgs_feed
from tests.integration.auth_support import org_path
from tests.integration.deploy_harness import DeployHarness
from tests.integration.disaster_support import (
    MID_PACIFIC,
    NEAR_TOKYO,
    allow_self_approval,
    disasters_path,
    tokyo_serving_seoul_standby,
)
from tests.integration.migration_support import Migration
from tests.test_live_disaster_feed_rules import HASAKI, collection, feature

FEED_URL = "https://feed.test/all_hour.geojson"
REAL_FEED = collection(
    feature("us7000twg9", 6.1, HASAKI),
    feature("ci41359335", 0.71, (-116.758, 33.585, 12.18), time_ms=1793985751440),
    feature("blast", 2.1, (-110.0, 40.0, 0.0), kind="quarry blast"),
)


class FakeFeed:
    """httpx.MockTransport 로 USGS 피드를 흉내 낸다. 부른 횟수를 센다."""

    def __init__(self, payload: Any = REAL_FEED, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        assert str(request.url) == FEED_URL
        return httpx.Response(self.status_code, json=self.payload)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture
async def tokyo(harness: DeployHarness) -> AsyncIterator[Migration]:
    setup = await tokyo_serving_seoul_standby(harness)
    try:
        yield setup
    finally:
        await setup.author_client.aclose()
        await setup.approver_client.aclose()


def _poller(harness: DeployHarness, feed: FakeFeed) -> LiveDisasterFeedPoller:
    return LiveDisasterFeedPoller(
        harness.session_factory, harness.clock, harness.settings, http_client=feed.client()
    )


def _enable(harness: DeployHarness) -> None:
    harness.use_settings(live_disaster_feed_enabled=True, live_disaster_feed_url=FEED_URL)


async def _disasters(harness: DeployHarness) -> list[Disaster]:
    async with harness.session_factory() as session:
        return list((await session.scalars(select(Disaster).order_by(Disaster.created_at))).all())


async def _count(harness: DeployHarness, model: Any) -> int:
    async with harness.session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _audit(harness: DeployHarness, action: AuditAction) -> list[AuditLog]:
    async with harness.session_factory() as session:
        statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.created_at)
        return list((await session.scalars(statement)).all())


async def _feed_status(harness: DeployHarness) -> dict[str, Any]:
    response = await harness.client.get(disasters_path(harness, "/feed"))
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def _inject(
    migration: Migration, magnitude: float = 7.2, epicenter: dict[str, float] | None = None
) -> httpx.Response:
    return await migration.author_client.post(
        disasters_path(migration.harness, "/feed:inject"),
        json={"magnitude": magnitude, "epicenter": epicenter or NEAR_TOKYO, "place": "demo"},
    )


# --- 스위치 ---


async def test_switch_off_does_nothing(tokyo: Migration) -> None:
    harness = tokyo.harness
    feed = FakeFeed()
    poller = _poller(harness, feed)

    assert harness.settings.live_disaster_feed_enabled is False
    assert await poller.poll_once() is None

    assert feed.calls == 0
    assert await _count(harness, Disaster) == 0
    assert await _count(harness, DisasterFeedState) == 0
    status = await _feed_status(harness)
    assert status["enabled"] is False
    assert status["connectionStatus"] == "DISABLED"
    assert status["ready"] is False
    assert status["threshold"] == {"minMagnitude": 6.0, "radiusKm": 300}


# --- 가짜 HTTP 피드 → 재난 → 제안 ---


async def test_feed_quake_near_tokyo_creates_live_feed_disaster_without_plans(
    tokyo: Migration,
) -> None:
    harness = tokyo.harness
    _enable(harness)
    feed = FakeFeed()

    result = await _poller(harness, feed).poll_once()

    assert result is not None and result.error_code is None
    assert (result.feed_count, result.new_event_count) == (2, 2)  # 발파는 뺀다
    assert feed.calls == 1
    (disaster,) = await _disasters(harness)
    assert result.created_disaster_ids == [disaster.id]
    response = await harness.client.get(disasters_path(harness, f"/{disaster.id}"))
    body = response.json()["data"]
    assert body["source"] == "LIVE_FEED"
    assert body["simulated"] is False
    assert body["createdBy"] is None
    assert body["magnitude"] == 6.1
    assert body["radiusKm"] == 300
    assert body["meetsThreshold"] is True
    assert body["status"] == "ACTIVE"
    assert body["affectedEnvironmentIds"] == [str(tokyo.aws.id)]
    assert body["safeEnvironmentIds"] == [str(tokyo.gcp.id)]
    assert body["plans"] == []  # 제안만: 계획은 사람이 evacuate 로 만든다
    assert body["feedEvent"]["provider"] == "usgs"
    assert body["feedEvent"]["eventId"] == "us7000twg9"
    assert body["feedEvent"]["place"] == "144 km SE of Hasaki, Japan"
    assert body["feedEvent"]["nearestDistanceKm"] == pytest.approx(234, abs=1)
    assert await _count(harness, MigrationPlan) == 0
    (line,) = await _audit(harness, AuditAction.DISASTER_DECLARED)
    assert line.actor_type == ActorType.SYSTEM
    assert line.component == AuditComponent.LIVE_DISASTER_FEED
    assert line.target_id == disaster.id
    assert line.details["feedEventId"] == "usgs:us7000twg9"
    assert line.details["evacuation"] == "SUGGESTED"

    status = await _feed_status(harness)
    assert status["connectionStatus"] == "CONNECTED"
    assert status["ready"] is True
    assert status["lastFeedCount"] == 2
    assert status["lastNewEventCount"] == 2
    assert status["lastErrorCode"] is None
    assert status["lastPolledAt"] is not None


async def test_same_quake_is_processed_once_even_if_id_changes(tokyo: Migration) -> None:
    harness = tokyo.harness
    _enable(harness)
    feed = FakeFeed()
    await _poller(harness, feed).poll_once()

    second = await _poller(harness, feed).poll_once()
    feed.payload = collection(
        feature("us7000zzzz", 6.2, HASAKI, ids=",us7000zzzz,us7000twg9,")  # 대표 id 바뀜
    )
    third = await _poller(harness, feed).poll_once()

    assert second is not None and second.new_event_count == 0
    assert third is not None and third.new_event_count == 0
    assert len(await _disasters(harness)) == 1


async def test_duplicate_quake_in_same_org_is_blocked_by_unique_index(tokyo: Migration) -> None:
    """기억 목록 저장 전에 일꾼이 죽거나 일꾼이 둘이어도 같은 조직에 재난은 하나다."""
    harness = tokyo.harness
    (event,) = [item for item in parse_usgs_feed(REAL_FEED) if item.event_id == "us7000twg9"]

    async with harness.session_factory() as session:
        first = await process_feed_event(session, harness.clock, event, DisasterSource.LIVE_FEED)
    async with harness.session_factory() as session:
        second = await process_feed_event(session, harness.clock, event, DisasterSource.LIVE_FEED)

    assert first[0].disaster_id is not None
    assert (second[0].disaster_id, second[0].reason) == (None, DUPLICATE)
    assert len(await _disasters(harness)) == 1


async def test_far_or_small_quakes_create_nothing(tokyo: Migration) -> None:
    harness = tokyo.harness
    _enable(harness)
    feed = FakeFeed(
        collection(
            feature("far", 7.5, (MID_PACIFIC["lng"], MID_PACIFIC["lat"], 10.0)),
            feature("small", 5.9, HASAKI),
        )
    )

    result = await _poller(harness, feed).poll_once()

    assert result is not None and result.new_event_count == 2
    assert result.created_disaster_ids == []
    assert await _count(harness, Disaster) == 0


async def test_feed_error_is_recorded_and_shown_as_error(tokyo: Migration) -> None:
    harness = tokyo.harness
    _enable(harness)

    result = await _poller(harness, FakeFeed(status_code=503)).poll_once()

    assert result is not None and result.error_code == FEED_HTTP_ERROR
    status = await _feed_status(harness)
    assert status["connectionStatus"] == "ERROR"
    assert status["lastErrorCode"] == FEED_HTTP_ERROR
    assert status["ready"] is False
    assert await _count(harness, Disaster) == 0


async def test_feed_shows_error_when_last_success_is_stale(tokyo: Migration) -> None:
    harness = tokyo.harness
    _enable(harness)
    await _poller(harness, FakeFeed()).poll_once()

    harness.clock.advance(harness.settings.live_disaster_feed_interval_seconds * 3 + 1)

    assert (await _feed_status(harness))["connectionStatus"] == "ERROR"


async def test_org_threshold_settings_change_matching(tokyo: Migration) -> None:
    harness = tokyo.harness
    _enable(harness)
    response = await harness.client.patch(
        org_path(harness.organization.id, "/settings"),
        json={"allowSelfApproval": False, "liveFeedMinMagnitude": 6.5, "liveFeedRadiusKm": 250},
    )
    assert response.status_code == 200, response.text

    result = await _poller(harness, FakeFeed()).poll_once()

    assert result is not None and result.created_disaster_ids == []  # M6.1 < 6.5
    assert (await _feed_status(harness))["threshold"] == {"minMagnitude": 6.5, "radiusKm": 250}
    (line,) = await _audit(harness, AuditAction.ORGANIZATION_SETTINGS_CHANGED)
    assert line.details["liveFeedRadiusKm"] == {"from": 300, "to": 250}


# --- 대피 제안 → 사람이 시작 ---


async def test_evacuate_live_feed_disaster_creates_emergency_plans_via_existing_flow(
    tokyo: Migration,
) -> None:
    harness = tokyo.harness
    _enable(harness)
    await _poller(harness, FakeFeed()).poll_once()
    (disaster,) = await _disasters(harness)

    response = await tokyo.author_client.post(disasters_path(harness, f"/{disaster.id}/evacuate"))

    assert response.status_code == 200, response.text
    (plan,) = response.json()["data"]["plans"]
    assert plan["outcome"] == "PENDING_APPROVAL"
    assert plan["reason"] == "SELF_APPROVAL_DISABLED"
    assert plan["targetEnvironmentId"] == str(tokyo.gcp.id)
    async with harness.session_factory() as session:
        stored = await session.get(MigrationPlan, uuid.UUID(plan["planId"]))
    assert stored is not None
    assert stored.mode == MigrationPlanMode.EMERGENCY
    assert stored.disaster_id == disaster.id
    assert stored.created_by_user_id == tokyo.author.user.id
    (line,) = await _audit(harness, AuditAction.DISASTER_EVACUATION_REQUESTED)
    assert line.actor_type == ActorType.USER
    assert line.details["source"] == "LIVE_FEED"

    again = await tokyo.author_client.post(disasters_path(harness, f"/{disaster.id}/evacuate"))
    (skipped,) = again.json()["data"]["plans"]
    assert (skipped["outcome"], skipped["reason"]) == ("SKIPPED", "MIGRATION_PLAN_OPEN")
    assert await _count(harness, MigrationPlan) == 1


async def test_evacuate_with_self_approval_starts_migration(tokyo: Migration) -> None:
    harness = tokyo.harness
    await allow_self_approval(harness)
    injected = (await _inject(tokyo)).json()["data"]["disaster"]

    response = await tokyo.author_client.post(
        disasters_path(harness, f"/{injected['id']}/evacuate")
    )

    (plan,) = response.json()["data"]["plans"]
    assert plan["outcome"] == "APPROVED"
    assert plan["planStatus"] == MigrationPlanStatus.APPROVED


async def test_evacuate_ended_disaster_is_conflict(tokyo: Migration) -> None:
    harness = tokyo.harness
    injected = (await _inject(tokyo)).json()["data"]["disaster"]
    ended = await tokyo.author_client.post(disasters_path(harness, f"/{injected['id']}/end"))
    assert ended.status_code == 200, ended.text

    response = await tokyo.author_client.post(
        disasters_path(harness, f"/{injected['id']}/evacuate")
    )

    assert response.status_code == 409
    assert response.json()["code"] == "DISASTER_NOT_ACTIVE"


# --- 시연 주입 ---


async def test_inject_flows_through_feed_path_marked_simulated(tokyo: Migration) -> None:
    harness = tokyo.harness
    assert harness.settings.live_disaster_feed_enabled is False  # 스위치와 상관없다

    response = await _inject(tokyo)

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["matched"] is True
    assert data["simulated"] is True
    assert data["reason"] is None
    assert data["feedEventId"].startswith("inject:")
    disaster = data["disaster"]
    assert disaster["source"] == "FEED_INJECTED"
    assert disaster["simulated"] is True
    assert disaster["createdBy"] is None
    assert disaster["feedEvent"]["provider"] == "inject"
    assert disaster["feedEvent"]["place"] == "demo"
    assert disaster["affectedEnvironmentIds"] == [str(tokyo.aws.id)]
    assert disaster["plans"] == []
    (system_line,) = await _audit(harness, AuditAction.DISASTER_DECLARED)
    assert system_line.actor_type == ActorType.SYSTEM
    assert system_line.details["source"] == "FEED_INJECTED"
    (user_line,) = await _audit(harness, AuditAction.DISASTER_FEED_INJECTED)
    assert user_line.actor_type == ActorType.USER
    assert user_line.actor_user_id == tokyo.author.user.id
    assert user_line.target_id == uuid.UUID(disaster["id"])
    # 피드 상태는 바꾸지 않는다.
    assert await _count(harness, DisasterFeedState) == 0
    listed = await harness.client.get(disasters_path(harness), params={"status": "ACTIVE"})
    assert [item["source"] for item in listed.json()["data"]["items"]] == ["FEED_INJECTED"]


async def test_inject_below_threshold_creates_no_disaster(tokyo: Migration) -> None:
    harness = tokyo.harness

    response = await _inject(tokyo, magnitude=5.0)

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert (data["matched"], data["reason"], data["disaster"]) == (False, "BELOW_THRESHOLD", None)
    assert await _count(harness, Disaster) == 0
    (line,) = await _audit(harness, AuditAction.DISASTER_FEED_INJECTED)
    assert line.details["matched"] is False


async def test_inject_only_touches_the_requesting_organization(
    tokyo: Migration, harness: DeployHarness
) -> None:
    response = await _inject(tokyo, epicenter=MID_PACIFIC)

    assert response.json()["data"]["reason"] == "NO_AFFECTED_ENVIRONMENT"
    assert await _count(harness, Disaster) == 0


@pytest.mark.parametrize(
    "body",
    [
        {"magnitude": 11, "epicenter": NEAR_TOKYO},
        {"magnitude": 7.25, "epicenter": NEAR_TOKYO},
        {"magnitude": 7.2, "epicenter": {"lat": 91, "lng": 0}},
        {"magnitude": 7.2},
    ],
)
async def test_inject_validates_body(tokyo: Migration, body: dict[str, Any]) -> None:
    response = await tokyo.author_client.post(
        disasters_path(tokyo.harness, "/feed:inject"), json=body
    )

    assert response.status_code == 422
