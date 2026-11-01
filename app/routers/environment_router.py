import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.dependencies import authorize_request, get_environment_service
from app.schemas.environment import (
    CreateEnvironmentRequest,
    EnvironmentDetailResponse,
    EnvironmentSummaryResponse,
    UpdateEnvironmentRequest,
)
from app.schemas.response import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, ApiResponse, Page
from app.services.environment_service import EnvironmentChanges, EnvironmentService

# 권한 검사 자리(API 서버 6): authorize_request 가 지금은 아무것도 검사하지 않는다.
router = APIRouter(
    prefix="/api/v1/environments",
    tags=["environment"],
    dependencies=[Depends(authorize_request)],
)

Service = Annotated[EnvironmentService, Depends(get_environment_service)]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[EnvironmentDetailResponse],
)
async def create_environment(
    request: CreateEnvironmentRequest, service: Service
) -> ApiResponse[EnvironmentDetailResponse]:
    view = await service.create_environment(
        name=request.name, kind=request.kind, config=request.config, secret_ref=request.secret_ref
    )
    return ApiResponse(data=EnvironmentDetailResponse.from_detail_view(view))


@router.get("", response_model=ApiResponse[Page[EnvironmentSummaryResponse]])
async def search_environments(
    service: Service,
    page: Annotated[int, Query(ge=0)] = 0,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    kind: Annotated[str | None, Query()] = None,
) -> ApiResponse[Page[EnvironmentSummaryResponse]]:
    result = await service.search_environments(kind=kind, page=page, size=size)
    return ApiResponse(
        data=Page(
            items=[EnvironmentSummaryResponse.from_view(view) for view in result.items],
            total=result.total,
            page=page,
            size=size,
        )
    )


@router.get("/{environment_id}", response_model=ApiResponse[EnvironmentDetailResponse])
async def get_environment(
    environment_id: uuid.UUID, service: Service
) -> ApiResponse[EnvironmentDetailResponse]:
    view = await service.get_environment(environment_id)
    return ApiResponse(data=EnvironmentDetailResponse.from_detail_view(view))


@router.patch("/{environment_id}", response_model=ApiResponse[EnvironmentDetailResponse])
async def update_environment(
    environment_id: uuid.UUID, request: UpdateEnvironmentRequest, service: Service
) -> ApiResponse[EnvironmentDetailResponse]:
    changes = EnvironmentChanges(
        name=request.name, kind=request.kind, config=request.config, secret_ref=request.secret_ref
    )
    view = await service.update_environment(environment_id, changes)
    return ApiResponse(data=EnvironmentDetailResponse.from_detail_view(view))


@router.delete("/{environment_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_environment(environment_id: uuid.UUID, service: Service) -> Response:
    await service.delete_environment(environment_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{environment_id}/connection-check",
    response_model=ApiResponse[EnvironmentDetailResponse],
)
async def check_environment_connection(
    environment_id: uuid.UUID, service: Service
) -> ApiResponse[EnvironmentDetailResponse]:
    view = await service.check_environment_connection(environment_id)
    return ApiResponse(data=EnvironmentDetailResponse.from_detail_view(view))
