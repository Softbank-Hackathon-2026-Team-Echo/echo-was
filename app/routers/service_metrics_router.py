import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.auth.guards import ViewerAccess
from app.dependencies import get_service_metrics_service
from app.schemas.response import ApiResponse
from app.schemas.service_metrics import ServiceMetricsResponse
from app.services.service_metrics import (
    DEFAULT_RANGE,
    DEFAULT_STEP,
    ServiceMetricsService,
    parse_metrics_window,
)

# 서비스 대시보드 지표(운영 2). 읽기만이라 뷰어(에이전트 가능).
router = APIRouter(prefix="/api/v1/orgs/{org_id}/services", tags=["service-metrics"])

Metrics = Annotated[ServiceMetricsService, Depends(get_service_metrics_service)]


@router.get("/{service_id}/metrics", response_model=ApiResponse[ServiceMetricsResponse])
async def get_service_metrics(
    access: ViewerAccess,
    service_id: uuid.UUID,
    metrics: Metrics,
    time_range: Annotated[str, Query(alias="range", max_length=8)] = DEFAULT_RANGE,
    step: Annotated[str, Query(max_length=8)] = DEFAULT_STEP,
) -> ApiResponse[ServiceMetricsResponse]:
    window = parse_metrics_window(time_range, step)
    view = await metrics.get_metrics(access.organization_id, service_id, window)
    return ApiResponse(data=ServiceMetricsResponse.from_view(view))
