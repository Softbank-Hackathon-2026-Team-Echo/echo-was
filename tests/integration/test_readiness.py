from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.database import get_engine
from app.main import app


async def test_check_readiness_database_reachable_returns_no_content(engine: AsyncEngine) -> None:
    app.dependency_overrides[get_engine] = lambda: engine
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            readiness = await client.get("/readyz")
            health = await client.get("/healthz")
    finally:
        app.dependency_overrides.clear()

    assert readiness.status_code == 204
    assert health.status_code == 204
