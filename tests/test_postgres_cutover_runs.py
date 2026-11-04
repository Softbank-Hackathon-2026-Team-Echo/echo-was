"""컷오버 결과 읽기 단위 시험(API 서버 16). 접속은 가짜 connect 로 바꿔 네트워크를 쓰지 않는다."""

import asyncio
import ssl
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.secrets import parse_secret_ref
from app.enums import CutoverKind, MigrationPlanMode
from app.executors import build_executor_set
from app.executors.base import (
    CutoverRun,
    CutoverRunNotFound,
    CutoverRunTarget,
    CutoverRunUnavailable,
    NoCutoverRuns,
)
from app.executors.postgres_cutover_runs import (
    CUTOVER_RESULT_ENV_UNMAPPED,
    CUTOVER_RESULT_QUERY_FAILED,
    CUTOVER_RESULT_SECRET_MISSING,
    CUTOVER_RESULT_UNREACHABLE,
    CUTOVER_RUNS_QUERY,
    PostgresCutoverRunReader,
)
from app.executors.postgres_data_sync import ReplicationEndpoint
from app.main import app
from app.services.migration_plan_result import CutoverResultCache, cutover_kind_for
from tests.real_executor_fakes import DictSecretClient, TestClock

DATABASE_URL = "postgresql+asyncpg://x:y@127.0.0.1:1/z"
PASSWORD = "monitor-secret"
AWS_HOST = "echo-pg-a.example.com"
GCP_HOST = "echo-pg.echo-data.svc.cluster.local"
ENDPOINTS = {
    "aws": ReplicationEndpoint(AWS_HOST, 5432, "echo_sub_rev"),
    "gcp": ReplicationEndpoint(GCP_HOST, 6432, "echo_sub"),
}
TARGET = CutoverRunTarget(uuid.uuid4(), "gcp")
SINCE = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)
UNTIL = SINCE + timedelta(minutes=3)
EXPECTED_QUERY = """
SELECT kind, paused_at, resumed_at, write_pause_ms, drain_ms, catchup_ms, switch_ms,
       rows_total, missing_rows, duplicate_rows
FROM echo_ops.cutover_runs
WHERE kind = $1 AND paused_at >= $2 AND ($3::timestamptz IS NULL OR paused_at < $3)
ORDER BY paused_at, created_at, id
LIMIT 1
"""


def _row(**changes: Any) -> dict[str, Any]:
    return {
        "kind": "CUTOVER",
        "paused_at": datetime(2026, 11, 4, 13, 51, 59, 670000, tzinfo=UTC),
        "resumed_at": datetime(2026, 11, 4, 13, 52, 5, 487000, tzinfo=UTC),
        "write_pause_ms": 5817,
        "drain_ms": 774,
        "catchup_ms": 428,
        "switch_ms": 4615,
        "rows_total": 168,
        "missing_rows": 0,
        "duplicate_rows": 0,
        **changes,
    }


class _Connection:
    def __init__(self, row: Mapping[str, Any] | None, error: Exception | None) -> None:
        self.row = row
        self.error = error
        self.queries: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False
        self.terminated = False
        self.hangs = False

    async def fetchrow(self, query: str, *args: Any) -> Mapping[str, Any] | None:
        self.queries.append((query, args))
        if self.hangs:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error
        return self.row

    async def close(self) -> None:
        if self.hangs:
            await asyncio.Event().wait()
        self.closed = True

    def terminate(self) -> None:
        self.terminated = True


class _Connector:
    def __init__(
        self,
        row: Mapping[str, Any] | None = None,
        *,
        connect_error: Exception | None = None,
        query_error: Exception | None = None,
        connect_hangs: bool = False,
    ) -> None:
        self.connection = _Connection(row, query_error)
        self.connect_error = connect_error
        self.connect_hangs = connect_hangs
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> _Connection:
        self.calls.append(kwargs)
        if self.connect_hangs:
            await asyncio.Event().wait()
        if self.connect_error is not None:
            raise self.connect_error
        return self.connection


def _reader(
    connector: _Connector,
    *,
    endpoints: Mapping[str, ReplicationEndpoint] = ENDPOINTS,
    secrets: dict[str, str] | None = None,
    ssl_mode: str = "require",
    timeout_seconds: float = 3.0,
) -> PostgresCutoverRunReader:
    return PostgresCutoverRunReader(
        endpoints,
        user="echo_monitor",
        database="app",
        password_ref=parse_secret_ref("env:ECHO_SECRET_DATA_SYNC_PASSWORD"),
        secret_client=DictSecretClient(
            {"ECHO_SECRET_DATA_SYNC_PASSWORD": PASSWORD} if secrets is None else secrets
        ),
        ssl_mode=ssl_mode,
        timeout_seconds=timeout_seconds,
        connect=connector,
    )


