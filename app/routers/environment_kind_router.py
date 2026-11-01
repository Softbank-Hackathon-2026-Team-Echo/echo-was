from typing import Annotated

from fastapi import APIRouter, Depends

from app.dependencies import authorize_request, get_environment_kind_service
from app.schemas.environment import EnvironmentKindResponse
from app.schemas.response import ApiResponse
from app.services.environment_kind_service import EnvironmentKindService

# 권한 검사 자리(API 서버 6): authorize_request 가 지금은 아무것도 검사하지 않는다.
router = APIRouter(
    prefix="/api/v1/environment-kinds",
    tags=["environment"],
    dependencies=[Depends(authorize_request)],
)


@router.get("", response_model=ApiResponse[list[EnvironmentKindResponse]])
async def search_environment_kinds(
    service: Annotated[EnvironmentKindService, Depends(get_environment_kind_service)],
) -> ApiResponse[list[EnvironmentKindResponse]]:
    config_classes = service.search_environment_kinds()
    return ApiResponse(
        data=[EnvironmentKindResponse.from_config_class(config) for config in config_classes]
    )
