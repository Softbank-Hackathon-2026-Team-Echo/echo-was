"""서비스 대시보드 지표(운영 2) 단위 시험.

범위 검증 · PromQL 조립 · 응답 변환 · 복제 지연 · Prometheus 클라이언트 · 설정.
"""

import base64
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.clients.prometheus_client import (
    PROMETHEUS_ERROR,
    PROMETHEUS_SECRET_MISSING,
    PROMETHEUS_UNAUTHORIZED,
    PROMETHEUS_UNREACHABLE,
    PrometheusClient,
    PrometheusQueryError,
    Sample,
    Series,
)
from app.core.config import Settings
from app.core.exceptions import RequestFieldInvalidError
from app.core.secrets import parse_secret_ref
from app.dependencies import get_prometheus_client
from app.executors.base import DataSyncLag, DataSyncResult, DataSyncUnavailable, DeployTarget
from app.executors.entrypoint import entrypoint_environment_name
from app.models import Service
from app.services.service_metrics import (
    LagTargets,
    MetricsConnection,
    MetricsWindow,
    build_series,
    build_summary,
    error_budget_left,
    label_matchers,
    parse_duration,
    parse_metrics_window,
    re2_escape,
    read_replication_lag_ms,
    series_queries,
    service_metrics_filter,
    summary_queries,
    window_bounds,
)
from tests.real_executor_fakes import DictSecretClient

AT = datetime(2026, 11, 5, 3, 0, 0, tzinfo=UTC)
AWS_ENV = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
GCP_ENV = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


def _service(health_check_path: str = "/healthz") -> Service:
    return Service(
        name="shop-api",
        public_host="shop-api-m1g2r3t4.echoport.uk",
        health_check_path=health_check_path,
    )


def _series(provider: str | None, *values: tuple[int, float | None]) -> Series:
    labels = {} if provider is None else {"cloud_provider": provider}
    return Series(
        labels=labels,
        samples=tuple(
            Sample(at=AT + timedelta(seconds=offset), value=value) for offset, value in values
        ),
    )


# --- 범위 검증 ---


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("5s", 5), ("10s", 10), ("15m", 900), ("24h", 86400), ("99999s", 99999)],
    ids=["5s", "10s", "15m", "24h", "five-digits"],
)
def test_parse_duration_reads_seconds_minutes_hours(text: str, seconds: int) -> None:
    assert parse_duration(text) == seconds


@pytest.mark.parametrize(
    "text",
    ["", "15", "m", "0s", "015m", "1d", "1.5m", "-5m", "100000s", " 15m", "15M"],
    ids=["empty", "no-unit", "no-number", "zero", "leading-zero", "days", "decimal", "negative",
         "six-digits", "space", "upper"],
)  # fmt: skip
def test_parse_duration_rejects_other_forms(text: str) -> None:
    assert parse_duration(text) is None


def test_parse_metrics_window_keeps_given_text() -> None:
    assert parse_metrics_window("15m", "10s") == MetricsWindow("15m", "10s", 900, 10)
    assert parse_metrics_window("900s", "10s").range_text == "900s"


@pytest.mark.parametrize(
    ("time_range", "step", "field", "reason"),
    [
        ("fifteen", "10s", "range", "INVALID_DURATION"),
        ("15m", "10x", "step", "INVALID_DURATION"),
        ("4m", "10s", "range", "RANGE_OUT_OF_BOUNDS"),
        ("25h", "5m", "range", "RANGE_OUT_OF_BOUNDS"),
        ("15m", "4s", "step", "STEP_OUT_OF_BOUNDS"),
        ("1h", "6m", "step", "STEP_OUT_OF_BOUNDS"),
        ("24h", "30s", "step", "TOO_MANY_POINTS"),
    ],
    ids=["range-form", "step-form", "range-low", "range-high", "step-low", "step-high", "points"],
)
def test_parse_metrics_window_rejects_out_of_bounds(
    time_range: str, step: str, field: str, reason: str
) -> None:
    with pytest.raises(RequestFieldInvalidError) as caught:
        parse_metrics_window(time_range, step)

    assert [(item.field, item.reason) for item in caught.value.details] == [(field, reason)]


def test_parse_metrics_window_accepts_edges() -> None:
    assert parse_metrics_window("5m", "5s").step_seconds == 5
    assert parse_metrics_window("24h", "5m").range_seconds == 86400
    # 24h / 1m + 1 = 1441 점은 상한 1500 안이다.
    assert parse_metrics_window("24h", "1m").step_seconds == 60
    # step 은 range 보다 클 수 없다(5m 은 둘 다 허용 경계).
    assert parse_metrics_window("5m", "5m").step_seconds == 300


