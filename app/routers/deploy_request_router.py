import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.auth.guards import ViewerAccess
from app.dependencies import get_deploy_request_service
from app.schemas.deploy_request import DeployRequestDetailResponse
from app.schemas.response import ApiResponse
from app.services.deploy_request_service import DeployRequestService

router = APIRouter(prefix="/api/v1/orgs/{org_id}/deploy-requests", tags=["deploy-request"])

DeployRequests = Annotated[DeployRequestService, Depends(get_deploy_request_service)]


@router.get("/{deploy_request_id}", response_model=ApiResponse[DeployRequestDetailResponse])
async def get_deploy_request(
    access: ViewerAccess, deploy_request_id: uuid.UUID, deploy_requests: DeployRequests
) -> ApiResponse[DeployRequestDetailResponse]:
    view = await deploy_requests.get_deploy_request(access.organization_id, deploy_request_id)
    return ApiResponse(data=DeployRequestDetailResponse.from_view(view))
