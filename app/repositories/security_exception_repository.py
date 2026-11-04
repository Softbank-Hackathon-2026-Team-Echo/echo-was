import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import SecurityControlId, SecurityExceptionStatus
from app.models import SecurityException
from app.repositories.conditional_update import update_where

# 판정 한 번에 읽는 예외 상한(만료가 늦은 것부터).
SECURITY_EXCEPTION_SEARCH_LIMIT = 200


@dataclass(frozen=True)
class SecurityExceptionConditions:
    """목록 조건. 비어 있는 칸은 거르지 않는다. 상태는 now 기준으로 정한다."""

    now: datetime
    control_ids: Sequence[SecurityControlId] = field(default_factory=tuple)
    service_id: uuid.UUID | None = None
    environment_id: uuid.UUID | None = None
    statuses: Sequence[SecurityExceptionStatus] = field(default_factory=tuple)


class SecurityExceptionRepository:
    """기한부 예외 저장·조회. 조직 조건은 늘 건다. 승인·철회는 조건부 UPDATE 로만 한다."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, exception: SecurityException) -> None:
        self._session.add(exception)

    async def find_in_organization(
        self, organization_id: uuid.UUID, exception_id: uuid.UUID
    ) -> SecurityException | None:
        statement = (
            select(SecurityException)
            .where(
                SecurityException.id == exception_id,
                SecurityException.organization_id == organization_id,
            )
            .execution_options(populate_existing=True)
        )
        return await self._session.scalar(statement)

    async def search_by_organization(
        self,
        organization_id: uuid.UUID,
        conditions: SecurityExceptionConditions,
        *,
        offset: int,
        limit: int,
    ) -> list[SecurityException]:
        statement = (
            select(SecurityException)
            .where(*_filters(organization_id, conditions))
            .order_by(SecurityException.created_at.desc(), SecurityException.id)
            .offset(offset)
            .limit(limit)
        )
        return list((await self._session.scalars(statement)).all())

    async def count_by_organization(
        self, organization_id: uuid.UUID, conditions: SecurityExceptionConditions
    ) -> int:
        statement = (
            select(func.count())
            .select_from(SecurityException)
            .where(*_filters(organization_id, conditions))
        )
        return int(await self._session.scalar(statement) or 0)

    async def approve(
        self, exception_id: uuid.UUID, approver_user_id: uuid.UUID, now: datetime
    ) -> bool:
        """승인 대기(승인 전·철회 전·만료 전)일 때만 승인한다. 0행이면 False."""
        return await update_where(
            self._session,
            SecurityException,
            exception_id,
            [_is_pending(now)],
            {"approved_by_user_id": approver_user_id, "approved_at": now, "updated_at": now},
        )

    async def revoke(
        self, exception_id: uuid.UUID, revoker_user_id: uuid.UUID, now: datetime
    ) -> bool:
        """철회 전·만료 전일 때만 철회한다(승인 대기여도 된다). 0행이면 False."""
        return await update_where(
            self._session,
            SecurityException,
            exception_id,
            [SecurityException.revoked_at.is_(None), SecurityException.expires_at > now],
            {"revoked_by_user_id": revoker_user_id, "revoked_at": now, "updated_at": now},
        )

    async def search_for_judgement(
        self,
        organization_id: uuid.UUID,
        expired_since: datetime,
        limit: int = SECURITY_EXCEPTION_SEARCH_LIMIT,
    ) -> list[SecurityException]:
        """판정에 넘길 예외: 승인됐고 철회되지 않았고 expired_since 뒤에 만료되는 것.

        이미 만료된 것도 expired_since 뒤면 읽는다(판정이 "예외 만료" 사유를 붙인다).
        철회한 예외는 읽지 않는다(그 뒤로 쓰지 않는다).
        """
        stmt = (
            select(SecurityException)
            .where(
                SecurityException.organization_id == organization_id,
                SecurityException.approved_by_user_id.is_not(None),
                SecurityException.revoked_at.is_(None),
                SecurityException.expires_at > expired_since,
            )
            .order_by(SecurityException.expires_at.desc(), SecurityException.id)
            .limit(limit)
        )
        return list((await self._session.scalars(stmt)).all())


def exception_status(exception: SecurityException, now: datetime) -> SecurityExceptionStatus:
    """저장된 칸과 지금 시각으로 상태를 정한다(`_status_condition` 과 같은 규칙)."""
    if exception.revoked_at is not None:
        return SecurityExceptionStatus.REVOKED
    if exception.expires_at <= now:
        return SecurityExceptionStatus.EXPIRED
    if exception.approved_by_user_id is not None:
        return SecurityExceptionStatus.ACTIVE
    return SecurityExceptionStatus.PENDING


def _is_pending(now: datetime) -> ColumnElement[bool]:
    return and_(
        SecurityException.approved_by_user_id.is_(None),
        SecurityException.revoked_at.is_(None),
        SecurityException.expires_at > now,
    )


def _status_condition(status: SecurityExceptionStatus, now: datetime) -> ColumnElement[bool]:
    not_revoked = SecurityException.revoked_at.is_(None)
    match status:
        case SecurityExceptionStatus.REVOKED:
            return SecurityException.revoked_at.is_not(None)
        case SecurityExceptionStatus.EXPIRED:
            return and_(not_revoked, SecurityException.expires_at <= now)
        case SecurityExceptionStatus.ACTIVE:
            return and_(
                not_revoked,
                SecurityException.expires_at > now,
                SecurityException.approved_by_user_id.is_not(None),
            )
        case SecurityExceptionStatus.PENDING:
            return _is_pending(now)


def _filters(
    organization_id: uuid.UUID, conditions: SecurityExceptionConditions
) -> list[ColumnElement[bool]]:
    filters: list[ColumnElement[bool]] = [SecurityException.organization_id == organization_id]
    if conditions.control_ids:
        filters.append(SecurityException.control_id.in_(list(conditions.control_ids)))
    if conditions.service_id is not None:
        filters.append(SecurityException.service_id == conditions.service_id)
    if conditions.environment_id is not None:
        filters.append(SecurityException.environment_id == conditions.environment_id)
    if conditions.statuses:
        filters.append(
            or_(*(_status_condition(status, conditions.now) for status in conditions.statuses))
        )
    return filters
