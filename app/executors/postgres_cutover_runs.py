"""컷오버 결과 읽기(API 서버 16, `data_sync_checker_mode = postgres`).

echo-infra 의 `cutover.sh`·`rollback.sh` 가 고객 DB `echo_ops.cutover_runs` 에 쓴 행을 읽는다. 쓰지
않는다. 접속처·계정·비밀번호·TLS·제한 시간은 데이터 확인과 같은 `data_sync_*` 를 쓰고, 매핑에서는
host·port 만 쓴다(구독 이름은 쓰지 않는다). DB 이름만 `cutover_runs_database` 로 다르다.

호출마다 연결 → fetchrow → close 한다(풀 없음, 부르는 쪽의 캐시가 횟수를 줄인다). 읽지 못하면 예외
대신 CutoverRunUnavailable(code) 이고, 코드에 주소·비밀값·외부 원문을 넣지 않는다.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

import asyncpg  # type: ignore[import-untyped]

from app.clients.secret_client import SecretClient, SecretLookupError
from app.core.secrets import SecretRef
from app.enums import CutoverKind
from app.executors.base import (
    CutoverRun,
    CutoverRunNotFound,
    CutoverRunResult,
    CutoverRunTarget,
    CutoverRunUnavailable,
)
from app.executors.postgres_data_sync import (
    ReplicationEndpoint,
    find_replication_endpoint,
    postgres_ssl_option,
)

CUTOVER_RESULT_ENV_UNMAPPED = "CUTOVER_RESULT_ENV_UNMAPPED"
CUTOVER_RESULT_SECRET_MISSING = "CUTOVER_RESULT_SECRET_MISSING"
CUTOVER_RESULT_UNREACHABLE = "CUTOVER_RESULT_UNREACHABLE"
CUTOVER_RESULT_QUERY_FAILED = "CUTOVER_RESULT_QUERY_FAILED"

# paused_at 이 같으면 created_at, id 순으로 골라 늘 같은 행을 돌려준다.
CUTOVER_RUNS_QUERY = """
SELECT kind, paused_at, resumed_at, write_pause_ms, drain_ms, catchup_ms, switch_ms,
       rows_total, missing_rows, duplicate_rows
FROM echo_ops.cutover_runs
WHERE kind = $1 AND paused_at >= $2 AND ($3::timestamptz IS NULL OR paused_at < $3)
ORDER BY paused_at, created_at, id
LIMIT 1
"""


class Connection(Protocol):
    async def fetchrow(self, query: str, *args: Any) -> Mapping[str, Any] | None: ...

    async def close(self) -> None: ...

    def terminate(self) -> None: ...


Connect = Callable[..., Awaitable[Connection]]


class PostgresCutoverRunReader:
    def __init__(
        self,
        endpoints: Mapping[str, ReplicationEndpoint],
        *,
        user: str,
        database: str,
        password_ref: SecretRef,
        secret_client: SecretClient,
        ssl_mode: str = "require",
        timeout_seconds: float = 3.0,
        connect: Connect = asyncpg.connect,
    ) -> None:
        self._endpoints = dict(endpoints)
        self._user = user
        self._database = database
        self._password_ref = password_ref
        self._secret_client = secret_client
        self._ssl = postgres_ssl_option(ssl_mode)
        self._timeout_seconds = timeout_seconds
        self._connect = connect

    async def find_first(
        self,
        target: CutoverRunTarget,
        kind: CutoverKind,
        since: datetime,
        until: datetime | None,
    ) -> CutoverRunResult:
        endpoint = find_replication_endpoint(
            self._endpoints, target.environment_id, target.environment_kind
        )
        if endpoint is None:
            return CutoverRunUnavailable(CUTOVER_RESULT_ENV_UNMAPPED)
        try:
            password = await self._secret_client.get_secret(self._password_ref)
        except SecretLookupError:
            return CutoverRunUnavailable(CUTOVER_RESULT_SECRET_MISSING)
        try:
            async with asyncio.timeout(self._timeout_seconds):
                connection = await self._connect(
                    host=endpoint.host,
                    port=endpoint.port,
                    user=self._user,
                    password=password.get_secret_value(),
                    database=self._database,
                    ssl=self._ssl,
                    timeout=self._timeout_seconds,
                )
                try:
                    row = await connection.fetchrow(CUTOVER_RUNS_QUERY, kind.value, since, until)
                except BaseException:
                    # 시간 초과로 취소된 뒤 close 를 기다리면 끊긴 망에서 제한 시간을 넘길 수 있다.
                    connection.terminate()
                    raise
                await connection.close()
        except asyncpg.PostgresError:
            return CutoverRunUnavailable(CUTOVER_RESULT_QUERY_FAILED)
        except (OSError, TimeoutError, asyncpg.InterfaceError):
            return CutoverRunUnavailable(CUTOVER_RESULT_UNREACHABLE)
        if row is None:
            return CutoverRunNotFound()
        return _cutover_run(row)


def _cutover_run(row: Mapping[str, Any]) -> CutoverRun:
    return CutoverRun(
        kind=CutoverKind(row["kind"]),
        paused_at=row["paused_at"].astimezone(UTC),
        resumed_at=row["resumed_at"].astimezone(UTC),
        write_pause_ms=row["write_pause_ms"],
        drain_ms=row["drain_ms"],
        catchup_ms=row["catchup_ms"],
        switch_ms=row["switch_ms"],
        rows_total=row["rows_total"],
        missing_rows=row["missing_rows"],
        duplicate_rows=row["duplicate_rows"],
    )
