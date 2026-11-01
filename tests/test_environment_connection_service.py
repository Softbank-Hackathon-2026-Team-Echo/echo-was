"""접속 확인 서비스(DB 없음): 단계별 이유 코드, 제한 시간, 예외 처리."""

import asyncio
import logging
import uuid
from time import perf_counter

import pytest

from app.adapters.base import (
    ClusterInfo,
    ConnectionCheckError,
    EnvironmentAdapter,
    EnvironmentConfig,
    SimulatedEnvironmentAdapter,
)
from app.adapters.registry import get_environment_adapter
from app.clients.secret_client import EnvVarSecretClient
from app.core.config import DEFAULT_ENVIRONMENT_CHECK_TIMEOUT_SECONDS, Settings
from app.core.secrets import SecretValue
from app.enums import ConnectionErrorCode
from app.services.environment_connection_service import (
    AdapterResolver,
    ConnectionCheckResult,
    ConnectionTarget,
    EnvironmentConnectionService,
)

SECRET_ENV = "ECHO_SECRET_CONNECTION_UNIT"
AWS_CONFIG = {"region": "ap-northeast-2", "cluster_name": "echo-eks"}


def _target(
    *, kind: str = "aws", config: dict[str, object] | None = None, secret_ref: str | None = None
) -> ConnectionTarget:
    return ConnectionTarget(
        environment_id=uuid.uuid4(),
        kind=kind,
        config=AWS_CONFIG if config is None else config,
        secret_ref=secret_ref,
    )


def _service(
    resolver: AdapterResolver = get_environment_adapter, timeout: float = 1.0
) -> EnvironmentConnectionService:
    return EnvironmentConnectionService(EnvVarSecretClient(), resolver, timeout)


def _resolver(adapter: EnvironmentAdapter) -> AdapterResolver:
    return lambda kind: adapter


class _Adapter(SimulatedEnvironmentAdapter):
    simulated_server_version = "v1.31.0"
    simulated_node_count = 3


class _SlowConnectAdapter(_Adapter):
    async def check_connection(self, config: EnvironmentConfig, credentials: SecretValue) -> None:
        await asyncio.sleep(5)


class _SlowClusterInfoAdapter(_Adapter):
    async def read_cluster_info(
        self, config: EnvironmentConfig, credentials: SecretValue
    ) -> ClusterInfo:
        await asyncio.sleep(5)
        raise AssertionError("unreachable")


class _UnreachableAdapter(_Adapter):
    async def check_connection(self, config: EnvironmentConfig, credentials: SecretValue) -> None:
        raise ConnectionCheckError(ConnectionErrorCode.UNREACHABLE)


class _ClusterInfoAuthFailedAdapter(_Adapter):
    async def read_cluster_info(
        self, config: EnvironmentConfig, credentials: SecretValue
    ) -> ClusterInfo:
        raise ConnectionCheckError(ConnectionErrorCode.AUTH_FAILED)


class _InnerTimeoutAdapter(_Adapter):
    async def check_connection(self, config: EnvironmentConfig, credentials: SecretValue) -> None:
        raise TimeoutError("sdk-level timeout")


class _BrokenAdapter(_Adapter):
    async def check_connection(self, config: EnvironmentConfig, credentials: SecretValue) -> None:
        raise RuntimeError(f"boom {credentials.get_secret_value()}")


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch) -> str:
    value = f"unit-secret-{uuid.uuid4().hex}"
    monkeypatch.setenv(SECRET_ENV, value)
    return value


def test_settings_default_check_timeout_is_five_seconds() -> None:
    assert DEFAULT_ENVIRONMENT_CHECK_TIMEOUT_SECONDS == 5.0
    assert Settings(database_url="postgresql+asyncpg://x").environment_check_timeout_seconds == 5.0


async def test_check_connection_success_returns_simulated_cluster_info(secret: str) -> None:
    result = await _service().check_connection(_target(secret_ref=f"env:{SECRET_ENV}"))

    assert result == ConnectionCheckResult(
        connection_error=None,
        cluster_info=ClusterInfo(server_version="v1.31.0", node_count=3, simulated=True),
        cluster_info_error=None,
    )


