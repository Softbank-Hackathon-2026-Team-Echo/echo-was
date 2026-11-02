"""가짜 실행기가 공통 계약을 지키는지, 실패 주입이 동작하는지 시험한다."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import ExecutorMode, HealthCheckerMode, Settings
from app.executors import build_executor_set
from app.executors.base import (
    BuildFailed,
    BuildSpec,
    BuildSucceeded,
    DeployFailed,
    HealthOk,
    MainRoute,
    RevertFailed,
    RouteApplied,
    SourceAccess,
    TeardownFailed,
)
from app.executors.fake import (
    LOG_LINE_INTERVAL,
    FakeBuildExecutor,
    FakeDeployExecutor,
    FakeHealthChecker,
    FakeMainRouteExecutor,
    FakeTeardownExecutor,
)
from app.executors.http_health_checker import HttpHealthChecker
from tests.executor_contract import (
    COMMIT_SHA,
    DEPLOYMENT,
    TARGET,
    assert_build_executor_contract,
    assert_build_log_contract,
    assert_deploy_executor_contract,
    assert_runtime_log_contract,
    assert_teardown_executor_contract,
)

STEP_SECONDS = 2.0


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def finish(self) -> None:
        self.value += timedelta(seconds=STEP_SECONDS)


async def test_fake_build_executor_satisfies_contract() -> None:
    clock = _Clock()
    await assert_build_executor_contract(FakeBuildExecutor(clock, STEP_SECONDS), clock.finish)


async def test_fake_deploy_executor_satisfies_contract() -> None:
    clock = _Clock()
    await assert_deploy_executor_contract(FakeDeployExecutor(clock, STEP_SECONDS), clock.finish)


async def test_fake_teardown_executor_satisfies_contract() -> None:
    clock = _Clock()
    await assert_teardown_executor_contract(FakeTeardownExecutor(clock, STEP_SECONDS), clock.finish)


async def test_fake_build_digest_is_sha256_of_service_and_commit() -> None:
    clock = _Clock()
    executor = FakeBuildExecutor(clock, 0)
    build = BuildSpec(uuid.uuid4(), TARGET.service_id, "shop-api", "https://x", COMMIT_SHA)

    result = await executor.poll(
        await executor.start(build, SourceAccess("https://x", COMMIT_SHA), "k")
    )

    expected = hashlib.sha256(f"{TARGET.service_id}{COMMIT_SHA}".encode()).hexdigest()
    assert result == BuildSucceeded("registry.echo.test/shop-api", f"sha256:{expected}")


async def test_fake_external_ref_is_readable_by_another_instance() -> None:
    clock = _Clock()
    build = BuildSpec(uuid.uuid4(), TARGET.service_id, "shop-api", "https://x", COMMIT_SHA)
    external_ref = await FakeBuildExecutor(clock, 0).start(
        build, SourceAccess("https://x", COMMIT_SHA), "k"
    )

    result = await FakeBuildExecutor(clock, 0).poll(external_ref)

    assert isinstance(result, BuildSucceeded)


async def test_injected_failures_return_stage_codes() -> None:
    clock = _Clock()
    build = BuildSpec(uuid.uuid4(), TARGET.service_id, "shop-api", "https://x", COMMIT_SHA)
    builder = FakeBuildExecutor(clock, 0, fail_commits={COMMIT_SHA})
    deployer = FakeDeployExecutor(
        clock, 0, fail_service_environment_ids={TARGET.service_environment_id}, fail_revert=True
    )
    teardown = FakeTeardownExecutor(
        clock, 0, fail_service_environment_ids={TARGET.service_environment_id}
    )

    build_result = await builder.poll(
        await builder.start(build, SourceAccess("https://x", COMMIT_SHA), "b")
    )
    deploy_result = await deployer.poll(await deployer.apply(DEPLOYMENT, TARGET, "d"))
    revert_result = await deployer.revert(TARGET, None, "d")
    teardown_result = await teardown.poll(await teardown.remove(TARGET, "t"))

    assert build_result == BuildFailed()
    assert deploy_result == DeployFailed()
    assert isinstance(revert_result, RevertFailed)
    assert teardown_result == TeardownFailed()


async def test_fake_main_route_and_health_checker_succeed() -> None:
    clock = _Clock()
    route = FakeMainRouteExecutor(clock)

    applied = await route.route_main(
        MainRoute(TARGET.service_id, "shop-api", "shop-api.apps.echo.test"), TARGET, "key"
    )
    health = await FakeHealthChecker().probe("https://x", "/healthz", 5)

    assert applied == RouteApplied(applied_at=clock.now())
    assert health == HealthOk(status_code=200)
    assert route.simulated and FakeHealthChecker().simulated


def _settings(**changes: object) -> Settings:
    return Settings(database_url="postgresql+asyncpg://x:y@127.0.0.1:1/z").model_copy(
        update=changes
    )


def test_build_executor_set_uses_http_health_checker_when_configured() -> None:
    clock = _Clock()

    fake = build_executor_set(_settings(), clock)
    http = build_executor_set(_settings(health_checker_mode=HealthCheckerMode.HTTP), clock)

    assert isinstance(fake.health, FakeHealthChecker)
    assert isinstance(http.health, HttpHealthChecker)
    assert all(
        executor.simulated for executor in (fake.build, fake.deploy, fake.main_route, fake.teardown)
    )


def test_build_executor_set_rejects_real_mode_until_implemented() -> None:
    with pytest.raises(NotImplementedError):
        build_executor_set(_settings(executor_mode=ExecutorMode.REAL), _Clock())


# --- 로그 읽기 계약(SPEC api-4 AC-14) ---


async def test_fake_build_log_reader_satisfies_contract() -> None:
    clock = _Clock()

    lines = await assert_build_log_contract(FakeBuildExecutor(clock, STEP_SECONDS))

    assert 5 <= len(lines) <= 8
    assert lines[0].at == clock.now()
    assert lines[1].at - lines[0].at == LOG_LINE_INTERVAL


async def test_fake_runtime_log_reader_satisfies_contract() -> None:
    lines = await assert_runtime_log_contract(FakeDeployExecutor(_Clock(), STEP_SECONDS))

    assert 5 <= len(lines) <= 8
    assert any(DEPLOYMENT.image_digest in line.message for line in lines)


async def test_fake_logs_do_not_depend_on_clock_or_instance() -> None:
    clock = _Clock()
    build = BuildSpec(uuid.uuid4(), TARGET.service_id, "shop-api", "https://x", COMMIT_SHA)
    external_ref = await FakeBuildExecutor(clock, 0).start(
        build, SourceAccess("https://x", COMMIT_SHA), "k"
    )

    before = await FakeBuildExecutor(clock, 0).read_logs(external_ref, None, 100)
    clock.value += timedelta(hours=5)
    after = await FakeBuildExecutor(clock, 0).read_logs(external_ref, None, 100)

    assert before == after


async def test_fake_log_injection_hook_inserts_raw_lines_after_first_line() -> None:
    clock = _Clock()
    builder = FakeBuildExecutor(clock, 0, log_injections=["ghs_injected_for_test"])
    build = BuildSpec(uuid.uuid4(), TARGET.service_id, "shop-api", "https://x", COMMIT_SHA)
    external_ref = await builder.start(build, SourceAccess("https://x", COMMIT_SHA), "k")

    chunk = await builder.read_logs(external_ref, None, 100)

    assert chunk.lines[1].message == "ghs_injected_for_test"


async def test_fake_failed_build_and_runtime_logs_end_with_error_line() -> None:
    clock = _Clock()
    builder = FakeBuildExecutor(clock, 0, fail_commits={COMMIT_SHA})
    deployer = FakeDeployExecutor(
        clock, 0, fail_service_environment_ids={TARGET.service_environment_id}
    )
    build = BuildSpec(uuid.uuid4(), TARGET.service_id, "shop-api", "https://x", COMMIT_SHA)

    build_logs = await builder.read_logs(
        await builder.start(build, SourceAccess("https://x", COMMIT_SHA), "b"), None, 100
    )
    runtime_logs = await deployer.read_runtime_logs(
        TARGET, DEPLOYMENT, await deployer.apply(DEPLOYMENT, TARGET, "d"), None, 100
    )

    assert build_logs.lines[-1].level == "ERROR"
    assert runtime_logs.lines[-1].level == "ERROR"
