import json

from app.core.exception_handlers import error_response
from app.schemas.response import ApiModel, ApiResponse, ErrorDetail, Page


class EnvironmentSummary(ApiModel):
    environment_id: str
    traffic_weight: int
    connection_error: str | None = None


def test_api_response_success_keeps_nulls_inside_data_and_omits_empty_envelope_fields() -> None:
    page = Page(
        items=[EnvironmentSummary(environment_id="env-1", traffic_weight=90)],
        total=1,
        page=0,
        size=20,
    )

    body = ApiResponse(data=page).model_dump(by_alias=True)

    assert body == {
        "success": True,
        "data": {
            "items": [{"environmentId": "env-1", "trafficWeight": 90, "connectionError": None}],
            "total": 1,
            "page": 0,
            "size": 20,
        },
    }


def test_api_response_success_json_mode_keeps_nulls_inside_data() -> None:
    body = ApiResponse(data=EnvironmentSummary(environment_id="e", traffic_weight=0)).model_dump(
        mode="json", by_alias=True
    )

    assert body["data"]["connectionError"] is None
    assert set(body) == {"success", "data"}


def test_error_response_validation_error_serializes_same_envelope() -> None:
    response = error_response(
        422,
        "VALIDATION_ERROR",
        "invalid request",
        [ErrorDetail(field="imageDigest", reason="required")],
    )

    assert json.loads(response.body) == {
        "success": False,
        "code": "VALIDATION_ERROR",
        "message": "invalid request",
        "details": [{"field": "imageDigest", "reason": "required"}],
    }


def test_error_response_without_details_omits_data_and_details() -> None:
    response = error_response(409, "CONFLICT", "conflict")

    assert json.loads(response.body) == {
        "success": False,
        "code": "CONFLICT",
        "message": "conflict",
    }


def test_api_response_openapi_schema_keeps_envelope_fields() -> None:
    schema = ApiResponse[EnvironmentSummary].model_json_schema(mode="serialization")

    assert set(schema["properties"]) == {"success", "code", "message", "data", "details"}
