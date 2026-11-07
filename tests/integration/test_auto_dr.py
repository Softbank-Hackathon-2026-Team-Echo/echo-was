"""실제 DB·기존 queue/worker, 외부 USGS·트래픽만 격리 전송으로 치환."""

import uuid
from datetime import timedelta

import httpx
from sqlalchemy import select

from app.models import AuditLog, MigrationPlan
from app.services.live_disaster_feed import LiveDisasterFeedPoller
from tests.integration.auth_support import org_path
from tests.integration.deploy_harness import DeployHarness
from tests.integration.disaster_support import tokyo_serving_seoul_standby
from tests.test_live_disaster_feed_rules import collection, feature


async def test_scoped_consent_poll_queue_worker_success(harness: DeployHarness) -> None:
    migration = await tokyo_serving_seoul_standby(harness)
    try:
        path = org_path(harness.organization.id, "/disasters/auto-dr/consents")
        response = await harness.client.post(
            path,
            json={
                "serviceId": migration.service_id,
                "sourceEnvironmentId": str(migration.aws.id),
                "targetEnvironmentId": str(migration.gcp.id),
                "expiresAt": (harness.clock.now() + timedelta(hours=1)).isoformat(),
                "acknowledgeAutomaticExecution": True,
            },
        )
        assert response.status_code == 201, response.text
        consent_id = response.json()["data"]["consentId"]
        harness.use_settings(
            live_disaster_feed_enabled=True, auto_dr_enabled=True, auto_dr_consent_ids=[consent_id]
        )
        payload = collection(
            feature(
                "test-automatic",
                7.2,
                (139.4, 35.2, 10),
                time_ms=int(harness.clock.now().timestamp() * 1000),
            )
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
        ) as client:
            poller = LiveDisasterFeedPoller(
                harness.session_factory, harness.clock, harness.settings, http_client=client
            )
            result = await poller.poll_once()
            assert result and result.error_code is None
        async with harness.session_factory() as session:
            plans = list(await session.scalars(select(MigrationPlan)))
        assert len(plans) == 1
        plan_id = str(plans[0].id)
        assert plans[0].status == "APPROVED"
        assert plans[0].fingerprint["autoDr"]["consentId"] == consent_id
        await harness.run_until_idle()
        plan = await migration.plan_row(plan_id)
        assert plan.status == "SUCCEEDED"
        assert await migration.weights() == (0, 100)
        assert len(migration.traffic_keys()) == 3
        async with harness.session_factory() as session:
            logs = list(
                await session.scalars(
                    select(AuditLog).where(AuditLog.target_id == uuid.UUID(plan_id))
                )
            )
        assert all(row.actor_type == "SYSTEM" for row in logs)
        assert not harness.settings.migration_db_cutover_on_disaster
    finally:
        await migration.author_client.aclose()
        await migration.approver_client.aclose()
