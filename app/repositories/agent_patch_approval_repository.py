import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentPatchApproval


class AgentPatchApprovalRepository:
    """AI 패치 결정 행 조회·추가. 행은 패치마다 하나이고 고치거나 지우지 않는다."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, approval: AgentPatchApproval) -> None:
        self._session.add(approval)

    async def find_by_run_and_patch(
        self, run_id: uuid.UUID, patch_id: str
    ) -> AgentPatchApproval | None:
        return await self._session.scalar(
            select(AgentPatchApproval)
            .where(AgentPatchApproval.run_id == run_id, AgentPatchApproval.patch_id == patch_id)
            .execution_options(populate_existing=True)
        )

    async def search_by_run(self, run_id: uuid.UUID) -> list[AgentPatchApproval]:
        statement = (
            select(AgentPatchApproval)
            .where(AgentPatchApproval.run_id == run_id)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())
