"""실제 실행기가 external_ref 만 받은 호출(poll)에서 환경 접속 정보를 DB 에서 다시 읽는다.

실행기 패키지는 app.models 를 import 하지 않으므로 이 클래스가 Environment 행을 값 객체로 바꿔
넘긴다.
지운 환경도 읽는다(내리기 중인 연결의 환경). 짧은 읽기 트랜잭션을 따로 연다.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.executors.base import ClusterEnvironment
from app.models import Environment
from app.services.deploy_support import read_environment_config


class DbEnvironmentLookup:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def find_environment(self, environment_id: uuid.UUID) -> ClusterEnvironment | None:
        async with self._session_factory() as session, session.begin():
            environment = await session.get(Environment, environment_id)
            if environment is None:
                return None
            return ClusterEnvironment(
                environment_id=environment.id,
                kind=environment.kind,
                name=environment.name,
                config=read_environment_config(environment),
                secret_ref=environment.secret_ref,
            )
