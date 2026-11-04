import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.auth.guards import AdminAccess, DeveloperAccess, ViewerAccess
from app.dependencies import get_environment_service
from app.schemas.environment import (
    CreateEnvironmentRequest,
    EnvironmentDetailResponse,
    EnvironmentSummaryResponse,
    UpdateEnvironmentRequest,
)
from app.schemas.response import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, ApiResponse, Page
from app.services.environment_service import EnvironmentChanges, EnvironmentService

# 조회는 뷰어(에이전트 가능), 등록·수정·다시 확인은 개발자, 삭제는 어드민.
router = APIRouter(prefix="/api/v1/orgs/{org_id}/environments", tags=["environment"])

Service = Annotated[EnvironmentService, Depends(get_environment_service)]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[EnvironmentDetailResponse],
)
async def create_environment(
    access: DeveloperAccess, request: CreateEnvironmentRequest, service: Service
) -> ApiResponse[EnvironmentDetailResponse]:
    view = await service.create_environment(
        access.organization,
        access.user.id,
        name=request.name,
        kind=request.kind,
        config=request.config,
        secret_ref=request.secret_ref,
    )
    return ApiResponse(data=EnvironmentDetailResponse.from_detail_view(view))


@router.get("", response_model=ApiResponse[Page[EnvironmentSummaryResponse]])
async def search_environments(
    access: ViewerAccess,
    service: Service,
    page: Annotated[int, Query(ge=0)] = 0,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    kind: Annotated[str | None, Query()] = None,
) -> ApiResponse[Page[EnvironmentSummaryResponse]]:
    result = await service.search_environments(
        access.organization_id, kind=kind, page=page, size=size
    )
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
    access: ViewerAccess, environment_id: uuid.UUID, service: Service
) -> ApiResponse[EnvironmentDetailResponse]:
    view = await service.get_environment(access.organization_id, environment_id)
    return ApiResponse(data=EnvironmentDetailResponse.from_detail_view(view))


@router.patch("/{environment_id}", response_model=ApiResponse[EnvironmentDetailResponse])
async def update_environment(
    access: DeveloperAccess,
    environment_id: uuid.UUID,
    request: UpdateEnvironmentRequest,
    service: Service,
) -> ApiResponse[EnvironmentDetailResponse]:
    changes = EnvironmentChanges(
        name=request.name, kind=request.kind, config=request.config, secret_ref=request.secret_ref
    )
    view = await service.update_environment(
        access.organization, environment_id, access.user.id, changes
    )
    return ApiResponse(data=EnvironmentDetailResponse.from_detail_view(view))


@router.delete("/{environment_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_environment(
    access: AdminAccess, environment_id: uuid.UUID, service: Service
) -> Response:
    await service.delete_environment(access.organization_id, environment_id, access.user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{environment_id}/connection-check",
    response_model=ApiResponse[EnvironmentDetailResponse],
)
async def check_environment_connection(
    access: DeveloperAccess, environment_id: uuid.UUID, service: Service
) -> ApiResponse[EnvironmentDetailResponse]:
    view = await service.check_environment_connection(
        access.organization_id, environment_id, access.user.id
    )
    return ApiResponse(data=EnvironmentDetailResponse.from_detail_view(view))
