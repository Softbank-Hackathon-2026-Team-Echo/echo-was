import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.dependencies import authorize_request, get_deploy_request_service, get_service_service
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
from app.services.deploy_request_service import CreateDeployCommand, DeployRequestService
from app.services.service_service import CreateServiceCommand, ServiceChanges, ServiceService

# 권한 검사 자리(API 서버 6): authorize_request 가 지금은 아무것도 검사하지 않는다.
router = APIRouter(
    prefix="/api/v1/services", tags=["service"], dependencies=[Depends(authorize_request)]
)

Services = Annotated[ServiceService, Depends(get_service_service)]
DeployRequests = Annotated[DeployRequestService, Depends(get_deploy_request_service)]


@router.post(
    "", status_code=status.HTTP_201_CREATED, response_model=ApiResponse[ServiceDetailResponse]
)
async def create_service(
    request: CreateServiceRequest, services: Services
) -> ApiResponse[ServiceDetailResponse]:
    view = await services.create_service(
        CreateServiceCommand(
            name=request.name,
            description=request.description,
            repo_url=request.repo_url,
            health_check_path=request.health_check_path,
            health_check_timeout_seconds=request.health_check_timeout_seconds,
            container_port=request.container_port,
        )
    )
    return ApiResponse(data=ServiceDetailResponse.from_view(view))


@router.get("", response_model=ApiResponse[Page[ServiceSummaryResponse]])
async def search_services(
    services: Services,
    page: Annotated[int, Query(ge=0)] = 0,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[ServiceSummaryResponse]]:
    result = await services.search_services(page=page, size=size)
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
    service_id: uuid.UUID, services: Services
) -> ApiResponse[ServiceDetailResponse]:
    view = await services.get_service(service_id)
    return ApiResponse(data=ServiceDetailResponse.from_view(view))


@router.patch("/{service_id}", response_model=ApiResponse[ServiceDetailResponse])
async def update_service(
    service_id: uuid.UUID, request: UpdateServiceRequest, services: Services
) -> ApiResponse[ServiceDetailResponse]:
    view = await services.update_service(
        service_id,
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
    service_id: uuid.UUID, services: Services
) -> ApiResponse[ServiceDetailResponse]:
    view = await services.delete_service(service_id)
    return ApiResponse(data=ServiceDetailResponse.from_view(view))


@router.post(
    "/{service_id}/deploy-requests",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ApiResponse[DeployRequestDetailResponse],
)
async def create_deploy_request(
    service_id: uuid.UUID, request: CreateDeployRequest, deploy_requests: DeployRequests
) -> ApiResponse[DeployRequestDetailResponse]:
    ref_type, ref_value = request.ref
    view = await deploy_requests.create_deploy_request(
        service_id,
        CreateDeployCommand(
            environment_id=request.environment_id,
            ref_type=ref_type,
            ref_value=ref_value,
            simulate_fail_at=request.simulate.fail_at if request.simulate is not None else None,
        ),
    )
    return ApiResponse(data=DeployRequestDetailResponse.from_view(view))


@router.get(
    "/{service_id}/deploy-requests",
    response_model=ApiResponse[Page[DeployRequestSummaryResponse]],
)
async def search_deploy_requests(
    service_id: uuid.UUID,
    deploy_requests: DeployRequests,
    page: Annotated[int, Query(ge=0)] = 0,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    environment_id: Annotated[uuid.UUID | None, Query(alias="environmentId")] = None,
) -> ApiResponse[Page[DeployRequestSummaryResponse]]:
    result = await deploy_requests.search_deploy_requests(service_id, environment_id, page, size)
    return ApiResponse(
        data=Page(
            items=[DeployRequestSummaryResponse.from_view(view) for view in result.items],
            total=result.total,
            page=page,
            size=size,
        )
    )
