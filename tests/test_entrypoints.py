import asyncio
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.adapters import registry
from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.database import get_engine, get_session_factory
from app.dependencies import build_source_repository_client
from app.executors import build_executor_set
from app.main import app
from app.worker.runner import Worker


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


def test_app_registers_service_and_deploy_request_routes() -> None:
    paths = app.openapi()["paths"]

    assert set(paths["/api/v1/services"]) == {"get", "post"}
    assert set(paths["/api/v1/services/{service_id}"]) == {"get", "patch", "delete"}
    assert set(paths["/api/v1/services/{service_id}/deploy-requests"]) == {"get", "post"}
    assert set(paths["/api/v1/deploy-requests/{deploy_request_id}"]) == {"get"}


async def test_worker_run_returns_when_stop_is_already_set() -> None:
    settings = get_settings().model_copy(update={"worker_concurrency": 2})
    clock = SystemClock()
    worker = Worker(
        session_factory=get_session_factory(),
        settings=settings,
        clock=clock,
        executors=build_executor_set(settings, clock),
        source_client=build_source_repository_client(settings, clock),
        owner="test",
    )
    stop = asyncio.Event()
    stop.set()

    await asyncio.wait_for(worker.run(stop), timeout=1)


def test_worker_module_has_entrypoint() -> None:
    from app.worker import __main__ as worker_main

    assert callable(worker_main.main)
