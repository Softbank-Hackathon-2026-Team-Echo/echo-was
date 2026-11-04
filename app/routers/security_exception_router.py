import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.auth.guards import HumanDeveloperAccess, ViewerAccess
from app.dependencies import get_security_exception_service
from app.enums import SecurityControlId, SecurityExceptionStatus
from app.schemas.response import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, ApiResponse, Page
from app.schemas.security_exception import (
    CreateSecurityExceptionRequest,
    SecurityExceptionResponse,
)
from app.services.security_exception_service import SecurityExceptionService

# 조회는 뷰어(에이전트 가능). 요청·승인·철회는 개발자 이상 사람(에이전트는 예외를 만들 수 없다).
router = APIRouter(prefix="/api/v1/orgs/{org_id}/security", tags=["security-exception"])

SecurityExceptions = Annotated[SecurityExceptionService, Depends(get_security_exception_service)]


@router.post(
    "/exceptions",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[SecurityExceptionResponse],
)
async def create_security_exception(
    access: HumanDeveloperAccess,
    request: CreateSecurityExceptionRequest,
    security_exceptions: SecurityExceptions,
) -> ApiResponse[SecurityExceptionResponse]:
    view = await security_exceptions.create_exception(
        access.organization_id, access.user.id, request.to_command()
    )
    return ApiResponse(data=SecurityExceptionResponse.from_view(view))


@router.get("/exceptions", response_model=ApiResponse[Page[SecurityExceptionResponse]])
async def search_security_exceptions(
    access: ViewerAccess,
    security_exceptions: SecurityExceptions,
    page: Annotated[int, Query(ge=0)] = 0,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    control_id: Annotated[list[SecurityControlId] | None, Query(alias="controlId")] = None,
    service_id: Annotated[uuid.UUID | None, Query(alias="serviceId")] = None,
    environment_id: Annotated[uuid.UUID | None, Query(alias="environmentId")] = None,
    status_: Annotated[list[SecurityExceptionStatus] | None, Query(alias="status")] = None,
) -> ApiResponse[Page[SecurityExceptionResponse]]:
    result = await security_exceptions.search_exceptions(
        access.organization_id,
        control_ids=control_id or (),
        service_id=service_id,
        environment_id=environment_id,
        statuses=status_ or (),
        page=page,
        size=size,
    )
    return ApiResponse(
        data=Page(
            items=[SecurityExceptionResponse.from_view(view) for view in result.items],
            total=result.total,
            page=page,
            size=size,
        )
    )


@router.get("/exceptions/{exception_id}", response_model=ApiResponse[SecurityExceptionResponse])
async def get_security_exception(
    access: ViewerAccess,
    exception_id: uuid.UUID,
    security_exceptions: SecurityExceptions,
) -> ApiResponse[SecurityExceptionResponse]:
    view = await security_exceptions.get_exception(access.organization_id, exception_id)
    return ApiResponse(data=SecurityExceptionResponse.from_view(view))


@router.post(
    "/exceptions/{exception_id}/approve",
    response_model=ApiResponse[SecurityExceptionResponse],
)
async def approve_security_exception(
    access: HumanDeveloperAccess,
    exception_id: uuid.UUID,
    security_exceptions: SecurityExceptions,
) -> ApiResponse[SecurityExceptionResponse]:
    view = await security_exceptions.approve_exception(
        access.organization_id, exception_id, access.user.id
    )
    return ApiResponse(data=SecurityExceptionResponse.from_view(view))


@router.post(
    "/exceptions/{exception_id}/revoke",
    response_model=ApiResponse[SecurityExceptionResponse],
)
async def revoke_security_exception(
    access: HumanDeveloperAccess,
    exception_id: uuid.UUID,
    security_exceptions: SecurityExceptions,
) -> ApiResponse[SecurityExceptionResponse]:
    view = await security_exceptions.revoke_exception(
        access.organization_id, exception_id, access.user.id
    )
    return ApiResponse(data=SecurityExceptionResponse.from_view(view))
