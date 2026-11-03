"""DB 통합 테스트 공용 픽스처.

- TEST_DATABASE_URL(환경변수 또는 .env)이 가리키는 서버를 쓴다. DB 이름은 `_test` 로 끝나야 하고,
  실제로는 작업 폴더마다 다른 이름(`echo_test` → `echo_<폴더 경로 해시 8자>_test`)을 쓴다.
  같은 기기의 다른 작업 폴더(다른 세션·worktree)가 한 DB 잠금에서 서로 기다리지 않게 하기 위해서다.
- 세션 시작 때 DB 가 없으면 만들고, public 스키마를 비운 뒤 `alembic upgrade head` 를 실행한다.
- DB 에 최대 10초 재시도해도 붙지 못하면 이유를 남기고 이 폴더의 테스트를 모두 건너뛴다.
- 테스트마다 모든 표를 TRUNCATE 한다(배포·배포 요청 삭제 금지 트리거는 행 DELETE 에만 걸려
  TRUNCATE 와 무관).
- 같은 테스트 DB 를 쓰는 pytest 가 동시에 돌면(예: git add·commit 훅이 함께 실행) 서로 표를
  지우므로, 세션 동안 PostgreSQL advisory lock 을 잡아 한 번에 하나만 돌게 한다.
"""

import asyncio
import threading
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from pathlib import Path
from time import monotonic
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL, Connection, make_url, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.clients.source_repository import FakeSourceRepositoryClient
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.dependencies import (
    get_clock,
    get_executor_set,
    get_github_user_client,
    get_host_suffix_generator,
    get_source_repository_client,
)
from app.enums import OrganizationRole
from app.main import app
from app.models import Organization
from tests.db_naming import checkout_database_name
from tests.github_user_fake import FakeGithubUserClient
from tests.integration.auth_support import (
    auth_settings,
    create_member,
    create_organization,
    make_client,
)
from tests.integration.deploy_harness import DeployHarness, FakeClock, make_fake_executors
from tests.integration.factories import DEFAULT_ORG_SLUG, add_organization
from tests.integration.migration_support import Migration, serving_and_standby

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONNECT_RETRY_SECONDS = 10.0
CONNECT_RETRY_INTERVAL_SECONDS = 0.5
TABLES = (
    "jobs",
    # --- 보안·거버넌스 2: 보안 증거 ---
    "findings",
    # --- API 서버 8: 이전 계획 ---
    "migration_plan_logs",
    "migration_plans",
    # --- API 서버 4: 배포 작업 로그 ---
    "deploy_request_logs",
    "deploy_requests",
    "builds",
    "deployments",
    "service_environments",
    "environments",
    "services",
    # --- API 서버 6: 로그인·조직 ---
    "audit_logs",
    "agent_tokens",
    "sessions",
    "invitations",
    "organization_members",
    "organizations",
    "users",
)


class _TestDatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    test_database_url: str | None = None


class DatabaseUnavailableError(Exception):
    pass


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    integration_dir = Path(__file__).parent
    for item in items:
        if integration_dir in item.path.parents:
            item.add_marker(pytest.mark.integration)


def alembic_config(connection: Connection) -> Config:
    """이미 연 연결로 Alembic 명령을 실행하는 설정. 앱 로깅 설정은 건드리지 않는다."""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["configure_logger"] = False
    config.attributes["connection"] = connection
    return config


def run_alembic(command_fn: Callable[[Config], None]) -> Callable[[Connection], None]:
    """`await connection.run_sync(run_alembic(...))` 형태로 Alembic 명령을 부른다."""

    def run(connection: Connection) -> None:
        command_fn(alembic_config(connection))

    return run


