import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.auth.guards import HumanAdminAccess, HumanDeveloperAccess, ViewerAccess
from app.core.secrets import SecretValue
from app.dependencies import get_service_variable_service
from app.schemas.response import ApiResponse
from app.schemas.service_variable import (
    PutServiceVariableRequest,
    SavedServiceVariableResponse,
    ServiceVariableListResponse,
    ServiceVariableResponse,
)
from app.services.service_variable_service import ServiceVariableService

# 목록은 뷰어(에이전트 가능), 넣기·바꾸기는 사람 개발자, 삭제는 사람 어드민.
router = APIRouter(
    prefix="/api/v1/orgs/{org_id}/services/{service_id}/environments/{environment_id}/variables",
    tags=["service-variable"],
)

Variables = Annotated[ServiceVariableService, Depends(get_service_variable_service)]


@router.get("", response_model=ApiResponse[ServiceVariableListResponse])
async def search_service_variables(
    access: ViewerAccess, service_id: uuid.UUID, environment_id: uuid.UUID, variables: Variables
) -> ApiResponse[ServiceVariableListResponse]:
    result = await variables.search_variables(access.organization_id, service_id, environment_id)
    return ApiResponse(
        data=ServiceVariableListResponse(
            items=[ServiceVariableResponse.from_view(item) for item in result.items],
            simulated=result.simulated,
        )
    )


@router.put("/{key}", response_model=ApiResponse[SavedServiceVariableResponse])
async def put_service_variable(
    access: HumanDeveloperAccess,
    service_id: uuid.UUID,
    environment_id: uuid.UUID,
    key: str,
    request: PutServiceVariableRequest,
    variables: Variables,
    response: Response,
) -> ApiResponse[SavedServiceVariableResponse]:
    saved = await variables.put_variable(
        access.organization,
        service_id,
        environment_id,
        key,
        SecretValue(request.value.get_secret_value()),
        access.user.id,
    )
    response.status_code = status.HTTP_201_CREATED if saved.created else status.HTTP_200_OK
    return ApiResponse(data=SavedServiceVariableResponse.from_saved(saved))


@router.delete("/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_service_variable(
    access: HumanAdminAccess,
    service_id: uuid.UUID,
    environment_id: uuid.UUID,
    key: str,
    variables: Variables,
) -> Response:
    await variables.delete_variable(
        access.organization_id, service_id, environment_id, key, access.user.id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
