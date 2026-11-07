from sqlalchemy import func, select

from app.models import AuditLog, Disaster, Job, MigrationPlan
from tests.integration.auth_support import org_path
from tests.integration.deploy_harness import DeployHarness


async def test_history_explicit_demo_only_has_no_current_side_effects(
    harness: DeployHarness,
) -> None:
    path = org_path(harness.organization.id, "/disasters/history")
    response = await harness.client.get(path)
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "enabled": False,
        "isSimulated": True,
        "source": "DEMO",
        "items": [],
    }
    harness.use_settings(disaster_history_demo_enabled=True)
    async with harness.session_factory() as session:
        before = [
            await session.scalar(select(func.count()).select_from(model))
            for model in (AuditLog, Disaster, Job, MigrationPlan)
        ]
    for _ in range(3):
        response = await harness.client.get(path)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["isSimulated"] and data["source"] == "DEMO"
        assert len(data["items"]) == 2
        for item in data["items"]:
            assert item["isSimulated"] and item["source"] == "DEMO"
            assert item["event"]["originSource"] == "REAL_HISTORICAL"
            assert item["scenarioLabel"].startswith("시연:")
            assert item["collectedAt"] and item["occurredAt"]
            assert item["event"]["rawSha256"].startswith("sha256:")
            assert item["radiusSource"] == "DEMO_ASSUMPTION"
    async with harness.session_factory() as session:
        after = [
            await session.scalar(select(func.count()).select_from(model))
            for model in (AuditLog, Disaster, Job, MigrationPlan)
        ]
    assert before == after
    assert await harness.open_job_count() == 0
