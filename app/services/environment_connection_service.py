"""환경 접속 확인: 비밀값을 읽고 어댑터로 접속 확인·클러스터 정보 읽기를 한다. DB 는 쓰지 않는다.

제한 시간 하나를 전체(비밀 참조 파싱 → config 파싱 → 비밀값 조회 → 접속 확인 → 클러스터 정보)에
건다.
접속 확인까지의 실패는 FAILED(이유 코드), 클러스터 정보 단계의 실패는 CONNECTED 그대로 두고
cluster_info_error 에 이유를 적는다. 비밀값은 메모리에서만 쓰고 저장·로그·응답에 넣지 않는다.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum, auto

from app.adapters.base import (
    ClusterInfo,
    ConnectionCheckError,
    EnvironmentAdapter,
    EnvironmentConfig,
    InvalidEnvironmentConfigError,
    UnknownEnvironmentKindError,
)
from app.adapters.registry import validate_environment_config
from app.clients.secret_client import SecretClient, SecretLookupError
from app.core.secrets import InvalidSecretRefError, SecretValue, parse_secret_ref
from app.enums import ConnectionErrorCode

logger = logging.getLogger(__name__)

AdapterResolver = Callable[[str], EnvironmentAdapter]


@dataclass(frozen=True)
class ConnectionTarget:
    """확인할 환경. config 는 저장된(snake_case) 값이다."""

    environment_id: uuid.UUID
    kind: str
    config: Mapping[str, object]
    secret_ref: str | None


@dataclass(frozen=True)
class ConnectionCheckResult:
    """connection_error 가 None 이면 CONNECTED.

    클러스터 정보가 없으면 cluster_info_error 에 이유가 있다.
    """

    connection_error: ConnectionErrorCode | None
    cluster_info: ClusterInfo | None
    cluster_info_error: ConnectionErrorCode | None


@dataclass(frozen=True)
class ClusterInfoResult:
    cluster_info: ClusterInfo | None
    error: ConnectionErrorCode | None


class _Stage(Enum):
    CONNECTION = auto()
    CLUSTER_INFO = auto()


class _PreparationError(Exception):
    def __init__(self, reason: ConnectionErrorCode) -> None:
        super().__init__(reason.value)
        self.reason = reason


class EnvironmentConnectionService:
    def __init__(
        self,
        secret_client: SecretClient,
        adapter_resolver: AdapterResolver,
        timeout_seconds: float,
    ) -> None:
        self._secret_client = secret_client
        self._adapter_resolver = adapter_resolver
        self._timeout_seconds = timeout_seconds

    async def check_connection(self, target: ConnectionTarget) -> ConnectionCheckResult:
        """접속 확인 후 클러스터 정보까지 읽는다. 예외를 내지 않고 결과로 돌려준다."""
        return await self._run(target, should_check_connection=True)

    async def read_cluster_info(self, target: ConnectionTarget) -> ClusterInfoResult:
        """접속 확인 없이 클러스터 정보만 읽는다(단건 조회용). 예외를 내지 않는다."""
        result = await self._run(target, should_check_connection=False)
        return ClusterInfoResult(cluster_info=result.cluster_info, error=result.cluster_info_error)

    async def _run(
        self, target: ConnectionTarget, *, should_check_connection: bool
    ) -> ConnectionCheckResult:
        stage = _Stage.CONNECTION if should_check_connection else _Stage.CLUSTER_INFO
        deadline = asyncio.timeout(self._timeout_seconds)
        try:
            async with deadline:
                try:
                    config, credentials, adapter = await self._prepare(target)
                    if should_check_connection:
                        await adapter.check_connection(config, credentials)
                except (_PreparationError, ConnectionCheckError) as exc:
                    return _failed(stage, _known_reason(exc.reason))

                stage = _Stage.CLUSTER_INFO
                try:
                    cluster_info = await adapter.read_cluster_info(config, credentials)
                except ConnectionCheckError as exc:
                    return _failed(stage, _known_reason(exc.reason))
                if not isinstance(cluster_info, ClusterInfo):
                    raise TypeError("adapter returned an unexpected cluster info type")
                return ConnectionCheckResult(
                    connection_error=None, cluster_info=cluster_info, cluster_info_error=None
                )
        except TimeoutError as exc:
            # 우리 제한 시간이 끝났을 때만 TIMEOUT. 어댑터 안에서 난 다른 TimeoutError 는 UNKNOWN.
            if deadline.expired():
                return _failed(stage, ConnectionErrorCode.TIMEOUT)
            self._log_unexpected(target, exc)
        except Exception as exc:
            # 예상하지 못한 예외를 받는 유일한 곳. CancelledError(BaseException)는 잡지 않는다.
            # 예외 메시지·값에 비밀값이 섞일 수 있어 종류 이름만 남긴다.
            self._log_unexpected(target, exc)
        return _failed(stage, ConnectionErrorCode.UNKNOWN)

    async def _prepare(
        self, target: ConnectionTarget
    ) -> tuple[EnvironmentConfig, SecretValue, EnvironmentAdapter]:
        if target.secret_ref is None:
            raise _PreparationError(ConnectionErrorCode.SECRET_REF_MISSING)
        try:
            secret_ref = parse_secret_ref(target.secret_ref)
        except InvalidSecretRefError:
            raise _PreparationError(ConnectionErrorCode.SECRET_REF_INVALID) from None
        try:
            config = validate_environment_config(target.kind, target.config)
        except (InvalidEnvironmentConfigError, UnknownEnvironmentKindError):
            raise _PreparationError(ConnectionErrorCode.CONFIG_INVALID) from None
        adapter = self._adapter_resolver(target.kind)
        try:
            credentials = await self._secret_client.get_secret(secret_ref)
        except SecretLookupError as exc:
            raise _PreparationError(exc.reason) from None
        return config, credentials, adapter

    @staticmethod
    def _log_unexpected(target: ConnectionTarget, exc: BaseException) -> None:
        logger.warning(
            "environment connection check raised unexpected error",
            extra={
                "action": "check_environment_connection",
                "environment_id": str(target.environment_id),
                "kind": target.kind,
                "exc_type": type(exc).__name__,
            },
        )


def _known_reason(reason: str) -> ConnectionErrorCode:
    try:
        return ConnectionErrorCode(reason)
    except ValueError:
        return ConnectionErrorCode.UNKNOWN


def _failed(stage: _Stage, reason: ConnectionErrorCode) -> ConnectionCheckResult:
    if stage is _Stage.CONNECTION:
        # 접속이 안 되면 클러스터 정보도 같은 이유로 비어 있다.
        return ConnectionCheckResult(
            connection_error=reason, cluster_info=None, cluster_info_error=reason
        )
    return ConnectionCheckResult(
        connection_error=None, cluster_info=None, cluster_info_error=reason
    )