async def test_find_first_fixes_query_args_database_and_account() -> None:
    connector = _Connector(_row())

    result = await _reader(connector).find_first(TARGET, CutoverKind.CUTOVER, SINCE, UNTIL)

    assert isinstance(result, CutoverRun)
    assert CUTOVER_RUNS_QUERY == EXPECTED_QUERY
    assert connector.connection.queries == [(EXPECTED_QUERY, ("CUTOVER", SINCE, UNTIL))]
    [call] = connector.calls
    assert (call["host"], call["port"], call["user"], call["database"], call["timeout"]) == (
        GCP_HOST,
        6432,
        "echo_monitor",
        "app",
        3.0,
    )
    assert call["password"] == PASSWORD
    assert connector.connection.closed
    assert not connector.connection.terminated


async def test_find_first_passes_none_until_when_window_has_no_end() -> None:
    connector = _Connector(_row(kind="ROLLBACK"))

    result = await _reader(connector).find_first(TARGET, CutoverKind.ROLLBACK, SINCE, None)

    assert isinstance(result, CutoverRun)
    assert result.kind is CutoverKind.ROLLBACK
    assert connector.connection.queries[0][1] == ("ROLLBACK", SINCE, None)


async def test_find_first_maps_every_column_and_converts_times_to_utc() -> None:
    seoul = timezone(timedelta(hours=9))
    row = _row(
        paused_at=datetime(2026, 11, 4, 22, 51, 59, 670000, tzinfo=seoul),
        resumed_at=datetime(2026, 11, 4, 22, 52, 5, 487000, tzinfo=seoul),
    )

    result = await _reader(_Connector(row)).find_first(TARGET, CutoverKind.CUTOVER, SINCE, None)

    assert result == CutoverRun(
        kind=CutoverKind.CUTOVER,
        paused_at=datetime(2026, 11, 4, 13, 51, 59, 670000, tzinfo=UTC),
        resumed_at=datetime(2026, 11, 4, 13, 52, 5, 487000, tzinfo=UTC),
        write_pause_ms=5817,
        drain_ms=774,
        catchup_ms=428,
        switch_ms=4615,
        rows_total=168,
        missing_rows=0,
        duplicate_rows=0,
    )
    assert isinstance(result, CutoverRun)
    assert result.paused_at.utcoffset() == timedelta(0)


async def test_find_first_keeps_nullable_columns_null() -> None:
    row = _row(drain_ms=None, missing_rows=None, duplicate_rows=None, rows_total=None)

    result = await _reader(_Connector(row)).find_first(TARGET, CutoverKind.CUTOVER, SINCE, None)

    assert isinstance(result, CutoverRun)
    assert (result.drain_ms, result.missing_rows, result.rows_total) == (None, None, None)


async def test_find_first_returns_not_found_when_no_row() -> None:
    connector = _Connector(None)

    result = await _reader(connector).find_first(TARGET, CutoverKind.CUTOVER, SINCE, None)

    assert result == CutoverRunNotFound()
    assert connector.connection.closed


async def test_find_first_environment_id_mapping_wins_over_kind() -> None:
    connector = _Connector(None)
    by_id = {**ENDPOINTS, str(TARGET.environment_id): ReplicationEndpoint("by-id", 15432, "sub")}

    await _reader(connector, endpoints=by_id).find_first(TARGET, CutoverKind.CUTOVER, SINCE, None)

    assert (connector.calls[0]["host"], connector.calls[0]["port"]) == ("by-id", 15432)


async def test_find_first_unmapped_environment_does_not_connect() -> None:
    connector = _Connector(_row())
    onprem = CutoverRunTarget(uuid.uuid4(), "onprem")

    result = await _reader(connector).find_first(onprem, CutoverKind.CUTOVER, SINCE, None)

    assert result == CutoverRunUnavailable(CUTOVER_RESULT_ENV_UNMAPPED)
    assert connector.calls == []


