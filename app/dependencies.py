"""FastAPI 의존성 주입 함수. 시험은 app.dependency_overrides 로 바꾼다."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.registry import get_environment_adapter
from app.clients.github_source_repository import GithubAppSourceRepositoryClient
from app.clients.secret_client import EnvVarSecretClient, SecretClient
from app.clients.source_repository import FakeSourceRepositoryClient, SourceRepositoryClient
from app.core.clock import Clock, SystemClock
from app.core.config import Settings, SourceClientMode, get_settings
from app.core.database import get_session_factory
from app.core.secrets import parse_secret_ref
from app.repositories.environment_repository import EnvironmentRepository
from app.services.deploy_request_service import DeployRequestService
from app.services.environment_connection_service import (
    AdapterResolver,
    EnvironmentConnectionService,
)
from app.services.environment_kind_service import EnvironmentKindService
from app.services.environment_service import EnvironmentService
from app.services.service_service import ServiceService


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


def get_clock() -> Clock:
    return SystemClock()


def build_source_repository_client(settings: Settings, clock: Clock) -> SourceRepositoryClient:
    """설정에 맞는 소스 저장소 클라이언트. 일꾼 진입점도 이 함수를 쓴다."""
    if settings.source_client_mode is SourceClientMode.GITHUB:
        return GithubAppSourceRepositoryClient(
            api_base_url=settings.github_api_base_url,
            app_id=settings.github_app_id,
            private_key_ref=(
                parse_secret_ref(settings.github_app_private_key_ref)
                if settings.github_app_private_key_ref
                else None
            ),
            secret_client=EnvVarSecretClient(),
            install_url=settings.github_app_install_url,
            timeout_seconds=settings.github_request_timeout_seconds,
            clock=clock,
        )
    return FakeSourceRepositoryClient(install_url=settings.github_app_install_url)


def get_source_repository_client(
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> SourceRepositoryClient:
    return build_source_repository_client(settings, clock)


def get_service_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    source_client: Annotated[SourceRepositoryClient, Depends(get_source_repository_client)],
) -> ServiceService:
    return ServiceService(
        session=session,
        source_client=source_client,
        clock=clock,
        public_base_domain=settings.public_base_domain,
    )


def get_deploy_request_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    source_client: Annotated[SourceRepositoryClient, Depends(get_source_repository_client)],
) -> DeployRequestService:
    return DeployRequestService(
        session=session,
        source_client=source_client,
        clock=clock,
        executor_mode=settings.executor_mode,
        public_base_domain=settings.public_base_domain,
    )
