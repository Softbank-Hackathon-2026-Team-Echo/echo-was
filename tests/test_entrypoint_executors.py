"""고정 진입점 실행기 시험(인프라 5·6·9 계약). httpx.MockTransport 로 가중치 API 를 흉내 낸다."""

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.clients.secret_client import SecretLookupError
from app.core.config import EntrypointExecutorMode, Settings
from app.core.secrets import SecretRef, SecretValue, parse_secret_ref
from app.enums import ConnectionErrorCode
from app.executors import build_executor_set
from app.executors.base import (
    Metrics,
    MetricsUnavailable,
    RouteApplied,
    RouteFailed,
    TrafficApplied,
    TrafficFailed,
    WeightedTarget,
)
from app.executors.entrypoint import (
    ENTRYPOINT_ENV_UNMAPPED,
    ENTRYPOINT_ERROR,
    ENTRYPOINT_REJECTED,
    ENTRYPOINT_TOKEN_MISSING,
    ENTRYPOINT_UNAUTHORIZED,
    ENTRYPOINT_UNREACHABLE,
    EntrypointClient,
    EntrypointMainRouteExecutor,
    EntrypointMetricsProvider,
    EntrypointTrafficExecutor,
)
from app.executors.fake import FakeMetricsProvider, FakeTrafficExecutor
from tests.executor_contract import (
    ROUTE,
    STANDBY_TARGET,
    TARGET,
    assert_metrics_provider_contract,
    assert_traffic_executor_contract,
)

BASE_URL = "http://entry.echo.test:8080"
TOKEN = "entry-test-token"
TOKEN_REF = parse_secret_ref("env:ECHO_SECRET_ENTRYPOINT_TOKEN")
WINDOW_SECONDS = 5


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self) -> None:
        self.value += timedelta(seconds=1)


class _Secrets:
    def __init__(self, token: str | None = TOKEN) -> None:
        self.token = token

    async def get_secret(self, ref: SecretRef) -> SecretValue:
        if self.token is None:
            raise SecretLookupError(ConnectionErrorCode.SECRET_NOT_FOUND)
        return SecretValue(self.token)


def _summary(window: str, weights: dict[str, int]) -> dict[str, Any]:
    return {
        "ts": "2026-11-02T03:00:05Z",
        "window": window,
        "weights": weights,
        "total": {"rps": 12.5, "error_ratio": 0.01},
        "envs": {
            "aws": {
                "rps": 10.0,
                "error_ratio": 0.012,
                "p95_ms": 87.5,
                "healthy_hosts": 1,
                "hosts": 1,
                "weight": weights.get("aws"),
            },
            "gcp": {
                "rps": 2.5,
                "error_ratio": 0.0,
                "p95_ms": 140.0,
                "healthy_hosts": 1,
                "hosts": 1,
                "weight": weights.get("gcp"),
            },
        },
        "prometheus": "ok",
    }


class _Entrypoint:
    """가중치 API 대본. 상태(weights)와 받은 요청을 기억한다."""

    def __init__(self, envs: tuple[str, ...] = ("aws", "gcp")) -> None:
        self.weights = {name: (100 if index == 0 else 0) for index, name in enumerate(envs)}
        self.requests: list[httpx.Request] = []
        self.summary_override: dict[str, Any] | None = None
        self.fail_status: int | None = None
        self.fail_error: Exception | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_error is not None:
            raise self.fail_error
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": "Bearer 토큰이 필요하다"})
        if self.fail_status is not None:
            return httpx.Response(self.fail_status, json={"error": "fail"})
        if request.url.path == "/weights" and request.method == "GET":
            return httpx.Response(200, json=self.weights)
        if request.url.path == "/weights" and request.method == "PUT":
            body = json.loads(request.content)
            if set(body) != set(self.weights) or sum(body.values()) != 100:
                return httpx.Response(400, json={"error": "합이 100 이어야 한다"})
            self.weights = {name: int(value) for name, value in body.items()}
            return httpx.Response(200, json=self.weights)
        if request.url.path == "/summary":
            window = request.url.params.get("window", "30s")
            return httpx.Response(200, json=self.summary_override or _summary(window, self.weights))
        return httpx.Response(404, json={"error": "없는 경로"})

    def puts(self) -> list[dict[str, int]]:
        return [
            json.loads(item.content)
            for item in self.requests
            if item.method == "PUT" and item.url.path == "/weights"
        ]


def _client(
    entrypoint: _Entrypoint,
    *,
    secrets: _Secrets | None = None,
    environment_map: dict[str, str] | None = None,
) -> EntrypointClient:
    return EntrypointClient(
        BASE_URL,
        TOKEN_REF,
        secrets or _Secrets(),
        environment_map=environment_map,
        timeout_seconds=1,
        transport=httpx.MockTransport(entrypoint.handle),
    )


