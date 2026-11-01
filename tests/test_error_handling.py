import json
import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from app.core.exception_handlers import register_exception_handlers
from app.core.exceptions import ConflictError, ErrorField, InvalidInputError, NotFoundError
from app.core.logging import ContextFilter, JsonFormatter
from app.core.middleware import REQUEST_ID_HEADER, RequestContextMiddleware
from app.schemas.response import ApiModel


class CreateDeploymentRequest(ApiModel):
    image_digest: str


def _create_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    @app.post("/deployments")
    async def create_deployment(request: CreateDeploymentRequest) -> None:
        raise ConflictError("deployment in progress", service_id="svc-1")

    @app.get("/deployments/{deployment_id}")
    async def get_deployment(deployment_id: int) -> None:
        raise NotFoundError(deployment_id=deployment_id)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    @app.get("/invalid")
    async def invalid() -> None:
        raise InvalidInputError(
            "invalid input", details=[ErrorField("config.region", "INVALID_FORMAT")], kind="aws"
        )

    return app


async def test_app_error_with_details_returns_details_in_envelope() -> None:
    response = await _request("GET", "/invalid")

    assert response.status_code == 422
    assert response.json() == {
        "success": False,
        "code": "INVALID_INPUT",
        "message": "invalid input",
        "details": [{"field": "config.region", "reason": "INVALID_FORMAT"}],
    }


async def _request(
    method: str, url: str, *, headers: dict[str, str] | None = None, json_body: object = None
) -> Response:
    async with AsyncClient(transport=ASGITransport(app=_create_app()), base_url="http://t") as c:
        return await c.request(method, url, headers=headers, json=json_body)


async def test_app_error_returns_envelope_and_request_id_header() -> None:
    response = await _request("POST", "/deployments", json_body={"imageDigest": "sha256:abc"})

    assert response.status_code == 409
    assert response.json() == {
        "success": False,
        "code": "CONFLICT",
        "message": "deployment in progress",
    }
    assert len(response.headers[REQUEST_ID_HEADER]) == 32


async def test_validation_error_returns_camel_case_details() -> None:
    response = await _request("POST", "/deployments", json_body={})

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert response.json()["details"] == [{"field": "imageDigest", "reason": "Field required"}]


async def test_unhandled_exception_returns_internal_error_envelope() -> None:
    response = await _request("GET", "/boom")

    assert response.status_code == 500
    assert response.json() == {
        "success": False,
        "code": "INTERNAL_ERROR",
        "message": "internal server error",
    }


async def test_unknown_route_returns_not_found_envelope() -> None:
    response = await _request("GET", "/nope")

    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


async def test_request_id_header_valid_incoming_is_reused() -> None:
    response = await _request("GET", "/nope", headers={REQUEST_ID_HEADER: "front-req_01.a"})

    assert response.headers[REQUEST_ID_HEADER] == "front-req_01.a"


async def test_request_id_header_invalid_incoming_is_replaced() -> None:
    response = await _request("GET", "/nope", headers={REQUEST_ID_HEADER: "bad id\nforged"})

    assert response.headers[REQUEST_ID_HEADER] != "bad id\nforged"
    assert len(response.headers[REQUEST_ID_HEADER]) == 32


async def test_access_log_is_one_json_line_with_route_template_and_request_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="app.core.middleware"):
        response = await _request("GET", "/deployments/7")

    access = next(r for r in caplog.records if r.getMessage() == "request completed")
    ContextFilter("echo-api").filter(access)
    line = JsonFormatter().format(access)
    payload = json.loads(line)

    assert "\n" not in line
    assert payload["route"] == "/deployments/{deployment_id}"
    assert payload["status_code"] == 404
    assert payload["component"] == "echo-api"
    assert payload["request_id"] == response.headers[REQUEST_ID_HEADER]
