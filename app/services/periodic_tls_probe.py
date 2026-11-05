"""공개 경로 TLS 주기 시험 루프(C-TLS-1 준비 증거). 보안 게이트가 켜진 일꾼 프로세스가 돌린다.

`SECURITY_TLS_PROBE_INTERVAL_SECONDS`(기본 1800)마다 현재 배포가 있는 살아 있는 연결을 모두 보고,
그 연결의 대표 주소·환경별 주소를 이전 게이트와 같은 시험기(`PublicHostTlsChecker`)로 시험해
서비스 + 환경 범위의 C-TLS-1 증거(`sourceRef = tls-probe:periodic`)로 남긴다.

- 이전 게이트는 실행 직전 증거를 따로 만들므로(방금 만든 증거만 쓴다) 이 루프가 게이트 판정을
  바꾸지 않는다. 매트릭스(준비 시점, 유효기간 1시간)가 볼 증거를 끊기지 않게 하는 것이 목적이다.
- 주기는 C-TLS-1 유효기간(1시간)보다 짧아야 증거가 비지 않는다(설정이 50분 이하로 막는다).
- 시험 결과가 나쁘면(FAIL·UNKNOWN) 그대로 남긴다. 좋게 바꾸지 않는다.
- 실패는 로그만 남기고 다음 주기에 다시 한다. 일꾼이 여럿이면 증거가 중복으로 쌓일 뿐 판정은 같다.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.tls_probe import PublicHostTlsChecker
from app.core.clock import Clock
from app.core.config import Settings
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.services.security_finding_service import SecurityFindingService
from app.services.security_normalizers import NormalizedFinding, normalize_tls_probes
from app.services.security_rules import SecurityScope

logger = logging.getLogger(__name__)

PERIODIC_TLS_SOURCE_REF = "tls-probe:periodic"


@dataclass(frozen=True)
class _ProbeTarget:
    organization_id: uuid.UUID
    service_id: uuid.UUID
    environment_id: uuid.UUID
    hosts: tuple[str, ...]


class PeriodicTlsProbe:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tls_checker: PublicHostTlsChecker,
        clock: Clock,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._tls_checker = tls_checker
        self._clock = clock
        self._interval = settings.security_tls_probe_interval_seconds

    async def run_once(self) -> int:
        """한 번 시험한다. 남긴 증거 수(연결마다 하나)."""
        async with self._session_factory() as session, session.begin():
            connections = await ServiceEnvironmentRepository(session).search_live_current()
            targets = [
                _ProbeTarget(
                    organization_id=connection.service.organization_id,
                    service_id=connection.service_id,
                    environment_id=connection.environment_id,
                    # 이전 게이트와 같은 주소: 대표 주소 + 그 환경 주소.
                    hosts=tuple(
                        host
                        for host in (connection.service.public_host, connection.public_host)
                        if host
                    ),
                )
                for connection in connections
            ]
        if not targets:
            return 0
        findings = await asyncio.gather(*(self._probe(target) for target in targets))
        async with self._session_factory() as session:
            await SecurityFindingService(session).record_all(
                list(findings), source_ref=PERIODIC_TLS_SOURCE_REF
            )
        logger.info(
            "periodic tls probe recorded",
            extra={"action": "probe_public_tls", "finding_count": len(findings)},
        )
        return len(findings)

    async def _probe(self, target: _ProbeTarget) -> NormalizedFinding:
        probes = await self._tls_checker.check_hosts(target.hosts)
        return normalize_tls_probes(
            probes,
            scope=SecurityScope(
                target.organization_id,
                service_id=target.service_id,
                environment_id=target.environment_id,
            ),
            collected_at=self._clock.now(),
        )

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                # 시험 실패로 일꾼이 멈추지 않게 한다. 다음 주기에 다시 한다.
                logger.exception("periodic tls probe crashed", extra={"action": "run_worker"})
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass
