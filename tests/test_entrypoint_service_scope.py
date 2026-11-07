"""서비스 scope는 kind나 Host 헤더가 아니라 UUID + 저장된 대표 주소다."""

import json
from dataclasses import replace

import httpx

from app.executors.base import TrafficApplied, TrafficFailed, WeightedTarget
from app.executors.entrypoint import EntrypointClient, EntrypointTrafficExecutor
from tests.executor_contract import ROUTE as LEGACY_ROUTE
from tests.executor_contract import STANDBY_TARGET, TARGET
from tests.test_entrypoint_executors import TOKEN_REF, _Clock, _Secrets

ROUTE = replace(LEGACY_ROUTE, public_host=TARGET.main_public_host)


async def test_service_mode_sends_same_scope_for_read_write_and_uuid_weights() -> None:
    requests: list[httpx.Request] = []
    envs = {str(TARGET.environment_id): 0, str(STANDBY_TARGET.environment_id): 100}

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "PUT":
            payload = json.loads(request.content)
            assert payload == {
                "publicHost": ROUTE.public_host,
                "expectedRevision": 7,
                "weights": {str(TARGET.environment_id): 10, str(STANDBY_TARGET.environment_id): 90},
            }
        return httpx.Response(
            200,
            json={
                "serviceId": str(ROUTE.service_id),
                "publicHost": ROUTE.public_host,
                "revision": 7 if request.method == "GET" else 8,
                "weights": envs
                if request.method == "GET"
                else json.loads(request.content)["weights"],
            },
        )

    client = EntrypointClient(
        "http://entry.test/v1", TOKEN_REF, _Secrets(), transport=httpx.MockTransport(handle)
    )
    result = await EntrypointTrafficExecutor(client, _Clock()).set_weights(
        ROUTE, (WeightedTarget(TARGET, 10), WeightedTarget(STANDBY_TARGET, 90)), "same-key"
    )
    assert isinstance(result, TrafficApplied)
    assert len(requests) == 2
    assert all(r.url.path == f"/v1/services/{ROUTE.service_id}/weights" for r in requests)
    assert all(r.url.params["publicHost"] == ROUTE.public_host for r in requests)


async def test_service_mode_rejects_foreign_target_before_any_http() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404)

    client = EntrypointClient(
        "http://entry.test/v1", TOKEN_REF, _Secrets(), transport=httpx.MockTransport(handle)
    )
    result = await EntrypointTrafficExecutor(client, _Clock()).set_weights(
        ROUTE,
        (WeightedTarget(replace(TARGET, service_id=STANDBY_TARGET.environment_id), 100),),
        "x",
    )
    assert result == TrafficFailed(code="ENTRYPOINT_SCOPE_MISMATCH")
    assert requests == []


async def test_service_counters_send_uuid_and_host_without_global_read() -> None:
    from app.executors.base import RequestCounts
    from app.executors.entrypoint import EntrypointMetricsProvider

    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "serviceId": str(TARGET.service_id),
                "publicHost": TARGET.main_public_host,
                "envs": {str(TARGET.environment_id): {"requests": 7, "errors_5xx": 1, "hosts": 1}},
            },
        )

    client = EntrypointClient(
        "http://entry.test/v1", TOKEN_REF, _Secrets(), transport=httpx.MockTransport(handle)
    )
    result = await EntrypointMetricsProvider(client).read_counts(TARGET)
    assert result == RequestCounts(7, 1)
    assert requests[0].url.path == f"/v1/services/{TARGET.service_id}/counters"
    assert requests[0].url.params["publicHost"] == TARGET.main_public_host


async def test_service_mode_errors_never_fall_back_to_global_weights() -> None:
    from app.executors.base import RouteFailed
    from app.executors.entrypoint import EntrypointMainRouteExecutor

    for status, reason, expected in (
        (404, "SERVICE_NOT_REGISTERED", "ENTRYPOINT_SERVICE_NOT_REGISTERED"),
        (409, "REVISION_CONFLICT", "ENTRYPOINT_REVISION_CONFLICT"),
        (409, "SERVICE_SCOPE_MISMATCH", "ENTRYPOINT_SCOPE_MISMATCH"),
    ):
        requests: list[httpx.Request] = []

        def handle(
            request: httpx.Request,
            requests: list[httpx.Request] = requests,
            status: int = status,
            reason: str = reason,
        ) -> httpx.Response:
            requests.append(request)
            return httpx.Response(status, json={"error": reason})

        client = EntrypointClient(
            "http://entry.test/v1", TOKEN_REF, _Secrets(), transport=httpx.MockTransport(handle)
        )
        result = await EntrypointMainRouteExecutor(client, _Clock()).route_main(ROUTE, TARGET, "x")
        assert result == RouteFailed(code=expected)
        assert len(requests) == 1 and requests[0].url.path.startswith("/v1/services/")


def test_reverse_gcp_to_aws_planned_and_failback_are_preserved() -> None:
    from app.enums import MigrationPlanMode
    from app.models.migration_plan import MigrationPlan
    from app.models.service_environment import ServiceEnvironment
    from app.services.migration_plan_service import decide_plan_mode

    source = ServiceEnvironment(id=STANDBY_TARGET.service_environment_id)
    target = ServiceEnvironment(id=TARGET.service_environment_id)
    assert decide_plan_mode(None, source, target) == MigrationPlanMode.PLANNED
    last = MigrationPlan(
        source_service_environment_id=target.id,
        target_service_environment_id=source.id,
        mode=MigrationPlanMode.PLANNED,
    )
    assert decide_plan_mode(last, source, target) == MigrationPlanMode.FAILBACK
    last.mode = MigrationPlanMode.FAILBACK
    assert decide_plan_mode(last, source, target) == MigrationPlanMode.PLANNED
