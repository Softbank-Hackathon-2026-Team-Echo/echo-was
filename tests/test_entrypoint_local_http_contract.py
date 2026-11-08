"""짝 저장소의 실제 weights Handler와 WAS 실행기를 loopback HTTP로 연결한다.

ECHO_INFRA_WEIGHTS_APP을 지정해야 한다. 클라우드/DB/Docker/LLM은 호출하지 않는다.
"""

import asyncio
import importlib.util
import json
import os
import socket
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.executors.base import (
    MainRoute,
    RouteApplied,
    TrafficApplied,
    TrafficFailed,
    WeightedTarget,
)
from app.executors.entrypoint import (
    EntrypointClient,
    EntrypointMainRouteExecutor,
    EntrypointTrafficExecutor,
)
from tests.executor_contract import STANDBY_TARGET, TARGET
from tests.test_entrypoint_executors import TOKEN_REF, _Clock, _Secrets


async def test_actual_was_to_weights_http_two_services_and_foreign_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = os.environ.get("ECHO_INFRA_WEIGHTS_APP")
    if not source:
        pytest.skip("짝 echo-infra app.py를 ECHO_INFRA_WEIGHTS_APP으로 지정하는 로컬 계약 시험")
    spec = importlib.util.spec_from_file_location("scoped_weights_contract", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.syspath_prepend(str(Path(source).parent))
    monkeypatch.setenv("UPSTREAMS", "aws=127.0.0.1:18001,gcp=127.0.0.1:18002")
    spec.loader.exec_module(module)
    original_connect = socket.socket.connect

    def only_loopback(sock: socket.socket, address: Any) -> Any:
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1"):
            raise AssertionError(f"외부 접속 금지: {address[0]}")
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", only_loopback)
    a = MainRoute(TARGET.service_id, "a", "a.echo.test")
    b = MainRoute(uuid.UUID("20000000-0000-0000-0000-000000000002"), "b", "b.echo.test")
    tenant = MainRoute(
        uuid.UUID("30000000-0000-0000-0000-000000000003"), "tenant", "tenant.echo.test"
    )
    env_a, env_g = str(TARGET.environment_id), str(STANDBY_TARGET.environment_id)
    registry = {
        "services": [
            {
                "serviceId": str(route.service_id),
                "publicHost": route.public_host,
                "environments": {
                    env_a: {"host": "127.0.0.1", "port": 18001},
                    env_g: {"host": "127.0.0.1", "port": 18002},
                },
                "initialWeights": {
                    env_a: 0 if route == a else 100,
                    env_g: 100 if route == a else 0,
                },
            }
            for route in (a, b, tenant)
        ]
    }
    registration_path = tmp_path / "registration.json"
    registration_path.write_text(json.dumps(registry))
    module.DYNAMIC_DIR = str(tmp_path)
    module.STATE_DIR = str(tmp_path / "state")
    module.SERVICE_ROUTING_ENABLED = True
    module.SERVICE_ROUTES_FILE = str(registration_path)
    module.TOKEN = "entry-test-token"
    module.seed()
    requests: list[tuple[str, str]] = []

    class RecordingHandler(module.Handler):  # type: ignore[name-defined, misc]
        def do_GET(self) -> None:
            requests.append(("GET", self.path))
            super().do_GET()

        def do_PUT(self) -> None:
            requests.append(("PUT", self.path))
            super().do_PUT()

    server = module.ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    client = EntrypointClient(url + "/v1", TOKEN_REF, _Secrets())
    executor = EntrypointTrafficExecutor(client, _Clock())
    main = EntrypointMainRouteExecutor(client, _Clock())
    log: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(base_url=url, trust_env=False) as http:

            async def read(route: MainRoute) -> dict[str, Any]:
                response = await http.get(
                    f"/v1/services/{route.service_id}/weights",
                    params={"publicHost": route.public_host},
                    headers={"Authorization": "Bearer entry-test-token"},
                )
                assert response.status_code == 200
                return response.json()  # type: ignore[no-any-return]

            before_b, before_tenant = await read(b), await read(tenant)
            assert (await read(a))["weights"] == {env_a: 0, env_g: 100}
            for step in (10, 50, 100):
                targets = tuple(
                    WeightedTarget(
                        replace(target, service_id=a.service_id, main_public_host=a.public_host),
                        weight,
                    )
                    for target, weight in ((TARGET, step), (STANDBY_TARGET, 100 - step))
                )
                result = await executor.set_weights(a, targets, f"step-{step}")
                assert isinstance(result, TrafficApplied)
                assert (await read(a))["weights"] == {env_a: step, env_g: 100 - step}
                assert await read(b) == before_b
                assert await read(tenant) == before_tenant
                log.append(
                    {
                        "awsTargetWeight": step,
                        "a": await read(a),
                        "b": await read(b),
                        "tenant": await read(tenant),
                    }
                )
            # 한 executor에 같은 키를 써도 다른 서비스 성공 결과를 재사용하면 안 된다.
            b_target = replace(
                STANDBY_TARGET, service_id=b.service_id, main_public_host=b.public_host
            )
            assert isinstance(await main.route_main(b, b_target, "same-key"), RouteApplied)
            a_target = replace(TARGET, main_public_host=a.public_host)
            assert isinstance(await main.route_main(a, a_target, "same-key"), RouteApplied)
            assert (await read(b))["weights"] == {env_a: 0, env_g: 100}
            assert (await read(a))["weights"] == {env_a: 100, env_g: 0}
            # 등록 안 된 UUID: GET까지만, PUT 없음.
            puts = sum(method == "PUT" for method, _ in requests)
            unknown = replace(a_target, environment_id=uuid.uuid4())
            result = await executor.set_weights(a, (WeightedTarget(unknown, 100),), "unknown")
            assert result == TrafficFailed(code="ENTRYPOINT_ENV_UNMAPPED")
            assert sum(method == "PUT" for method, _ in requests) == puts
            # publicHost 불일치는 WAS 응답 검사/서버 게이트 모두 거절, global fallback 없음.
            wrong = replace(a, public_host=b.public_host)
            result = await main.route_main(
                wrong, replace(a_target, main_public_host=b.public_host), "wrong"
            )
            assert not isinstance(result, RouteApplied)
            assert all(path.startswith("/v1/") for _, path in requests)
            final = module.SCOPED_STORE.snapshot()
            (tmp_path / "state/services.json").write_text("corrupt")
            module.seed()
            assert module.SCOPED_STORE.snapshot() == final
            assert await read(tenant) == before_tenant
            evidence = os.environ.get("ECHO_ROUTING_EVIDENCE_DIR")
            if evidence:
                destination = Path(evidence)
                await asyncio.to_thread(destination.mkdir, parents=True, exist_ok=True)
                for name in ("routes.yaml", "clusters.yaml", "registration.json"):
                    (destination / name).write_bytes((tmp_path / name).read_bytes())
                (destination / "http-contract.json").write_text(
                    json.dumps({"steps": log, "requests": requests, "final": final}, indent=2)
                )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
