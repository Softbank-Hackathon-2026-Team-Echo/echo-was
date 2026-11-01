"""FastAPI 의존성 주입 함수. 시험은 app.dependency_overrides 로 바꾼다."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.registry import get_environment_adapter
from app.clients.secret_client import EnvVarSecretClient, SecretClient
from app.core.config import Settings, get_settings
from app.core.database import get_session_factory
from app.repositories.environment_repository import EnvironmentRepository
from app.services.environment_connection_service import (
    AdapterResolver,
    EnvironmentConnectionService,
)
from app.services.environment_kind_service import EnvironmentKindService
from app.services.environment_service import EnvironmentService


async def authorize_request() -> None:
    """권한 검사 자리(API 서버 6: GitHub 로그인 + 역할 4종). 지금은 아무것도 검사하지 않는다.

    인증이 없으므로 이 서버는 인터넷에 열지 않는다(팀 내부 실행만).
    """


async def get_session(
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> AsyncIterator[AsyncSession]:
    """트랜잭션을 열지 않은 세션. 서비스가 session.begin() 블록 단위로 커밋한다."""
    async with session_factory() as session:
        yield session


def get_secret_client() -> SecretClient:
    return EnvVarSecretClient()


def get_adapter_resolver() -> AdapterResolver:
    return get_environment_adapter


def get_environment_connection_service(
    settings: Annotated[Settings, Depends(get_settings)],
    secret_client: Annotated[SecretClient, Depends(get_secret_client)],
    adapter_resolver: Annotated[AdapterResolver, Depends(get_adapter_resolver)],
) -> EnvironmentConnectionService:
    return EnvironmentConnectionService(
        secret_client=secret_client,
        adapter_resolver=adapter_resolver,
        timeout_seconds=settings.environment_check_timeout_seconds,
    )


def get_environment_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    connection_service: Annotated[
        EnvironmentConnectionService, Depends(get_environment_connection_service)
    ],
) -> EnvironmentService:
    return EnvironmentService(
        session=session,
        environment_repository=EnvironmentRepository(session),
        connection_service=connection_service,
    )


def get_environment_kind_service() -> EnvironmentKindService:
    return EnvironmentKindService()
