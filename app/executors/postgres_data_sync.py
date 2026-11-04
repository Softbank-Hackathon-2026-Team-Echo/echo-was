"""실제 데이터 확인 실행기(`data_sync_checker_mode = postgres`, 인프라 7).

출발(발행 쪽) PostgreSQL 의 `pg_stat_replication` 에서 도착 구독의 `replay_lag` 를 읽는다.
echo-infra `replication/k8s/scripts/lag.sh` 와 같은 질의다. 도착 구독 이름은 walsender 의
application_name 으로 보인다(정방향 A→B 는 B 의 `echo_sub`, 역방향 B→A 는 A 의 `echo_sub_rev`).

접속처는 `data_sync_databases`(`<환경 id 또는 종류>=<host>:<port>/<그 환경의 구독 이름>`)로 정한다.
환경 id 매핑 → 환경 종류 매핑 순서로 찾는다(진입점 환경 매핑과 같다). 계정은 `pg_monitor`
권한만 가진 읽기 전용 계정이고 비밀번호는 비밀 참조로만 받는다.

매핑이 없거나·비밀값을 못 읽거나·접속이 안 되거나·구독이 붙어 있지 않으면(streaming 아님)
예외 대신 DataSyncUnavailable 이다. 경고 로그에는 코드와 환경만 남긴다(주소·비밀값 없음).
"""

import asyncio
import logging
import ssl
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import asyncpg  # type: ignore[import-untyped]

from app.clients.secret_client import SecretClient, SecretLookupError
from app.core.config import REPLICATION_ENDPOINT_PATTERN
from app.core.secrets import SecretRef
from app.executors.base import DataSyncLag, DataSyncResult, DataSyncUnavailable, DeployTarget

logger = logging.getLogger(__name__)

DATA_SYNC_ENV_UNMAPPED = "DATA_SYNC_ENV_UNMAPPED"
DATA_SYNC_SECRET_MISSING = "DATA_SYNC_SECRET_MISSING"
DATA_SYNC_UNREACHABLE = "DATA_SYNC_UNREACHABLE"
DATA_SYNC_NOT_STREAMING = "DATA_SYNC_NOT_STREAMING"

# 발행 쪽에서 본 구독자 하나. replay_lag 가 NULL 이면(쓰기가 멈춰 측정할 것이 없음)
# 남은 WAL 바이트로 따라잡았는지 본다.
REPLICATION_LAG_QUERY = """
SELECT state,
       EXTRACT(EPOCH FROM replay_lag)::float8 AS lag_seconds,
       pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)::float8 AS bytes_behind
FROM pg_stat_replication
WHERE application_name = $1
ORDER BY replay_lag DESC NULLS LAST
LIMIT 1
"""


@dataclass(frozen=True)
class ReplicationEndpoint:
    """환경 하나의 PostgreSQL 주소와 그 환경이 구독자일 때의 구독 이름."""

    host: str
    port: int
    subscription: str


def parse_replication_endpoint(value: str) -> ReplicationEndpoint:
    """`<host>:<port>/<구독 이름>` 을 나눈다. 형식이 틀리면 ValueError."""
    match = REPLICATION_ENDPOINT_PATTERN.fullmatch(value.strip())
    if match is None or not 1 <= int(match["port"]) <= 65535:
        raise ValueError("replication endpoint must be <host>:<port>/<subscription>")
    return ReplicationEndpoint(match["host"], int(match["port"]), match["subscription"])


def parse_replication_endpoints(items: list[str]) -> dict[str, ReplicationEndpoint]:
    """`<환경 id 또는 종류>=<host>:<port>/<구독 이름>` 목록을 사전으로. 형식이 틀리면 ValueError."""
    endpoints: dict[str, ReplicationEndpoint] = {}
    for item in items:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise ValueError("data_sync_databases items must be <environment>=<endpoint>")
        endpoints[key.strip()] = parse_replication_endpoint(value)
    return endpoints


def find_replication_endpoint(
    endpoints: Mapping[str, ReplicationEndpoint], environment_id: uuid.UUID, environment_kind: str
) -> ReplicationEndpoint | None:
    """환경 id 매핑 → 환경 종류 매핑 순서로 찾는다. 없으면 None."""
    by_id = endpoints.get(str(environment_id))
    if by_id is not None:
        return by_id
    return endpoints.get(environment_kind)


class Connection(Protocol):
    async def fetchrow(self, query: str, *args: Any) -> Mapping[str, Any] | None: ...

    async def close(self) -> None: ...


Connect = Callable[..., Awaitable[Connection]]


class PostgresDataSyncChecker:
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

    @property
    def simulated(self) -> bool:
        return False

    def endpoint(self, target: DeployTarget) -> ReplicationEndpoint | None:
        """연결의 PostgreSQL. 환경 id 매핑 → 환경 종류 매핑. 없으면 None."""
        return find_replication_endpoint(
            self._endpoints, target.environment_id, target.environment_kind
        )

    async def replication_lag(self, source: DeployTarget, target: DeployTarget) -> DataSyncResult:
        publisher = self.endpoint(source)
        subscriber = self.endpoint(target)
        if publisher is None or subscriber is None:
            return _unavailable(source, target, DATA_SYNC_ENV_UNMAPPED)
        try:
            password = await self._secret_client.get_secret(self._password_ref)
        except SecretLookupError:
            return _unavailable(source, target, DATA_SYNC_SECRET_MISSING)
        try:
            async with asyncio.timeout(self._timeout_seconds):
                connection = await self._connect(
                    host=publisher.host,
                    port=publisher.port,
                    user=self._user,
                    password=password.get_secret_value(),
                    database=self._database,
                    ssl=self._ssl,
                    timeout=self._timeout_seconds,
                )
                try:
                    row = await connection.fetchrow(REPLICATION_LAG_QUERY, subscriber.subscription)
                finally:
                    await connection.close()
        except (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError):
            return _unavailable(source, target, DATA_SYNC_UNREACHABLE)
        lag = _lag_seconds(row)
        if lag is None:
            return _unavailable(source, target, DATA_SYNC_NOT_STREAMING)
        return DataSyncLag(seconds=lag)


def _lag_seconds(row: Mapping[str, Any] | None) -> float | None:
    """streaming 인 구독자의 지연(초). NULL 이면 남은 WAL 이 0 일 때만 0 초로 본다."""
    if row is None or row["state"] != "streaming":
        return None
    if row["lag_seconds"] is not None:
        return max(float(row["lag_seconds"]), 0.0)
    if row["bytes_behind"] is not None and float(row["bytes_behind"]) <= 0:
        return 0.0
    return None


def postgres_ssl_option(ssl_mode: str) -> ssl.SSLContext | bool:
    """require 는 암호화만(자체 서명 인증서, 검증 없음), verify-full 은 시스템 CA 로 검증,
    disable 은 평문(로컬 PoC 시험용)."""
    if ssl_mode == "disable":
        return False
    if ssl_mode == "verify-full":
        return ssl.create_default_context()
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _unavailable(source: DeployTarget, target: DeployTarget, code: str) -> DataSyncUnavailable:
    logger.warning(
        "data sync lag unavailable",
        extra={
            "action": "replication_lag",
            "source_environment_id": str(source.environment_id),
            "target_environment_id": str(target.environment_id),
            "error_code": code,
        },
    )
    return DataSyncUnavailable()