async def test_find_first_missing_secret_does_not_connect() -> None:
    connector = _Connector(_row())

    result = await _reader(connector, secrets={}).find_first(
        TARGET, CutoverKind.CUTOVER, SINCE, None
    )

    assert result == CutoverRunUnavailable(CUTOVER_RESULT_SECRET_MISSING)
    assert connector.calls == []


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (OSError("connect to echo-pg.echo-data refused"), CUTOVER_RESULT_UNREACHABLE),
        (TimeoutError(), CUTOVER_RESULT_UNREACHABLE),
        (asyncpg.InterfaceError("connection is closed"), CUTOVER_RESULT_UNREACHABLE),
        (asyncpg.InvalidPasswordError("bad password"), CUTOVER_RESULT_QUERY_FAILED),
    ],
)
async def test_find_first_connect_errors_become_unavailable(error: Exception, code: str) -> None:
    connector = _Connector(connect_error=error)

    result = await _reader(connector).find_first(TARGET, CutoverKind.CUTOVER, SINCE, None)

    assert result == CutoverRunUnavailable(code)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (asyncpg.InsufficientPrivilegeError("denied"), CUTOVER_RESULT_QUERY_FAILED),
        (asyncpg.UndefinedTableError("no table"), CUTOVER_RESULT_QUERY_FAILED),
        (OSError("reset"), CUTOVER_RESULT_UNREACHABLE),
        (TimeoutError(), CUTOVER_RESULT_UNREACHABLE),
        (asyncpg.InterfaceError("closed"), CUTOVER_RESULT_UNREACHABLE),
    ],
)
async def test_find_first_query_errors_become_unavailable_and_end_connection(
    error: Exception, code: str
) -> None:
    connector = _Connector(query_error=error)

    result = await _reader(connector).find_first(TARGET, CutoverKind.CUTOVER, SINCE, None)

    assert result == CutoverRunUnavailable(code)
    assert connector.connection.terminated


async def test_find_first_stops_at_timeout_when_connect_hangs() -> None:
    connector = _Connector(connect_hangs=True)

    async with asyncio.timeout(1):
        result = await _reader(connector, timeout_seconds=0.05).find_first(
            TARGET, CutoverKind.CUTOVER, SINCE, None
        )

    assert result == CutoverRunUnavailable(CUTOVER_RESULT_UNREACHABLE)


async def test_find_first_does_not_wait_for_close_after_timeout() -> None:
    connector = _Connector(_row())
    connector.connection.hangs = True

    async with asyncio.timeout(1):
        result = await _reader(connector, timeout_seconds=0.05).find_first(
            TARGET, CutoverKind.CUTOVER, SINCE, None
        )

    assert result == CutoverRunUnavailable(CUTOVER_RESULT_UNREACHABLE)
    assert connector.connection.terminated
    assert not connector.connection.closed


@pytest.mark.parametrize(
    ("ssl_mode", "expected"),
    [("require", ssl.CERT_NONE), ("verify-full", ssl.CERT_REQUIRED)],
)
async def test_find_first_ssl_mode(ssl_mode: str, expected: ssl.VerifyMode) -> None:
    connector = _Connector(None)

    await _reader(connector, ssl_mode=ssl_mode).find_first(TARGET, CutoverKind.CUTOVER, SINCE, None)

    assert connector.calls[0]["ssl"].verify_mode is expected


async def test_find_first_ssl_disable() -> None:
    connector = _Connector(None)

    await _reader(connector, ssl_mode="disable").find_first(
        TARGET, CutoverKind.CUTOVER, SINCE, None
    )

    assert connector.calls[0]["ssl"] is False


async def test_no_cutover_runs_always_returns_not_found() -> None:
    result = await NoCutoverRuns().find_first(TARGET, CutoverKind.CUTOVER, SINCE, None)

    assert result == CutoverRunNotFound()


@pytest.mark.parametrize(
    ("mode", "kind"),
    [
        (MigrationPlanMode.PLANNED, CutoverKind.CUTOVER),
        (MigrationPlanMode.EMERGENCY, CutoverKind.CUTOVER),
        (MigrationPlanMode.FAILBACK, CutoverKind.ROLLBACK),
    ],
)
def test_cutover_kind_for_mode(mode: MigrationPlanMode, kind: CutoverKind) -> None:
    assert cutover_kind_for(mode) is kind


def _postgres_settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    monkeypatch.setenv("DATA_SYNC_CHECKER_MODE", "postgres")
    monkeypatch.setenv("DATA_SYNC_DATABASES", f"gcp={GCP_HOST}:5432/echo_sub")
    monkeypatch.setenv("DATA_SYNC_PASSWORD_REF", "env:ECHO_SECRET_DATA_SYNC_PASSWORD")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(database_url=DATABASE_URL, _env_file=None)  # type: ignore[call-arg]


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "CUTOVER_RUNS_DATABASE",
        "CUTOVER_RESULTS_NOT_BEFORE",
        "CUTOVER_RESULT_CACHE_SECONDS",
        "DATA_SYNC_CHECKER_MODE",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(database_url=DATABASE_URL, _env_file=None)  # type: ignore[call-arg]

    assert settings.cutover_runs_database == "app"
    assert settings.cutover_results_not_before is None
    assert settings.cutover_result_cache_seconds == 5
    assert not settings.cutover_results_enabled


