"""진입점 Prometheus 읽기 클라이언트(운영 2, 서비스 대시보드 지표).

진입점 VM 의 Prometheus(:9090) HTTP API `GET /api/v1/query_range`·`GET /api/v1/query` 만 부른다.
basic auth 비밀번호는 비밀 참조로만 받아 호출마다 비밀 조회기에서 읽는다(참조가 없으면 인증 없이).
비밀번호·응답 원문은 저장·로그·실패 코드에 넣지 않는다. 실패는 `PrometheusQueryError(code)` 하나다.
"""

import asyncio
import base64
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.clients.secret_client import SecretClient, SecretLookupError
from app.core.secrets import SecretRef

PROMETHEUS_UNREACHABLE = "PROMETHEUS_UNREACHABLE"
PROMETHEUS_UNAUTHORIZED = "PROMETHEUS_UNAUTHORIZED"
PROMETHEUS_SECRET_MISSING = "PROMETHEUS_SECRET_MISSING"
PROMETHEUS_ERROR = "PROMETHEUS_ERROR"


class PrometheusQueryError(Exception):
    """Prometheus 질의 실패. code 는 고정 실패 코드이고 비밀값·응답 원문을 담지 않는다."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Sample:
    """시각 하나의 값. NaN·±Inf 는 None(값을 지어내지 않는다)."""

    at: datetime
    value: float | None


@dataclass(frozen=True)
class Series:
    """라벨 묶음 하나의 값들. instant 질의는 값이 하나다."""

    labels: dict[str, str]
    samples: tuple[Sample, ...]


class PrometheusClient:
    """요청마다 AsyncClient 를 새로 연다(진입점 실행기와 같은 방식, 프로세스 상태 없음)."""

    def __init__(
        self,
        base_url: str,
        *,
        secret_client: SecretClient,
        username: str,
        password_ref: SecretRef | None,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._secret_client = secret_client
        self._username = username
        self._password_ref = password_ref
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def query_range(
        self, queries: dict[str, str], start: datetime, end: datetime, step_seconds: int
    ) -> dict[str, list[Series]]:
        """이름 → PromQL 을 같은 구간·간격으로 읽는다. 하나라도 실패하면 PrometheusQueryError."""
        params = {
            "start": _unix(start),
            "end": _unix(end),
            "step": f"{step_seconds}s",
        }
        return await self._run("/api/v1/query_range", queries, params, "matrix")

    async def query(self, queries: dict[str, str], at: datetime) -> dict[str, list[Series]]:
        """이름 → PromQL 을 한 시각(instant)으로 읽는다."""
        return await self._run("/api/v1/query", queries, {"time": _unix(at)}, "vector")

    async def _run(
        self,
        path: str,
        queries: dict[str, str],
        params: dict[str, str],
        result_type: str,
    ) -> dict[str, list[Series]]:
        headers = await self._headers()
        names = list(queries)
        async with httpx.AsyncClient(
            base_url=self._base_url,
            transport=self._transport,
            timeout=self._timeout_seconds,
            follow_redirects=False,
        ) as http:
            # 질의를 한꺼번에 보내 전체 대기가 제한 시간 하나로 묶이게 한다. 모두 끝난 뒤 닫는다.
            outcomes = await asyncio.gather(
                *(
                    _get(http, path, {**params, "query": queries[name]}, headers, result_type)
                    for name in names
                ),
                return_exceptions=True,
            )
        results: dict[str, list[Series]] = {}
        for name, outcome in zip(names, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                raise outcome
            results[name] = outcome
        return results

    async def _headers(self) -> dict[str, str]:
        if self._password_ref is None:
            return {}
        try:
            password = await self._secret_client.get_secret(self._password_ref)
        except SecretLookupError:
            raise PrometheusQueryError(PROMETHEUS_SECRET_MISSING) from None
        raw = f"{self._username}:{password.get_secret_value()}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


async def _get(
    http: httpx.AsyncClient,
    path: str,
    params: dict[str, str],
    headers: dict[str, str],
    result_type: str,
) -> list[Series]:
    try:
        response = await http.get(path, params=params, headers=headers)
    except httpx.TransportError:
        raise PrometheusQueryError(PROMETHEUS_UNREACHABLE) from None
    except httpx.HTTPError:
        raise PrometheusQueryError(PROMETHEUS_ERROR) from None
    return _parse(response, result_type)


def _unix(value: datetime) -> str:
    return f"{value.timestamp():.3f}"


def _parse(response: httpx.Response, result_type: str) -> list[Series]:
    if response.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
        raise PrometheusQueryError(PROMETHEUS_UNAUTHORIZED)
    if not response.is_success:
        raise PrometheusQueryError(PROMETHEUS_ERROR)
    try:
        body = response.json()
    except ValueError:
        raise PrometheusQueryError(PROMETHEUS_ERROR) from None
    data = body.get("data") if isinstance(body, dict) else None
    if (
        not isinstance(body, dict)
        or body.get("status") != "success"
        or not isinstance(data, dict)
        or data.get("resultType") != result_type
        or not isinstance(data.get("result"), list)
    ):
        raise PrometheusQueryError(PROMETHEUS_ERROR)
    return [_series(item, result_type) for item in data["result"]]


def _series(item: Any, result_type: str) -> Series:
    if not isinstance(item, dict) or not isinstance(item.get("metric"), dict):
        raise PrometheusQueryError(PROMETHEUS_ERROR)
    labels = {str(key): str(value) for key, value in item["metric"].items()}
    raw_samples = item.get("values") if result_type == "matrix" else [item.get("value")]
    if not isinstance(raw_samples, list):
        raise PrometheusQueryError(PROMETHEUS_ERROR)
    return Series(labels=labels, samples=tuple(_sample(raw) for raw in raw_samples))


def _sample(raw: Any) -> Sample:
    if not isinstance(raw, list) or len(raw) != 2:
        raise PrometheusQueryError(PROMETHEUS_ERROR)
    at, value = raw
    if not isinstance(at, int | float) or isinstance(at, bool) or not isinstance(value, str):
        raise PrometheusQueryError(PROMETHEUS_ERROR)
    try:
        number = float(value)
    except ValueError:
        raise PrometheusQueryError(PROMETHEUS_ERROR) from None
    return Sample(
        at=datetime.fromtimestamp(at, tz=UTC),
        value=number if math.isfinite(number) else None,
    )
