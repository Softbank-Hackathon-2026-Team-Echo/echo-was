"""정확 scope·동의·재처리·실행 직전 거부를 기존 worker까지 검증한다."""

import asyncio
import json
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from app.models import Disaster, MigrationPlan, Organization
from app.services import security_rules
from app.services.live_disaster_feed import LiveDisasterFeedPoller
from tests.integration.auth_support import org_path
from tests.integration.deploy_harness import DeployHarness
from tests.integration.disaster_support import tokyo_serving_seoul_standby
from tests.integration.migration_support import Migration
from tests.integration.security_gate_support import enable_gate, prepare_passing_gate
from tests.test_live_disaster_feed_rules import collection, feature


@pytest.fixture
async def automatic(harness: DeployHarness):
    migration = await tokyo_serving_seoul_standby(harness)
    path = org_path(harness.organization.id, "/disasters/auto-dr/consents")
    response = await harness.client.post(
        path,
        json={
            "serviceId": migration.service_id,
            "sourceEnvironmentId": str(migration.aws.id),
            "targetEnvironmentId": str(migration.gcp.id),
            "expiresAt": (harness.clock.now() + timedelta(minutes=10)).isoformat(),
            "acknowledgeAutomaticExecution": True,
        },
    )
    assert response.status_code == 201, response.text
    consent_id = response.json()["data"]["consentId"]
    harness.use_settings(
        live_disaster_feed_enabled=True, auto_dr_enabled=True, auto_dr_consent_ids=[consent_id]
    )
    yield migration, consent_id
    await migration.author_client.aclose()
    await migration.approver_client.aclose()


def payload(migration: Migration, magnitude=7.2, event_id="test-auto", *, age=0, aliases=None):
    return collection(
        feature(
            event_id,
            magnitude,
            (139.4, 35.2, 10),
            time_ms=int(
                (migration.harness.clock.now() - timedelta(seconds=age)).timestamp() * 1000
            ),
            ids=aliases,
        )
    )


async def poll(migration: Migration, data):
    harness = migration.harness
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data))
    ) as client:
        return await LiveDisasterFeedPoller(
            harness.session_factory,
            harness.clock,
            harness.settings,
            http_client=client,
            tls_checker=harness.tls_checker,
        ).poll_once()


async def plans(migration: Migration):
    async with migration.harness.session_factory() as session:
        return list(await session.scalars(select(MigrationPlan)))


async def reasons(migration: Migration):
    async with migration.harness.session_factory() as session:
        rows = list(await session.scalars(select(Disaster)))
    return [
        value.get("reason")
        for row in rows
        for value in (row.feed_event or {}).get("autoDrAttempts", {}).values()
    ]


@pytest.mark.parametrize("magnitude,count", [(5.9, 0), (6.0, 1), (6.1, 1)])
async def test_organization_magnitude_boundary(automatic, magnitude, count):
    migration, _ = automatic
    await poll(migration, payload(migration, magnitude))
    assert len(await plans(migration)) == count


@pytest.mark.parametrize(
    "case,reason",
    [
        ("feed_off", None),
        ("auto_off", "AUTO_DR_DISABLED"),
        ("no_consent", "CONSENT_MISSING"),
        ("expired", "CONSENT_EXPIRED"),
        ("revoked", "CONSENT_REVOKED"),
        ("policy_version", "POLICY_CHANGED"),
        ("policy_digest", "POLICY_CHANGED"),
        ("old_event", "EVENT_STALE"),
        ("future_event", "EVENT_STALE"),
        ("open_plan", "MIGRATION_PLAN_OPEN"),
        ("scope_changed", "SCOPE_CHANGED"),
        ("db_cutover", "DB_PAIR_NOT_READY"),
    ],
)
async def test_auto_dr_refusals(automatic, case, reason, monkeypatch):
    migration, consent_id = automatic
    harness = migration.harness
    data = payload(migration)
    if case == "feed_off":
        harness.use_settings(live_disaster_feed_enabled=False)
    elif case == "auto_off":
        harness.use_settings(auto_dr_enabled=False)
    elif case == "no_consent":
        harness.use_settings(auto_dr_consent_ids=[])
    elif case == "expired":
        harness.clock.advance(601)
        data = payload(migration)
    elif case == "revoked":
        response = await harness.client.post(
            org_path(harness.organization.id, f"/disasters/auto-dr/consents/{consent_id}/revoke")
        )
        assert response.status_code == 200, response.text
    elif case == "policy_version":
        monkeypatch.setattr(security_rules, "POLICY_VERSION", "changed")
    elif case == "policy_digest":
        monkeypatch.setattr(security_rules, "RULES", ())
    elif case == "old_event":
        data = payload(migration, age=901)
    elif case == "future_event":
        data = payload(migration, age=-1)
    elif case == "open_plan":
        await migration.create_plan()
    elif case == "scope_changed":
        response = await harness.client.patch(
            f"{harness.services}/{migration.service_id}", json={"healthCheckPath": "/changed"}
        )
        assert response.status_code == 200, response.text
    elif case == "db_cutover":
        from app.core.config import DbCutoverExecutorMode

        harness.use_settings(
            migration_db_cutover_on_disaster=True,
            db_cutover_executor_mode=DbCutoverExecutorMode.FAKE,
        )
    await poll(migration, data)
    assert len(await plans(migration)) == (1 if case == "open_plan" else 0)
    if reason:
        assert reason in await reasons(migration)
    assert await migration.weights() == (100, 0)
    assert migration.traffic_keys() == []


