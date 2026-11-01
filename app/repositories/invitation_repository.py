import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import InvitationStatus
from app.models import Invitation
from app.repositories.conditional_update import update_where


class InvitationRepository:
    """초대 조회·저장. 상태는 PENDING 일 때만 바뀌는 조건부 UPDATE 로 바꾼다."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, invitation: Invitation) -> None:
        self._session.add(invitation)

    async def find_in_organization(
        self, organization_id: uuid.UUID, invitation_id: uuid.UUID
    ) -> Invitation | None:
        return await self._session.scalar(
            select(Invitation)
            .where(Invitation.id == invitation_id, Invitation.organization_id == organization_id)
            .execution_options(populate_existing=True)
        )

    async def search_pending_by_github_id(self, github_id: int) -> list[Invitation]:
        statement = (
            select(Invitation)
            .where(
                Invitation.github_id == github_id,
                Invitation.status == InvitationStatus.PENDING,
            )
            .order_by(Invitation.created_at, Invitation.id)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())

    async def close_if_pending(self, invitation_id: uuid.UUID, values: dict[str, Any]) -> bool:
        """PENDING 일 때만 values(status 등)를 적는다. 적었으면 True."""
        return await update_where(
            self._session,
            Invitation,
            invitation_id,
            [Invitation.status == InvitationStatus.PENDING],
            values,
        )

    async def revoke_pending(
        self, organization_id: uuid.UUID, github_id: int, now: datetime
    ) -> int:
        statement = (
            update(Invitation)
            .where(
                Invitation.organization_id == organization_id,
                Invitation.github_id == github_id,
                Invitation.status == InvitationStatus.PENDING,
            )
            .values(status=InvitationStatus.REVOKED, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        result = cast(CursorResult[Any], await self._session.execute(statement))
        return result.rowcount

    async def expire_overdue(self, organization_id: uuid.UUID, now: datetime) -> int:
        """기한이 지난 PENDING 을 EXPIRED 로 닫는다."""
        statement = (
            update(Invitation)
            .where(
                Invitation.organization_id == organization_id,
                Invitation.status == InvitationStatus.PENDING,
                Invitation.expires_at <= now,
            )
            .values(status=InvitationStatus.EXPIRED, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        result = cast(CursorResult[Any], await self._session.execute(statement))
        return result.rowcount

    async def search_by_organization(
        self, organization_id: uuid.UUID, offset: int, limit: int
    ) -> list[Invitation]:
        """최신순."""
        statement = (
            select(Invitation)
            .where(Invitation.organization_id == organization_id)
            .order_by(Invitation.created_at.desc(), Invitation.id.desc())
            .offset(offset)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.scalars(statement)).all())

    async def count_by_organization(self, organization_id: uuid.UUID) -> int:
        statement = (
            select(func.count())
            .select_from(Invitation)
            .where(Invitation.organization_id == organization_id)
        )
        return await self._session.scalar(statement) or 0
