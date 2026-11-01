import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.auth.guards import HumanDeveloperAccess
from app.dependencies import get_agent_token_service
from app.schemas.organization import (
    AgentTokenResponse,
    CreateAgentTokenRequest,
    IssuedAgentTokenResponse,
)
from app.schemas.response import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, ApiResponse, Page
from app.services.agent_token_service import AgentTokenService

router = APIRouter(prefix="/api/v1/orgs/{org_id}/agent-tokens", tags=["organization"])

AgentTokens = Annotated[AgentTokenService, Depends(get_agent_token_service)]


@router.post(
    "", status_code=status.HTTP_201_CREATED, response_model=ApiResponse[IssuedAgentTokenResponse]
)
async def issue_agent_token(
    access: HumanDeveloperAccess, request: CreateAgentTokenRequest, agent_tokens: AgentTokens
) -> ApiResponse[IssuedAgentTokenResponse]:
    """원문 token 은 이 응답에만 한 번 싣는다."""
    issued = await agent_tokens.issue(
        access.organization_id, access.user.id, request.name, request.expires_in_days
    )
    return ApiResponse(data=IssuedAgentTokenResponse.from_issued(issued))


@router.get("", response_model=ApiResponse[Page[AgentTokenResponse]])
async def search_agent_tokens(
    access: HumanDeveloperAccess,
    agent_tokens: AgentTokens,
    page: Annotated[int, Query(ge=0)] = 0,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[AgentTokenResponse]]:
    result = await agent_tokens.search(access.organization_id, page=page, size=size)
    return ApiResponse(
        data=Page(
            items=[AgentTokenResponse.from_model(item) for item in result.items],
            total=result.total,
            page=page,
            size=size,
        )
    )


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def revoke_agent_token(
    access: HumanDeveloperAccess, token_id: uuid.UUID, agent_tokens: AgentTokens
) -> Response:
    """이미 폐기된 토큰이어도 204."""
    await agent_tokens.revoke(access.organization_id, token_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
