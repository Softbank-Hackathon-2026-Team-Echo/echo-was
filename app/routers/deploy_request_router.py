import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, status

from app.auth.guards import DeveloperAccess, ViewerAccess
from app.dependencies import get_deploy_request_service
from app.enums import LogSource
from app.schemas.deploy_request import (
    DeployRequestDetailResponse,
    LogPageResponse,
    RollbackRequest,
)
from app.schemas.response import ApiResponse
from app.services.deploy_request_service import DeployRequestService

# 조회·로그는 뷰어(에이전트 가능), 되돌리기는 개발자(배포 요청과 같은 검사기).
router = APIRouter(prefix="/api/v1/orgs/{org_id}/deploy-requests", tags=["deploy-request"])

DeployRequests = Annotated[DeployRequestService, Depends(get_deploy_request_service)]

DEFAULT_LOG_PAGE_SIZE = 200
MAX_LOG_PAGE_SIZE = 500


@router.get("/{deploy_request_id}", response_model=ApiResponse[DeployRequestDetailResponse])
async def get_deploy_request(
    access: ViewerAccess, deploy_request_id: uuid.UUID, deploy_requests: DeployRequests
) -> ApiResponse[DeployRequestDetailResponse]:
    view = await deploy_requests.get_deploy_request(access.organization_id, deploy_request_id)
    return ApiResponse(data=DeployRequestDetailResponse.from_view(view))


@router.post(
    "/{deploy_request_id}/rollback",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ApiResponse[DeployRequestDetailResponse],
)
async def create_rollback(
    access: DeveloperAccess,
    deploy_request_id: uuid.UUID,
    deploy_requests: DeployRequests,
    request: Annotated[RollbackRequest | None, Body()] = None,
) -> ApiResponse[DeployRequestDetailResponse]:
    simulate = request.simulate if request is not None else None
    view = await deploy_requests.create_rollback(
        access.organization_id,
        deploy_request_id,
        access.user.id,
        simulate.fail_at if simulate is not None else None,
    )
    return ApiResponse(data=DeployRequestDetailResponse.from_view(view))


@router.get("/{deploy_request_id}/logs", response_model=ApiResponse[LogPageResponse])
async def get_deploy_request_logs(
    access: ViewerAccess,
    deploy_request_id: uuid.UUID,
    deploy_requests: DeployRequests,
    source: Annotated[LogSource, Query()],
    cursor: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LOG_PAGE_SIZE)] = DEFAULT_LOG_PAGE_SIZE,
) -> ApiResponse[LogPageResponse]:
    view = await deploy_requests.get_deploy_request_logs(
        access.organization_id, deploy_request_id, source, cursor, limit
    )
    return ApiResponse(data=LogPageResponse.from_view(view))
