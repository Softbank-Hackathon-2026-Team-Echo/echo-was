from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from app.auth.cookies import (
    OAUTH_STATE_COOKIE,
    clear_session_cookie,
    clear_state_cookie,
    set_session_cookie,
    set_state_cookie,
)
from app.auth.guards import Human
from app.core.config import Settings, get_settings
from app.dependencies import get_auth_service
from app.schemas.organization import MeResponse
from app.schemas.response import ApiResponse
from app.services.auth_service import AuthService

# 로그인 시작·콜백·로그아웃은 공개 경로다(인증 검사기를 걸지 않는다).
router = APIRouter(prefix="/api/v1", tags=["auth"])

Auth = Annotated[AuthService, Depends(get_auth_service)]
AppSettings = Annotated[Settings, Depends(get_settings)]


@router.get(
    "/auth/github/login",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
)
async def start_github_login(
    auth: Auth,
    settings: AppSettings,
    next_path: Annotated[str | None, Query(alias="next")] = None,
) -> RedirectResponse:
    """GitHub 인증 화면으로 보낸다. next 는 로그인 뒤 갈 프론트 경로(`/` 로 시작하는 상대 경로)."""
    start = auth.start_login(next_path)
    response = RedirectResponse(start.authorize_url, status_code=status.HTTP_302_FOUND)
    set_state_cookie(response, settings, start.state_cookie)
    return response


@router.get(
    "/auth/github/callback",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
)
async def complete_github_login(
    request: Request,
    auth: Auth,
    settings: AppSettings,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    """성공하면 세션 쿠키와 함께 프론트(next)로, 실패하면 프론트 `/login?error=<코드>` 로 보낸다."""
    result = await auth.complete_login(
        code=code,
        state=state,
        error=error,
        state_cookie=request.cookies.get(OAUTH_STATE_COOKIE),
    )
    response = RedirectResponse(result.redirect_url, status_code=status.HTTP_302_FOUND)
    clear_state_cookie(response, settings)
    if result.session_token is not None:
        set_session_cookie(response, settings, result.session_token)
    return response


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def logout(request: Request, auth: Auth, settings: AppSettings) -> Response:
    """세션을 폐기하고 쿠키를 지운다. 세션이 없어도 204."""
    await auth.logout(request.cookies.get(settings.session_cookie_name))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookie(response, settings)
    return response


@router.get("/me", response_model=ApiResponse[MeResponse])
async def get_me(actor: Human, auth: Auth) -> ApiResponse[MeResponse]:
    """나와 내 조직(역할 포함). 부를 때 대기 초대를 적용한다."""
    view = await auth.get_me(actor.user)
    return ApiResponse(data=MeResponse.from_view(view))
