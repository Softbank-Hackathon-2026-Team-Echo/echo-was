"""이전 계획 상세의 컷오버 결과 result(API 서버 16).

행 목록을 주입한 읽기(SQL 과 같은 규칙) 또는 실제 읽기 + 가짜 connect 로 시험하고 주입 시계를 쓴다.
고객 DB 에 붙지 않는다. 질의 자체는 맨 끝 시험이 시험 DB 의 임시 스키마에서 한 번 실행해 본다.
"""

import asyncio
import dataclasses
import logging
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import DataSyncCheckerMode
from app.core.secrets import parse_secret_ref
from app.dependencies import get_executor_set
from app.enums import CutoverKind, MigrationPlanMode, MigrationPlanStatus
from app.executors.base import (
    CutoverRun,
    CutoverRunNotFound,
    CutoverRunReader,
    CutoverRunResult,
    CutoverRunTarget,
    CutoverRunUnavailable,
)
from app.executors.postgres_cutover_runs import (
    CUTOVER_RESULT_ENV_UNMAPPED,
    CUTOVER_RESULT_QUERY_FAILED,
    CUTOVER_RESULT_SECRET_MISSING,
    CUTOVER_RESULT_UNREACHABLE,
    PostgresCutoverRunReader,
)
from app.executors.postgres_data_sync import ReplicationEndpoint
from app.main import app
from app.services.migration_plan_result import cutover_result_cache
from tests.integration.deploy_harness import DeployHarness
from tests.integration.migration_support import Migration
from tests.integration.rollback_support import deploy_and_finish
from tests.real_executor_fakes import DictSecretClient

FINISHED_AT = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)
NEXT_CREATED_AT = FINISHED_AT + timedelta(minutes=3)
MINUTE = timedelta(minutes=1)
PASSWORD = "monitor-secret"
GCP_HOST = "echo-pg.echo-data.svc.cluster.local"
ENDPOINTS = {"gcp": ReplicationEndpoint(GCP_HOST, 5432, "echo_sub")}
EMPTY_RESULT = {
    "writePauseMs": None,
    "missingRows": None,
    "duplicateRows": None,
    "rowsTotal": None,
    "kind": None,
    "measuredBy": None,
    "pausedAt": None,
    "resumedAt": None,
    "drainMs": None,
    "catchupMs": None,
    "switchMs": None,
}
_ERROR_CODE_BY_STATUS = {
    MigrationPlanStatus.ROLLED_BACK: "THRESHOLD_EXCEEDED",
    MigrationPlanStatus.FAILED: "PREFLIGHT_FAILED",
}


def _run(
    paused_at: datetime = FINISHED_AT + MINUTE,
    *,
    kind: CutoverKind = CutoverKind.CUTOVER,
    write_pause_ms: int = 5817,
    **changes: Any,
) -> CutoverRun:
    values: dict[str, Any] = {
        "kind": kind,
        "paused_at": paused_at,
        "resumed_at": paused_at + timedelta(milliseconds=write_pause_ms),
        "write_pause_ms": write_pause_ms,
        "drain_ms": 774,
        "catchup_ms": 428,
        "switch_ms": 4615,
        "rows_total": 168,
        "missing_rows": 0,
        "duplicate_rows": 0,
    }
    return CutoverRun(**{**values, **changes})


@dataclasses.dataclass
class _Call:
    target: CutoverRunTarget
    kind: CutoverKind
    since: datetime
    until: datetime | None


class _RowsReader:
    """행 목록을 질의와 같은 규칙(종류·반열린 창·paused_at 순)으로 찾는 시험용 읽기."""

    def __init__(self, rows: list[CutoverRun] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[_Call] = []

    async def find_first(
        self,
        target: CutoverRunTarget,
        kind: CutoverKind,
        since: datetime,
        until: datetime | None,
    ) -> CutoverRunResult:
        self.calls.append(_Call(target, kind, since, until))
        found = [
            row
            for row in self.rows
            if row.kind is kind
            and row.paused_at >= since
            and (until is None or row.paused_at < until)
        ]
        return min(found, key=lambda row: row.paused_at) if found else CutoverRunNotFound()


class _FailingReader(_RowsReader):
    async def find_first(
        self,
        target: CutoverRunTarget,
        kind: CutoverKind,
        since: datetime,
        until: datetime | None,
    ) -> CutoverRunResult:
        self.calls.append(_Call(target, kind, since, until))
        return CutoverRunUnavailable(CUTOVER_RESULT_UNREACHABLE)


class _Connection:
    def __init__(self, error: Exception | None, hangs: bool) -> None:
        self.error = error
        self.hangs = hangs

    async def fetchrow(self, query: str, *args: Any) -> Mapping[str, Any] | None:
        if self.hangs:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error
        return None

    async def close(self) -> None:
        if self.hangs:
            await asyncio.Event().wait()

    def terminate(self) -> None:
        pass


def _postgres_reader(
    *,
    endpoints: Mapping[str, ReplicationEndpoint] = ENDPOINTS,
    secrets: dict[str, str] | None = None,
    connect_error: Exception | None = None,
    query_error: Exception | None = None,
    query_hangs: bool = False,
    timeout_seconds: float = 3.0,
) -> PostgresCutoverRunReader:
    async def connect(**_: Any) -> _Connection:
        if connect_error is not None:
            raise connect_error
        return _Connection(query_error, query_hangs)

    return PostgresCutoverRunReader(
        endpoints,
        user="echo_monitor",
        database="app",
        password_ref=parse_secret_ref("env:ECHO_SECRET_DATA_SYNC_PASSWORD"),
        secret_client=DictSecretClient(
            {"ECHO_SECRET_DATA_SYNC_PASSWORD": PASSWORD} if secrets is None else secrets
        ),
        timeout_seconds=timeout_seconds,
        connect=connect,
    )


def _use_reader(harness: DeployHarness, reader: CutoverRunReader, **settings: Any) -> None:
    """읽는 조건을 켜고(시험이 settings 로 다시 끌 수 있다) 실행기 묶음에 읽기를 넣는다."""
    harness.settings = harness.settings.model_copy(
        update={
            "data_sync_checker_mode": DataSyncCheckerMode.POSTGRES,
            "cutover_runs_database": "app",
            "cutover_results_not_before": None,
            "cutover_result_cache_seconds": 5,
            **settings,
        }
    )
    app.dependency_overrides[get_executor_set] = lambda: dataclasses.replace(
        harness.executors.as_set(), cutover_runs=reader
    )


async def _set_plan(migration: Migration, plan_id: str, **columns: Any) -> None:
    """계획 행을 바로 고친다(실행 없이 이력을 만든다). ORM 이 아니라서 값을 그대로 쓴다."""
    assignments = ", ".join(f"{column} = :{column}" for column in columns)
    async with migration.harness.session_factory() as session, session.begin():
        await session.execute(
            text(f"UPDATE migration_plans SET {assignments} WHERE id = :id"),
            {**columns, "id": plan_id},
        )


async def _finished_plan(
    migration: Migration,
    *,
    status: MigrationPlanStatus = MigrationPlanStatus.SUCCEEDED,
    mode: MigrationPlanMode = MigrationPlanMode.PLANNED,
    finished_at: datetime = FINISHED_AT,
    simulated: bool = False,
) -> str:
    """실제 이전으로 끝난 계획 하나. created_at 은 finished_at 10분 전이다."""
    plan = await migration.create_plan()
    await _set_plan(
        migration,
        plan["id"],
        status=str(status),
        mode=str(mode),
        finished_at=finished_at,
        created_at=finished_at - timedelta(minutes=10),
        error_code=_ERROR_CODE_BY_STATUS.get(status),
        simulated=simulated,
    )
    return str(plan["id"])


async def _next_plan(migration: Migration, created_at: datetime = NEXT_CREATED_AT) -> str:
    plan = await migration.create_plan()
    await _set_plan(migration, plan["id"], created_at=created_at)
    return str(plan["id"])


async def _result(migration: Migration, plan_id: str) -> dict[str, Any] | None:
    result: dict[str, Any] | None = (await migration.get_plan(plan_id))["result"]
    return result


# --- AC-1: 행 값이 그대로 ---


async def test_get_plan_finished_with_row_returns_all_eleven_fields(migration: Migration) -> None:
    run = _run(
        datetime(2026, 11, 5, 3, 1, 59, 670000, tzinfo=UTC),
        write_pause_ms=5817,
        resumed_at=datetime(2026, 11, 5, 3, 2, 5, 487000, tzinfo=UTC),
        rows_total=168,
        missing_rows=1,
        duplicate_rows=2,
    )
    reader = _RowsReader([run])
    _use_reader(migration.harness, reader)
    plan_id = await _finished_plan(migration)

    assert await _result(migration, plan_id) == {
        "writePauseMs": 5817,
        "missingRows": 1,
        "duplicateRows": 2,
        "rowsTotal": 168,
        "kind": "CUTOVER",
        "measuredBy": "INFRA_SCRIPT",
        "pausedAt": "2026-11-05T03:01:59.670000Z",
        "resumedAt": "2026-11-05T03:02:05.487000Z",
        "drainMs": 774,
        "catchupMs": 428,
        "switchMs": 4615,
    }


async def test_get_plan_row_with_null_column_nulls_only_that_field(migration: Migration) -> None:
    _use_reader(migration.harness, _RowsReader([_run(drain_ms=None, missing_rows=None)]))
    plan_id = await _finished_plan(migration)

    result = await _result(migration, plan_id)

    assert result is not None
    assert result["drainMs"] is None
    assert result["missingRows"] is None
    assert result["writePauseMs"] == 5817
    assert result["measuredBy"] == "INFRA_SCRIPT"


async def test_get_plan_reads_target_environment_of_the_plan(migration: Migration) -> None:
    reader = _RowsReader([_run()])
    _use_reader(migration.harness, reader)
    plan_id = await _finished_plan(migration)

    await migration.get_plan(plan_id)

    assert reader.calls[0].target == CutoverRunTarget(migration.gcp.id, "gcp")


async def test_get_plan_after_real_worker_success_returns_row(migration: Migration) -> None:
    reader = _RowsReader()
    _use_reader(migration.harness, reader)
    plan = await migration.create_plan()
    await _set_plan(migration, plan["id"], simulated=False)
    await migration.approve(plan["id"])

    await migration.harness.run_until_idle()

    finished = await migration.get_plan(plan["id"])
    assert finished["status"] == "SUCCEEDED"
    assert finished["result"] == EMPTY_RESULT
    finished_at = datetime.fromisoformat(finished["finishedAt"])
    reader.rows.append(_run(finished_at + MINUTE))
    migration.harness.clock.advance(5)
    assert (await _result(migration, plan["id"]) or {})["writePauseMs"] == 5817


# --- AC-2: 창 규칙 ---


async def test_window_excludes_row_before_finished_at(migration: Migration) -> None:
    _use_reader(migration.harness, _RowsReader([_run(FINISHED_AT - timedelta(microseconds=1))]))
    plan_id = await _finished_plan(migration)

    assert await _result(migration, plan_id) == EMPTY_RESULT


async def test_window_includes_row_exactly_at_finished_at(migration: Migration) -> None:
    _use_reader(migration.harness, _RowsReader([_run(FINISHED_AT)]))
    plan_id = await _finished_plan(migration)

    assert (await _result(migration, plan_id) or {})["writePauseMs"] == 5817


async def test_window_excludes_row_at_or_after_next_plan_created_at(migration: Migration) -> None:
    reader = _RowsReader([_run(NEXT_CREATED_AT), _run(NEXT_CREATED_AT + MINUTE)])
    _use_reader(migration.harness, reader)
    plan_id = await _finished_plan(migration)
    await _next_plan(migration)

    assert await _result(migration, plan_id) == EMPTY_RESULT
    assert reader.calls[0].until == NEXT_CREATED_AT


async def test_window_includes_row_just_before_next_plan_created_at(migration: Migration) -> None:
    just_before = NEXT_CREATED_AT - timedelta(microseconds=1)
    _use_reader(migration.harness, _RowsReader([_run(just_before)]))
    plan_id = await _finished_plan(migration)
    await _next_plan(migration)

    assert (await _result(migration, plan_id) or {})["kind"] == "CUTOVER"


async def test_window_uses_earliest_row_when_several_match(migration: Migration) -> None:
    rows = [
        _run(FINISHED_AT + 2 * MINUTE, write_pause_ms=2222),
        _run(FINISHED_AT + MINUTE, write_pause_ms=1111),
    ]
    _use_reader(migration.harness, _RowsReader(rows))
    plan_id = await _finished_plan(migration)

    assert (await _result(migration, plan_id) or {})["writePauseMs"] == 1111


@pytest.mark.parametrize(
    ("mode", "kind", "other"),
    [
        (MigrationPlanMode.PLANNED, CutoverKind.CUTOVER, CutoverKind.ROLLBACK),
        (MigrationPlanMode.EMERGENCY, CutoverKind.CUTOVER, CutoverKind.ROLLBACK),
        (MigrationPlanMode.FAILBACK, CutoverKind.ROLLBACK, CutoverKind.CUTOVER),
    ],
)
async def test_window_kind_follows_plan_mode(
    migration: Migration, mode: MigrationPlanMode, kind: CutoverKind, other: CutoverKind
) -> None:
    rows = [
        _run(FINISHED_AT + MINUTE, kind=other, write_pause_ms=9999),
        _run(FINISHED_AT + 2 * MINUTE, kind=kind, write_pause_ms=1234),
    ]
    reader = _RowsReader(rows)
    _use_reader(migration.harness, reader)
    plan_id = await _finished_plan(migration, mode=mode)

    result = await _result(migration, plan_id)

    assert result is not None
    assert (result["kind"], result["writePauseMs"]) == (str(kind), 1234)
    assert reader.calls[0].kind is kind


@pytest.mark.parametrize(
    "mode", [MigrationPlanMode.PLANNED, MigrationPlanMode.FAILBACK, MigrationPlanMode.EMERGENCY]
)
async def test_window_other_kind_only_gives_empty_result(
    migration: Migration, mode: MigrationPlanMode
) -> None:
    other = CutoverKind.CUTOVER if mode is MigrationPlanMode.FAILBACK else CutoverKind.ROLLBACK
    _use_reader(migration.harness, _RowsReader([_run(kind=other)]))
    plan_id = await _finished_plan(migration, mode=mode)

    assert await _result(migration, plan_id) == EMPTY_RESULT


async def test_window_without_next_plan_has_no_end(migration: Migration) -> None:
    far_later = FINISHED_AT + timedelta(days=30)
    reader = _RowsReader([_run(far_later)])
    _use_reader(migration.harness, reader)
    plan_id = await _finished_plan(migration)

    assert (await _result(migration, plan_id) or {})["writePauseMs"] == 5817
    assert reader.calls[0].since == FINISHED_AT
    assert reader.calls[0].until is None


async def test_window_ignores_next_plan_of_other_service(migration: Migration) -> None:
    harness = migration.harness
    reader = _RowsReader([_run(NEXT_CREATED_AT + MINUTE)])
    _use_reader(harness, reader)
    plan_id = await _finished_plan(migration)
    harness.host_suffixes.queue("o2t3h4e5")
    other = await harness.create_service("other-api")
    await deploy_and_finish(harness, other["id"], migration.aws.id, branch="main")
    await deploy_and_finish(harness, other["id"], migration.gcp.id, branch="main")
    created = await migration.author_client.post(
        f"{harness.services}/{other['id']}/migration-plans",
        json={"targetEnvironmentId": str(migration.gcp.id)},
    )
    assert created.status_code == 201, created.text
    await _set_plan(migration, created.json()["data"]["id"], created_at=NEXT_CREATED_AT)

    assert (await _result(migration, plan_id) or {})["writePauseMs"] == 5817
    assert reader.calls[0].until is None


async def test_window_failback_flow_keeps_earlier_plan_value(migration: Migration) -> None:
    """이전 계획 값은 다음 계획이 생겨도 그대로이고, 복귀 계획은 ROLLBACK 만 본다(시연 순서)."""
    cutover = _run(FINISHED_AT + MINUTE, write_pause_ms=5817)
    rollback_paused_at = NEXT_CREATED_AT + 2 * MINUTE
    rollback = _run(rollback_paused_at, kind=CutoverKind.ROLLBACK, write_pause_ms=6645)
    _use_reader(migration.harness, _RowsReader([cutover, rollback]))
    first = await _finished_plan(migration)
    second = await _next_plan(migration)
    assert await _result(migration, second) is None
    await _set_plan(
        migration,
        second,
        status=str(MigrationPlanStatus.SUCCEEDED),
        mode=str(MigrationPlanMode.FAILBACK),
        finished_at=NEXT_CREATED_AT + MINUTE,
        simulated=False,
    )

    first_result = await _result(migration, first)
    second_result = await _result(migration, second)

    assert first_result is not None
    assert (first_result["kind"], first_result["writePauseMs"]) == ("CUTOVER", 5817)
    assert second_result is not None
    assert (second_result["kind"], second_result["writePauseMs"]) == ("ROLLBACK", 6645)


@pytest.mark.parametrize(
    "status",
    [
        MigrationPlanStatus.SUCCEEDED,
        MigrationPlanStatus.ROLLED_BACK,
        MigrationPlanStatus.FAILED,
        MigrationPlanStatus.REJECTED,
        MigrationPlanStatus.CANCELLED,
        MigrationPlanStatus.EXPIRED,
        MigrationPlanStatus.STALE,
    ],
)
async def test_window_applies_to_every_terminal_status(
    migration: Migration, status: MigrationPlanStatus
) -> None:
    _use_reader(migration.harness, _RowsReader([_run()]))
    plan_id = await _finished_plan(migration, status=status)

    assert (await _result(migration, plan_id) or {})["writePauseMs"] == 5817


# --- AC-3: null·200 ---


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.msg == "cutover result unavailable"]


