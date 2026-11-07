import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.guards import ViewerAccess
from app.core.clock import Clock
from app.dependencies import get_clock, get_cost_service, get_session
from app.schemas.cost import CloudCostsResponse
from app.schemas.cost_recommendation import CostRecommendationsResponse
from app.schemas.response import ApiResponse
from app.services.cost_catalog import CostCatalog, get_cost_catalog
from app.services.cost_recommendation import CostRecommendationService
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


# 이 leaf 전용 의존성을 여기에 둬 공용 dependencies/main 파일 편집을 피한다.
def get_cost_recommendation_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
    catalog: Annotated[CostCatalog, Depends(get_cost_catalog)],
) -> CostRecommendationService:
    return CostRecommendationService(session, clock, catalog)


@router.get("/recommendations", response_model=ApiResponse[CostRecommendationsResponse])
async def get_cost_recommendations(
    access: ViewerAccess,
    service: Annotated[CostRecommendationService, Depends(get_cost_recommendation_service)],
    service_id: Annotated[uuid.UUID, Query(alias="serviceId")],
) -> ApiResponse[CostRecommendationsResponse]:
    view = await service.get_recommendations(access.organization_id, service_id)
    return ApiResponse(data=CostRecommendationsResponse.from_view(view))
