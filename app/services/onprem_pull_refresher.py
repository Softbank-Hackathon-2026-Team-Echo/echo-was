"""온프레 이미지 받기 자격(Secret echo-pull) 갱신 루프. 일꾼 프로세스가 실제 실행기 모드에서 돌린다.

`ONPREM_PULL_CHECK_SECONDS`(기본 300)마다 현재 배포가 있는 살아 있는 온프레 연결을 모두 보고,
Secret 어노테이션의 만료 시각까지 남은 시간이 `ONPREM_PULL_MIN_REMAINING_SECONDS`(기본 1800)
미만이면 새 토큰으로 다시 넣는다. 메타데이터 서버 토큰은 이미 일부 수명을 쓴 캐시 토큰일 수 있어
고정 주기로는 빈 구간이 생기기 때문이다. 실패는 경고 로그만 남긴다. 일꾼이 여럿이면 중복으로
돌아도 결과가 같다.
"""

import asyncio
import logging
from datetime import timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.executors.base import DeploymentSpec, DeployTarget
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.services.deploy_support import deploy_target, deployment_spec

logger = logging.getLogger(__name__)

ONPREM_KIND = "onprem"


class PullSecretRefresher(Protocol):
    async def refresh_pull_secret(
        self, target: DeployTarget, deployment: DeploymentSpec, min_remaining: timedelta
    ) -> bool: ...


class OnpremPullSecretRefresher:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        refresher: PullSecretRefresher,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._refresher = refresher
        self._interval = settings.onprem_pull_check_seconds
        self._min_remaining = timedelta(seconds=settings.onprem_pull_min_remaining_seconds)

    async def run_once(self) -> int:
        """한 번 확인한다. 다시 넣은 연결 수."""
        async with self._session_factory() as session, session.begin():
            connections = await ServiceEnvironmentRepository(
                session
            ).search_live_current_by_environment_kind(ONPREM_KIND)
            targets = [
                (
                    deploy_target(connection.service, connection, connection.environment),
                    deployment_spec(connection.current_deployment),
                )
                for connection in connections
                if connection.current_deployment is not None
            ]
        refreshed = 0
        for target, deployment in targets:
            if await self._refresher.refresh_pull_secret(target, deployment, self._min_remaining):
                refreshed += 1
                logger.info(
                    "onprem pull secret refreshed",
                    extra={
                        "action": "refresh_pull_secret",
                        "service_environment_id": str(target.service_environment_id),
                    },
                )
        return refreshed

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                # 갱신 실패로 일꾼이 멈추지 않게 한다. 다음 주기에 다시 한다.
                logger.exception("onprem pull secret check crashed", extra={"action": "run_worker"})
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass
