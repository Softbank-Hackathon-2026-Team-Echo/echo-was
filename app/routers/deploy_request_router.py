import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.dependencies import authorize_request, get_deploy_request_service
from app.schemas.deploy_request import DeployRequestDetailResponse
from app.schemas.response import ApiResponse
from app.services.deploy_request_service import DeployRequestService

# 권한 검사 자리(API 서버 6): authorize_request 가 지금은 아무것도 검사하지 않는다.
router = APIRouter(
    prefix="/api/v1/deploy-requests",
    tags=["deploy-request"],
    dependencies=[Depends(authorize_request)],
)

DeployRequests = Annotated[DeployRequestService, Depends(get_deploy_request_service)]


@router.get("/{deploy_request_id}", response_model=ApiResponse[DeployRequestDetailResponse])
async def get_deploy_request(
    deploy_request_id: uuid.UUID, deploy_requests: DeployRequests
) -> ApiResponse[DeployRequestDetailResponse]:
    view = await deploy_requests.get_deploy_request(deploy_request_id)
    return ApiResponse(data=DeployRequestDetailResponse.from_view(view))