async def test_no_row_returns_empty_result_without_warning(
    migration: Migration, caplog: pytest.LogCaptureFixture
) -> None:
    _use_reader(migration.harness, _RowsReader())
    plan_id = await _finished_plan(migration)

    assert await _result(migration, plan_id) == EMPTY_RESULT
    assert _warnings(caplog) == []


@pytest.mark.parametrize(
    ("reader_args", "code"),
    [
        ({"endpoints": {}}, CUTOVER_RESULT_ENV_UNMAPPED),
        ({"secrets": {}}, CUTOVER_RESULT_SECRET_MISSING),
        (
            {"connect_error": OSError(f"could not connect to {GCP_HOST} with {PASSWORD}")},
            CUTOVER_RESULT_UNREACHABLE,
        ),
        ({"connect_error": TimeoutError()}, CUTOVER_RESULT_UNREACHABLE),
        ({"query_hangs": True, "timeout_seconds": 0.05}, CUTOVER_RESULT_UNREACHABLE),
        (
            {"query_error": asyncpg.InsufficientPrivilegeError("permission denied")},
            CUTOVER_RESULT_QUERY_FAILED,
        ),
        (
            {"query_error": asyncpg.UndefinedTableError("relation does not exist")},
            CUTOVER_RESULT_QUERY_FAILED,
        ),
    ],
    ids=[
        "unmapped",
        "secret-missing",
        "connect-refused",
        "connect-timeout",
        "query-over-limit",
        "no-permission",
        "no-table",
    ],
)
async def test_read_failure_returns_200_with_empty_result_and_safe_warning(
    migration: Migration,
    caplog: pytest.LogCaptureFixture,
    reader_args: dict[str, Any],
    code: str,
) -> None:
    _use_reader(migration.harness, _postgres_reader(**reader_args))
    plan_id = await _finished_plan(migration)

    with caplog.at_level(logging.WARNING):
        assert await _result(migration, plan_id) == EMPTY_RESULT

    [record] = _warnings(caplog)
    assert record.levelno == logging.WARNING
    assert record.action == "find_cutover_run"  # type: ignore[attr-defined]
    assert record.plan_id == plan_id  # type: ignore[attr-defined]
    assert record.target_environment_id == str(migration.gcp.id)  # type: ignore[attr-defined]
    assert record.error_code == code  # type: ignore[attr-defined]
    logged = caplog.text + repr(vars(record))
    assert PASSWORD not in logged
    assert GCP_HOST not in logged


async def test_read_failure_response_holds_no_address_or_secret(migration: Migration) -> None:
    error = OSError(f"could not connect to {GCP_HOST} with {PASSWORD}")
    _use_reader(migration.harness, _postgres_reader(connect_error=error))
    plan_id = await _finished_plan(migration)

    response = await migration.harness.client.get(migration.plan_path(plan_id))

    assert response.status_code == 200
    assert GCP_HOST not in response.text
    assert PASSWORD not in response.text


async def test_failed_read_stays_under_three_seconds_when_network_hangs(
    migration: Migration,
) -> None:
    """응답 시간 상한: 접속·질의·닫기가 모두 멈춘 망에서도 기본 제한 3초로 끝난다."""
    _use_reader(migration.harness, _postgres_reader(query_hangs=True, timeout_seconds=3.0))
    plan_id = await _finished_plan(migration)

    started = time.monotonic()
    assert await _result(migration, plan_id) == EMPTY_RESULT
    elapsed = time.monotonic() - started

    assert 2.9 < elapsed < 3.5


async def test_unfinished_plan_has_null_result_and_no_read(migration: Migration) -> None:
    reader = _RowsReader([_run()])
    _use_reader(migration.harness, reader)
    plan = await migration.create_plan()
    await _set_plan(migration, plan["id"], simulated=False)

    assert await _result(migration, plan["id"]) is None
    await migration.approve(plan["id"])
    assert await _result(migration, plan["id"]) is None

    assert reader.calls == []


