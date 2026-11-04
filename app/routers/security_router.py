import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.auth.guards import ViewerAccess
from app.dependencies import get_security_matrix_service
from app.enums import SecurityControlId
from app.schemas.response import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, ApiResponse, Page
from app.schemas.security import SecurityFindingResponse, SecurityMatrixResponse
from app.services.security_matrix_service import FINDING_IDS_LIMIT, SecurityMatrixService

# 보안 매트릭스·증거 조회(보안·거버넌스 4, 계약 4-7). 읽기만이라 뷰어(에이전트 가능).
router = APIRouter(prefix="/api/v1/orgs/{org_id}/security", tags=["security"])

Service = Annotated[SecurityMatrixService, Depends(get_security_matrix_service)]


@router.get("/matrix", response_model=ApiResponse[SecurityMatrixResponse])
async def get_security_matrix(
    access: ViewerAccess, service: Service
) -> ApiResponse[SecurityMatrixResponse]:
    view = await service.get_matrix(access.organization_id)
    return ApiResponse(data=SecurityMatrixResponse.from_view(view))


@router.get("/findings", response_model=ApiResponse[Page[SecurityFindingResponse]])
async def search_security_findings(
    access: ViewerAccess,
    service: Service,
    page: Annotated[int, Query(ge=0)] = 0,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    environment_id: Annotated[uuid.UUID | None, Query(alias="environmentId")] = None,
    service_id: Annotated[uuid.UUID | None, Query(alias="serviceId")] = None,
    control_id: Annotated[SecurityControlId | None, Query(alias="controlId")] = None,
    finding_ids: Annotated[
        list[uuid.UUID] | None, Query(alias="findingId", max_length=FINDING_IDS_LIMIT)
    ] = None,
    include_expired: Annotated[bool, Query(alias="includeExpired")] = False,
) -> ApiResponse[Page[SecurityFindingResponse]]:
    result = await service.search_findings(
        access.organization_id,
        environment_id=environment_id,
        service_id=service_id,
        control_id=control_id,
        finding_ids=finding_ids or [],
        include_expired=include_expired,
        page=page,
        size=size,
    )
    return ApiResponse(
        data=Page(
            items=[SecurityFindingResponse.from_model(model) for model in result.items],
            total=result.total,
            page=page,
            size=size,
        )
    )
