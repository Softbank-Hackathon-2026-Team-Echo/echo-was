"""인증 시험 도구: 사용자·조직·멤버·세션·에이전트 토큰 행을 DB 에 직접 넣고, 그 쿠키·토큰을 실은
클라이언트를 만든다.

로그인 흐름 자체를 시험하는 곳(test_auth_login.py)만 가짜 GitHub 로 콜백을 거친다. 다른 시험은 이
도구로 세션 행을 바로 만든다.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.tokens import hash_token, new_agent_token, new_session_token
from app.core.config import Settings
from app.enums import OrganizationRole
from app.main import app
from app.models import AgentToken, Organization, OrganizationMember, User, UserSession
from tests.github_user_fake import CLIENT_SECRET_ENV
from tests.integration.factories import add_organization, add_user

SessionFactory = async_sessionmaker[AsyncSession]

BASE_URL = "http://test"
TEST_ORIGIN = "https://app.echo.test"
FRONTEND_BASE_URL = "https://app.echo.test"
REDIRECT_URI = f"{BASE_URL}/api/v1/auth/github/callback"
SESSION_COOKIE = "echo_session"

# 시험 설정: 로그인 설정을 모두 채우고, httpx 가 http://test 에 쿠키를 돌려보내도록 Secure 를 끈다.
AUTH_SETTINGS: Mapping[str, Any] = {
    "github_app_client_id": "Iv1.test-client-id",
    "github_app_client_secret_ref": f"env:{CLIENT_SECRET_ENV}",
    "github_oauth_redirect_uri": REDIRECT_URI,
    "frontend_base_url": FRONTEND_BASE_URL,
    "frontend_allowed_origins": [TEST_ORIGIN],
    "session_cookie_secure": False,
}


def auth_settings(settings: Settings, **changes: Any) -> Settings:
    return settings.model_copy(update={**AUTH_SETTINGS, **changes})


@dataclass(frozen=True)
class Member:
    user: User
    organization: Organization
    role: OrganizationRole
    session_token: str


def org_path(organization_id: object, suffix: str = "") -> str:
    """`/api/v1/orgs/{org_id}{suffix}`."""
    return f"/api/v1/orgs/{organization_id}{suffix}"


def make_client(
    *,
    session_token: str | None = None,
    agent_token: str | None = None,
    origin: str | None = TEST_ORIGIN,
    base_path: str = "",
) -> AsyncClient:
    """세션 쿠키(사람) 또는 Bearer 토큰(에이전트)을 실은 클라이언트. 둘 다 없으면 익명.

    base_path 를 주면 base_url 이 그 경로가 되고, 요청 경로는 그 아래 상대 경로로 붙는다.
    """
    headers: dict[str, str] = {}
    if origin is not None:
        headers["Origin"] = origin
    if agent_token is not None:
        headers["Authorization"] = f"Bearer {agent_token}"
    cookies = {SESSION_COOKIE: session_token} if session_token is not None else None
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url=f"{BASE_URL}{base_path}",
        headers=headers,
        cookies=cookies,
    )


async def create_session(
    session_factory: SessionFactory, user: User, now: datetime, ttl_seconds: int = 604800
) -> str:
    token = new_session_token().get_secret_value()
    async with session_factory() as session, session.begin():
        session.add(
            UserSession(
                user_id=user.id,
                token_hash=hash_token(token),
                expires_at=now + timedelta(seconds=ttl_seconds),
                created_at=now,
            )
        )
    return token


async def create_organization(
    session_factory: SessionFactory, slug: str, creator: User | None = None
) -> Organization:
    async with session_factory() as session, session.begin():
        return await add_organization(session, slug, creator)


async def create_user(
    session_factory: SessionFactory, login: str, github_id: int | None = None
) -> User:
    async with session_factory() as session, session.begin():
        return await add_user(session, login, github_id)


async def add_member(
    session_factory: SessionFactory,
    organization: Organization,
    user: User,
    role: OrganizationRole,
) -> None:
    async with session_factory() as session, session.begin():
        session.add(OrganizationMember(organization_id=organization.id, user_id=user.id, role=role))


async def create_member(
    session_factory: SessionFactory,
    organization: Organization,
    role: OrganizationRole,
    now: datetime,
    login: str | None = None,
) -> Member:
    """사용자 + 멤버 + 세션 행. 쿠키 값(세션 토큰)을 돌려준다."""
    user = await create_user(session_factory, login or f"{organization.slug}-{role.lower()}")
    await add_member(session_factory, organization, user, role)
    token = await create_session(session_factory, user, now)
    return Member(user=user, organization=organization, role=role, session_token=token)


async def create_agent_token(
    session_factory: SessionFactory,
    organization: Organization,
    created_by: User,
    now: datetime,
    *,
    days: int = 30,
) -> tuple[AgentToken, str]:
    raw = new_agent_token().get_secret_value()
    token = AgentToken(
        organization_id=organization.id,
        name="test-agent",
        token_hash=hash_token(raw),
        last4=raw[-4:],
        created_by_user_id=created_by.id,
        expires_at=now + timedelta(days=days),
        created_at=now,
    )
    async with session_factory() as session, session.begin():
        session.add(token)
    return token, raw
