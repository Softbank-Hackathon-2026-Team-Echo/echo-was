"""실행기 공통 계약 시험. 실제 실행기(인프라 3·5 이후)도 같은 검사를 통과해야 한다.

- 같은 멱등 키로 다시 부르면 같은 작업(external_ref)을 돌려준다.
- 끝나기 전 poll 은 진행 중, 끝나면 성공 결과를 준다.
- external_ref 에 비밀값(읽기 토큰)이 들어가지 않는다.
- 빌드 결과는 sha256 digest 다(배포는 digest 로만 한다).
- 로그 읽기: 같은 cursor 면 같은 줄, limit 을 넘지 않음, 시간 순, cursor 로 이어 읽으면 겹침·누락
  없음, 알아보지 못하는 cursor 는 InvalidLogCursor.
- 트래픽(이전 계획): 같은 멱등 키면 같은 결과, 비율 합계가 100 이 아니면 ValueError.
- 지표·데이터 확인: 같은 입력이면 같은 값(결정적), 값 범위(오류율 0~100, p95·요청 수·지연 ≥ 0).
- 누적 카운터(read_counts): RequestCounts, 음수 없음, 시간이 지나도 줄지 않음.
"""

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.core.secrets import SecretValue
from app.executors.base import (
    BuildExecutor,
    BuildRunning,
    BuildSpec,
    BuildSucceeded,
    DataSyncChecker,
    DataSyncLag,
    DeployApplied,
    DeployExecutor,
    DeploymentSpec,
    DeployPending,
    DeployTarget,
    InvalidLogCursor,
    LogChunk,
    LogLine,
    MainRoute,
    Metrics,
    MetricsProvider,
    RequestCounts,
    Reverted,
    SourceAccess,
    TeardownExecutor,
    TeardownPending,
    TeardownRemoved,
    TrafficApplied,
    TrafficExecutor,
    WeightedTarget,
)

DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
READ_TOKEN = "ghs_contract_read_token"
COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"

TARGET = DeployTarget(
    service_id=uuid.uuid4(),
    service_name="shop-api",
    service_environment_id=uuid.uuid4(),
    environment_id=uuid.uuid4(),
    environment_kind="aws",
    public_host="shop-api.aws-seoul.apps.echo.test",
    container_port=8080,
)
DEPLOYMENT = DeploymentSpec(
    deployment_id=uuid.uuid4(),
    image_repository="registry.echo.test/shop-api",
    image_digest="sha256:" + "a" * 64,
    config_snapshot={"containerPort": 8080},
)

Advance = Callable[[], None]


async def assert_build_executor_contract(executor: BuildExecutor, finish: Advance) -> None:
    build = BuildSpec(
        build_id=uuid.uuid4(),
        service_id=TARGET.service_id,
        service_name="shop-api",
        repo_url="https://github.com/team-echo/shop-api",
        commit_sha=COMMIT_SHA,
    )
    source = SourceAccess(build.repo_url, COMMIT_SHA, SecretValue(READ_TOKEN))
    key = str(build.build_id)

    first = await executor.start(build, source, key)
    again = await executor.start(build, source, key)
    before = await executor.poll(first)
    finish()
    after = await executor.poll(first)

    assert first == again
    assert READ_TOKEN not in first
    assert isinstance(before, BuildRunning)
    assert isinstance(after, BuildSucceeded)
    assert DIGEST.fullmatch(after.image_digest)


async def assert_deploy_executor_contract(executor: DeployExecutor, finish: Advance) -> None:
    key = str(DEPLOYMENT.deployment_id)

    first = await executor.apply(DEPLOYMENT, TARGET, key)
    again = await executor.apply(DEPLOYMENT, TARGET, key)
    before = await executor.poll(first)
    finish()
    after = await executor.poll(first)
    reverted = await executor.revert(TARGET, None, key)

    assert first == again
    assert isinstance(before, DeployPending)
    assert isinstance(after, DeployApplied)
    assert isinstance(reverted, Reverted)


async def assert_teardown_executor_contract(executor: TeardownExecutor, finish: Advance) -> None:
    key = f"{TARGET.service_environment_id}:0"

    first = await executor.remove(TARGET, key)
    again = await executor.remove(TARGET, key)
    before = await executor.poll(first)
    finish()
    after = await executor.poll(first)
    other_attempt = await executor.remove(TARGET, f"{TARGET.service_environment_id}:1")

    assert first == again
    assert other_attempt != first
    assert isinstance(before, TeardownPending)
    assert isinstance(after, TeardownRemoved)


ReadLogs = Callable[[str | None, int], Awaitable[LogChunk]]
INVALID_CURSORS = ("not-a-cursor", "-1", "1.5")


