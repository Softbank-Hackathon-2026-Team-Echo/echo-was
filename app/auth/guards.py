"""권한 검사기(FastAPI 의존성). 라우트마다 하나를 건다.

- require_login: 로그인한 누구나(사람·에이전트).
- require_human: 사람만(에이전트는 403 AGENT_READ_ONLY).
- require_org_role(min_role, human_only=False): `/api/v1/orgs/{org_id}/…` 경로. 앞으로 API 서버
  4(롤백)·8(이전)·AI 파트의 엔드포인트도 이것을 건다.

검사 순서(본문 검증·리소스 조회보다 먼저):
1. 인증: `Authorization: Bearer echo_agt_…` 가 있으면 에이전트(쿠키는 보지 않는다), 아니면 세션
   쿠키로 사람. 둘 다 없거나 무효·만료·폐기면 401 UNAUTHENTICATED.
2. 조직: 사람은 그 조직 멤버가 아니면(조직이 없어도) 404 ORGANIZATION_NOT_FOUND. 에이전트는 토큰의
   조직과 다르면 같은 404. org_id 가 uuid 형식이 아니어도 404.
3. 역할: 최소 역할보다 낮으면 403 ROLE_FORBIDDEN. 에이전트는 GET·HEAD 와 뷰어 경로만, 그 밖은
   403 AGENT_READ_ONLY.
4. 쿠키로 인증한 변경 요청은 Origin 이 허용 목록에 정확히 있어야 한다(아니면 403
   ORIGIN_NOT_ALLOWED).

검사기는 요청 공용 세션(get_session)을 쓰지 않고 팩토리로 짧은 세션을 따로 연다. 공용 세션에 자동
트랜잭션이 열려 서비스의 session.begin() 이 실패하는 것을 막기 위해서다. 행위자·조직 id·팩토리는
request.state 에 두어 403 처리기가 거부 기록을 따로 커밋할 수 있게 한다.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import Depends, Path, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.actors import Actor, AgentActor, HumanActor, OrganizationAccess
from app.auth.cookies import clear_session_cookie_headers
from app.auth.tokens import AGENT_TOKEN_PREFIX, hash_token
from app.core.clock import Clock
from app.core.config import Settings, get_settings
from app.core.database import get_session_factory
from app.core.exceptions import (
    AgentReadOnlyError,
    OrganizationNotFoundError,
    OriginNotAllowedError,
    RoleForbiddenError,
    UnauthenticatedError,
)
from app.dependencies import get_clock
from app.enums import OrganizationRole
from app.models import Organization
from app.repositories.agent_token_repository import AgentTokenRepository
from app.repositories.organization_member_repository import OrganizationMemberRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.user_session_repository import UserSessionRepository

SessionFactory = async_sessionmaker[AsyncSession]
SettingsDep = Annotated[Settings, Depends(get_settings)]
ClockDep = Annotated[Clock, Depends(get_clock)]
FactoryDep = Annotated[SessionFactory, Depends(get_session_factory)]

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
AGENT_METHODS = frozenset({"GET", "HEAD"})
AGENT_LAST_USED_INTERVAL = timedelta(seconds=60)
_BEARER_PREFIX = "bearer "

# 경로 자동 수집 시험(AC-10)이 라우트 의존성에서 찾는 검사기들.
AUTH_GUARDS: set[Callable[..., Awaitable[object]]] = set()
ORGANIZATION_GUARDS: set[Callable[..., Awaitable[object]]] = set()


async def authenticate(
    request: Request, settings: Settings, clock: Clock, session_factory: SessionFactory
) -> Actor | None:
    """요청한 쪽을 정한다. 자격 증명이 없으면 None, 있는데 무효면 401."""
    request.state.session_factory = session_factory
    request.state.clock = clock
    authorization = request.headers.get("authorization")
    now = clock.now()
    if authorization is not None:
        return await _authenticate_agent(authorization, session_factory, now)
    cookie = request.cookies.get(settings.session_cookie_name)
    if cookie is None:
        return None
    async with session_factory() as session, session.begin():
        found = await UserSessionRepository(session).find_active_by_hash(hash_token(cookie), now)
    if found is None:
        raise UnauthenticatedError(headers=clear_session_cookie_headers(settings))
    user_session, user = found
    return HumanActor(user=user, session_id=user_session.id)


async def _authenticate_agent(
    authorization: str, session_factory: SessionFactory, now: datetime
) -> AgentActor:
    if not authorization.lower().startswith(_BEARER_PREFIX):
        raise UnauthenticatedError
    raw = authorization[len(_BEARER_PREFIX) :].strip()
    if not raw.startswith(AGENT_TOKEN_PREFIX):
        raise UnauthenticatedError
    async with session_factory() as session, session.begin():
        repository = AgentTokenRepository(session)
        token = await repository.find_active_by_hash(hash_token(raw), now)
        if token is not None:
            await repository.touch_last_used(token.id, now, AGENT_LAST_USED_INTERVAL)
    if token is None:
        raise UnauthenticatedError
    return AgentActor(token=token)


def _remember(request: Request, actor: Actor) -> None:
    request.state.actor = actor


def _check_origin(request: Request, actor: Actor, settings: Settings) -> None:
    if not isinstance(actor, HumanActor) or request.method in SAFE_METHODS:
        return
    if request.headers.get("origin") not in settings.frontend_allowed_origins:
        raise OriginNotAllowedError


async def require_login(
    request: Request, settings: SettingsDep, clock: ClockDep, session_factory: FactoryDep
) -> Actor:
    actor = await authenticate(request, settings, clock, session_factory)
    if actor is None:
        raise UnauthenticatedError
    _remember(request, actor)
    if isinstance(actor, AgentActor):
        request.state.organization_id = actor.organization_id
        if request.method not in AGENT_METHODS:
            raise AgentReadOnlyError
    _check_origin(request, actor, settings)
    return actor


async def require_human(
    request: Request, settings: SettingsDep, clock: ClockDep, session_factory: FactoryDep
) -> HumanActor:
    actor = await authenticate(request, settings, clock, session_factory)
    if actor is None:
        raise UnauthenticatedError
    _remember(request, actor)
    if isinstance(actor, AgentActor):
        request.state.organization_id = actor.organization_id
        raise AgentReadOnlyError
    _check_origin(request, actor, settings)
    return actor


AUTH_GUARDS.update({require_login, require_human})


def require_org_role(
    min_role: OrganizationRole, *, human_only: bool = False
) -> Callable[..., Awaitable[OrganizationAccess]]:
    """조직 경로 검사기를 만든다. min_role 이상인 멤버(에이전트는 GET·HEAD 뷰어 경로만)만 통과."""

    async def check_organization_access(
        request: Request,
        org_id: Annotated[str, Path(description="조직 id(uuid)")],
        settings: SettingsDep,
        clock: ClockDep,
        session_factory: FactoryDep,
    ) -> OrganizationAccess:
        actor = await authenticate(request, settings, clock, session_factory)
        if actor is None:
            raise UnauthenticatedError
        _remember(request, actor)
        organization_id = _parse_organization_id(org_id)
        organization, role = await _load_membership(session_factory, actor, organization_id)
        request.state.organization_id = organization.id
        if isinstance(actor, AgentActor):
            is_agent_readable = (
                not human_only
                and request.method in AGENT_METHODS
                and min_role is OrganizationRole.VIEWER
            )
            if not is_agent_readable:
                raise AgentReadOnlyError
        elif role is None or not role.includes(min_role):
            raise RoleForbiddenError
        _check_origin(request, actor, settings)
        return OrganizationAccess(organization=organization, actor=actor, role=role)

    ORGANIZATION_GUARDS.add(check_organization_access)
    AUTH_GUARDS.add(check_organization_access)
    return check_organization_access


def _parse_organization_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise OrganizationNotFoundError from None


async def _load_membership(
    session_factory: SessionFactory, actor: Actor, organization_id: uuid.UUID
) -> tuple[Organization, OrganizationRole | None]:
    if isinstance(actor, AgentActor) and actor.organization_id != organization_id:
        raise OrganizationNotFoundError
    async with session_factory() as session, session.begin():
        organization = await OrganizationRepository(session).find_by_id(organization_id)
        role: OrganizationRole | None = None
        if organization is not None and isinstance(actor, HumanActor):
            member = await OrganizationMemberRepository(session).find(
                organization_id, actor.user.id
            )
            role = OrganizationRole(member.role) if member is not None else None
    if organization is None or (isinstance(actor, HumanActor) and role is None):
        raise OrganizationNotFoundError
    return organization, role


# 라우트가 쓰는 검사기. 같은 검사기 객체를 여러 라우트가 함께 쓴다.
ViewerAccess = Annotated[OrganizationAccess, Depends(require_org_role(OrganizationRole.VIEWER))]
DeveloperAccess = Annotated[
    OrganizationAccess, Depends(require_org_role(OrganizationRole.DEVELOPER))
]
AdminAccess = Annotated[OrganizationAccess, Depends(require_org_role(OrganizationRole.ADMIN))]
HumanViewerAccess = Annotated[
    OrganizationAccess, Depends(require_org_role(OrganizationRole.VIEWER, human_only=True))
]
HumanDeveloperAccess = Annotated[
    OrganizationAccess, Depends(require_org_role(OrganizationRole.DEVELOPER, human_only=True))
]
HumanAdminAccess = Annotated[
    OrganizationAccess, Depends(require_org_role(OrganizationRole.ADMIN, human_only=True))
]
LoggedIn = Annotated[Actor, Depends(require_login)]
Human = Annotated[HumanActor, Depends(require_human)]