def test_window_bounds_floors_end_to_step() -> None:
    window = parse_metrics_window("15m", "10s")

    start, end = window_bounds(datetime(2026, 11, 5, 3, 0, 17, 400000, tzinfo=UTC), window)

    assert end == datetime(2026, 11, 5, 3, 0, 10, tzinfo=UTC)
    assert start == end - timedelta(minutes=15)


# --- PromQL 조립 ---


def test_service_metrics_filter_uses_namespace_and_health_paths() -> None:
    metrics_filter = service_metrics_filter(_service("/health"))

    assert metrics_filter.namespace == "shop-api-m1g2r3t4"
    assert metrics_filter.excluded_paths == ("/healthz", "/readyz", "/metrics", "/health")
    assert service_metrics_filter(_service("/readyz")).excluded_paths == (
        "/healthz",
        "/readyz",
        "/metrics",
    )


def test_label_matchers_escape_regex_and_string() -> None:
    metrics_filter = service_metrics_filter(_service("/v1.0/health"))

    assert label_matchers(metrics_filter) == (
        '{namespace="shop-api-m1g2r3t4",path!~"/healthz|/readyz|/metrics|/v1\\\\.0/health"}'
    )
    assert label_matchers(metrics_filter, errors_only=True).endswith(',status=~"5.."}')
    assert re2_escape("/a+b(c)") == "/a\\+b\\(c\\)"


def test_series_queries_group_by_cloud_provider_with_minute_window() -> None:
    queries = series_queries(service_metrics_filter(_service()), 10)
    every = '{namespace="shop-api-m1g2r3t4",path!~"/healthz|/readyz|/metrics"}'
    errors = '{namespace="shop-api-m1g2r3t4",path!~"/healthz|/readyz|/metrics",status=~"5.."}'

    assert queries == {
        "rps": f"sum by (cloud_provider) (rate(http_requests_total{every}[60s]))",
        "errors": f"sum by (cloud_provider) (rate(http_requests_total{errors}[60s]))",
        "p95": (
            "histogram_quantile(0.95, sum by (cloud_provider, le) "
            f"(rate(http_request_duration_seconds_bucket{every}[60s]))) * 1000"
        ),
    }
    # step 이 1분보다 길면 rate 창도 step 이다.
    assert "[300s]" in series_queries(service_metrics_filter(_service()), 300)["rps"]


def test_summary_queries_cover_whole_range() -> None:
    queries = summary_queries(service_metrics_filter(_service()), 900)

    assert queries["total"].startswith("sum(increase(http_requests_total{")
    assert queries["total"].endswith("[900s]))")
    assert 'status=~"5.."' in queries["errors"]
    assert queries["p95"].startswith("histogram_quantile(0.95, sum by (le) ")


def test_entrypoint_environment_name_prefers_id_then_kind() -> None:
    mapping = {str(AWS_ENV): "aws-b", "gcp": "google"}

    assert entrypoint_environment_name(AWS_ENV, "aws", mapping) == "aws-b"
    assert entrypoint_environment_name(GCP_ENV, "gcp", mapping) == "google"
    assert entrypoint_environment_name(GCP_ENV, "onprem", mapping) == "onprem"


# --- 응답 변환 ---


AWS = MetricsConnection(AWS_ENV, "aws-tokyo", "aws")
GCP = MetricsConnection(GCP_ENV, "gcp-seoul", "gcp")


def test_build_series_joins_rps_errors_p95_by_time() -> None:
    results = {
        "rps": [_series("aws", (0, 10.0), (10, 20.0)), _series("gcp", (0, 2.5))],
        "errors": [_series("aws", (10, 0.5))],
        "p95": [_series("aws", (0, 182.04), (10, None))],
    }

    aws, gcp = build_series([AWS, GCP], results)

    assert (aws.environment_id, aws.environment_name, aws.cloud_provider) == (
        AWS_ENV,
        "aws-tokyo",
        "aws",
    )
    assert [(p.at, p.rps, p.success_rate, p.p95_ms) for p in aws.points] == [
        (AT, 10.0, 1.0, 182.0),
        (AT + timedelta(seconds=10), 20.0, 0.975, None),
    ]
    assert [(p.rps, p.success_rate, p.p95_ms) for p in gcp.points] == [(2.5, 1.0, None)]


def test_build_series_leaves_zero_traffic_success_rate_null_and_skips_nan_rps() -> None:
    results = {"rps": [_series("aws", (0, 0.0), (10, None), (20, 1.0))], "errors": [], "p95": []}

    (aws,) = build_series([AWS], results)

    assert [(p.rps, p.success_rate) for p in aws.points] == [(0.0, None), (1.0, 1.0)]


