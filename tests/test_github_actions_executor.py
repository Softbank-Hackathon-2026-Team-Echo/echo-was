"""실제 빌드 실행기(GitHub Actions) 시험(SPEC api-3-2 AC-1·AC-2). 가짜 GitHub 전송만 쓴다."""

import logging
import uuid
from collections.abc import Callable
from datetime import timedelta

import httpx
import pytest

from app.core.secrets import parse_secret_ref
from app.enums import DeployRequestErrorCode, LogLevel
from app.executors.base import (
    BuildFailed,
    BuildRunning,
    BuildSpec,
    BuildStartError,
    BuildSucceeded,
    ExecutorUnavailableError,
    SourceAccess,
)
from app.executors.github_actions import GithubActionsBuildExecutor
from tests.executor_contract import (
    COMMIT_SHA,
    assert_build_executor_contract,
    assert_build_log_contract,
)
from tests.github_fake import APP_ID, TEST_APP_PRIVATE_KEY_PEM
from tests.real_executor_fakes import (
    ACTIONS_TOKEN,
    API_BASE,
    AR_REPOSITORY,
    BUILD_REPOSITORY,
    DIGEST,
    DictSecretClient,
    FakeActions,
    RecordingSleep,
    TestClock,
    result_zip,
)

KEY_ENV = "ECHO_SECRET_GITHUB_APP_PRIVATE_KEY"
# Actions 가 가리지 못한 토큰 모양 줄(실행기가 한 번 더 가린다).
LEAKED_TOKEN = "ghs_" + "Q1w2E3r4T5y6U7i8O9p0" * 2


def _executor(
    actions: FakeActions, clock: TestClock, sleep: RecordingSleep | None = None
) -> GithubActionsBuildExecutor:
    return GithubActionsBuildExecutor(
        api_base_url=API_BASE,
        repository=BUILD_REPOSITORY,
        workflow="customer-build.yml",
        ref="main",
        app_id=APP_ID,
        private_key_ref=parse_secret_ref(f"env:{KEY_ENV}"),
        secret_client=DictSecretClient({KEY_ENV: TEST_APP_PRIVATE_KEY_PEM}),
        clock=clock,
        transport=actions.transport(),
        sleep=sleep or RecordingSleep(),
    )


def _build(**changes: object) -> BuildSpec:
    values: dict[str, object] = {
        "build_id": uuid.uuid4(),
        "service_id": uuid.uuid4(),
        "service_name": "shop-api",
        "repo_url": "https://github.com/team-echo/shop-api",
        "commit_sha": COMMIT_SHA,
    }
    values.update(changes)
    return BuildSpec(**values)  # type: ignore[arg-type]


def _source(build: BuildSpec) -> SourceAccess:
    return SourceAccess(build.repo_url, build.commit_sha)


# --- AC-1 공통 계약 ---


async def test_github_actions_build_executor_satisfies_contract() -> None:
    clock = TestClock()
    actions = FakeActions(clock)

    await assert_build_executor_contract(_executor(actions, clock), actions.finish_all)


async def test_github_actions_build_log_reader_satisfies_contract() -> None:
    clock = TestClock()
    actions = FakeActions(clock)
    executor = _executor(actions, clock)
    original_add_run = actions.add_run

    def add_finished_run(build_id: str, **fields: object) -> object:
        run = original_add_run(build_id, **fields)  # type: ignore[arg-type]
        run.log = (
            "2026-11-03T06:00:01.1234567Z ##[group]checkout\n"
            f"2026-11-03T06:00:02.0000000Z Fetching with {LEAKED_TOKEN}\n"
            "2026-11-03T06:00:03.5Z build step\n"
            "continued line without time\n"
            "2026-11-03T06:00:04Z ##[error]push failed\n"
        )
        actions.finish(run)
        return run

    actions.add_run = add_finished_run  # type: ignore[method-assign]

    lines = await assert_build_log_contract(executor)

    assert len(lines) == 5
    assert lines[-1].level is LogLevel.ERROR and lines[-1].message == "push failed"
    assert lines[3].at == lines[2].at  # 시각 없는 줄은 앞 줄 시각
    assert all(ACTIONS_TOKEN not in line.message for line in lines)
    assert all(LEAKED_TOKEN not in line.message for line in lines)
    assert lines[1].message == "Fetching with ***"


# --- AC-2 시작: 멱등·찾기·dispatch 본문 ---


