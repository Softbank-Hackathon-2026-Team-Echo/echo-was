"""실제 데이터 확인 실행기 시험(인프라 7). 접속은 가짜 connect 로 바꿔 네트워크를 쓰지 않는다."""

import asyncio
import dataclasses
import logging
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
    DATA_SYNC_NOT_STREAMING,
    DATA_SYNC_UNREACHABLE,
    REPLICATION_LAG_QUERY,
    SOURCE_SUBSCRIPTION_QUERY,
    TARGET_WALSENDER_QUERY,
    Connect,
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
    connector: Connect,
    *,
    endpoints: Mapping[str, ReplicationEndpoint] = ENDPOINTS,
    secrets: dict[str, str] | None = None,
    ssl_mode: str = "require",
    timeout_seconds: float = 3.0,
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
        timeout_seconds=timeout_seconds,
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


class _Database:
    """PostgreSQL 한 대의 복제 상태. 질의 상수와 인자로 행을 고른다."""

    def __init__(
        self,
        *,
        walsenders: Mapping[str, Mapping[str, Any]] | None = None,
        subscriptions: Mapping[str, bool] | None = None,
        connect_error: Exception | None = None,
        subscription_error: Exception | None = None,
        walsender_error: Exception | None = None,
    ) -> None:
        self.walsenders = walsenders or {}
        self.subscriptions = subscriptions or {}
        self.connect_error = connect_error
        self.subscription_error = subscription_error
        self.walsender_error = walsender_error
        self.queries: list[tuple[str, tuple[Any, ...]]] = []
        self.close_count = 0

    async def fetchrow(self, query: str, *args: Any) -> Mapping[str, Any] | None:
        self.queries.append((query, args))
        [name] = args
        if query == REPLICATION_LAG_QUERY:
            return self.walsenders.get(name)
        if query == SOURCE_SUBSCRIPTION_QUERY:
            if self.subscription_error is not None:
                raise self.subscription_error
            # 질의가 enabled 를 거르지 않게 되면 꺼진 구독도 행으로 돌아가 시험이 실패한다.
            is_visible = self.subscriptions.get(name, False) or (
                name in self.subscriptions and "subenabled" not in query
            )
            return {"subname": name} if is_visible else None
        assert query == TARGET_WALSENDER_QUERY
        if self.walsender_error is not None:
            raise self.walsender_error
        return {"application_name": name} if name in self.walsenders else None

    async def close(self) -> None:
        self.close_count += 1


class _Cluster:
    """호스트별로 다른 PostgreSQL 에 붙는 가짜 connect."""

    def __init__(self, databases: Mapping[str, _Database]) -> None:
        self.databases = databases
        self.calls: list[dict[str, Any]] = []

    @property
    def hosts(self) -> list[str]:
        return [call["host"] for call in self.calls]

    async def __call__(self, **kwargs: Any) -> _Database:
        self.calls.append(kwargs)
        database = self.databases[kwargs["host"]]
        if database.connect_error is not None:
            raise database.connect_error
        return database


AWS_HOST = ENDPOINTS["aws"].host
GCP_HOST = ENDPOINTS["gcp"].host
TARGET_IS_WRITER = DataSyncLag(seconds=0.0, reason="TARGET_IS_WRITER")
DATABASE_ERRORS = [
    OSError("connection refused"),
    TimeoutError(),
    asyncpg.InvalidPasswordError("bad password"),
    asyncpg.InterfaceError("closed"),
]
DATABASE_ERROR_IDS = ["os-error", "timeout", "invalid-password", "interface-error"]


def _cluster(source: _Database, target: _Database) -> _Cluster:
    """SOURCE(aws, 구독 echo_sub_rev) → DESTINATION(gcp, 구독 echo_sub) 방향."""
    return _Cluster({AWS_HOST: source, GCP_HOST: target})


def _warning_codes(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.__dict__["error_code"]
        for record in caplog.records
        if record.message == "data sync lag unavailable"
    ]


# --- 규칙 2: 출발이 발행자면 지금 그대로 지연을 잰다 ---


async def test_source_publisher_streaming_returns_lag_without_writer_checks() -> None:
    source = _Database(walsenders={"echo_sub": _row(lag=0.07)})
    target = _Database()
    cluster = _cluster(source, target)

    result = await _checker(cluster).replication_lag(SOURCE, DESTINATION)

    assert result == DataSyncLag(seconds=0.07)
    assert isinstance(result, DataSyncLag)
    assert result.reason is None
    assert cluster.hosts == [AWS_HOST]
    assert source.queries == [(REPLICATION_LAG_QUERY, ("echo_sub",))]
    assert source.close_count == 1


