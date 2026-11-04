"""실제 데이터 확인 실행기 시험(인프라 7). 접속은 가짜 connect 로 바꿔 네트워크를 쓰지 않는다."""

import dataclasses
import ssl
import uuid
from collections.abc import Mapping
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
from pydantic import ValidationError

from app.core.config import DataSyncCheckerMode, Settings
from app.core.secrets import parse_secret_ref
from app.executors import build_executor_set
from app.executors.base import DataSyncLag, DataSyncUnavailable
from app.executors.fake import FakeDataSyncChecker
from app.executors.postgres_data_sync import (
    REPLICATION_LAG_QUERY,
    PostgresDataSyncChecker,
    ReplicationEndpoint,
    parse_replication_endpoint,
    parse_replication_endpoints,
)
from tests.executor_contract import TARGET
from tests.real_executor_fakes import DictSecretClient, TestClock

DATABASE_URL = "postgresql+asyncpg://x:y@127.0.0.1:1/z"
PASSWORD = "monitor-secret"
SOURCE = TARGET
DESTINATION = dataclasses.replace(
    TARGET,
    service_environment_id=uuid.uuid4(),
    environment_id=uuid.uuid4(),
    environment_kind="gcp",
    environment_name="gcp-seoul",
)
ENDPOINTS = {
    "aws": ReplicationEndpoint("echo-pg-a.example.com", 5432, "echo_sub_rev"),
    "gcp": ReplicationEndpoint("echo-pg.echo-data.svc.cluster.local", 5432, "echo_sub"),
}


class _Connection:
    def __init__(self, row: Mapping[str, Any] | None) -> None:
        self.row = row
        self.queries: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False

    async def fetchrow(self, query: str, *args: Any) -> Mapping[str, Any] | None:
        self.queries.append((query, args))
        return self.row

    async def close(self) -> None:
        self.closed = True


class _Connector:
    def __init__(self, row: Mapping[str, Any] | None = None, error: Exception | None = None):
        self.connection = _Connection(row)
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> _Connection:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.connection


def _row(
    state: str = "streaming", lag: float | None = 0.07, behind: float | None = 0
) -> dict[str, Any]:
    return {"state": state, "lag_seconds": lag, "bytes_behind": behind}


def _checker(
    connector: _Connector,
    *,
    endpoints: Mapping[str, ReplicationEndpoint] = ENDPOINTS,
    secrets: dict[str, str] | None = None,
    ssl_mode: str = "require",
) -> PostgresDataSyncChecker:
    return PostgresDataSyncChecker(
        endpoints,
        user="echo_monitor",
        database="postgres",
        password_ref=parse_secret_ref("env:ECHO_SECRET_DATA_SYNC_PASSWORD"),
        secret_client=DictSecretClient(
            {"ECHO_SECRET_DATA_SYNC_PASSWORD": PASSWORD} if secrets is None else secrets
        ),
        ssl_mode=ssl_mode,
        connect=connector,
    )


async def test_reads_lag_from_source_for_destination_subscription() -> None:
    connector = _Connector(_row(lag=0.07))

    result = await _checker(connector).replication_lag(SOURCE, DESTINATION)

    assert result == DataSyncLag(seconds=0.07)
    [call] = connector.calls
    assert (call["host"], call["port"], call["user"], call["database"]) == (
        "echo-pg-a.example.com",
        5432,
        "echo_monitor",
        "postgres",
    )
    assert call["password"] == PASSWORD
    assert connector.connection.queries == [(REPLICATION_LAG_QUERY, ("echo_sub",))]
    assert connector.connection.closed


async def test_reverse_direction_reads_the_other_side() -> None:
    connector = _Connector(_row(lag=0.2))

    result = await _checker(connector).replication_lag(DESTINATION, SOURCE)

    assert result == DataSyncLag(seconds=0.2)
    assert connector.calls[0]["host"] == "echo-pg.echo-data.svc.cluster.local"
    assert connector.connection.queries[0][1] == ("echo_sub_rev",)


async def test_environment_id_mapping_wins_over_kind() -> None:
    by_id = ReplicationEndpoint("by-id.example.com", 6543, "echo_sub")
    connector = _Connector(_row())
    endpoints = {**ENDPOINTS, str(SOURCE.environment_id): by_id}

    await _checker(connector, endpoints=endpoints).replication_lag(SOURCE, DESTINATION)

    assert (connector.calls[0]["host"], connector.calls[0]["port"]) == ("by-id.example.com", 6543)


async def test_idle_caught_up_subscription_is_zero_lag() -> None:
    result = await _checker(_Connector(_row(lag=None, behind=0))).replication_lag(
        SOURCE, DESTINATION
    )

    assert result == DataSyncLag(seconds=0.0)


