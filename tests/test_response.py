from app.schemas.response import ApiModel, ApiResponse, ErrorDetail, Page


class EnvironmentSummary(ApiModel):
    environment_id: str
    traffic_weight: int


def test_api_response_page_serializes_camel_case_without_nulls() -> None:
    page = Page(
        items=[EnvironmentSummary(environment_id="env-1", traffic_weight=90)],
        total=1,
        page=0,
        size=20,
    )

    body = ApiResponse(data=page).model_dump(by_alias=True, exclude_none=True)

    assert body == {
        "success": True,
        "data": {
            "items": [{"environmentId": "env-1", "trafficWeight": 90}],
            "total": 1,
            "page": 0,
            "size": 20,
        },
    }


def test_api_response_validation_error_serializes_same_envelope() -> None:
    response: ApiResponse[None] = ApiResponse(
        success=False,
        code="VALIDATION_ERROR",
        message="invalid request",
        details=[ErrorDetail(field="imageDigest", reason="required")],
    )

    body = response.model_dump(by_alias=True, exclude_none=True)

    assert body == {
        "success": False,
        "code": "VALIDATION_ERROR",
        "message": "invalid request",
        "details": [{"field": "imageDigest", "reason": "required"}],
    }
