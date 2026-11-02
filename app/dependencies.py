"""FastAPI 의존성 주입 함수. 시험은 app.dependency_overrides 로 바꾼다."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.registry import get_environment_adapter
from app.clients.github_source_repository import GithubAppSourceRepositoryClient
from app.clients.github_user import GithubUserClient, HttpGithubUserClient
from app.clients.secret_client import EnvVarSecretClient, SecretClient
from app.clients.source_repository import FakeSourceRepositoryClient, SourceRepositoryClient
from app.core.clock import Clock, SystemClock
from app.core.config import ExecutorMode, Settings, SourceClientMode, get_settings
from app.core.database import get_session_factory
from app.core.secrets import parse_secret_ref
from app.executors import ExecutorSet, build_executor_set
from app.repositories.environment_repository import EnvironmentRepository
from app.services.agent_token_service import AgentTokenService
from app.services.auth_service import AuthService
from app.services.deploy_request_service import DeployRequestService
from app.services.deploy_support import HostSuffixGenerator, generate_host_suffix
from app.services.environment_connection_service import (
    AdapterResolver,
    EnvironmentConnectionService,
)
from app.services.environment_kind_service import EnvironmentKindService
from app.services.environment_service import EnvironmentService
from app.services.invitation_service import InvitationService
from app.services.migration_plan_service import MigrationPlanService
from app.services.organization_service import OrganizationService
from app.services.service_service import ServiceService


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


def get_host_suffix_generator() -> HostSuffixGenerator:
    """대표·환경별 주소에 붙일 무작위 suffix 생성기. 시험은 고정 값 생성기로 바꾼다."""
    return generate_host_suffix


def get_service_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    source_client: Annotated[SourceRepositoryClient, Depends(get_source_repository_client)],
    host_suffix_generator: Annotated[HostSuffixGenerator, Depends(get_host_suffix_generator)],
) -> ServiceService:
    return ServiceService(
        session=session,
        source_client=source_client,
        clock=clock,
        public_base_domain=settings.public_base_domain,
        host_suffix_generator=host_suffix_generator,
    )


def get_executor_set(
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> ExecutorSet | None:
    """API 프로세스가 로그를 읽을 실행기 묶음(배포는 일꾼만 한다).

    실제 실행기가 아직 없는 모드(executor_mode != fake)면 None 이고, 빌드·실행 로그는 빈 페이지다.
    시험은 dependency_overrides 로 시험용 가짜 실행기를 넣는다.
    """
    if settings.executor_mode is not ExecutorMode.FAKE:
        return None
    return build_executor_set(settings, clock)


def get_deploy_request_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    source_client: Annotated[SourceRepositoryClient, Depends(get_source_repository_client)],
    executors: Annotated[ExecutorSet | None, Depends(get_executor_set)],
) -> DeployRequestService:
    return DeployRequestService(
        session=session,
        source_client=source_client,
        clock=clock,
        executor_mode=settings.executor_mode,
        public_base_domain=settings.public_base_domain,
        executors=executors,
    )


def get_github_user_client(
    settings: Annotated[Settings, Depends(get_settings)],
    secret_client: Annotated[SecretClient, Depends(get_secret_client)],
) -> GithubUserClient:
    """실제 GitHub 사용자 클라이언트. 가짜는 시험의 dependency_overrides 로만 넣는다."""
    return HttpGithubUserClient(
        oauth_base_url=settings.github_oauth_base_url,
        api_base_url=settings.github_api_base_url,
        client_id=settings.github_app_client_id,
        client_secret_ref=(
            parse_secret_ref(settings.github_app_client_secret_ref)
            if settings.github_app_client_secret_ref
            else None
        ),
        secret_client=secret_client,
        timeout_seconds=settings.github_request_timeout_seconds,
    )


def get_auth_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    github_client: Annotated[GithubUserClient, Depends(get_github_user_client)],
) -> AuthService:
    return AuthService(session=session, settings=settings, clock=clock, github_client=github_client)


def get_organization_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> OrganizationService:
    return OrganizationService(
        session=session, clock=clock, reserved_slugs=settings.reserved_org_slugs
    )


def get_invitation_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    github_client: Annotated[GithubUserClient, Depends(get_github_user_client)],
) -> InvitationService:
    return InvitationService(
        session=session,
        clock=clock,
        github_client=github_client,
        invitation_ttl_seconds=settings.invitation_ttl_seconds,
    )


def get_agent_token_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> AgentTokenService:
    return AgentTokenService(
        session=session,
        clock=clock,
        default_days=settings.agent_token_default_days,
        max_days=settings.agent_token_max_days,
    )


def get_migration_plan_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> MigrationPlanService:
    return MigrationPlanService(
        session=session,
        clock=clock,
        executor_mode=settings.executor_mode,
        observe_seconds_default=settings.migration_observe_seconds_default,
        step_overhead_seconds=settings.migration_step_overhead_seconds,
        approval_ttl_seconds=settings.migration_plan_approval_ttl_seconds,
    )
