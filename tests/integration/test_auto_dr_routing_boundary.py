"""미병합 서비스별 HTTP 라우팅을 운영에서 실수로 사용하지 않는다."""

from app.core.config import EntrypointExecutorMode
from tests.integration.test_auto_dr_safety import automatic as automatic  # noqa: F401
from tests.integration.test_auto_dr_safety import payload, plans, poll, reasons


async def test_unscoped_http_traffic_is_not_authorized(automatic):
    migration, _ = automatic
    migration.harness.use_settings(entrypoint_executor_mode=EntrypointExecutorMode.HTTP)
    await poll(migration, payload(migration))
    assert not await plans(migration)
    assert "SERVICE_SCOPED_ROUTING_NOT_READY" in await reasons(migration)
    assert migration.traffic_keys() == []
