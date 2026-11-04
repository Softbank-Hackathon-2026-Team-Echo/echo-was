"""감사 로그 조회. 조직 조건은 필수이고, 쓰기는 AuditRecorder 가 각 작업의 트랜잭션에서 한다."""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog
from app.repositories.audit_log_repository import AuditLogFilter, AuditLogRepository


@dataclass(frozen=True)
class AuditLogView:
    audit_log: AuditLog
    github_login: str | None
    agent_token_name: str | None


@dataclass(frozen=True)
class AuditLogSearchResult:
    items: list[AuditLogView]
    total: int


class AuditLogService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit_logs = AuditLogRepository(session)

    async def search_audit_logs(
        self, organization_id: uuid.UUID, filter_: AuditLogFilter, page: int, size: int
    ) -> AuditLogSearchResult:
        async with self._session.begin():
            rows = await self._audit_logs.search(
                organization_id, filter_, offset=page * size, limit=size
            )
            total = await self._audit_logs.count(organization_id, filter_)
        return AuditLogSearchResult(
            items=[AuditLogView(row[0], row[1], row[2]) for row in rows], total=total
        )