@pytest.mark.parametrize(
    "settings",
    [
        {"data_sync_checker_mode": DataSyncCheckerMode.FAKE},
        {"cutover_runs_database": ""},
        {"cutover_results_not_before": FINISHED_AT + timedelta(microseconds=1)},
    ],
    ids=["fake-mode", "empty-database", "before-not-before"],
)
async def test_read_conditions_off_give_empty_result_and_no_read(
    migration: Migration, settings: dict[str, Any]
) -> None:
    reader = _RowsReader([_run()])
    _use_reader(migration.harness, reader, **settings)
    plan_id = await _finished_plan(migration)

    assert await _result(migration, plan_id) == EMPTY_RESULT
    assert reader.calls == []


async def test_simulated_plan_gives_empty_result_and_no_read(migration: Migration) -> None:
    reader = _RowsReader([_run()])
    _use_reader(migration.harness, reader)
    plan_id = await _finished_plan(migration, simulated=True)

    assert await _result(migration, plan_id) == EMPTY_RESULT
    assert reader.calls == []


async def test_not_before_includes_plan_finished_exactly_then(migration: Migration) -> None:
    reader = _RowsReader([_run()])
    _use_reader(migration.harness, reader, cutover_results_not_before=FINISHED_AT)
    plan_id = await _finished_plan(migration)

    assert (await _result(migration, plan_id) or {})["writePauseMs"] == 5817


