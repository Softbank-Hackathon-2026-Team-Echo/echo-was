import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.auth.guards import Human, HumanAdminAccess, HumanViewerAccess, ViewerAccess
from app.dependencies import get_organization_service
from app.schemas.organization import (
    CreateOrganizationRequest,
    MemberResponse,
    OrganizationResponse,
    UpdateMemberRoleRequest,
    UpdateOrganizationSettingsRequest,
)
from app.schemas.response import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, ApiResponse, Page
from app.services.organization_service import OrganizationService, OrganizationView

router = APIRouter(prefix="/api/v1/orgs", tags=["organization"])

Organizations = Annotated[OrganizationService, Depends(get_organization_service)]
PageNumber = Annotated[int, Query(ge=0)]
PageSize = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]


@router.post(
    "", status_code=status.HTTP_201_CREATED, response_model=ApiResponse[OrganizationResponse]
)
async def create_organization(
    actor: Human, request: CreateOrganizationRequest, organizations: Organizations
) -> ApiResponse[OrganizationResponse]:
    view = await organizations.create_organization(actor.user, request.slug, request.name)
    return ApiResponse(data=OrganizationResponse.from_view(view))


@router.get("", response_model=ApiResponse[Page[OrganizationResponse]])
async def search_organizations(
    actor: Human,
    organizations: Organizations,
    page: PageNumber = 0,
    size: PageSize = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[OrganizationResponse]]:
    result = await organizations.search_my_organizations(actor.user.id, page=page, size=size)
    return ApiResponse(
        data=Page(
            items=[OrganizationResponse.from_view(view) for view in result.items],
            total=result.total,
            page=page,
            size=size,
        )
    )


@router.get("/{org_id}", response_model=ApiResponse[OrganizationResponse])
async def get_organization(access: ViewerAccess) -> ApiResponse[OrganizationResponse]:
    view = OrganizationView(organization=access.organization, role=access.role)
    return ApiResponse(data=OrganizationResponse.from_view(view))


@router.patch("/{org_id}/settings", response_model=ApiResponse[OrganizationResponse])
async def update_organization_settings(
    access: HumanAdminAccess,
    request: UpdateOrganizationSettingsRequest,
    organizations: Organizations,
) -> ApiResponse[OrganizationResponse]:
    view = await organizations.update_settings(
        access.organization_id, access.role, request.allow_self_approval
    )
    return ApiResponse(data=OrganizationResponse.from_view(view))


@router.get("/{org_id}/members", response_model=ApiResponse[Page[MemberResponse]])
async def search_members(
    access: HumanViewerAccess,
    organizations: Organizations,
    page: PageNumber = 0,
    size: PageSize = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[MemberResponse]]:
    result = await organizations.search_members(access.organization_id, page=page, size=size)
    return ApiResponse(
        data=Page(
            items=[MemberResponse.from_view(view) for view in result.items],
            total=result.total,
            page=page,
            size=size,
        )
    )


@router.patch("/{org_id}/members/{user_id}", response_model=ApiResponse[MemberResponse])
async def update_member_role(
    access: HumanAdminAccess,
    user_id: uuid.UUID,
    request: UpdateMemberRoleRequest,
    organizations: Organizations,
) -> ApiResponse[MemberResponse]:
    view = await organizations.update_member_role(
        access.organization_id, access.user.id, user_id, request.role
    )
    return ApiResponse(data=MemberResponse.from_view(view))


@router.delete(
    "/{org_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response
)
async def remove_member(
    access: HumanViewerAccess, user_id: uuid.UUID, organizations: Organizations
) -> Response:
    """어드민은 다른 멤버를 내보내고, 누구나 자기 자신은 탈퇴한다."""
    await organizations.remove_member(access.organization_id, access.user.id, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