@pytest.mark.parametrize(
    "row",
    [_row(state="catchup"), _row(state="startup", lag=None), _row(lag=None, behind=1024)],
    ids=["catchup", "startup", "wal-remaining"],
)
async def test_source_row_not_streaming_is_unavailable_even_if_target_looks_like_writer(
    row: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    source = _Database(walsenders={"echo_sub": row}, subscriptions={"echo_sub_rev": True})
    target = _Database(walsenders={"echo_sub_rev": _row()})
    cluster = _cluster(source, target)

    result = await _checker(cluster).replication_lag(SOURCE, DESTINATION)

    assert result == DataSyncUnavailable()
    assert _warning_codes(caplog) == [DATA_SYNC_NOT_STREAMING]
    assert cluster.hosts == [AWS_HOST]
    assert source.queries == [(REPLICATION_LAG_QUERY, ("echo_sub",))]


async def test_both_sides_publishing_measures_lag_from_source() -> None:
    source = _Database(walsenders={"echo_sub": _row(lag=0.2)}, subscriptions={"echo_sub_rev": True})
    target = _Database(walsenders={"echo_sub_rev": _row()})
    cluster = _cluster(source, target)

    result = await _checker(cluster).replication_lag(SOURCE, DESTINATION)

    assert result == DataSyncLag(seconds=0.2)
    assert cluster.hosts == [AWS_HOST]


# --- 규칙 3·4: 도착이 이미 쓰기 담당이면 지연 0 으로 통과한다 ---


async def test_source_subscription_enabled_passes_without_connecting_to_target(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="app.executors.postgres_data_sync")
    source = _Database(subscriptions={"echo_sub_rev": True})
    target = _Database()
    cluster = _cluster(source, target)

    result = await _checker(cluster).replication_lag(SOURCE, DESTINATION)

    assert result == TARGET_IS_WRITER
    assert cluster.hosts == [AWS_HOST]
    assert source.queries == [
        (REPLICATION_LAG_QUERY, ("echo_sub",)),
        (SOURCE_SUBSCRIPTION_QUERY, ("echo_sub_rev",)),
    ]
    assert source.close_count == 1
    [record] = [r for r in caplog.records if r.message == "data sync target is writer"]
    assert record.__dict__["evidence"] == "SOURCE_SUBSCRIPTION"
    assert record.__dict__["reason"] == "TARGET_IS_WRITER"
    assert record.__dict__["source_environment_id"] == str(SOURCE.environment_id)
    assert record.__dict__["target_environment_id"] == str(DESTINATION.environment_id)
    assert AWS_HOST not in caplog.text
    assert PASSWORD not in caplog.text


async def test_target_walsender_passes_with_target_mapping_and_source_subscription_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="app.executors.postgres_data_sync")
    source = _Database()
    # 도착이 발행자라는 근거만 보므로 walsender 상태는 streaming 이 아니어도 된다.
    target = _Database(walsenders={"echo_sub_rev": _row(state="catchup")})
    cluster = _cluster(source, target)

    result = await _checker(cluster).replication_lag(SOURCE, DESTINATION)

    assert result == TARGET_IS_WRITER
    assert cluster.hosts == [AWS_HOST, GCP_HOST]
    target_call = cluster.calls[1]
    assert (target_call["host"], target_call["port"]) == (GCP_HOST, 5432)
    assert (target_call["user"], target_call["database"]) == ("echo_monitor", "postgres")
    assert target_call["password"] == PASSWORD
    assert target_call["ssl"] is cluster.calls[0]["ssl"]
    assert target_call["timeout"] == cluster.calls[0]["timeout"]
    assert target.queries == [(TARGET_WALSENDER_QUERY, ("echo_sub_rev",))]
    assert (source.close_count, target.close_count) == (1, 1)
    [record] = [r for r in caplog.records if r.message == "data sync target is writer"]
    assert record.__dict__["evidence"] == "TARGET_WALSENDER"
    assert GCP_HOST not in caplog.text
    assert PASSWORD not in caplog.text


async def test_measured_failback_layout_passes_on_source_subscription() -> None:
    # 11:54 실측: 트래픽만 aws → gcp 로 옮긴 뒤 복귀(출발 gcp/B → 도착 aws/A). 쓰기는 A 그대로다.
    # A 에 walsender echo_sub 가 streaming, B 에 구독 echo_sub 가 enabled, B 에 walsender 없음.
    node_a = _Database(walsenders={"echo_sub": _row()})
    node_b = _Database(subscriptions={"echo_sub": True})
    cluster = _Cluster({AWS_HOST: node_a, GCP_HOST: node_b})

    result = await _checker(cluster).replication_lag(DESTINATION, SOURCE)

    assert result == TARGET_IS_WRITER
    assert cluster.hosts == [GCP_HOST]
    assert node_b.queries == [
        (REPLICATION_LAG_QUERY, ("echo_sub_rev",)),
        (SOURCE_SUBSCRIPTION_QUERY, ("echo_sub",)),
    ]
    assert node_a.queries == []