def _weights(source: int, target: int) -> tuple[WeightedTarget, ...]:
    return (WeightedTarget(TARGET, source), WeightedTarget(STANDBY_TARGET, target))


SINCE = datetime(2026, 11, 2, 3, 0, 0, tzinfo=UTC)
UNTIL = SINCE + timedelta(seconds=WINDOW_SECONDS)


# --- 공통 계약 ---


async def test_entrypoint_traffic_executor_satisfies_contract() -> None:
    clock = _Clock()
    await assert_traffic_executor_contract(
        EntrypointTrafficExecutor(_client(_Entrypoint()), clock), clock.advance
    )


async def test_entrypoint_metrics_provider_satisfies_contract() -> None:
    metrics = await assert_metrics_provider_contract(
        EntrypointMetricsProvider(_client(_Entrypoint()))
    )

    assert metrics == Metrics(error_rate_percent=0.0, p95_ms=140.0, request_count=13)


def test_entrypoint_executors_are_not_simulated() -> None:
    client = _client(_Entrypoint())
    clock = _Clock()

    assert not EntrypointTrafficExecutor(client, clock).simulated
    assert not EntrypointMainRouteExecutor(client, clock).simulated
    assert not EntrypointMetricsProvider(client).simulated


# --- 트래픽 ---


async def test_set_weights_puts_mapped_weights_with_bearer_token() -> None:
    entrypoint = _Entrypoint()
    clock = _Clock()

    result = await EntrypointTrafficExecutor(_client(entrypoint), clock).set_weights(
        ROUTE, _weights(90, 10), "plan:1"
    )

    assert result == TrafficApplied(applied_at=clock.now())
    assert entrypoint.puts() == [{"aws": 90, "gcp": 10}]
    assert all(item.headers["Authorization"] == f"Bearer {TOKEN}" for item in entrypoint.requests)
    assert entrypoint.weights == {"aws": 90, "gcp": 10}


async def test_set_weights_zeroes_environments_not_in_the_plan() -> None:
    entrypoint = _Entrypoint(("aws", "gcp", "onprem"))

    await EntrypointTrafficExecutor(_client(entrypoint), _Clock()).set_weights(
        ROUTE, _weights(50, 50), "plan:2"
    )

    assert entrypoint.puts() == [{"aws": 50, "gcp": 50, "onprem": 0}]


async def test_set_weights_same_key_calls_entrypoint_once() -> None:
    entrypoint = _Entrypoint()
    clock = _Clock()
    executor = EntrypointTrafficExecutor(_client(entrypoint), clock)

    first = await executor.set_weights(ROUTE, _weights(90, 10), "plan:1")
    clock.advance()
    again = await executor.set_weights(ROUTE, _weights(90, 10), "plan:1")
    other = await executor.set_weights(ROUTE, _weights(50, 50), "plan:2")

    assert first == again
    assert isinstance(other, TrafficApplied) and other.applied_at == clock.now()
    assert entrypoint.puts() == [{"aws": 90, "gcp": 10}, {"aws": 50, "gcp": 50}]


async def test_set_weights_failed_result_is_not_remembered() -> None:
    """실패를 기억하면 진입점이 돌아와도 같은 키는 계속 실패한다. 실패는 다시 시도한다."""
    entrypoint = _Entrypoint()
    entrypoint.fail_status = 503
    executor = EntrypointTrafficExecutor(_client(entrypoint), _Clock())

    failed = await executor.set_weights(ROUTE, _weights(90, 10), "plan:1")
    entrypoint.fail_status = None
    retried = await executor.set_weights(ROUTE, _weights(90, 10), "plan:1")

    assert isinstance(failed, TrafficFailed)
    assert isinstance(retried, TrafficApplied)
    assert entrypoint.puts() == [{"aws": 90, "gcp": 10}]


async def test_set_weights_remembers_only_recent_results() -> None:
    """기억은 상한(256)이 있어 오래 돌아도 메모리가 늘지 않는다. 오래된 키는 다시 호출된다."""
    entrypoint = _Entrypoint()
    executor = EntrypointTrafficExecutor(_client(entrypoint), _Clock())

    for index in range(257):
        await executor.set_weights(ROUTE, _weights(90, 10), f"plan:{index}")
    await executor.set_weights(ROUTE, _weights(90, 10), "plan:0")
    await executor.set_weights(ROUTE, _weights(90, 10), "plan:256")

    assert len(executor._results) == 256
    assert len(entrypoint.puts()) == 258