async def test_default_executor_set_gives_empty_result(migration: Migration) -> None:
    """기본 읽기(NoCutoverRuns)는 조건이 맞아도 가짜 숫자를 만들지 않는다."""
    harness = migration.harness
    _use_reader(harness, _RowsReader())
    app.dependency_overrides[get_executor_set] = lambda: harness.executors.as_set()
    plan_id = await _finished_plan(migration)

    assert await _result(migration, plan_id) == EMPTY_RESULT


# --- AD-7: 상세 GET 만 읽는다 ---


@pytest.mark.parametrize(
    ("action", "client_attr"), [("reject", "approver_client"), ("cancel", "author_client")]
)
async def test_write_response_of_finished_plan_has_empty_result_and_no_read(
    migration: Migration, action: str, client_attr: str
) -> None:
    reader = _RowsReader([_run()])
    _use_reader(migration.harness, reader)
    plan = await migration.create_plan()
    await _set_plan(migration, plan["id"], simulated=False)

    response = await migration.act(plan["id"], action, client=getattr(migration, client_attr))

    assert response.status_code == 200, response.text
    assert response.json()["data"]["result"] == EMPTY_RESULT
    assert reader.calls == []


async def test_write_responses_of_open_plan_have_null_result(migration: Migration) -> None:
    reader = _RowsReader([_run()])
    _use_reader(migration.harness, reader)

    created = await migration.create_plan()
    approved = await migration.approve(created["id"])

    assert created["result"] is None
    assert approved["result"] is None
    assert reader.calls == []


async def test_list_and_logs_have_no_result_and_do_not_read(migration: Migration) -> None:
    reader = _RowsReader([_run()])
    _use_reader(migration.harness, reader)
    plan_id = await _finished_plan(migration)

    listed = await migration.harness.client.get(migration.plans_path)
    logs = await migration.logs(plan_id)

    assert listed.status_code == 200
    assert "result" not in listed.json()["data"]["items"][0]
    assert logs.status_code == 200
    assert "writePauseMs" not in logs.text
    assert reader.calls == []


# --- AC-7: 캐시 ---


async def test_cache_two_gets_within_five_seconds_read_once(migration: Migration) -> None:
    reader = _RowsReader()
    _use_reader(migration.harness, reader)
    plan_id = await _finished_plan(migration)

    await migration.get_plan(plan_id)
    migration.harness.clock.advance(4.9)
    await migration.get_plan(plan_id)

    assert len(reader.calls) == 1