async def test_measured_failback_layout_passes_on_target_walsender_without_subscription() -> None:
    # 같은 배치에서 B 의 구독을 읽어도 없을 때 A 의 walsender 로 판단한다.
    node_a = _Database(walsenders={"echo_sub": _row()})
    node_b = _Database()
    cluster = _Cluster({AWS_HOST: node_a, GCP_HOST: node_b})

    result = await _checker(cluster).replication_lag(DESTINATION, SOURCE)

    assert result == TARGET_IS_WRITER
    assert cluster.hosts == [GCP_HOST, AWS_HOST]
    assert node_a.queries == [(TARGET_WALSENDER_QUERY, ("echo_sub",))]


# --- 규칙 1·5: 근거가 없으면 지금 그대로 판단 불가다 ---


@pytest.mark.parametrize(
    "subscriptions", [{}, {"echo_sub_rev": False}], ids=["no-subscription", "disabled-subscription"]
)
async def test_no_writer_evidence_is_unavailable(
    subscriptions: dict[str, bool], caplog: pytest.LogCaptureFixture
) -> None:
    source = _Database(subscriptions=subscriptions)
    # 다른 구독의 walsender 는 근거가 아니다(질의 인자가 출발 구독 이름이어야 한다).
    target = _Database(walsenders={"echo_sub": _row()})
    cluster = _cluster(source, target)

    result = await _checker(cluster).replication_lag(SOURCE, DESTINATION)

    assert result == DataSyncUnavailable()
    assert _warning_codes(caplog) == [DATA_SYNC_NOT_STREAMING]
    assert cluster.hosts == [AWS_HOST, GCP_HOST]
    assert target.queries == [(TARGET_WALSENDER_QUERY, ("echo_sub_rev",))]


@pytest.mark.parametrize(
    "subscriptions", [{}, {"echo_sub_rev": False}], ids=["no-subscription", "disabled-subscription"]
)
@pytest.mark.parametrize("error", DATABASE_ERRORS, ids=DATABASE_ERROR_IDS)
async def test_target_connection_failure_is_not_streaming(
    subscriptions: dict[str, bool], error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    source = _Database(subscriptions=subscriptions)
    target = _Database(connect_error=error)
    cluster = _cluster(source, target)

    result = await _checker(cluster).replication_lag(SOURCE, DESTINATION)

    assert result == DataSyncUnavailable()
    assert _warning_codes(caplog) == [DATA_SYNC_NOT_STREAMING]
    assert cluster.hosts == [AWS_HOST, GCP_HOST]
    assert GCP_HOST not in caplog.text
    assert PASSWORD not in caplog.text


@pytest.mark.parametrize("error", DATABASE_ERRORS, ids=DATABASE_ERROR_IDS)
async def test_target_query_failure_is_not_streaming_and_closes_connection(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    target = _Database(walsender_error=error)

    result = await _checker(_cluster(_Database(), target)).replication_lag(SOURCE, DESTINATION)

    assert result == DataSyncUnavailable()
    assert _warning_codes(caplog) == [DATA_SYNC_NOT_STREAMING]
    assert target.close_count == 1


async def test_hanging_target_is_cut_by_timeout(caplog: pytest.LogCaptureFixture) -> None:
    class _HangingCluster(_Cluster):
        async def __call__(self, **kwargs: Any) -> _Database:
            if kwargs["host"] == GCP_HOST:
                self.calls.append(kwargs)
                await asyncio.Event().wait()
            return await super().__call__(**kwargs)

    cluster = _HangingCluster({AWS_HOST: _Database()})
    checker = _checker(cluster, timeout_seconds=0.01)

    result = await checker.replication_lag(SOURCE, DESTINATION)

    assert result == DataSyncUnavailable()
    assert _warning_codes(caplog) == [DATA_SYNC_NOT_STREAMING]


@pytest.mark.parametrize("error", DATABASE_ERRORS, ids=DATABASE_ERROR_IDS)
async def test_source_connection_failure_is_unreachable_without_checking_target(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    source = _Database(connect_error=error)
    target = _Database(walsenders={"echo_sub_rev": _row()})
    cluster = _cluster(source, target)

    result = await _checker(cluster).replication_lag(SOURCE, DESTINATION)

    assert result == DataSyncUnavailable()
    assert _warning_codes(caplog) == [DATA_SYNC_UNREACHABLE]
    assert cluster.hosts == [AWS_HOST]


async def test_source_subscription_query_failure_is_unreachable_without_checking_target(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = _Database(subscription_error=asyncpg.InsufficientPrivilegeError("denied"))
    target = _Database(walsenders={"echo_sub_rev": _row()})
    cluster = _cluster(source, target)

    result = await _checker(cluster).replication_lag(SOURCE, DESTINATION)

    assert result == DataSyncUnavailable()
    assert _warning_codes(caplog) == [DATA_SYNC_UNREACHABLE]
    assert cluster.hosts == [AWS_HOST]
    assert source.close_count == 1


def test_data_sync_lag_reason_defaults_to_none() -> None:
    assert DataSyncLag(seconds=0.07).reason is None
    assert DataSyncLag(seconds=0.07) != DataSyncLag(seconds=0.07, reason="TARGET_IS_WRITER")


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
