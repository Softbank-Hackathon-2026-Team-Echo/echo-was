"""이전 계획 상세의 컷오버 결과(API 서버 16): 읽는 조건·찾는 창·프로세스 캐시.

행에는 서비스·계획 id 가 없어 시간 창으로 찾는다. 창은 [그 계획 finished_at, 같은 서비스의 다음 계획
created_at)이고(다음 계획이 없으면 끝이 없다), 복귀(FAILBACK) 계획은 ROLLBACK, 그 밖은 CUTOVER 행을
본다. 고객 DB 는 트랜잭션 밖에서 읽고 저장하지 않는다. 읽지 못해도 예외를 올리지 않는다(상세는 200).
"""

import asyncio
import logging
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import State

from app.core.clock import Clock
from app.enums import CutoverKind, MigrationPlanMode
from app.executors.base import (
    CutoverRun,
    CutoverRunReader,
    CutoverRunTarget,
    CutoverRunUnavailable,
)
from app.models import Environment, MigrationPlan
from app.repositories.migration_plan_repository import MigrationPlanRepository
from app.services.migration_plan_service import MigrationPlanView

logger = logging.getLogger(__name__)

CUTOVER_RESULT_CACHE_MAX_ENTRIES = 512


@dataclass(frozen=True)
class CachedCutoverRun:
    run: CutoverRun | None
    # None 이면 캐시에서 밀려날 때까지 쓴다.
    expires_at: datetime | None


@dataclass
class _PlanLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    holders: int = 0


class CutoverResultCache:
    """계획 id → 읽은 결과. 프로세스 메모리에 두고, 가득 차면 먼저 넣은 것부터 버린다."""

    def __init__(self, max_entries: int = CUTOVER_RESULT_CACHE_MAX_ENTRIES) -> None:
        self._max_entries = max_entries
        self._entries: OrderedDict[uuid.UUID, CachedCutoverRun] = OrderedDict()
        self._locks: dict[uuid.UUID, _PlanLock] = {}

    def find(self, plan_id: uuid.UUID, now: datetime) -> CachedCutoverRun | None:
        entry = self._entries.get(plan_id)
        if entry is None:
            return None
        if entry.expires_at is not None and entry.expires_at <= now:
            del self._entries[plan_id]
            return None
        return entry

    def store(
        self, plan_id: uuid.UUID, run: CutoverRun | None, expires_at: datetime | None
    ) -> None:
        self._entries.pop(plan_id, None)
        self._entries[plan_id] = CachedCutoverRun(run, expires_at)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    @asynccontextmanager
    async def reading(self, plan_id: uuid.UUID) -> AsyncIterator[None]:
        """같은 계획을 동시에 읽지 않게 줄 세운다. 기다리는 쪽이 없으면 잠금을 치운다."""
        plan_lock = self._locks.setdefault(plan_id, _PlanLock())
        plan_lock.holders += 1
        try:
            async with plan_lock.lock:
                yield
        finally:
            plan_lock.holders -= 1
            if plan_lock.holders == 0:
                del self._locks[plan_id]


def cutover_result_cache(state: State) -> CutoverResultCache:
    """앱 state 의 캐시(없으면 만든다)."""
    cache = getattr(state, "cutover_result_cache", None)
    if not isinstance(cache, CutoverResultCache):
        cache = CutoverResultCache()
        state.cutover_result_cache = cache
    return cache


def cutover_kind_for(mode: MigrationPlanMode) -> CutoverKind:
    return CutoverKind.ROLLBACK if mode == MigrationPlanMode.FAILBACK else CutoverKind.CUTOVER


class MigrationPlanResultService:
    def __init__(
        self,
        session: AsyncSession,
        reader: CutoverRunReader,
        cache: CutoverResultCache,
        clock: Clock,
        *,
        is_enabled: bool,
        not_before: datetime | None,
        cache_seconds: int,
    ) -> None:
        self._session = session
        self._plans = MigrationPlanRepository(session)
        self._reader = reader
        self._cache = cache
        self._clock = clock
        self._is_enabled = is_enabled
        self._not_before = not_before
        self._cache_ttl = timedelta(seconds=cache_seconds)

    async def find_cutover_run(self, view: MigrationPlanView) -> CutoverRun | None:
        """그 계획의 컷오버 결과. 읽는 조건 밖이거나 행이 없거나 읽지 못하면 None."""
        plan = view.plan
        if not self._should_read(plan):
            return None
        cached = self._cache.find(plan.id, self._clock.now())
        if cached is not None:
            return cached.run
        async with self._cache.reading(plan.id):
            cached = self._cache.find(plan.id, self._clock.now())
            if cached is not None:
                return cached.run
            return await self._read(view)

    def _should_read(self, plan: MigrationPlan) -> bool:
        # finished_at 은 끝 상태일 때만 있다(CHECK). simulated 계획은 실제 이전이 아니다.
        if not self._is_enabled or plan.finished_at is None or plan.simulated:
            return False
        return self._not_before is None or plan.finished_at >= self._not_before

    async def _read(self, view: MigrationPlanView) -> CutoverRun | None:
        plan = view.plan
        assert plan.finished_at is not None
        async with self._session.begin():
            until = await self._plans.find_next_created_at(plan.service_id, plan.created_at)
            environment = await self._session.get_one(Environment, view.target_environment_id)
            environment_kind = environment.kind
        result = await self._reader.find_first(
            CutoverRunTarget(view.target_environment_id, environment_kind),
            cutover_kind_for(plan.mode),
            since=plan.finished_at,
            until=until,
        )
        if isinstance(result, CutoverRunUnavailable):
            logger.warning(
                "cutover result unavailable",
                extra={
                    "action": "find_cutover_run",
                    "plan_id": str(plan.id),
                    "target_environment_id": str(view.target_environment_id),
                    "error_code": result.code,
                },
            )
        run = result if isinstance(result, CutoverRun) else None
        # 찾았고 창이 닫혔으면 답이 바뀌지 않는다. 그 밖은 곧 행이 생기거나 나아질 수 있다.
        is_final = run is not None and until is not None
        expires_at = None if is_final else self._clock.now() + self._cache_ttl
        self._cache.store(plan.id, run, expires_at)
        return run
