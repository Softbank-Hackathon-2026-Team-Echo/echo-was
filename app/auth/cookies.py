"""세션 쿠키·로그인 상태 쿠키. 둘 다 HttpOnly·SameSite=Lax 이고 Secure 는 설정을 따른다."""

from starlette.responses import Response

from app.core.config import Settings
from app.core.secrets import SecretValue

OAUTH_STATE_COOKIE = "echo_oauth_state"
# 상태 쿠키는 로그인 시작·콜백 경로에만 보낸다.
OAUTH_STATE_COOKIE_PATH = "/api/v1/auth/github"
SESSION_COOKIE_PATH = "/"


def set_session_cookie(response: Response, settings: Settings, token: SecretValue) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token.get_secret_value(),
        max_age=settings.session_ttl_seconds,
        path=SESSION_COOKIE_PATH,
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.session_cookie_name,
        path=SESSION_COOKIE_PATH,
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie_headers(settings: Settings) -> dict[str, str]:
    """401 응답에 실을 쿠키 삭제 헤더."""
    response = Response()
    clear_session_cookie(response, settings)
    return {"set-cookie": response.headers["set-cookie"]}


def set_state_cookie(response: Response, settings: Settings, value: str) -> None:
    response.set_cookie(
        OAUTH_STATE_COOKIE,
        value,
        max_age=settings.oauth_state_ttl_seconds,
        path=OAUTH_STATE_COOKIE_PATH,
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_state_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        OAUTH_STATE_COOKIE,
        path=OAUTH_STATE_COOKIE_PATH,
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