@pytest.mark.parametrize(
    "row",
    [
        None,
        _row(state="catchup"),
        _row(state="startup", lag=None),
        _row(lag=None, behind=1024),
        _row(lag=None, behind=None),
    ],
)
async def test_missing_or_not_streaming_subscription_is_unavailable(row: Any) -> None:
    result = await _checker(_Connector(row)).replication_lag(SOURCE, DESTINATION)

    assert result == DataSyncUnavailable()


async def test_unmapped_environment_is_unavailable_without_connecting() -> None:
    connector = _Connector(_row())
    endpoints = {"aws": ENDPOINTS["aws"]}

    result = await _checker(connector, endpoints=endpoints).replication_lag(SOURCE, DESTINATION)

    assert result == DataSyncUnavailable()
    assert connector.calls == []


async def test_missing_password_is_unavailable_without_connecting() -> None:
    connector = _Connector(_row())

    result = await _checker(connector, secrets={}).replication_lag(SOURCE, DESTINATION)

    assert result == DataSyncUnavailable()
    assert connector.calls == []


@pytest.mark.parametrize(
    "error",
    [
        OSError("connection refused"),
        TimeoutError(),
        asyncpg.InvalidPasswordError("bad password"),
        asyncpg.InterfaceError("closed"),
    ],
)
async def test_connection_failure_is_unavailable(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    result = await _checker(_Connector(error=error)).replication_lag(SOURCE, DESTINATION)

    assert result == DataSyncUnavailable()
    assert PASSWORD not in caplog.text
    assert "echo-pg-a.example.com" not in caplog.text


@pytest.mark.parametrize(
    ("ssl_mode", "expected"),
    [("require", ssl.CERT_NONE), ("verify-full", ssl.CERT_REQUIRED)],
)
async def test_ssl_mode(ssl_mode: str, expected: ssl.VerifyMode) -> None:
    connector = _Connector(_row())

    await _checker(connector, ssl_mode=ssl_mode).replication_lag(SOURCE, DESTINATION)

    context = connector.calls[0]["ssl"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode is expected


async def test_ssl_disable_is_plain() -> None:
    connector = _Connector(_row())

    await _checker(connector, ssl_mode="disable").replication_lag(SOURCE, DESTINATION)

    assert connector.calls[0]["ssl"] is False


def test_postgres_data_sync_is_not_simulated() -> None:
    assert _checker(_Connector()).simulated is False


def test_parse_replication_endpoints() -> None:
    assert parse_replication_endpoints(
        ["aws = echo-pg-a.example.com:5432/echo_sub_rev", "gcp=10.0.0.5:6432/echo_sub"]
    ) == {
        "aws": ReplicationEndpoint("echo-pg-a.example.com", 5432, "echo_sub_rev"),
        "gcp": ReplicationEndpoint("10.0.0.5", 6432, "echo_sub"),
    }


@pytest.mark.parametrize(
    "value", ["host:5432", "host/echo_sub", "host:0/echo_sub", "host:5432/Bad-Name", ""]
)
def test_parse_replication_endpoint_rejects_bad_format(value: str) -> None:
    with pytest.raises(ValueError):
        parse_replication_endpoint(value)


def test_default_mode_is_fake() -> None:
    settings = Settings(database_url=DATABASE_URL)

    assert settings.data_sync_checker_mode is DataSyncCheckerMode.FAKE
    assert isinstance(build_executor_set(settings, TestClock()).data_sync, FakeDataSyncChecker)


def test_postgres_mode_builds_real_checker_from_comma_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_SYNC_CHECKER_MODE", "postgres")
    monkeypatch.setenv(
        "DATA_SYNC_DATABASES",
        "aws=echo-pg-a.example.com:5432/echo_sub_rev,gcp=echo-pg.echo-data.svc:5432/echo_sub",
    )
    monkeypatch.setenv("DATA_SYNC_PASSWORD_REF", "env:ECHO_SECRET_DATA_SYNC_PASSWORD")
    settings = Settings(database_url=DATABASE_URL)

    checker = build_executor_set(settings, TestClock()).data_sync

    assert isinstance(checker, PostgresDataSyncChecker)
    assert checker.endpoint(DESTINATION) == ReplicationEndpoint(
        "echo-pg.echo-data.svc", 5432, "echo_sub"
    )


@pytest.mark.parametrize(
    "values",
    [
        {"data_sync_checker_mode": "postgres"},
        {"data_sync_checker_mode": "postgres", "data_sync_databases": ["aws=h:5432/echo_sub"]},
        {"data_sync_checker_mode": "postgres", "data_sync_password_ref": "env:ECHO_SECRET_X"},
        {"data_sync_databases": ["aws=h:5432"]},
        {"data_sync_password_ref": "not-a-ref"},
        {"data_sync_ssl_mode": "prefer"},
    ],
)
def test_invalid_data_sync_settings_fail_at_startup(values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Settings(database_url=DATABASE_URL, **values)
