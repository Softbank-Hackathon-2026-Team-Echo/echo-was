"""서비스 대시보드 지표 API 통합 시험(운영 2 AC-1~AC-8).

Prometheus 는 httpx.MockTransport 로 흉내 낸다(질의 글자로 rps·5xx·p95·요약을 고른다). 복제 지연은
비-simulated 시험 확인기로 바꿔 끼운다. 시각은 하네스의 주입 시계다.
"""

import base64
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
import pytest

from app.clients.prometheus_client import PrometheusClient
from app.core.secrets import parse_secret_ref
from app.dependencies import get_prometheus_client
from app.executors.base import DataSyncLag, DataSyncResult, DataSyncUnavailable, DeployTarget
from app.main import app
from app.services.service_metrics import parse_metrics_window, window_bounds
from tests.integration.deploy_harness import DeployHarness
from tests.integration.migration_support import Migration
from tests.real_executor_fakes import DictSecretClient

NAMESPACE = "shop-api-m1g2r3t4"
PASSWORD_LOCATION = "ECHO_SECRET_ENTRYPOINT_PROMETHEUS_PASSWORD"


class _LagChecker:
    """실제 확인기처럼 simulated = false 인 시험용 복제 지연."""

    def __init__(self, result: DataSyncResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    @property
    def simulated(self) -> bool:
        return False

    async def replication_lag(self, source: DeployTarget, target: DeployTarget) -> DataSyncResult:
        self.calls.append((source.environment_kind, target.environment_kind))
        return self.result


@dataclass
class FakePrometheus:
    """질의 글자로 응답을 고른다. fail 이 있으면 모든 요청에 그 응답(또는 예외)."""

    fail: Callable[[httpx.Request], httpx.Response] | None = None
    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail is not None:
            return self.fail(request)
        query = request.url.params["query"]
        if request.url.path == "/api/v1/query_range":
            end = float(request.url.params["end"])
            return httpx.Response(200, json=_matrix(_range_series(query, end)))
        return httpx.Response(
            200, json=_vector(_instant_value(query), float(request.url.params["time"]))
        )

    def client(self, password_ref: str | None = None) -> PrometheusClient:
        return PrometheusClient(
            "http://prom.test:9090",
            secret_client=DictSecretClient({PASSWORD_LOCATION: "prom-pw"}),
            username="echo-otel",
            password_ref=parse_secret_ref(password_ref) if password_ref else None,
            timeout_seconds=1.0,
            transport=httpx.MockTransport(self.handler),
        )

    @property
    def queries(self) -> list[str]:
        return [request.url.params["query"] for request in self.requests]


def _range_series(query: str, end: float) -> list[dict[str, Any]]:
    # 끝 두 점만 준다: aws 는 요청 20/s(5xx 0.5/s)·p95 182ms, gcp 는 요청 2/s(5xx 없음)·p95 NaN.
    times = [end - 10, end]
    if query.startswith("histogram_quantile"):
        return [
            {"metric": {"cloud_provider": "aws"}, "values": [[t, "182.04"] for t in times]},
            {"metric": {"cloud_provider": "gcp"}, "values": [[t, "NaN"] for t in times]},
        ]
    if 'status=~"5.."' in query:
        return [{"metric": {"cloud_provider": "aws"}, "values": [[t, "0.5"] for t in times]}]
    return [
        {"metric": {"cloud_provider": "aws"}, "values": [[t, "20"] for t in times]},
        {"metric": {"cloud_provider": "gcp"}, "values": [[t, "2"] for t in times]},
    ]


def _instant_value(query: str) -> str:
    if query.startswith("histogram_quantile"):
        return "181.96"
    if 'status=~"5.."' in query:
        return "28"
    return "100000"


def _matrix(result: list[dict[str, Any]]) -> dict[str, Any]:
    return {"status": "success", "data": {"resultType": "matrix", "result": result}}


def _vector(value: str, at: float) -> dict[str, Any]:
    return {
        "status": "success",
        "data": {"resultType": "vector", "result": [{"metric": {}, "value": [at, value]}]},
    }


@pytest.fixture
def prometheus() -> FakePrometheus:
    return FakePrometheus()


def _use_prometheus(fake: FakePrometheus, password_ref: str | None = None) -> None:
    app.dependency_overrides[get_prometheus_client] = lambda: fake.client(password_ref)


async def _get_metrics(migration: Migration, **params: str) -> dict[str, Any]:
    response = await migration.harness.client.get(_path(migration), params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    data: dict[str, Any] = body["data"]
    return data


def _path(migration: Migration) -> str:
    return f"{migration.harness.services}/{migration.service_id}/metrics"


def _iso(harness: DeployHarness, offset_seconds: float = 0) -> str:
    _, end = window_bounds(harness.clock.now(), parse_metrics_window("15m", "10s"))
    return (end + timedelta(seconds=offset_seconds)).isoformat().replace("+00:00", "Z")


async def test_metrics_follow_frontend_contract_from_prometheus(
    migration: Migration, prometheus: FakePrometheus
) -> None:
    harness = migration.harness
    lag = _LagChecker(DataSyncLag(seconds=0.12))
    harness.replace_executors(data_sync=lag)
    _use_prometheus(prometheus)

    data = await _get_metrics(migration, range="15m", step="10s")

    assert data == {
        "range": "15m",
        "step": "10s",
        "asOf": _iso(harness),
        "metricsStatus": "OK",
        "metricsErrorCode": None,
        "series": [
            {
                "environmentId": str(migration.aws.id),
                "environmentName": "aws-seoul",
                "cloudProvider": "aws",
                "points": [
                    {"t": _iso(harness, -10), "rps": 20.0, "successRate": 0.975, "p95Ms": 182.0},
                    {"t": _iso(harness), "rps": 20.0, "successRate": 0.975, "p95Ms": 182.0},
                ],
            },
            {
                "environmentId": str(migration.gcp.id),
                "environmentName": "gcp-seoul",
                "cloudProvider": "gcp",
                "points": [
                    {"t": _iso(harness, -10), "rps": 2.0, "successRate": 1.0, "p95Ms": None},
                    {"t": _iso(harness), "rps": 2.0, "successRate": 1.0, "p95Ms": None},
                ],
            },
        ],
        "summary": {
            "successRate": 0.99972,
            "p95Ms": 182.0,
            "replicationLagMs": 120.0,
            "errorBudgetLeft": 0.72,
            "sloTarget": 0.999,
        },
        "simulated": False,
    }
    # 서비스 중(aws) → 예비(gcp) 복제 지연을 한 번 읽었다.
    assert lag.calls == [("aws", "gcp")]
    # 3 개 범위 질의 + 3 개 요약 질의. 모두 그 서비스 네임스페이스로 거르고 헬스 경로를 뺀다.
    assert len(prometheus.requests) == 6
    assert all(f'namespace="{NAMESPACE}"' in query for query in prometheus.queries)
    assert all('path!~"/healthz|/readyz|/metrics"' in query for query in prometheus.queries)
    range_requests = [r for r in prometheus.requests if r.url.path == "/api/v1/query_range"]
    assert {r.url.params["step"] for r in range_requests} == {"10s"}
    assert all("Authorization" not in r.headers for r in prometheus.requests)


async def test_metrics_default_to_15m_and_10s(
    migration: Migration, prometheus: FakePrometheus
) -> None:
    _use_prometheus(prometheus)

    data = await _get_metrics(migration)

    assert (data["range"], data["step"], data["metricsStatus"]) == ("15m", "10s", "OK")


async def test_metrics_send_basic_auth_when_password_ref_is_set(
    migration: Migration, prometheus: FakePrometheus
) -> None:
    _use_prometheus(prometheus, password_ref=f"env:{PASSWORD_LOCATION}")

    data = await _get_metrics(migration)

    assert data["metricsStatus"] == "OK"
    expected = "Basic " + base64.b64encode(b"echo-otel:prom-pw").decode()
    assert {request.headers["Authorization"] for request in prometheus.requests} == {expected}


def _connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("down", request=request)


@pytest.mark.parametrize(
    ("fail", "code"),
    [
        (_connect_error, "PROMETHEUS_UNREACHABLE"),
        (lambda request: httpx.Response(401), "PROMETHEUS_UNAUTHORIZED"),
        (lambda request: httpx.Response(200, text="<html>"), "PROMETHEUS_ERROR"),
    ],
    ids=["unreachable", "unauthorized", "not-json"],
)
async def test_metrics_stay_200_with_empty_series_when_prometheus_fails(
    migration: Migration,
    fail: Callable[[httpx.Request], httpx.Response],
    code: str,
) -> None:
    migration.harness.replace_executors(data_sync=_LagChecker(DataSyncLag(seconds=0.05)))
    _use_prometheus(FakePrometheus(fail=fail))

    data = await _get_metrics(migration)

    assert (data["metricsStatus"], data["metricsErrorCode"], data["series"]) == (
        "UNAVAILABLE",
        code,
        [],
    )
    # Prometheus 와 따로 읽는 복제 지연은 그대로 보인다.
    assert data["summary"] == {
        "successRate": None,
        "p95Ms": None,
        "replicationLagMs": 50.0,
        "errorBudgetLeft": None,
        "sloTarget": 0.999,
    }
    assert data["simulated"] is False


async def test_metrics_without_prometheus_url_are_not_configured(migration: Migration) -> None:
    # 하네스 설정은 Prometheus 주소가 비어 있고, 가짜 복제 지연(simulated)은 보이지 않는다.
    assert migration.harness.settings.entrypoint_prometheus_url is None

    data = await _get_metrics(migration)

    assert (data["metricsStatus"], data["metricsErrorCode"], data["series"]) == (
        "NOT_CONFIGURED",
        None,
        [],
    )
    assert data["summary"]["replicationLagMs"] is None
    assert data["summary"]["successRate"] is None


async def test_metrics_hide_unmeasured_replication_lag(
    migration: Migration, prometheus: FakePrometheus
) -> None:
    migration.harness.replace_executors(data_sync=_LagChecker(DataSyncUnavailable()))
    _use_prometheus(prometheus)

    data = await _get_metrics(migration)

    assert data["summary"]["replicationLagMs"] is None
    assert data["summary"]["successRate"] == 0.99972


async def test_metrics_reuse_result_for_five_seconds(
    migration: Migration, prometheus: FakePrometheus
) -> None:
    _use_prometheus(prometheus)

    first = await _get_metrics(migration)
    second = await _get_metrics(migration)
    assert first == second
    assert len(prometheus.requests) == 6

    # 다른 구간은 다른 캐시 칸이다.
    await _get_metrics(migration, range="30m", step="30s")
    assert len(prometheus.requests) == 12

    migration.harness.clock.advance(5)
    await _get_metrics(migration)
    assert len(prometheus.requests) == 18


@pytest.mark.parametrize(
    ("params", "field_name", "reason"),
    [
        ({"range": "15x"}, "range", "INVALID_DURATION"),
        ({"range": "1m"}, "range", "RANGE_OUT_OF_BOUNDS"),
        ({"step": "1s"}, "step", "STEP_OUT_OF_BOUNDS"),
        ({"range": "24h", "step": "10s"}, "step", "TOO_MANY_POINTS"),
    ],
    ids=["form", "range", "step", "points"],
)
async def test_metrics_reject_out_of_bounds_window(
    migration: Migration,
    prometheus: FakePrometheus,
    params: dict[str, str],
    field_name: str,
    reason: str,
) -> None:
    _use_prometheus(prometheus)

    response = await migration.harness.client.get(_path(migration), params=params)

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["details"] == [{"field": field_name, "reason": reason}]
    assert prometheus.requests == []


async def test_metrics_of_unknown_service_is_not_found(
    harness: DeployHarness, prometheus: FakePrometheus
) -> None:
    _use_prometheus(prometheus)

    response = await harness.client.get(f"{harness.services}/{uuid.uuid4()}/metrics")

    assert (response.status_code, response.json()["code"]) == (404, "SERVICE_NOT_FOUND")
    assert prometheus.requests == []