def test_settings_reads_operating_values(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _postgres_settings(
        monkeypatch,
        CUTOVER_RUNS_DATABASE="app",
        CUTOVER_RESULTS_NOT_BEFORE="2026-11-04T13:30:00Z",
    )

    assert settings.cutover_results_enabled
    assert settings.cutover_results_not_before == datetime(2026, 11, 4, 13, 30, tzinfo=UTC)


def test_settings_empty_database_turns_feature_off(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _postgres_settings(monkeypatch, CUTOVER_RUNS_DATABASE="")

    assert not settings.cutover_results_enabled
    assert isinstance(build_executor_set(settings, TestClock()).cutover_runs, NoCutoverRuns)


def test_settings_rejects_time_without_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        _postgres_settings(monkeypatch, CUTOVER_RESULTS_NOT_BEFORE="2026-11-04T13:30:00")


def test_settings_rejects_cache_seconds_below_one(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        _postgres_settings(monkeypatch, CUTOVER_RESULT_CACHE_SECONDS="0")


def test_build_executor_set_uses_postgres_reader_in_postgres_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _postgres_settings(monkeypatch, CUTOVER_RUNS_DATABASE="app")

    reader = build_executor_set(settings, TestClock()).cutover_runs

    assert isinstance(reader, PostgresCutoverRunReader)


def test_build_executor_set_never_reads_in_fake_data_sync_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_SYNC_CHECKER_MODE", "fake")
    settings = Settings(database_url=DATABASE_URL, _env_file=None)  # type: ignore[call-arg]

    assert isinstance(build_executor_set(settings, TestClock()).cutover_runs, NoCutoverRuns)


def test_openapi_documents_result_schema() -> None:
    schemas = app.openapi()["components"]["schemas"]

    assert set(schemas["MigrationPlanResultResponse"]["properties"]) == {
        "writePauseMs",
        "missingRows",
        "duplicateRows",
        "rowsTotal",
        "kind",
        "measuredBy",
        "pausedAt",
        "resumedAt",
        "drainMs",
        "catchupMs",
        "switchMs",
    }
    assert "result" in schemas["MigrationPlanDetailResponse"]["properties"]
    assert "result" not in schemas["MigrationPlanSummaryResponse"]["properties"]


NOW = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)
RUN = CutoverRun(
    kind=CutoverKind.CUTOVER,
    paused_at=NOW,
    resumed_at=NOW,
    write_pause_ms=1,
    drain_ms=None,
    catchup_ms=None,
    switch_ms=None,
    rows_total=None,
    missing_rows=None,
    duplicate_rows=None,
)


def test_cache_entry_with_expiry_disappears_at_expiry() -> None:
    cache = CutoverResultCache()
    plan_id = uuid.uuid4()
    expires_at = NOW + timedelta(seconds=5)

    cache.store(plan_id, None, expires_at)

    assert cache.find(plan_id, NOW + timedelta(seconds=4)) is not None
    assert cache.find(plan_id, expires_at) is None


def test_cache_entry_without_expiry_stays() -> None:
    cache = CutoverResultCache()
    plan_id = uuid.uuid4()

    cache.store(plan_id, RUN, None)

    entry = cache.find(plan_id, NOW + timedelta(days=365))
    assert entry is not None
    assert entry.run == RUN


def test_cache_drops_oldest_entry_when_full() -> None:
    cache = CutoverResultCache(max_entries=512)
    plan_ids = [uuid.uuid4() for _ in range(513)]

    for plan_id in plan_ids:
        cache.store(plan_id, RUN, None)

    assert cache.find(plan_ids[0], NOW) is None
    assert cache.find(plan_ids[1], NOW) is not None
    assert cache.find(plan_ids[512], NOW) is not None


def test_cache_store_again_counts_as_newest() -> None:
    cache = CutoverResultCache(max_entries=2)
    first, second, third = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    cache.store(first, RUN, None)
    cache.store(second, RUN, None)

    cache.store(first, RUN, None)
    cache.store(third, RUN, None)

    assert cache.find(second, NOW) is None
    assert cache.find(first, NOW) is not None


async def test_cache_reading_lets_one_task_in_and_removes_lock_afterwards() -> None:
    cache = CutoverResultCache()
    plan_id = uuid.uuid4()
    order: list[str] = []

    async def read(name: str) -> None:
        async with cache.reading(plan_id):
            order.append(f"{name} in")
            await asyncio.sleep(0)
            order.append(f"{name} out")

    await asyncio.gather(read("a"), read("b"))

    assert order == ["a in", "a out", "b in", "b out"]
    assert cache._locks == {}


async def test_cache_reading_removes_lock_when_task_is_cancelled() -> None:
    cache = CutoverResultCache()
    plan_id = uuid.uuid4()
    entered = asyncio.Event()

    async def read() -> None:
        async with cache.reading(plan_id):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(read())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cache._locks == {}
