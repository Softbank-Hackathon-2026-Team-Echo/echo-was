"""실제 HTTP 헬스체커 시험(SPEC api-3 AC-15). httpx.MockTransport 만 쓴다."""

import httpx
import pytest

from app.executors.base import HealthHttpError, HealthOk, HealthUnreachable
from app.executors.http_health_checker import HttpHealthChecker

URL = "https://shop-api.aws-seoul.apps.echo.test"


async def _probe(handler: httpx.MockTransport) -> object:
    return await HttpHealthChecker(transport=handler).probe(URL, "/healthz", 5)


@pytest.mark.parametrize("status", [200, 204])
async def test_probe_2xx_returns_ok(status: int) -> None:
    requested: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(status)

    result = await _probe(httpx.MockTransport(handle))

    assert result == HealthOk(status_code=status)
    assert requested == [f"{URL}/healthz"]


@pytest.mark.parametrize("status", [404, 500, 503])
async def test_probe_non_2xx_returns_http_error(status: int) -> None:
    result = await _probe(httpx.MockTransport(lambda request: httpx.Response(status)))

    assert result == HealthHttpError(status_code=status)


async def test_probe_redirect_is_not_followed() -> None:
    requested: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(302, headers={"location": f"{URL}/login"})

    result = await _probe(httpx.MockTransport(handle))

    assert result == HealthHttpError(status_code=302)
    assert requested == ["/healthz"]


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.ConnectTimeout("x")]
)
async def test_probe_connection_failure_returns_unreachable(error: httpx.TransportError) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise error

    result = await _probe(httpx.MockTransport(handle))

    assert result == HealthUnreachable()


def test_http_health_checker_is_not_simulated() -> None:
    assert HttpHealthChecker().simulated is False