async def test_start_twice_with_same_key_dispatches_once_and_finds_by_display_title() -> None:
    clock = TestClock()
    actions = FakeActions(clock)
    executor = _executor(actions, clock)
    build = _build(repo_private=True)
    key = str(build.build_id)

    first = await executor.start(build, _source(build), key)
    again = await executor.start(build, _source(build), key)

    assert first == again == f"gha:{next(iter(actions.runs))}"
    assert len(actions.dispatches) == 1
    assert actions.dispatches[0] == {
        "ref": "main",
        "inputs": {
            "build_id": key,
            "repo": "team-echo/shop-api",
            "commit_sha": COMMIT_SHA,
            "private": "true",
        },
        "return_run_details": True,
    }
    # 실행 객체의 name 은 워크플로 이름이라 다르다. 찾기는 display_title 로 했다.
    run = next(iter(actions.runs.values())).as_json()
    assert run["name"] != run["display_title"] == f"build {key}"


async def test_start_requests_actions_write_installation_token_once() -> None:
    clock = TestClock()
    actions = FakeActions(clock)
    executor = _executor(actions, clock)
    build = _build()

    await executor.start(build, _source(build), str(build.build_id))
    await executor.poll(f"gha:{next(iter(actions.runs))}")

    assert actions.token_requests == [
        {"repositories": ["echo-was"], "permissions": {"actions": "write"}}
    ]


async def test_resumed_start_finds_late_run_without_dispatch() -> None:
    clock = TestClock()
    actions = FakeActions(clock)
    sleep = RecordingSleep()
    executor = _executor(actions, clock, sleep)
    build = _build(resumed_after=clock.now())
    run = actions.add_run(str(build.build_id), visible_after_list_calls=2)

    external_ref = await executor.start(build, _source(build), str(build.build_id))

    assert external_ref == f"gha:{run.id}"
    assert actions.dispatches == []
    assert sleep.delays == [5.0, 5.0]


async def test_resumed_start_ignores_runs_created_long_before_request() -> None:
    clock = TestClock()
    actions = FakeActions(clock)
    sleep = RecordingSleep()
    executor = _executor(actions, clock, sleep)
    build = _build()
    actions.add_run(str(build.build_id))
    clock.advance(600)
    resumed = _build(build_id=build.build_id, resumed_after=clock.now())

    await executor.start(resumed, _source(resumed), str(build.build_id))

    assert len(actions.dispatches) == 1
    assert sleep.delays == [5.0, 5.0, 5.0]


async def test_dispatch_204_finds_run_in_list() -> None:
    clock = TestClock()
    actions = FakeActions(clock, dispatch_status=204, new_run_visible_after_list_calls=1)
    sleep = RecordingSleep()
    executor = _executor(actions, clock, sleep)
    build = _build()

    external_ref = await executor.start(build, _source(build), str(build.build_id))

    assert external_ref == f"gha:{next(iter(actions.runs))}"
    assert sleep.delays == [5.0]


@pytest.mark.parametrize("status", [403, 404, 422])
async def test_dispatch_rejection_raises_build_dispatch_failed(status: int) -> None:
    clock = TestClock()
    actions = FakeActions(clock, dispatch_status=status)
    build = _build()

    with pytest.raises(BuildStartError) as exc_info:
        await _executor(actions, clock).start(build, _source(build), str(build.build_id))

    assert exc_info.value.code is DeployRequestErrorCode.BUILD_DISPATCH_FAILED


async def test_missing_installation_raises_build_dispatch_failed() -> None:
    clock = TestClock()
    actions = FakeActions(clock)
    executor = GithubActionsBuildExecutor(
        api_base_url=API_BASE,
        repository="team-echo/not-installed",
        workflow="customer-build.yml",
        ref="main",
        app_id=APP_ID,
        private_key_ref=parse_secret_ref(f"env:{KEY_ENV}"),
        secret_client=DictSecretClient({KEY_ENV: TEST_APP_PRIVATE_KEY_PEM}),
        clock=clock,
        transport=actions.transport(),
        sleep=RecordingSleep(),
    )
    build = _build()

    with pytest.raises(BuildStartError):
        await executor.start(build, _source(build), str(build.build_id))


async def test_dispatch_server_errors_retry_three_times_then_raise() -> None:
    clock = TestClock()
    actions = FakeActions(clock, dispatch_status=503)
    sleep = RecordingSleep()
    build = _build()

    with pytest.raises(ExecutorUnavailableError):
        await _executor(actions, clock, sleep).start(build, _source(build), str(build.build_id))

    assert len(actions.dispatches) == 4
    assert sleep.delays == [1.0, 2.0, 4.0]


