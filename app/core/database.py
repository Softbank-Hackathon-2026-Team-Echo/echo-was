from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """요청마다 세션을 만드는 팩토리. 커밋 뒤에도 객체 값을 읽을 수 있게 만료하지 않는다."""
    return async_sessionmaker(get_engine(), expire_on_commit=False)
