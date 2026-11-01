"""실제 HTTP 헬스체커(`health_checker_mode = http`).

`GET {url}{path}` 를 한 번 보낸다. 2xx 면 통과, 그 밖의 응답은 HealthHttpError(상태 코드),
연결 실패·시간 초과는 HealthUnreachable. 리다이렉트는 따르지 않는다(3xx 는 HealthHttpError).
"""

import httpx

from app.executors.base import HealthHttpError, HealthOk, HealthProbeResult, HealthUnreachable


class HttpHealthChecker:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    @property
    def simulated(self) -> bool:
        return False

    async def probe(self, url: str, path: str, timeout_seconds: float) -> HealthProbeResult:
        async with httpx.AsyncClient(
            transport=self._transport, timeout=timeout_seconds, follow_redirects=False
        ) as http:
            try:
                response = await http.get(f"{url.rstrip('/')}{path}")
            except httpx.TransportError:
                return HealthUnreachable()
        if response.is_success:
            return HealthOk(status_code=response.status_code)
        return HealthHttpError(status_code=response.status_code)