def test_build_series_keeps_empty_points_for_missing_or_ambiguous_environment() -> None:
    other_aws = MetricsConnection(uuid.uuid4(), "aws-osaka", "aws")
    results = {"rps": [_series("aws", (0, 1.0)), _series(None, (0, 9.0))]}

    first, second, gcp = build_series([AWS, other_aws, GCP], results)

    assert (first.points, second.points, gcp.points) == ((), (), ())


def test_build_summary_computes_success_rate_and_error_budget() -> None:
    results = {
        "total": [_series(None, (0, 100000.0))],
        "errors": [_series(None, (0, 28.0))],
        "p95": [_series(None, (0, 181.96))],
    }

    summary = build_summary(results, 120.0)

    assert summary.success_rate == 0.99972
    assert summary.error_budget_left == 0.72
    assert (summary.p95_ms, summary.replication_lag_ms, summary.slo_target) == (182.0, 120.0, 0.999)


def test_build_summary_without_traffic_is_null() -> None:
    summary = build_summary({"total": [_series(None, (0, 0.0))], "errors": [], "p95": []}, None)

    assert (summary.success_rate, summary.p95_ms, summary.error_budget_left) == (None, None, None)
    assert build_summary({}, None).success_rate is None


@pytest.mark.parametrize(
    ("success_rate", "left"),
    [(1.0, 1.0), (0.9995, 0.5), (0.999, 0.0), (0.95, 0.0), (None, None)],
    ids=["perfect", "half", "spent", "overspent", "unknown"],
)
def test_error_budget_left_against_99_9_slo(success_rate: float | None, left: float | None) -> None:
    assert error_budget_left(success_rate) == left


# --- 복제 지연 ---


def _target(name: str) -> DeployTarget:
    return DeployTarget(
        service_id=uuid.uuid4(),
        service_name="shop-api",
        service_environment_id=uuid.uuid4(),
        environment_id=uuid.uuid4(),
        environment_kind=name,
        public_host=f"shop-api-{name}.echoport.uk",
        container_port=8080,
    )


class _LagChecker:
    def __init__(self, results: list[DataSyncResult], *, simulated: bool = False) -> None:
        self._results = list(results)
        self._simulated = simulated
        self.calls = 0

    @property
    def simulated(self) -> bool:
        return self._simulated

    async def replication_lag(self, source: DeployTarget, target: DeployTarget) -> DataSyncResult:
        self.calls += 1
        return self._results.pop(0)


async def test_read_replication_lag_ms_takes_largest_measured_lag() -> None:
    targets = LagTargets(_target("aws"), (_target("gcp"), _target("onprem")))
    checker = _LagChecker([DataSyncLag(seconds=0.12), DataSyncLag(seconds=0.3456)])

    assert await read_replication_lag_ms(checker, targets) == 345.6
    assert checker.calls == 2


@pytest.mark.parametrize(
    ("results", "simulated"),
    [
        ([DataSyncLag(seconds=0.3)], True),
        ([DataSyncUnavailable()], False),
        ([DataSyncLag(seconds=0.0, reason="TARGET_IS_WRITER")], False),
    ],
    ids=["simulated", "unavailable", "not-measured"],
)
async def test_read_replication_lag_ms_is_null_when_not_measured(
    results: list[DataSyncResult], simulated: bool
) -> None:
    targets = LagTargets(_target("aws"), (_target("gcp"),))

    assert await read_replication_lag_ms(_LagChecker(results, simulated=simulated), targets) is None


async def test_read_replication_lag_ms_without_targets_is_null() -> None:
    assert await read_replication_lag_ms(_LagChecker([]), None) is None
    assert await read_replication_lag_ms(None, None) is None


# --- Prometheus 클라이언트 ---


def _matrix(*series: dict[str, Any]) -> dict[str, Any]:
    return {"status": "success", "data": {"resultType": "matrix", "result": list(series)}}


def _client(
    handler: Any,
    *,
    secrets: dict[str, str] | None = None,
    password_ref: str | None = None,
) -> PrometheusClient:
    return PrometheusClient(
        "http://prom.test:9090/",
        secret_client=DictSecretClient(secrets or {}),
        username="echo-otel",
        password_ref=parse_secret_ref(password_ref) if password_ref else None,
        timeout_seconds=1.0,
        transport=httpx.MockTransport(handler),
    )


