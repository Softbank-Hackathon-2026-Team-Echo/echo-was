import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.auth.guards import HumanAdminAccess
from app.dependencies import get_invitation_service
from app.schemas.organization import CreateInvitationRequest, InvitationResponse
from app.schemas.response import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, ApiResponse, Page
from app.services.invitation_service import InvitationService

router = APIRouter(prefix="/api/v1/orgs/{org_id}/invitations", tags=["organization"])

Invitations = Annotated[InvitationService, Depends(get_invitation_service)]


@router.post(
    "", status_code=status.HTTP_201_CREATED, response_model=ApiResponse[InvitationResponse]
)
async def create_invitation(
    access: HumanAdminAccess, request: CreateInvitationRequest, invitations: Invitations
) -> ApiResponse[InvitationResponse]:
    invitation = await invitations.create_invitation(
        access.organization_id, access.user.id, request.github_login, request.role
    )
    return ApiResponse(data=InvitationResponse.from_model(invitation))


@router.get("", response_model=ApiResponse[Page[InvitationResponse]])
async def search_invitations(
    access: HumanAdminAccess,
    invitations: Invitations,
    page: Annotated[int, Query(ge=0)] = 0,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[InvitationResponse]]:
    result = await invitations.search_invitations(access.organization_id, page=page, size=size)
    return ApiResponse(
        data=Page(
            items=[InvitationResponse.from_model(item) for item in result.items],
            total=result.total,
            page=page,
            size=size,
        )
    )


@router.delete("/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def revoke_invitation(
    access: HumanAdminAccess, invitation_id: uuid.UUID, invitations: Invitations
) -> Response:
    await invitations.revoke_invitation(access.organization_id, invitation_id, access.user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
