"""CVE 주기 감시 루프(SPEC sec-6-cve-watch). `CVE_WATCH_ENABLED=true` 인 일꾼 프로세스가 돌린다.

`CVE_WATCH_INTERVAL_SECONDS`(기본 21600 = 6시간)마다 현재 배포가 있는 조직을 모두 보고, 조직마다
`CveWatchService.check`(실제 피드, SYSTEM 감사)를 부른다.

- 스위치가 꺼져 있으면 `run_once` 는 아무것도 하지 않는다(0).
- 한 조직의 실패(피드 장애·DB 오류)는 로그만 남기고 다음 조직으로 간다. 다음 주기에 다시 한다.
- 일꾼이 여럿이어도 같은 조직 기록은 조직 잠금 + 중복 제거로 한 번만 남는다.
"""

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.osv import AdvisoryFeed
from app.core.clock import Clock
from app.core.config import Settings
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.services.cve_watch_service import CveWatchService

logger = logging.getLogger(__name__)


class CveWatchLoop:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        feed: AdvisoryFeed,
        clock: Clock,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._feed = feed
        self._clock = clock
        self._enabled = settings.cve_watch_enabled
        self._interval = settings.cve_watch_interval_seconds

    async def run_once(self) -> int:
        """한 번 감시한다. 새로 남긴 증거 수."""
        if not self._enabled:
            return 0
        async with self._session_factory() as session, session.begin():
            connections = await ServiceEnvironmentRepository(session).search_live_current()
            organization_ids = sorted({c.service.organization_id for c in connections})
        created = 0
        for organization_id in organization_ids:
            try:
                async with self._session_factory() as session:
                    view = await CveWatchService(session, self._clock).check(
                        organization_id, self._feed, actor_user_id=None
                    )
                created += view.created_count
            except Exception as exc:
                logger.warning(
                    "cve watch failed for organization",
                    extra={
                        "action": "check_cve_watch",
                        "organization_id": str(organization_id),
                        "error_type": type(exc).__name__,
                    },
                )
        return created

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("cve watch crashed", extra={"action": "run_worker"})
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass
