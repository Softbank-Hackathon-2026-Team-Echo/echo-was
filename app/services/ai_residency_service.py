"""조직의 AI 데이터 레지던시 요약 조회. 계산은 ai_residency(순수 함수)가 한다."""

import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.enums import AgentRunKind, AgentRunStatus
from app.models import AgentRun
from app.repositories.agent_run_repository import AgentRunRepository
from app.services.ai_residency import (
    DEFAULT_RESIDENCY_POLICY,
    ResidencyRun,
    ResidencySummary,
    summarize_residency,
)

RESIDENCY_RUN_LIMIT = 5_000


def _to_residency_run(run: AgentRun) -> ResidencyRun:
    # 조회 조건이 끝 상태만 고르므로 finished_at 이 있다(CHECK finished_consistency).
    assert run.finished_at is not None
    return ResidencyRun(
        run_id=run.id,
        service_id=run.service_id,
        kind=AgentRunKind(run.kind),
        status=AgentRunStatus(run.status),
        finished_at=run.finished_at,
        simulated=run.simulated,
        is_ai_reviewed=run.model_id is not None,
        processing=run.processing,
    )


class AiResidencyService:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock
        self._runs = AgentRunRepository(session)

    async def get_summary(self, organization_id: uuid.UUID, days: int) -> ResidencySummary:
        """[지금 − days, 지금) 안에 끝난 실행의 요약. 최신 5,000건까지 읽고 넘으면 truncated 다."""
        to_at = self._clock.now()
        from_at = to_at - timedelta(days=days)
        async with self._session.begin():
            rows = await self._runs.search_finished_for_residency(
                organization_id, from_at, to_at, RESIDENCY_RUN_LIMIT + 1
            )
        return summarize_residency(
            [_to_residency_run(row) for row in rows[:RESIDENCY_RUN_LIMIT]],
            policy=DEFAULT_RESIDENCY_POLICY,
            from_at=from_at,
            to_at=to_at,
            is_truncated=len(rows) > RESIDENCY_RUN_LIMIT,
        )
