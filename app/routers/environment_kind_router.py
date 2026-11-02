from typing import Annotated

from fastapi import APIRouter, Depends

from app.auth.guards import LoggedIn
from app.dependencies import get_environment_kind_service
from app.schemas.environment import EnvironmentKindResponse
from app.schemas.response import ApiResponse
from app.services.environment_kind_service import EnvironmentKindService

# 조직 밖 경로. 로그인한 누구나(에이전트 포함) 읽는다.
router = APIRouter(prefix="/api/v1/environment-kinds", tags=["environment"])


@router.get("", response_model=ApiResponse[list[EnvironmentKindResponse]])
async def search_environment_kinds(
    _actor: LoggedIn,
    service: Annotated[EnvironmentKindService, Depends(get_environment_kind_service)],
) -> ApiResponse[list[EnvironmentKindResponse]]:
    config_classes = service.search_environment_kinds()
    return ApiResponse(
        data=[EnvironmentKindResponse.from_config_class(config) for config in config_classes]
    )
