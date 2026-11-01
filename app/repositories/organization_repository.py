import uuid
from datetime import datetime

from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import OrganizationRole
from app.models import Organization, OrganizationMember


class OrganizationRepository:
    """조직 표 조회·저장. lock=True 는 `FOR NO KEY UPDATE`(멤버 변경을 줄 세우는 잠금)다.

    FOR NO KEY UPDATE 는 서비스·환경·멤버 행을 넣을 때 조직을 FK 로 확인하는 FOR KEY SHARE 와
    부딪히지 않는다.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, organization: Organization) -> None:
        self._session.add(organization)

    async def insert_if_slug_free(
        self,
        *,
        slug: str,
        name: str,
        personal: bool,
        created_by_user_id: uuid.UUID,
        now: datetime,
    ) -> uuid.UUID | None:
        """slug 가 비어 있으면 조직을 넣고 id 를, 이미 쓰이고 있으면 None 을 돌려준다."""
        statement = (
            insert(Organization)
            .values(
                id=uuid.uuid4(),
                slug=slug,
                name=name,
                personal=personal,
                created_by_user_id=created_by_user_id,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=[Organization.slug])
            .returning(Organization.id)
        )
        return await self._session.scalar(statement)

    async def find_by_id(
        self, organization_id: uuid.UUID, *, lock: bool = False
    ) -> Organization | None:
        statement = (
            select(Organization)
            .where(Organization.id == organization_id)
            .execution_options(populate_existing=True)
        )
        if lock:
            statement = statement.with_for_update(key_share=True)
        return await self._session.scalar(statement)

    async def find_by_slug(self, slug: str) -> Organization | None:
        return await self._session.scalar(
            select(Organization)
            .where(Organization.slug == slug)
            .execution_options(populate_existing=True)
        )

    async def exists(self, organization_id: uuid.UUID) -> bool:
        return bool(
            await self._session.scalar(select(exists().where(Organization.id == organization_id)))
        )

    async def search_by_member(
        self, user_id: uuid.UUID, offset: int, limit: int | None
    ) -> list[tuple[Organization, OrganizationRole]]:
        """사용자가 멤버인 조직과 그 역할. 개인 조직을 먼저, 그다음 만든 순서."""
        statement = (
            select(Organization, OrganizationMember.role)
            .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
            .where(OrganizationMember.user_id == user_id)
            .order_by(Organization.personal.desc(), Organization.created_at, Organization.id)
            .offset(offset)
            .limit(limit)
        )
        rows = await self._session.execute(statement)
        return [(organization, OrganizationRole(role)) for organization, role in rows.all()]

    async def count_by_member(self, user_id: uuid.UUID) -> int:
        statement = (
            select(func.count())
            .select_from(OrganizationMember)
            .where(OrganizationMember.user_id == user_id)
        )
        return await self._session.scalar(statement) or 0