async def assert_log_reader_contract(read: ReadLogs) -> list[LogLine]:
    """한 작업의 로그를 읽는 함수(cursor, limit → LogChunk)가 로그 읽기 계약을 지키는지 본다."""
    whole = await read(None, 500)
    same = await read(None, 500)
    first = await read(None, 2)
    again = await read(None, 2)

    paged: list[LogLine] = []
    cursor: str | None = None
    for _ in range(100):
        chunk = await read(cursor, 2)
        assert len(chunk.lines) <= 2
        paged += chunk.lines
        cursor = chunk.next_cursor
        if not chunk.has_more:
            break
    resumed = await read(cursor, 2)

    assert whole.lines and not whole.has_more
    assert whole == same
    assert first == again
    assert len(first.lines) == 2 and first.has_more
    assert paged == list(whole.lines)
    assert [line.at for line in whole.lines] == sorted(line.at for line in whole.lines)
    assert resumed.lines == () and not resumed.has_more and resumed.next_cursor == cursor
    for invalid in INVALID_CURSORS:
        with pytest.raises(InvalidLogCursor):
            await read(invalid, 2)
    return list(whole.lines)


async def assert_build_log_contract(executor: BuildExecutor) -> list[LogLine]:
    build = BuildSpec(
        build_id=uuid.uuid4(),
        service_id=TARGET.service_id,
        service_name="shop-api",
        repo_url="https://github.com/team-echo/shop-api",
        commit_sha=COMMIT_SHA,
    )
    source = SourceAccess(build.repo_url, COMMIT_SHA, SecretValue(READ_TOKEN))
    external_ref = await executor.start(build, source, str(build.build_id))

    async def read(cursor: str | None, limit: int) -> LogChunk:
        return await executor.read_logs(external_ref, cursor, limit)

    lines = await assert_log_reader_contract(read)
    assert all(READ_TOKEN not in line.message for line in lines)
    return lines


async def assert_runtime_log_contract(executor: DeployExecutor) -> list[LogLine]:
    external_ref = await executor.apply(DEPLOYMENT, TARGET, str(DEPLOYMENT.deployment_id))

    async def read(cursor: str | None, limit: int) -> LogChunk:
        return await executor.read_runtime_logs(TARGET, DEPLOYMENT, external_ref, cursor, limit)

    return await assert_log_reader_contract(read)


# --- 이전 계획: 트래픽·지표·데이터 확인 ---

STANDBY_TARGET = replace(
    TARGET,
    service_environment_id=uuid.uuid4(),
    environment_id=uuid.uuid4(),
    environment_kind="gcp",
    public_host="shop-api-gcp-seoul.apps.echo.test",
)
ROUTE = MainRoute(
    service_id=TARGET.service_id, service_name="shop-api", public_host="shop-api.apps.echo.test"
)
INVALID_WEIGHTS = ((90, 20), (50, 40), (110, -10))
WINDOW_END = datetime(2026, 11, 2, 3, 0, 5, tzinfo=UTC)


async def assert_traffic_executor_contract(executor: TrafficExecutor, advance: Advance) -> None:
    key = f"{uuid.uuid4()}:1"
    weights = (WeightedTarget(TARGET, 90), WeightedTarget(STANDBY_TARGET, 10))

    first = await executor.set_weights(ROUTE, weights, key)
    advance()
    again = await executor.set_weights(ROUTE, weights, key)
    rollback = await executor.set_weights(
        ROUTE,
        (WeightedTarget(TARGET, 100), WeightedTarget(STANDBY_TARGET, 0)),
        f"{key}:rollback:0",
    )

    assert isinstance(first, TrafficApplied)
    assert again == first
    assert isinstance(rollback, TrafficApplied)
    for source_weight, target_weight in INVALID_WEIGHTS:
        bad = (WeightedTarget(TARGET, source_weight), WeightedTarget(STANDBY_TARGET, target_weight))
        with pytest.raises(ValueError):
            await executor.set_weights(ROUTE, bad, f"{key}:bad")


async def assert_metrics_provider_contract(provider: MetricsProvider) -> Metrics:
    until = WINDOW_END
    since = until - timedelta(seconds=5)

    first = await provider.read(STANDBY_TARGET, since, until)
    again = await provider.read(STANDBY_TARGET, since, until)

    assert isinstance(first, Metrics)
    assert first == again
    assert 0 <= first.error_rate_percent <= 100
    assert first.p95_ms >= 0 and first.request_count >= 0
    return first


async def assert_counts_contract(
    provider: MetricsProvider, advance: Advance
) -> tuple[RequestCounts, RequestCounts]:
    """누적 카운터: RequestCounts 이고 음수가 없으며, 시간이 지나도 줄지 않는다(단조 증가).

    실제 구현(진입점 GET /counters)은 재시작하면 줄 수 있지만 계약 시험 동안은 재시작하지 않는다.
    읽지 못하면 CountsUnavailable 이다(구현마다 만드는 방법이 달라 여기서 보지 않는다).
    """
    first = await provider.read_counts(STANDBY_TARGET)
    advance()
    second = await provider.read_counts(STANDBY_TARGET)

    assert isinstance(first, RequestCounts) and isinstance(second, RequestCounts)
    for counts in (first, second):
        assert counts.requests >= 0 and counts.errors_5xx >= 0
    assert second.requests >= first.requests
    assert second.errors_5xx >= first.errors_5xx
    return first, second


async def assert_data_sync_checker_contract(checker: DataSyncChecker) -> DataSyncLag:
    first = await checker.replication_lag(TARGET, STANDBY_TARGET)
    again = await checker.replication_lag(TARGET, STANDBY_TARGET)

    assert isinstance(first, DataSyncLag)
    assert first == again
    assert first.seconds >= 0
    return first


ContractCheck = Callable[..., Awaitable[None]]
