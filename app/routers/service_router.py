import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.auth.guards import AdminAccess, DeveloperAccess, ViewerAccess
from app.dependencies import get_deploy_request_service, get_service_service
from app.enums import DeployRequestStatus, DeployRequestTrigger
from app.schemas.deploy_request import (
    CreateDeployRequest,
    DeployRequestDetailResponse,
    DeployRequestSummaryResponse,
)
from app.schemas.response import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, ApiResponse, Page
from app.schemas.service import (
    CreateServiceRequest,
    ServiceDetailResponse,
    ServiceSummaryResponse,
    UpdateServiceRequest,
)
from app.services.deploy_request_service import (
    CreateDeployCommand,
    DeployRequestFilter,
    DeployRequestService,
)
from app.services.service_service import CreateServiceCommand, ServiceChanges, ServiceService

# 조회는 뷰어(에이전트 가능), 등록·수정·배포 요청은 개발자, 삭제는 어드민.
router = APIRouter(prefix="/api/v1/orgs/{org_id}/services", tags=["service"])

Services = Annotated[ServiceService, Depends(get_service_service)]
DeployRequests = Annotated[DeployRequestService, Depends(get_deploy_request_service)]


@router.post(
    "", status_code=status.HTTP_201_CREATED, response_model=ApiResponse[ServiceDetailResponse]
)
async def create_service(
    access: DeveloperAccess, request: CreateServiceRequest, services: Services
) -> ApiResponse[ServiceDetailResponse]:
    view = await services.create_service(
        CreateServiceCommand(
            organization_id=access.organization_id,
            name=request.name,
            description=request.description,
            repo_url=request.repo_url,
            health_check_path=request.health_check_path,
            health_check_timeout_seconds=request.health_check_timeout_seconds,
            container_port=request.container_port,
        ),
        access.user.id,
    )
    return ApiResponse(data=ServiceDetailResponse.from_view(view))


@router.get("", response_model=ApiResponse[Page[ServiceSummaryResponse]])
async def search_services(
    access: ViewerAccess,
    services: Services,
    page: Annotated[int, Query(ge=0)] = 0,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[ServiceSummaryResponse]]:
    result = await services.search_services(access.organization_id, page=page, size=size)
    return ApiResponse(
        data=Page(
            items=[ServiceSummaryResponse.from_model(service) for service in result.items],
            total=result.total,
            page=page,
            size=size,
        )
    )


@router.get("/{service_id}", response_model=ApiResponse[ServiceDetailResponse])
async def get_service(
    access: ViewerAccess, service_id: uuid.UUID, services: Services
) -> ApiResponse[ServiceDetailResponse]:
    view = await services.get_service(access.organization_id, service_id)
    return ApiResponse(data=ServiceDetailResponse.from_view(view))


@router.patch("/{service_id}", response_model=ApiResponse[ServiceDetailResponse])
async def update_service(
    access: DeveloperAccess,
    service_id: uuid.UUID,
    request: UpdateServiceRequest,
    services: Services,
) -> ApiResponse[ServiceDetailResponse]:
    view = await services.update_service(
        access.organization_id,
        service_id,
        access.user.id,
        ServiceChanges(
            name=request.name,
            description=request.description,
            description_set=request.is_description_set,
            repo_url=request.repo_url,
            health_check_path=request.health_check_path,
            health_check_timeout_seconds=request.health_check_timeout_seconds,
            container_port=request.container_port,
        ),
    )
    return ApiResponse(data=ServiceDetailResponse.from_view(view))


@router.delete(
    "/{service_id}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ApiResponse[ServiceDetailResponse],
)
async def delete_service(
    access: AdminAccess, service_id: uuid.UUID, services: Services
) -> ApiResponse[ServiceDetailResponse]:
    view = await services.delete_service(access.organization_id, service_id, access.user.id)
    return ApiResponse(data=ServiceDetailResponse.from_view(view))


@router.post(
    "/{service_id}/deploy-requests",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ApiResponse[DeployRequestDetailResponse],
)
async def create_deploy_request(
    access: DeveloperAccess,
    service_id: uuid.UUID,
    request: CreateDeployRequest,
    deploy_requests: DeployRequests,
) -> ApiResponse[DeployRequestDetailResponse]:
    ref_type, ref_value = request.ref
    view = await deploy_requests.create_deploy_request(
        access.organization_id,
        service_id,
        access.user.id,
        CreateDeployCommand(
            environment_id=request.environment_id,
            ref_type=ref_type,
            ref_value=ref_value,
            simulate_fail_at=request.simulate.fail_at if request.simulate is not None else None,
            standby_environment_ids=tuple(request.standby_environment_ids),
        ),
    )
    return ApiResponse(data=DeployRequestDetailResponse.from_view(view))


@router.get(
    "/{service_id}/deploy-requests",
    response_model=ApiResponse[Page[DeployRequestSummaryResponse]],
)
async def search_deploy_requests(
    access: ViewerAccess,
    service_id: uuid.UUID,
    deploy_requests: DeployRequests,
    page: Annotated[int, Query(ge=0)] = 0,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    environment_id: Annotated[uuid.UUID | None, Query(alias="environmentId")] = None,
    trigger: Annotated[list[DeployRequestTrigger] | None, Query()] = None,
    status_: Annotated[list[DeployRequestStatus] | None, Query(alias="status")] = None,
) -> ApiResponse[Page[DeployRequestSummaryResponse]]:
    result = await deploy_requests.search_deploy_requests(
        access.organization_id,
        service_id,
        DeployRequestFilter(
            environment_id=environment_id, triggers=trigger or (), statuses=status_ or ()
        ),
        page,
        size,
    )
    return ApiResponse(
        data=Page(
            items=[DeployRequestSummaryResponse.from_view(view) for view in result.items],
            total=result.total,
            page=page,
            size=size,
        )
    )