@pytest.mark.parametrize("case", ["outside", "same_disaster_zone", "target_risk", "unregistered"])
async def test_target_and_source_scope_safety(automatic, case):
    migration, _ = automatic
    harness = migration.harness
    data = payload(migration)
    if case == "outside":
        data = collection(
            feature(
                "outside", 8, (-150, 0, 10), time_ms=int(harness.clock.now().timestamp() * 1000)
            )
        )
    elif case == "same_disaster_zone":
        async with harness.session_factory() as session, session.begin():
            organization = await session.get_one(Organization, harness.organization.id)
            organization.live_feed_radius_km = 2000
    elif case == "target_risk":
        # 별도 서울 피드 이벤트를 먼저 저장한다(시험 입력, 실제 지진이 아님).
        # 사람의 재난 버튼은 사용하지 않는다.
        from app.enums import DisasterSource
        from app.services.live_disaster_feed import process_feed_event
        from app.services.live_disaster_feed_rules import parse_usgs_feed

        event = parse_usgs_feed(
            collection(
                feature(
                    "seoul-risk",
                    7.2,
                    (127, 37.5, 10),
                    time_ms=int(harness.clock.now().timestamp() * 1000),
                )
            )
        )[0]
        async with harness.session_factory() as session:
            await process_feed_event(session, harness.clock, event, DisasterSource.LIVE_FEED)
    else:
        from app.models import ServiceEnvironment

        async with harness.session_factory() as session, session.begin():
            target = await session.scalar(
                select(ServiceEnvironment).where(
                    ServiceEnvironment.service_id == uuid.UUID(migration.service_id),
                    ServiceEnvironment.environment_id == migration.gcp.id,
                )
            )
            target.current_deployment_id = None
    await poll(migration, data)
    assert not await plans(migration)
    assert await migration.weights() == (100, 0)


async def test_concurrent_events_and_revision_execute_once(automatic):
    migration, _ = automatic
    data = payload(migration)
    await asyncio.gather(poll(migration, data), poll(migration, data))
    await poll(migration, payload(migration, 8, aliases=",test-auto,"))
    assert len(await plans(migration)) == 1
    await migration.harness.run_until_idle()
    await poll(migration, payload(migration, 8))
    assert len(await plans(migration)) == 1
    assert len(migration.traffic_keys()) == 3


async def test_concurrent_aliases_are_one_event(automatic):
    migration, _ = automatic
    await asyncio.gather(
        poll(migration, payload(migration, event_id="id-a", aliases=",id-a,id-b,")),
        poll(migration, payload(migration, event_id="id-b", aliases=",id-a,id-b,")),
    )
    async with migration.harness.session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(Disaster))
    assert count == 1


@pytest.mark.parametrize("case", ["disabled", "expired", "revoked", "policy", "threshold"])
async def test_worker_revalidates_before_any_traffic(automatic, case, monkeypatch):
    migration, consent_id = automatic
    harness = migration.harness
    await poll(migration, payload(migration))
    (plan,) = await plans(migration)
    if case == "disabled":
        harness.use_settings(auto_dr_enabled=False)
    elif case == "expired":
        harness.clock.advance(601)
    elif case == "revoked":
        await harness.client.post(
            org_path(harness.organization.id, f"/disasters/auto-dr/consents/{consent_id}/revoke")
        )
    elif case == "policy":
        monkeypatch.setattr(security_rules, "POLICY_VERSION", "changed")
    else:
        async with harness.session_factory() as session, session.begin():
            organization = await session.get_one(Organization, harness.organization.id)
            organization.live_feed_radius_km = 301
    await harness.run_until_idle()
    result = await migration.plan_row(str(plan.id))
    assert result.status == "FAILED" and result.error_code == "SECURITY_GATE_BLOCKED"
    assert await migration.weights() == (100, 0)
    assert migration.traffic_keys() == []


async def test_real_gate_refuses_and_valid_gate_flows(automatic):
    migration, _ = automatic
    enable_gate(migration)
    await poll(migration, payload(migration))
    (plan,) = await plans(migration)
    assert plan.status == "PENDING_APPROVAL"
    assert "SECURITY_GATE_BLOCKED" in await reasons(migration)
    assert migration.traffic_keys() == []


async def test_valid_security_gate_is_used_by_approval_and_worker(automatic):
    migration, _ = automatic
    await prepare_passing_gate(migration)
    await poll(migration, payload(migration))
    (plan,) = await plans(migration)
    assert plan.status == "APPROVED"
    await migration.harness.run_until_idle()
    assert (await migration.plan_row(str(plan.id))).status == "SUCCEEDED"
    assert (await migration.log_codes(str(plan.id))).count("SECURITY_GATE_PASSED") >= 2


async def test_real_historical_quake_cannot_trigger_current_jobs(automatic):
    migration, _ = automatic
    harness = migration.harness
    raw = Path(__file__).parents[2] / "app/data/disaster_history/tohoku.geojson"
    async with harness.session_factory() as session, session.begin():
        organization = await session.get_one(Organization, harness.organization.id)
        organization.live_feed_radius_km = 600
    await poll(migration, json.loads(raw.read_text()))
    assert not await plans(migration)
    assert "EVENT_STALE" in await reasons(migration)
    assert await harness.open_job_count() == 0