async def test_cache_found_with_closed_window_never_reads_again(migration: Migration) -> None:
    reader = _RowsReader([_run()])
    _use_reader(migration.harness, reader)
    plan_id = await _finished_plan(migration)
    await _next_plan(migration)

    first = await _result(migration, plan_id)
    migration.harness.clock.advance(86400)
    second = await _result(migration, plan_id)

    assert first == second
    assert first is not None
    assert first["writePauseMs"] == 5817
    assert len(reader.calls) == 1


async def test_cache_found_with_open_window_reads_again_after_five_seconds(
    migration: Migration,
) -> None:
    reader = _RowsReader([_run()])
    _use_reader(migration.harness, reader)
    plan_id = await _finished_plan(migration)

    await migration.get_plan(plan_id)
    migration.harness.clock.advance(5)
    await migration.get_plan(plan_id)

    assert len(reader.calls) == 2


async def test_cache_not_found_reads_again_after_five_seconds_and_shows_new_row(
    migration: Migration,
) -> None:
    reader = _RowsReader()
    _use_reader(migration.harness, reader)
    plan_id = await _finished_plan(migration)

    assert await _result(migration, plan_id) == EMPTY_RESULT
    reader.rows.append(_run())
    assert await _result(migration, plan_id) == EMPTY_RESULT
    migration.harness.clock.advance(5)

    assert (await _result(migration, plan_id) or {})["writePauseMs"] == 5817
    assert len(reader.calls) == 2


async def test_cache_not_found_with_closed_window_reads_again_after_five_seconds(
    migration: Migration,
) -> None:
    reader = _RowsReader()
    _use_reader(migration.harness, reader)
    plan_id = await _finished_plan(migration)
    await _next_plan(migration)

    await migration.get_plan(plan_id)
    migration.harness.clock.advance(5)
    await migration.get_plan(plan_id)

    assert len(reader.calls) == 2


async def test_cache_failure_reads_again_after_five_seconds(
    migration: Migration, caplog: pytest.LogCaptureFixture
) -> None:
    reader = _FailingReader()
    _use_reader(migration.harness, reader)
    plan_id = await _finished_plan(migration)

    with caplog.at_level(logging.WARNING):
        await migration.get_plan(plan_id)
        await migration.get_plan(plan_id)
        migration.harness.clock.advance(5)
        await migration.get_plan(plan_id)

    assert len(reader.calls) == 2
    assert len(_warnings(caplog)) == 2


async def test_cache_seconds_follow_setting(migration: Migration) -> None:
    reader = _RowsReader()
    _use_reader(migration.harness, reader, cutover_result_cache_seconds=30)
    plan_id = await _finished_plan(migration)

    await migration.get_plan(plan_id)
    migration.harness.clock.advance(29)
    await migration.get_plan(plan_id)
    migration.harness.clock.advance(1)
    await migration.get_plan(plan_id)

    assert len(reader.calls) == 2


async def test_running_plan_polled_every_second_reads_nothing(migration: Migration) -> None:
    harness = migration.harness
    reader = _RowsReader()
    _use_reader(harness, reader)
    plan = await migration.create_plan()
    await _set_plan(migration, plan["id"], simulated=False)
    await migration.approve(plan["id"])
    worker = harness.worker()
    seen: set[str] = set()

    for _ in range(400):
        data = await migration.get_plan(plan["id"])
        if data["status"] == "SUCCEEDED":
            break
        seen.add(data["status"])
        assert data["result"] is None
        assert reader.calls == []
        if not await worker.run_once():
            harness.clock.advance(1)

    # 끝나기 전 폴링은 읽지 않았다(루프 안에서 확인). 끝난 뒤의 조회 한 번만 읽는다.
    assert "RUNNING" in seen
    assert len(reader.calls) == 1


