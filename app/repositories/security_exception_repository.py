import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SecurityException

# 판정 한 번에 읽는 예외 상한(만료가 늦은 것부터).
SECURITY_EXCEPTION_SEARCH_LIMIT = 200


class SecurityExceptionRepository:
    """기한부 예외 조회. 조직 조건은 늘 건다."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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
