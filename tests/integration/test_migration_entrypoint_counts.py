"""고정 진입점 카운터로 이전 비율 단계 판정(SPEC api-8-2 AC-8-2-6).

http 모드 실행기(트래픽·대표 주소·지표, 같은 EntrypointClient)를 httpx.MockTransport 진입점에
붙여, 8-1 처리기가 실제 `GET /counters` 누적값으로 단계 하나를 건수 기준(COUNT_REACHED)으로
통과시키는지 본다. 진입점 대본은 적용된 비율 1%당 초당 요청 1건을 쌓는다.
"""

import json
from datetime import datetime
from typing import Any

import httpx

from app.core.secrets import SecretRef, SecretValue, parse_secret_ref
from app.enums import MigrationJudgeReason
from app.executors.entrypoint import (
    EntrypointClient,
    EntrypointMainRouteExecutor,
    EntrypointMetricsProvider,
    EntrypointTrafficExecutor,
)
from app.services.migration_plan_support import RESULT_JUDGE_REASON
from tests.integration.deploy_harness import FakeClock
from tests.integration.migration_support import Migration

BASE_URL = "http://entry.echo.test:8080"
TOKEN = "entry-integration-token"
TOKEN_REF = parse_secret_ref("env:ECHO_SECRET_ENTRYPOINT_TOKEN")


class _Secrets:
    async def get_secret(self, ref: SecretRef) -> SecretValue:
        return SecretValue(TOKEN)


class _CountingEntrypoint:
    """가중치 API + 누적 카운터 대본. 읽을 때마다 지난 초 × 비율만큼 요청이 쌓인다."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self._last: datetime = clock.now()
        self.weights = {"aws": 100, "gcp": 0}
        self.requests = {"aws": 0.0, "gcp": 0.0}
        self.paths: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(f"{request.method} {request.url.path}")
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": "Bearer 토큰이 필요하다"})
        self._accumulate()
        if request.url.path == "/weights" and request.method == "PUT":
            self.weights = {name: int(value) for name, value in json.loads(request.content).items()}
        if request.url.path == "/weights":
            return httpx.Response(200, json=self.weights)
        if request.url.path == "/counters":
            return httpx.Response(200, json=self._counters())
        return httpx.Response(404, json={"error": "없는 경로"})

    def _accumulate(self) -> None:
        elapsed = (self._clock.now() - self._last).total_seconds()
        self._last = self._clock.now()
        for name, weight in self.weights.items():
            self.requests[name] += weight * elapsed

    def _counters(self) -> dict[str, Any]:
        return {
            "ts": self._clock.now().isoformat(),
            "envs": {
                name: {"requests": int(value), "errors_5xx": 0, "hosts": 1}
                for name, value in self.requests.items()
            },
        }


async def test_entrypoint_counters_pass_a_shift_step_by_request_count(
    migration: Migration,
) -> None:
    harness = migration.harness
    entrypoint = _CountingEntrypoint(harness.clock)
    client = EntrypointClient(
        BASE_URL, TOKEN_REF, _Secrets(), transport=httpx.MockTransport(entrypoint.handle)
    )
    harness.replace_executors(
        traffic=EntrypointTrafficExecutor(client, harness.clock),
        main_route=EntrypointMainRouteExecutor(client, harness.clock),
        metrics=EntrypointMetricsProvider(client),
    )
    plan = await migration.create_plan()
    await migration.approve(plan["id"])

    assert await harness.worker().run_once()
    started = await migration.plan_row(plan["id"])
    positions = []
    for _ in range(4):
        harness.clock.advance(1)
        assert await harness.worker().run_once()
        row = await migration.plan_row(plan["id"])
        positions.append((row.current_step, row.step_phase))
    final = await migration.get_plan(plan["id"])
    stored = await migration.plan_row(plan["id"])

    # 단계 1(5%): 초당 5건으로 2초에 10건을 채우고 최소 관찰 4초에 다음 단계로 간다.
    assert (started.current_step, started.step_phase) == (1, "OBSERVE")
    assert positions == [(1, "OBSERVE"), (1, "JUDGE"), (1, "JUDGE"), (2, "OBSERVE")]
    step = final["steps"][1]["result"]
    assert step["status"] == "PASSED"
    assert step["metrics"]["lowTraffic"] is False
    assert step["metrics"]["requestCount"] == 10
    assert step["metrics"]["sourceRequestCount"] == 190
    assert stored.step_results[1][RESULT_JUDGE_REASON] == MigrationJudgeReason.COUNT_REACHED.value
    assert final["status"] == "RUNNING" and final["errorCode"] is None
    assert entrypoint.weights == {"aws": 90, "gcp": 10}
    assert await migration.weights() == (90, 10)
    assert "GET /counters" in entrypoint.paths