async def test_concurrent_gets_of_same_plan_read_once(migration: Migration) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class _BlockingReader(_RowsReader):
        async def find_first(
            self,
            target: CutoverRunTarget,
            kind: CutoverKind,
            since: datetime,
            until: datetime | None,
        ) -> CutoverRunResult:
            entered.set()
            await release.wait()
            return await super().find_first(target, kind, since, until)

    reader = _BlockingReader([_run()])
    _use_reader(migration.harness, reader)
    plan_id = await _finished_plan(migration)

    gets = [asyncio.create_task(_result(migration, plan_id)) for _ in range(3)]
    await entered.wait()
    await asyncio.sleep(0.05)
    release.set()
    results = await asyncio.gather(*gets)

    assert len(reader.calls) == 1
    assert all(result == results[0] for result in results)
    assert (results[0] or {})["writePauseMs"] == 5817
    assert cutover_result_cache(app.state)._locks == {}


# --- 질의 자체: 시험 DB 임시 스키마에서 실제 asyncpg 로 실행 ---


async def test_query_runs_on_postgres_with_window_order_and_null_until(
    engine: AsyncEngine, test_database_url: str
) -> None:
    url = make_url(test_database_url)
    connection = await asyncpg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        database=url.database,
    )
    tie_at = FINISHED_AT + MINUTE
    first_id = uuid.UUID(int=1)
    second_id = uuid.UUID(int=2)
    try:
        await connection.execute("DROP SCHEMA IF EXISTS echo_ops CASCADE")
        await connection.execute("CREATE SCHEMA echo_ops")
        await connection.execute(
            """
            CREATE TABLE echo_ops.cutover_runs (
              id uuid PRIMARY KEY,
              kind text NOT NULL CHECK (kind IN ('CUTOVER', 'ROLLBACK')),
              from_side text NOT NULL,
              to_side text NOT NULL,
              paused_at timestamptz NOT NULL,
              resumed_at timestamptz NOT NULL,
              write_pause_ms integer NOT NULL,
              drain_ms integer, catchup_ms integer, switch_ms integer,
              rows_total bigint, missing_rows bigint, duplicate_rows bigint, max_n bigint,
              created_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        rows = [
            # 창 앞, 다른 종류, 같은 시각 둘(created_at 순), 창 끝 뒤.
            (uuid.UUID(int=3), "CUTOVER", FINISHED_AT - MINUTE, 1000, tie_at),
            (uuid.UUID(int=4), "ROLLBACK", tie_at - timedelta(seconds=1), 2000, tie_at),
            (second_id, "CUTOVER", tie_at, 4000, tie_at + timedelta(seconds=1)),
            (first_id, "CUTOVER", tie_at, 3000, tie_at),
            (uuid.UUID(int=5), "CUTOVER", NEXT_CREATED_AT, 5000, NEXT_CREATED_AT),
        ]
        for row_id, kind, paused_at, write_pause_ms, created_at in rows:
            await connection.execute(
                "INSERT INTO echo_ops.cutover_runs (id, kind, from_side, to_side, paused_at, "
                "resumed_at, write_pause_ms, created_at) VALUES ($1, $2, 'A', 'B', $3, $3, $4, $5)",
                row_id,
                kind,
                paused_at,
                write_pause_ms,
                created_at,
            )
        reader = PostgresCutoverRunReader(
            {"gcp": ReplicationEndpoint(str(url.host), int(url.port or 5432), "unused")},
            user=str(url.username),
            database=str(url.database),
            password_ref=parse_secret_ref("env:ECHO_SECRET_DATA_SYNC_PASSWORD"),
            secret_client=DictSecretClient({"ECHO_SECRET_DATA_SYNC_PASSWORD": str(url.password)}),
            ssl_mode="disable",
        )
        target = CutoverRunTarget(uuid.uuid4(), "gcp")

        with_end = await reader.find_first(
            target, CutoverKind.CUTOVER, FINISHED_AT, NEXT_CREATED_AT
        )
        no_end_found = await reader.find_first(target, CutoverKind.CUTOVER, NEXT_CREATED_AT, None)
        closed_empty = await reader.find_first(
            target, CutoverKind.ROLLBACK, FINISHED_AT + 2 * MINUTE, NEXT_CREATED_AT
        )

        assert isinstance(with_end, CutoverRun)
        assert (with_end.write_pause_ms, with_end.paused_at) == (3000, tie_at)
        assert with_end.drain_ms is None
        assert isinstance(no_end_found, CutoverRun)
        assert no_end_found.write_pause_ms == 5000
        assert closed_empty == CutoverRunNotFound()
    finally:
        await connection.execute("DROP SCHEMA IF EXISTS echo_ops CASCADE")
        await connection.close()