class _TestDatabaseSession:
    """테스트 세션 동안 백그라운드 이벤트 루프에서 관리 DB 연결과 advisory lock 을 잡고 있는다.

    pytest-asyncio 는 테스트마다 이벤트 루프를 새로 만들기 때문에, 세션 내내 살아 있어야 하는 연결은
    별도 스레드의 루프에 둔다.
    """

    def __init__(self, url: URL) -> None:
        self._url = url
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._admin_engine = create_async_engine(
            url.set(database="postgres"), poolclass=NullPool, isolation_level="AUTOCOMMIT"
        )
        self._lock_connection: AsyncConnection | None = None

    def _run[T](self, coroutine: Coroutine[Any, Any, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result()

    def start(self) -> None:
        self._thread.start()
        self._run(self._start())

    def close(self) -> None:
        try:
            self._run(self._close())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()
            self._loop.close()

    async def _start(self) -> None:
        self._lock_connection = await self._connect_with_retry()
        await self._lock_connection.execute(
            text("SELECT pg_advisory_lock(hashtext(:key))"),
            {"key": f"echo-was-tests:{self._url.database}"},
        )
        exists = await self._lock_connection.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": self._url.database}
        )
        if not exists:
            await self._lock_connection.execute(text(f'CREATE DATABASE "{self._url.database}"'))
        await self._reset_schema_to_head()

    async def _connect_with_retry(self) -> AsyncConnection:
        deadline = monotonic() + CONNECT_RETRY_SECONDS
        while True:
            try:
                return await self._admin_engine.connect()
            except (OSError, SQLAlchemyError) as exc:
                if monotonic() >= deadline:
                    raise DatabaseUnavailableError(type(exc).__name__) from None
                await asyncio.sleep(CONNECT_RETRY_INTERVAL_SECONDS)

    async def _reset_schema_to_head(self) -> None:
        engine = create_async_engine(self._url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(text("DROP SCHEMA public CASCADE"))
                await connection.execute(text("CREATE SCHEMA public"))
                await connection.run_sync(
                    run_alembic(lambda config: command.upgrade(config, "head"))
                )
        finally:
            await engine.dispose()

    async def _close(self) -> None:
        if self._lock_connection is not None:
            # 연결을 닫으면 advisory lock 도 풀린다.
            await self._lock_connection.close()
        await self._admin_engine.dispose()


@pytest.fixture(scope="session")
def test_database_url() -> Iterator[str]:
    raw_url = _TestDatabaseSettings().test_database_url
    if not raw_url:
        pytest.skip("TEST_DATABASE_URL 이 없어 DB 통합 테스트를 건너뜀 (.env.example 참고)")
    url = make_url(raw_url)
    if not (url.database or "").endswith("_test"):
        pytest.fail("TEST_DATABASE_URL 의 DB 이름은 _test 로 끝나야 한다 (스키마를 지운다)")
    url = url.set(database=checkout_database_name(url.database or ""))
    raw_url = url.render_as_string(hide_password=False)

    database_session = _TestDatabaseSession(url)
    try:
        database_session.start()
    except DatabaseUnavailableError as exc:
        database_session.close()
        pytest.skip(
            f"DB 통합 테스트를 건너뜀: {url.render_as_string(hide_password=True)} 에 "
            f"{CONNECT_RETRY_SECONDS:.0f}초 동안 붙지 못함 ({exc}). "
            "`docker compose up -d --wait db` 로 DB 를 띄운다"
        )
    try:
        yield raw_url
    finally:
        database_session.close()


@pytest.fixture
async def engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {', '.join(TABLES)}"))
        await engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def organization(session_factory: async_sessionmaker[AsyncSession]) -> Organization:
    """DB 에 직접 넣는 시험용 team-echo 조직(만든 사람 행 포함, 멤버 행 없음)."""
    async with session_factory() as session, session.begin():
        return await add_organization(session)


@pytest.fixture
async def harness(engine: AsyncEngine, test_database_url: str) -> AsyncIterator[DeployHarness]:
    """원터치 배포 시험 도구. API 의 세션·설정·시계·소스 클라이언트를 시험 값으로 바꾼다.

    team-echo 조직과 그 ADMIN 멤버를 만들고, harness.client 는 그 멤버의 세션 쿠키와 허용
    Origin 을 싣는다. 경로는 harness.services 등 조직 경로를 쓴다.

    일꾼은 한 단계마다 트랜잭션을 여러 번 연다. 연결을 다시 쓰도록 이 시험 전용 풀 엔진을 쓰고
    시험이 끝나면 닫는다(표 비우기는 engine 픽스처가 한다).
    """
    pooled_engine = create_async_engine(test_database_url, pool_size=10, max_overflow=10)
    session_factory = async_sessionmaker(pooled_engine, expire_on_commit=False)
    clock = FakeClock()
    settings = auth_settings(get_settings(), fake_executor_step_seconds=0.0)
    source_client = FakeSourceRepositoryClient(install_url=settings.github_app_install_url)
    team = await create_organization(session_factory, DEFAULT_ORG_SLUG)
    admin = await create_member(session_factory, team, OrganizationRole.ADMIN, clock.now())
    github_users = FakeGithubUserClient()
    async with make_client(session_token=admin.session_token) as client:
        harness = DeployHarness(
            client=client,
            session_factory=session_factory,
            clock=clock,
            settings=settings,
            source_client=source_client,
            executors=make_fake_executors(clock),
            organization=team,
            admin=admin,
            github_users=github_users,
        )
        app.dependency_overrides[get_session_factory] = lambda: session_factory
        app.dependency_overrides[get_settings] = lambda: harness.settings
        app.dependency_overrides[get_clock] = lambda: clock
        app.dependency_overrides[get_source_repository_client] = lambda: harness.source_client
        app.dependency_overrides[get_github_user_client] = lambda: harness.github_users
        app.dependency_overrides[get_host_suffix_generator] = lambda: harness.host_suffixes
        # 로그 API 가 시험이 바꿔 끼운 가짜 실행기(토큰 주입 훅 포함)를 쓰게 한다(요청마다 읽는다).
        app.dependency_overrides[get_executor_set] = lambda: harness.executors.as_set()
        try:
            yield harness
        finally:
            app.dependency_overrides.clear()
            await pooled_engine.dispose()


@pytest.fixture
async def migration(harness: DeployHarness) -> AsyncIterator[Migration]:
    """aws-seoul 서비스 중(100) + gcp-seoul 예비(같은 digest), 개발자 A(만든 사람)·B(승인)."""
    setup = await serving_and_standby(harness)
    try:
        yield setup
    finally:
        await setup.author_client.aclose()
        await setup.approver_client.aclose()
