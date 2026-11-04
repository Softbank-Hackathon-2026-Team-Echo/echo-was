import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.actions import AuditAction
from app.enums import ActorType, AuditResult
from app.models import AgentToken, AuditLog, User


@dataclass(frozen=True)
class AuditLogFilter:
    """목록 조건. None 은 조건 없음. created_at 은 from_at 이상, to_at 미만."""

    actor_type: ActorType | None = None
    result: AuditResult | None = None
    action: str | None = None
    target_type: str | None = None
    target_id: uuid.UUID | None = None
    from_at: datetime | None = None
    to_at: datetime | None = None
    query: str | None = None
    include_reads: bool = True


class AuditLogRepository:
    """감사 줄 저장·조회. 줄은 지우지도 고치지도 않는다(DB 트리거가 막는다)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, audit_log: AuditLog) -> None:
        self._session.add(audit_log)

    async def search(
        self, organization_id: uuid.UUID, filter_: AuditLogFilter, offset: int, limit: int
    ) -> list[tuple[AuditLog, str | None, str | None]]:
        """(줄, 사람 GitHub 아이디, 토큰 이름) 최신순. 같은 시각이면 seq 로 나중 줄이 먼저다."""
        statement = _joined(select(AuditLog, User.github_login, AgentToken.name)).where(
            *_conditions(organization_id, filter_)
        )
        rows = await self._session.execute(
            statement.order_by(AuditLog.created_at.desc(), AuditLog.seq.desc())
            .offset(offset)
            .limit(limit)
        )
        return [(row[0], row[1], row[2]) for row in rows]

    async def count(self, organization_id: uuid.UUID, filter_: AuditLogFilter) -> int:
        statement = _joined(select(func.count()).select_from(AuditLog)).where(
            *_conditions(organization_id, filter_)
        )
        return await self._session.scalar(statement) or 0


def _joined[*Columns](statement: Select[*Columns]) -> Select[*Columns]:
    """폐기된 토큰도 이름을 보여 준다(폐기 여부로 거르지 않는다)."""
    return statement.outerjoin(User, User.id == AuditLog.actor_user_id).outerjoin(
        AgentToken, AgentToken.id == AuditLog.actor_agent_token_id
    )


def _conditions(organization_id: uuid.UUID, filter_: AuditLogFilter) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [AuditLog.organization_id == organization_id]
    if filter_.actor_type is not None:
        conditions.append(AuditLog.actor_type == filter_.actor_type)
    if filter_.result is not None:
        conditions.append(AuditLog.result == filter_.result)
    if filter_.action is not None:
        conditions.append(AuditLog.action == filter_.action)
    if filter_.target_type is not None:
        conditions.append(AuditLog.target_type == filter_.target_type)
    if filter_.target_id is not None:
        conditions.append(AuditLog.target_id == filter_.target_id)
    if filter_.from_at is not None:
        conditions.append(AuditLog.created_at >= filter_.from_at)
    if filter_.to_at is not None:
        conditions.append(AuditLog.created_at < filter_.to_at)
    if not filter_.include_reads:
        conditions.append(AuditLog.action != AuditAction.RESOURCE_READ)
    if filter_.query is not None:
        conditions.append(
            or_(
                AuditLog.action.icontains(filter_.query, autoescape=True),
                AuditLog.target_label.icontains(filter_.query, autoescape=True),
                User.github_login.icontains(filter_.query, autoescape=True),
                AgentToken.name.icontains(filter_.query, autoescape=True),
            )
        )
    return conditions