async def test_prometheus_query_range_parses_matrix_and_sends_basic_auth() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=_matrix(
                {
                    "metric": {"cloud_provider": "aws"},
                    "values": [[AT.timestamp(), "1.5"], [AT.timestamp() + 10, "NaN"]],
                }
            ),
        )

    client = _client(
        handler,
        secrets={"ECHO_SECRET_ENTRYPOINT_PROMETHEUS_PASSWORD": "pw-1"},
        password_ref="env:ECHO_SECRET_ENTRYPOINT_PROMETHEUS_PASSWORD",
    )

    results = await client.query_range({"rps": "up"}, AT - timedelta(minutes=15), AT, 10)

    assert results == {
        "rps": [
            Series(
                labels={"cloud_provider": "aws"},
                samples=(
                    Sample(at=AT, value=1.5),
                    Sample(at=AT + timedelta(seconds=10), value=None),
                ),
            )
        ]
    }
    (request,) = seen
    assert request.url.path == "/api/v1/query_range"
    assert request.url.params["query"] == "up"
    assert request.url.params["step"] == "10s"
    assert request.url.params["end"] == f"{AT.timestamp():.3f}"
    expected = base64.b64encode(b"echo-otel:pw-1").decode()
    assert request.headers["Authorization"] == f"Basic {expected}"


async def test_prometheus_without_password_ref_sends_no_auth_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = {"status": "success", "data": {"resultType": "vector", "result": []}}
        return httpx.Response(200, json=body)

    assert await _client(handler).query({"a": "up", "b": "up"}, AT) == {"a": [], "b": []}
    assert [request.url.path for request in seen] == ["/api/v1/query", "/api/v1/query"]
    assert all("Authorization" not in request.headers for request in seen)


def _raise_connect(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("down", request=request)


@pytest.mark.parametrize(
    ("handler", "code"),
    [
        (_raise_connect, PROMETHEUS_UNREACHABLE),
        (lambda request: httpx.Response(401), PROMETHEUS_UNAUTHORIZED),
        (lambda request: httpx.Response(503), PROMETHEUS_ERROR),
        (lambda request: httpx.Response(200, text="not json"), PROMETHEUS_ERROR),
        (lambda request: httpx.Response(200, json={"status": "error"}), PROMETHEUS_ERROR),
        (
            lambda request: httpx.Response(
                200, json={"status": "success", "data": {"resultType": "vector", "result": []}}
            ),
            PROMETHEUS_ERROR,
        ),
        (
            lambda request: httpx.Response(200, json=_matrix({"metric": {}, "values": [[1, 2.0]]})),
            PROMETHEUS_ERROR,
        ),
    ],
    ids=["unreachable", "401", "503", "not-json", "status-error", "wrong-type", "bad-sample"],
)
async def test_prometheus_failures_become_fixed_codes(handler: Any, code: str) -> None:
    with pytest.raises(PrometheusQueryError) as caught:
        await _client(handler).query_range({"rps": "up"}, AT - timedelta(minutes=5), AT, 10)

    assert caught.value.code == code


async def test_prometheus_missing_password_is_secret_missing_without_call() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_matrix())

    client = _client(handler, password_ref="env:ECHO_SECRET_ENTRYPOINT_PROMETHEUS_PASSWORD")

    with pytest.raises(PrometheusQueryError) as caught:
        await client.query({"a": "up"}, AT)

    assert (caught.value.code, calls) == (PROMETHEUS_SECRET_MISSING, [])


# --- 설정·의존성 ---


DATABASE_URL = "postgresql+asyncpg://echo:echo@localhost:5432/echo"


def test_prometheus_settings_strip_url_and_check_secret_ref() -> None:
    settings = Settings(
        database_url=DATABASE_URL,
        entrypoint_prometheus_url=" http://10.60.0.6:9090/ ",
        entrypoint_prometheus_basic_auth_password_ref="",
    )

    assert settings.entrypoint_prometheus_url == "http://10.60.0.6:9090"
    assert settings.entrypoint_prometheus_basic_auth_password_ref is None
    assert settings.entrypoint_prometheus_basic_auth_user == "echo-otel"
    with pytest.raises(ValidationError):
        Settings(database_url=DATABASE_URL, entrypoint_prometheus_basic_auth_password_ref="plain")


def test_get_prometheus_client_is_none_without_url() -> None:
    assert get_prometheus_client(Settings(database_url=DATABASE_URL), DictSecretClient({})) is None
    configured = Settings(
        database_url=DATABASE_URL,
        entrypoint_prometheus_url="http://10.60.0.6:9090",
        entrypoint_prometheus_basic_auth_password_ref="env:ECHO_SECRET_ENTRYPOINT_PROMETHEUS_PASSWORD",
    )
    assert isinstance(get_prometheus_client(configured, DictSecretClient({})), PrometheusClient)