async def test_start_never_puts_tokens_in_ref_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    clock = TestClock()
    actions = FakeActions(clock)
    build = _build()
    source = SourceAccess(build.repo_url, build.commit_sha)

    with caplog.at_level(logging.DEBUG):
        external_ref = await _executor(actions, clock).start(build, source, str(build.build_id))

    assert ACTIONS_TOKEN not in external_ref and ACTIONS_TOKEN not in caplog.text


# --- AC-2 결과 ---


async def _started(
    actions: FakeActions, clock: TestClock
) -> tuple[GithubActionsBuildExecutor, str, BuildSpec]:
    executor = _executor(actions, clock)
    build = _build()
    return executor, await executor.start(build, _source(build), str(build.build_id)), build


async def test_poll_success_returns_artifact_registry_repository_and_digest() -> None:
    clock = TestClock()
    actions = FakeActions(clock)
    executor, external_ref, _ = await _started(actions, clock)

    running = await executor.poll(external_ref)
    actions.finish_all()
    done = await executor.poll(external_ref)

    assert running == BuildRunning()
    assert done == BuildSucceeded(image_repository=AR_REPOSITORY, image_digest=DIGEST)


async def test_poll_failed_run_returns_build_failed() -> None:
    clock = TestClock()
    actions = FakeActions(clock)
    executor, external_ref, _ = await _started(actions, clock)
    actions.finish(next(iter(actions.runs.values())), success=False)

    assert await executor.poll(external_ref) == BuildFailed(DeployRequestErrorCode.BUILD_FAILED)


@pytest.mark.parametrize(
    "make_artifact",
    [
        lambda build_id: b"not a zip",
        lambda build_id: result_zip(build_id, digest="sha256:ABC"),
        lambda build_id: result_zip(build_id, repository="registry/with:tag"),
        lambda build_id: result_zip("other-build-id"),
    ],
    ids=["broken-zip", "bad-digest", "bad-repository", "other-build"],
)
async def test_poll_invalid_result_returns_build_failed(
    make_artifact: Callable[[str], bytes], caplog: pytest.LogCaptureFixture
) -> None:
    clock = TestClock()
    actions = FakeActions(clock)
    executor, external_ref, build = await _started(actions, clock)
    actions.finish(next(iter(actions.runs.values())), artifact=make_artifact(str(build.build_id)))

    result = await executor.poll(external_ref)

    assert result == BuildFailed()
    codes = [getattr(record, "error_code", None) for record in caplog.records]
    assert [code for code in codes if code is not None] == ["BUILD_RESULT_INVALID"]


async def test_poll_missing_artifact_returns_build_failed() -> None:
    clock = TestClock()
    actions = FakeActions(clock)
    executor, external_ref, _ = await _started(actions, clock)
    run = next(iter(actions.runs.values()))
    actions.finish(run)
    run.artifact = None

    assert await executor.poll(external_ref) == BuildFailed()


@pytest.mark.parametrize("status", [500, 502, 429])
async def test_poll_server_error_is_still_running(status: int) -> None:
    clock = TestClock()
    actions = FakeActions(clock)
    executor, external_ref, _ = await _started(actions, clock)
    actions.override = lambda request: (
        httpx.Response(status) if "/actions/runs/" in request.url.path else None
    )

    assert await executor.poll(external_ref) == BuildRunning()


async def test_poll_unknown_ref_fails_without_calls() -> None:
    clock = TestClock()
    actions = FakeActions(clock)

    result = await _executor(actions, clock).poll("fake-build|key|0|x|y|z")

    assert result == BuildFailed()
    assert actions.requests == []


async def test_installation_token_is_reissued_five_minutes_before_expiry() -> None:
    clock = TestClock()
    actions = FakeActions(clock)
    executor, external_ref, _ = await _started(actions, clock)

    clock.advance(timedelta(minutes=54).total_seconds())
    await executor.poll(external_ref)
    clock.advance(timedelta(minutes=2).total_seconds())
    await executor.poll(external_ref)

    assert len(actions.token_requests) == 2


async def test_read_logs_before_run_finishes_returns_empty_chunk_with_more() -> None:
    clock = TestClock()
    actions = FakeActions(clock)
    executor, external_ref, _ = await _started(actions, clock)

    chunk = await executor.read_logs(external_ref, "0:3", 10)

    assert (chunk.lines, chunk.next_cursor, chunk.has_more) == ((), "0:3", True)