@pytest.mark.parametrize(
    ("target_kwargs", "reason"),
    [
        ({"secret_ref": None}, ConnectionErrorCode.SECRET_REF_MISSING),
        ({"secret_ref": "env:DATABASE_URL"}, ConnectionErrorCode.SECRET_REF_INVALID),
        (
            {"secret_ref": f"env:{SECRET_ENV}", "config": {"region": "seoul"}},
            ConnectionErrorCode.CONFIG_INVALID,
        ),
        (
            {"secret_ref": f"env:{SECRET_ENV}", "kind": "deleted-kind"},
            ConnectionErrorCode.CONFIG_INVALID,
        ),
        ({"secret_ref": "env:ECHO_SECRET_NOT_SET_ANYWHERE"}, ConnectionErrorCode.SECRET_NOT_FOUND),
        ({"secret_ref": "k8s:ns/sec/key"}, ConnectionErrorCode.SECRET_STORE_UNSUPPORTED),
    ],
)
async def test_check_connection_preparation_failures_return_reason(
    secret: str, target_kwargs: dict[str, object], reason: ConnectionErrorCode
) -> None:
    result = await _service().check_connection(_target(**target_kwargs))  # type: ignore[arg-type]

    assert result == ConnectionCheckResult(
        connection_error=reason, cluster_info=None, cluster_info_error=reason
    )


async def test_check_connection_adapter_error_returns_its_reason(secret: str) -> None:
    result = await _service(_resolver(_UnreachableAdapter())).check_connection(
        _target(secret_ref=f"env:{SECRET_ENV}")
    )

    assert result.connection_error is ConnectionErrorCode.UNREACHABLE


async def test_check_connection_slow_connect_times_out_within_budget(secret: str) -> None:
    started_at = perf_counter()
    result = await _service(_resolver(_SlowConnectAdapter()), timeout=0.2).check_connection(
        _target(secret_ref=f"env:{SECRET_ENV}")
    )

    assert perf_counter() - started_at < 1.0
    assert result.connection_error is ConnectionErrorCode.TIMEOUT
    assert result.cluster_info_error is ConnectionErrorCode.TIMEOUT


async def test_check_connection_slow_cluster_info_stays_connected(secret: str) -> None:
    result = await _service(_resolver(_SlowClusterInfoAdapter()), timeout=0.2).check_connection(
        _target(secret_ref=f"env:{SECRET_ENV}")
    )

    assert result == ConnectionCheckResult(
        connection_error=None, cluster_info=None, cluster_info_error=ConnectionErrorCode.TIMEOUT
    )


async def test_check_connection_cluster_info_error_stays_connected(secret: str) -> None:
    result = await _service(_resolver(_ClusterInfoAuthFailedAdapter())).check_connection(
        _target(secret_ref=f"env:{SECRET_ENV}")
    )

    assert result.connection_error is None
    assert result.cluster_info_error is ConnectionErrorCode.AUTH_FAILED


async def test_check_connection_adapter_inner_timeout_error_is_unknown(secret: str) -> None:
    result = await _service(_resolver(_InnerTimeoutAdapter())).check_connection(
        _target(secret_ref=f"env:{SECRET_ENV}")
    )

    assert result.connection_error is ConnectionErrorCode.UNKNOWN


async def test_check_connection_unexpected_error_is_unknown_and_logs_type_only(
    secret: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        result = await _service(_resolver(_BrokenAdapter())).check_connection(
            _target(secret_ref=f"env:{SECRET_ENV}")
        )

    assert result.connection_error is ConnectionErrorCode.UNKNOWN
    record = next(r for r in caplog.records if r.name.endswith("environment_connection_service"))
    assert record.exc_type == "RuntimeError"  # type: ignore[attr-defined]
    assert record.exc_info is None
    assert secret not in caplog.text
    assert not any(secret in repr(r.__dict__) for r in caplog.records)


async def test_check_connection_cancellation_is_not_swallowed(secret: str) -> None:
    task = asyncio.create_task(
        _service(_resolver(_SlowConnectAdapter()), timeout=5).check_connection(
            _target(secret_ref=f"env:{SECRET_ENV}")
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_read_cluster_info_skips_connection_check(secret: str) -> None:
    # check_connection 이 느려도 read_cluster_info 는 부르지 않으므로 바로 끝난다.
    result = await _service(_resolver(_SlowConnectAdapter()), timeout=0.2).read_cluster_info(
        _target(secret_ref=f"env:{SECRET_ENV}")
    )

    assert result.error is None
    assert result.cluster_info is not None


async def test_read_cluster_info_preparation_failure_returns_error() -> None:
    result = await _service().read_cluster_info(_target(secret_ref=None))

    assert result.cluster_info is None
    assert result.error is ConnectionErrorCode.SECRET_REF_MISSING
