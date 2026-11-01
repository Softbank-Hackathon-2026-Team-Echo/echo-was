import uuid
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import OrganizationRole
from app.models import OrganizationMember, User


class OrganizationMemberRepository:
    """조직 멤버 조회·저장. 멤버 변경은 서비스가 조직 행을 잠근 뒤 한다."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, member: OrganizationMember) -> None:
        self._session.add(member)

    async def insert_if_absent(
        self,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        role: OrganizationRole,
        now: datetime,
    ) -> bool:
        """이미 멤버면 역할을 바꾸지 않고 False. `ON CONFLICT DO NOTHING` 이라 동시에 와도 한 줄."""
        statement = (
            insert(OrganizationMember)
            .values(
                id=uuid.uuid4(),
                organization_id=organization_id,
                user_id=user_id,
                role=role,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=[OrganizationMember.organization_id, OrganizationMember.user_id]
            )
            .returning(OrganizationMember.id)
        )
        return await self._session.scalar(statement) is not None

    async def find(
        self, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> OrganizationMember | None:
        return await self._session.scalar(
            select(OrganizationMember)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == user_id,
            )
            .execution_options(populate_existing=True)
        )

    async def delete(self, member: OrganizationMember) -> None:
        await self._session.execute(
            delete(OrganizationMember)
            .where(OrganizationMember.id == member.id)
            .execution_options(synchronize_session=False)
        )

    async def count_admins(self, organization_id: uuid.UUID) -> int:
        statement = (
            select(func.count())
            .select_from(OrganizationMember)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.role == OrganizationRole.ADMIN,
            )
        )
        return await self._session.scalar(statement) or 0

    async def search_with_users(
        self, organization_id: uuid.UUID, offset: int, limit: int
    ) -> list[tuple[OrganizationMember, User]]:
        """들어온 순서(created_at, user id)."""
        statement = (
            select(OrganizationMember, User)
            .join(User, User.id == OrganizationMember.user_id)
            .where(OrganizationMember.organization_id == organization_id)
            .order_by(OrganizationMember.created_at, OrganizationMember.user_id)
            .offset(offset)
            .limit(limit)
        )
        rows = (await self._session.execute(statement)).all()
        return [(member, user) for member, user in rows]

    async def count(self, organization_id: uuid.UUID) -> int:
        statement = (
            select(func.count())
            .select_from(OrganizationMember)
            .where(OrganizationMember.organization_id == organization_id)
        )
        return await self._session.scalar(statement) or 0
