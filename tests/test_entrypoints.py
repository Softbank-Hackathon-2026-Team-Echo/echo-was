from pathlib import Path

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.adapters import registry
from app.core.database import get_engine
from app.main import app


async def test_check_health_returns_no_content() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 204


async def test_check_readiness_database_unreachable_returns_503() -> None:
    unreachable = create_async_engine("postgresql+asyncpg://u:p@127.0.0.1:1/db")
    app.dependency_overrides[get_engine] = lambda: unreachable
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/readyz")
    finally:
        app.dependency_overrides.clear()
        await unreachable.dispose()

    assert response.status_code == 503


def test_app_title_is_echo_control_api() -> None:
    assert app.title == "Echo Control API"


def test_app_module_discovers_environment_kinds_on_load() -> None:
    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()

    assert "\ndiscover()\n" in source
    assert registry.DEFAULT_PACKAGE in registry._discovered_packages


def test_app_registers_environment_routes() -> None:
    paths = set(app.openapi()["paths"])

    assert {
        "/api/v1/environments",
        "/api/v1/environments/{environment_id}",
        "/api/v1/environments/{environment_id}/connection-check",
        "/api/v1/environment-kinds",
    } <= paths