async def test_set_weights_uses_environment_id_mapping_over_kind() -> None:
    entrypoint = _Entrypoint(("aws", "gcp"))
    aws_osaka = replace(STANDBY_TARGET, environment_kind="aws")
    executor = EntrypointTrafficExecutor(
        _client(entrypoint, environment_map={str(aws_osaka.environment_id): "gcp"}), _Clock()
    )

    result = await executor.set_weights(
        ROUTE, (WeightedTarget(TARGET, 90), WeightedTarget(aws_osaka, 10)), "plan:1"
    )

    assert isinstance(result, TrafficApplied)
    assert entrypoint.puts() == [{"aws": 90, "gcp": 10}]


async def test_set_weights_fails_when_two_targets_map_to_one_name() -> None:
    entrypoint = _Entrypoint()
    aws_twice = replace(STANDBY_TARGET, environment_kind="aws")

    result = await EntrypointTrafficExecutor(_client(entrypoint), _Clock()).set_weights(
        ROUTE, (WeightedTarget(TARGET, 90), WeightedTarget(aws_twice, 10)), "plan:1"
    )

    assert result == TrafficFailed(code=ENTRYPOINT_ENV_UNMAPPED)
    assert entrypoint.puts() == []


async def test_set_weights_fails_when_entrypoint_does_not_know_the_environment() -> None:
    entrypoint = _Entrypoint(("aws", "onprem"))

    result = await EntrypointTrafficExecutor(_client(entrypoint), _Clock()).set_weights(
        ROUTE, _weights(90, 10), "plan:1"
    )

    assert result == TrafficFailed(code=ENTRYPOINT_ENV_UNMAPPED)
    assert entrypoint.puts() == []


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, ENTRYPOINT_REJECTED),
        (401, ENTRYPOINT_UNAUTHORIZED),
        (503, ENTRYPOINT_TOKEN_MISSING),
        (500, ENTRYPOINT_ERROR),
    ],
)
async def test_set_weights_maps_http_errors_to_codes(status: int, code: str) -> None:
    entrypoint = _Entrypoint()
    entrypoint.fail_status = status

    result = await EntrypointTrafficExecutor(_client(entrypoint), _Clock()).set_weights(
        ROUTE, _weights(90, 10), "plan:1"
    )

    assert result == TrafficFailed(code=code)


async def test_set_weights_wrong_token_is_unauthorized_not_timeout() -> None:
    entrypoint = _Entrypoint()

    result = await EntrypointTrafficExecutor(
        _client(entrypoint, secrets=_Secrets("wrong")), _Clock()
    ).set_weights(ROUTE, _weights(90, 10), "plan:1")

    assert result == TrafficFailed(code=ENTRYPOINT_UNAUTHORIZED)


async def test_set_weights_missing_secret_is_token_missing() -> None:
    entrypoint = _Entrypoint()

    result = await EntrypointTrafficExecutor(
        _client(entrypoint, secrets=_Secrets(None)), _Clock()
    ).set_weights(ROUTE, _weights(90, 10), "plan:1")

    assert result == TrafficFailed(code=ENTRYPOINT_TOKEN_MISSING)
    assert entrypoint.requests == []


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.ConnectTimeout("x")]
)
async def test_set_weights_transport_failure_is_unreachable(error: httpx.TransportError) -> None:
    entrypoint = _Entrypoint()
    entrypoint.fail_error = error

    result = await EntrypointTrafficExecutor(_client(entrypoint), _Clock()).set_weights(
        ROUTE, _weights(90, 10), "plan:1"
    )

    assert result == TrafficFailed(code=ENTRYPOINT_UNREACHABLE)


async def test_set_weights_rejects_invalid_totals_before_calling() -> None:
    entrypoint = _Entrypoint()

    with pytest.raises(ValueError):
        await EntrypointTrafficExecutor(_client(entrypoint), _Clock()).set_weights(
            ROUTE, _weights(90, 20), "plan:bad"
        )

    assert entrypoint.requests == []


# --- 대표 주소 ---


async def test_route_main_sends_all_traffic_to_the_target() -> None:
    entrypoint = _Entrypoint(("aws", "gcp", "onprem"))
    entrypoint.weights = {"aws": 0, "gcp": 0, "onprem": 100}
    clock = _Clock()
    executor = EntrypointMainRouteExecutor(_client(entrypoint), clock)

    result = await executor.route_main(ROUTE, STANDBY_TARGET, "deployment-1")
    clock.advance()
    again = await executor.route_main(ROUTE, STANDBY_TARGET, "deployment-1")

    assert result == RouteApplied(applied_at=SINCE) and again == result
    assert entrypoint.puts() == [{"aws": 0, "gcp": 100, "onprem": 0}]


async def test_route_main_failure_returns_code() -> None:
    entrypoint = _Entrypoint()
    entrypoint.fail_status = 503

    result = await EntrypointMainRouteExecutor(_client(entrypoint), _Clock()).route_main(
        ROUTE, TARGET, "deployment-1"
    )

    assert result == RouteFailed(code=ENTRYPOINT_TOKEN_MISSING)


