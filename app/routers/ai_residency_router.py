from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.auth.guards import ViewerAccess
from app.dependencies import get_ai_residency_service
from app.schemas.ai_residency import AiResidencySummaryResponse
from app.schemas.response import ApiResponse
from app.services.ai_residency_service import AiResidencyService

DEFAULT_RESIDENCY_DAYS = 7
MAX_RESIDENCY_DAYS = 90

# 읽기만이라 뷰어(에이전트 가능).
router = APIRouter(prefix="/api/v1/orgs/{org_id}/ai", tags=["ai-residency"])

Residency = Annotated[AiResidencyService, Depends(get_ai_residency_service)]


@router.get("/residency", response_model=ApiResponse[AiResidencySummaryResponse])
async def get_ai_residency_summary(
    access: ViewerAccess,
    residency: Residency,
    days: Annotated[int, Query(ge=1, le=MAX_RESIDENCY_DAYS)] = DEFAULT_RESIDENCY_DAYS,
) -> ApiResponse[AiResidencySummaryResponse]:
    summary = await residency.get_summary(access.organization_id, days)
    return ApiResponse(data=AiResidencySummaryResponse.from_summary(summary))
