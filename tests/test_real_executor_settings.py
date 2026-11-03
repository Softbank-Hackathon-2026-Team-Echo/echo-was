"""실제 실행기 설정·생성 시험(SPEC api-3-2 AC-7)."""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import ExecutorMode, HealthCheckerMode, Settings, SourceClientMode
from app.core.secrets import SecretRef, SecretValue
from app.dependencies import executor_set_holder, get_executor_set
from app.executors import RealExecutorTransports, build_executor_set
from app.executors.fake import FakeBuildExecutor, FakeDeployExecutor, FakeTeardownExecutor
from app.executors.github_actions import GithubActionsBuildExecutor
from app.executors.kubernetes.executors import (
    KubernetesDeployExecutor,
    KubernetesTeardownExecutor,
)
from tests.real_executor_fakes import TestClock

DATABASE_URL = "postgresql+asyncpg://x:y@127.0.0.1:1/z"
REAL = {
    "executor_mode": ExecutorMode.REAL,
    "github_app_id": "123",
    "github_app_private_key_ref": "env:ECHO_SECRET_GITHUB_APP_PRIVATE_KEY",
    "source_client_mode": SourceClientMode.GITHUB,
    "health_checker_mode": HealthCheckerMode.HTTP,
}


@pytest.mark.parametrize(
    "missing",
    ["github_app_id", "github_app_private_key_ref", "source_client_mode", "health_checker_mode"],
)
def test_real_mode_without_required_setting_fails_at_startup(missing: str) -> None:
    values: dict[str, Any] = {key: value for key, value in REAL.items() if key != missing}

    with pytest.raises(ValidationError) as exc_info:
        Settings(database_url=DATABASE_URL, **values)

    assert missing in str(exc_info.value)


def test_real_mode_with_required_settings_is_accepted() -> None:
    settings = Settings(database_url=DATABASE_URL, **REAL)

    assert settings.executor_mode is ExecutorMode.REAL
    assert settings.build_github_repository == "Softbank-Hackathon-2026-Team-Echo/echo-was"
    assert (settings.build_github_workflow, settings.build_github_ref) == (
        "customer-build.yml",
        "main",
    )
    assert (
        settings.onprem_pull_check_seconds,
        settings.onprem_pull_min_remaining_seconds,
        settings.deploy_image_pull_grace_seconds,
        settings.kube_request_timeout_seconds,
    ) == (300, 1800, 30, 15)


def test_default_executor_mode_stays_fake() -> None:
    settings = Settings(database_url=DATABASE_URL)
    executors = build_executor_set(settings, TestClock())

    assert settings.executor_mode is ExecutorMode.FAKE
    assert isinstance(executors.build, FakeBuildExecutor)
    assert isinstance(executors.deploy, FakeDeployExecutor)
    assert isinstance(executors.teardown, FakeTeardownExecutor)
    assert executors.closers == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("image_registry_mirrors", "no-equals-sign"),
        ("image_registry_mirrors", "=only-mirror"),
        ("cloudflare_api_token_ref", "plain-token-value"),
        ("build_github_repository", "not a repo"),
    ],
)
def test_real_executor_settings_reject_invalid_values(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(database_url=DATABASE_URL, **{field: value})


class _ExplodingSecretClient:
    async def get_secret(self, ref: SecretRef) -> SecretValue:
        raise AssertionError("secret lookup during construction")


def _explode(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"network call during construction: {request.url}")


async def test_building_real_executors_has_no_network_or_secret_lookup() -> None:
    settings = Settings(
        database_url=DATABASE_URL,
        cloudflare_api_token_ref="env:ECHO_SECRET_CLOUDFLARE_DNS_TOKEN",
        cloudflare_zone_id="zone",
        **REAL,
    )
    transport = httpx.MockTransport(_explode)

    def no_kube_client(server: str, ca: str | None, timeout: float) -> httpx.AsyncClient:
        raise AssertionError("kube client during construction")

    executors = build_executor_set(
        settings,
        TestClock(),
        _ExplodingSecretClient(),
        transports=RealExecutorTransports(
            github=transport,
            cloud=transport,
            metadata=transport,
            cloudflare=transport,
            kube_http_factory=no_kube_client,
        ),
    )
    await executors.aclose()

    assert isinstance(executors.build, GithubActionsBuildExecutor)
    assert isinstance(executors.deploy, KubernetesDeployExecutor)
    assert isinstance(executors.teardown, KubernetesTeardownExecutor)
    assert len(executors.closers) == 2


async def test_api_process_reuses_one_real_executor_set_and_closes_it() -> None:
    settings = Settings(database_url=DATABASE_URL, **REAL)
    application = FastAPI()
    request = Request({"type": "http", "app": application})
    session_factory: async_sessionmaker[Any] = async_sessionmaker()

    first = await get_executor_set(request, settings, TestClock(), session_factory)
    again = await get_executor_set(request, settings, TestClock(), session_factory)
    fake = await get_executor_set(
        request, Settings(database_url=DATABASE_URL), TestClock(), session_factory
    )
    await executor_set_holder(application.state).aclose()
    rebuilt = await get_executor_set(request, settings, TestClock(), session_factory)

    assert first is again
    assert first is not None and isinstance(first.build, GithubActionsBuildExecutor)
    assert fake is not None and isinstance(fake.build, FakeBuildExecutor)
    assert rebuilt is not first