# --- 지표 ---


async def test_read_metrics_maps_summary_fields_and_window() -> None:
    entrypoint = _Entrypoint()
    provider = EntrypointMetricsProvider(_client(entrypoint))

    aws = await provider.read(TARGET, SINCE, UNTIL)
    gcp = await provider.read(STANDBY_TARGET, SINCE, UNTIL)

    assert aws == Metrics(error_rate_percent=1.2, p95_ms=87.5, request_count=50)
    assert gcp == Metrics(error_rate_percent=0.0, p95_ms=140.0, request_count=13)
    assert [item.url.params["window"] for item in entrypoint.requests] == ["5s", "5s"]


async def test_read_metrics_window_is_at_least_one_second() -> None:
    entrypoint = _Entrypoint()

    await EntrypointMetricsProvider(_client(entrypoint)).read(TARGET, SINCE, SINCE)

    assert entrypoint.requests[0].url.params["window"] == "1s"


async def test_read_metrics_null_values_are_unavailable() -> None:
    entrypoint = _Entrypoint()
    summary = _summary("5s", entrypoint.weights)
    summary["envs"]["gcp"]["rps"] = None
    entrypoint.summary_override = summary

    result = await EntrypointMetricsProvider(_client(entrypoint)).read(STANDBY_TARGET, SINCE, UNTIL)

    assert result == MetricsUnavailable()


async def test_read_metrics_prometheus_unreachable_is_unavailable() -> None:
    entrypoint = _Entrypoint()
    summary = _summary("5s", entrypoint.weights)
    summary["prometheus"] = "unreachable"
    entrypoint.summary_override = summary

    result = await EntrypointMetricsProvider(_client(entrypoint)).read(TARGET, SINCE, UNTIL)

    assert result == MetricsUnavailable()


@pytest.mark.parametrize("status", [401, 500, 503])
async def test_read_metrics_http_error_is_unavailable(status: int) -> None:
    entrypoint = _Entrypoint()
    entrypoint.fail_status = status

    result = await EntrypointMetricsProvider(_client(entrypoint)).read(TARGET, SINCE, UNTIL)

    assert result == MetricsUnavailable()


# --- 고르기·설정 ---


def _settings(**changes: object) -> Settings:
    return Settings(database_url="postgresql+asyncpg://x:y@127.0.0.1:1/z").model_copy(
        update=changes
    )


def test_build_executor_set_uses_entrypoint_executors_when_configured() -> None:
    fake = build_executor_set(_settings(), _Clock())
    http = build_executor_set(
        _settings(
            entrypoint_executor_mode=EntrypointExecutorMode.HTTP,
            entrypoint_base_url=BASE_URL,
            entrypoint_token_ref="env:ECHO_SECRET_ENTRYPOINT_TOKEN",
        ),
        _Clock(),
        _Secrets(),
    )

    assert isinstance(fake.traffic, FakeTrafficExecutor)
    assert isinstance(fake.metrics, FakeMetricsProvider)
    assert isinstance(http.traffic, EntrypointTrafficExecutor)
    assert isinstance(http.main_route, EntrypointMainRouteExecutor)
    assert isinstance(http.metrics, EntrypointMetricsProvider)
    assert http.build.simulated and http.deploy.simulated and http.teardown.simulated


def test_settings_http_mode_requires_url_and_token_ref() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            database_url="postgresql+asyncpg://x:y@127.0.0.1:1/z",
            entrypoint_executor_mode=EntrypointExecutorMode.HTTP,
            entrypoint_base_url=BASE_URL,
        )


def test_settings_token_ref_must_be_secret_reference() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            database_url="postgresql+asyncpg://x:y@127.0.0.1:1/z",
            entrypoint_token_ref="plain-token",
        )


def test_settings_environment_map_is_parsed_from_comma_list() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://x:y@127.0.0.1:1/z",
        entrypoint_environment_map=f"{uuid.uuid4()}=gcp, aws=aws",  # type: ignore[arg-type]
        entrypoint_base_url=f"{BASE_URL}/",
    )

    assert len(settings.entrypoint_environment_map) == 2
    assert settings.entrypoint_base_url == BASE_URL
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            database_url="postgresql+asyncpg://x:y@127.0.0.1:1/z",
            entrypoint_environment_map="aws",  # type: ignore[arg-type]
        )


def test_deploy_target_kind_is_the_default_entrypoint_name() -> None:
    client = _client(_Entrypoint(), environment_map={"gcp": "gcp-seoul"})

    assert client.environment_name(TARGET) == "aws"
    assert client.environment_name(STANDBY_TARGET) == "gcp-seoul"
