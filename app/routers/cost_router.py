from typing import Annotated

from fastapi import APIRouter, Depends

from app.auth.guards import ViewerAccess
from app.dependencies import get_cost_service
from app.schemas.cost import CloudCostsResponse
from app.schemas.response import ApiResponse
from app.services.cost_service import CostService

# 클라우드 비용 보기(운영 1). 읽기만이라 뷰어(에이전트 가능).
router = APIRouter(prefix="/api/v1/orgs/{org_id}/costs", tags=["costs"])

Service = Annotated[CostService, Depends(get_cost_service)]


@router.get("", response_model=ApiResponse[CloudCostsResponse])
async def get_cloud_costs(
    access: ViewerAccess, service: Service
) -> ApiResponse[CloudCostsResponse]:
    view = await service.get_cloud_costs(access.organization_id)
    return ApiResponse(data=CloudCostsResponse.from_view(view))
