"""조직 만들기·내 목록, 멤버 목록·역할 변경·내보내기/탈퇴, 처음 로그인 때 개인 조직.

멤버 변경은 조직 행을 `FOR NO KEY UPDATE` 로 잠근 뒤 잠금 안에서 행위자의 멤버 행과 역할을 다시
읽는다. 그래서 두 어드민이 동시에 서로를 내리거나 내보내도 어드민은 1명 이상 남는다(진 쪽은 403
ROLE_FORBIDDEN 또는 409 LAST_ADMIN). 잠금 순서는 조직이 맨 앞이다.
"""

import logging
import re
import uuid
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.actions import AuditAction, AuditTargetType
from app.audit.recorder import AuditActor, AuditRecorder, AuditTarget
from app.core.clock import Clock
from app.core.exceptions import (
    CannotChangeOwnRoleError,
    LastAdminError,
    MemberNotFoundError,
    OrganizationNotFoundError,
    OrganizationSlugConflictError,
    OrganizationSlugReservedError,
    RoleForbiddenError,
)
from app.enums import OrganizationRole
from app.models import Organization, OrganizationMember, User
from app.models.organization import ORGANIZATION_SLUG_MAX_LENGTH, ORGANIZATION_SLUG_PATTERN
from app.repositories.organization_member_repository import OrganizationMemberRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.user_repository import UserRepository
from app.services.integrity import violated_constraint_name

logger = logging.getLogger(__name__)

SLUG_UNIQUE_CONSTRAINT = "uq_organizations_slug"
_SLUG_PATTERN = re.compile(ORGANIZATION_SLUG_PATTERN)
# 개인 조직 slug 를 `아이디-2` … `아이디-99` 까지 시도한 뒤 `user-{github 번호}` 로 넘어간다.
_MAX_NUMBERED_SUFFIX = 99


@dataclass(frozen=True)
class OrganizationView:
    organization: Organization
    role: OrganizationRole | None


@dataclass(frozen=True)
class OrganizationSearchResult:
    items: list[OrganizationView]
    total: int


@dataclass(frozen=True)
class MemberView:
    member: OrganizationMember
    user: User


@dataclass(frozen=True)
class MemberSearchResult:
    items: list[MemberView]
    total: int


def is_valid_slug(slug: str) -> bool:
    return len(slug) <= ORGANIZATION_SLUG_MAX_LENGTH and bool(_SLUG_PATTERN.fullmatch(slug))


def personal_slug_candidates(github_login: str, github_id: int) -> Iterator[str]:
    """개인 조직 slug 후보: 아이디 소문자, `-2`, `-3` …, 그다음 `user-{번호}` 와 그 번호 붙임.

    39자를 넘으면 앞을 줄이고 줄인 끝의 `-` 는 뗀다. 규칙에 맞지 않는 후보는 건너뛴다.
    """
    base = github_login.lower()
    if is_valid_slug(base):
        yield base
    for number in range(2, _MAX_NUMBERED_SUFFIX + 1):
        candidate = _with_suffix(base, f"-{number}")
        if is_valid_slug(candidate):
            yield candidate
    fallback = f"user-{github_id}"
    yield fallback
    for number in range(2, _MAX_NUMBERED_SUFFIX + 1):
        yield _with_suffix(fallback, f"-{number}")


def _with_suffix(base: str, suffix: str) -> str:
    return base[: ORGANIZATION_SLUG_MAX_LENGTH - len(suffix)].rstrip("-") + suffix


async def create_personal_organization(
    session: AsyncSession,
    *,
    user: User,
    reserved_slugs: Collection[str],
    clock: Clock,
) -> uuid.UUID:
    """처음 로그인한 사람의 개인 조직과 ADMIN 멤버를 만든다. 호출하는 쪽이 트랜잭션을 연다.

    slug 시도는 `ON CONFLICT DO NOTHING` 이라 겹쳐도 트랜잭션이 멈추지 않는다.
    """
    organizations = OrganizationRepository(session)
    now = clock.now()
    for slug in personal_slug_candidates(user.github_login, user.github_id):
        if slug in reserved_slugs:
            continue
        organization_id = await organizations.insert_if_slug_free(
            slug=slug,
            name=user.github_login,
            personal=True,
            created_by_user_id=user.id,
            now=now,
        )
        if organization_id is None:
            continue
        await OrganizationMemberRepository(session).insert_if_absent(
            organization_id=organization_id,
            user_id=user.id,
            role=OrganizationRole.ADMIN,
            now=now,
        )
        logger.info(
            "personal organization created",
            extra={
                "action": "create_personal_organization",
                "organization_id": str(organization_id),
                "user_id": str(user.id),
            },
        )
        return organization_id
    raise RuntimeError("no personal organization slug available")


@dataclass(frozen=True)
class LiveFeedThresholdChange:
    """실제 재난 피드 조직 기준 변경(API 서버 18). None 인 칸은 그대로 둔다."""

    min_magnitude: Decimal | None
    radius_km: int | None


class OrganizationService:
    def __init__(
        self, session: AsyncSession, clock: Clock, reserved_slugs: Collection[str]
    ) -> None:
        self._session = session
        self._clock = clock
        self._reserved_slugs = frozenset(reserved_slugs)
        self._organizations = OrganizationRepository(session)
        self._members = OrganizationMemberRepository(session)
        self._users = UserRepository(session)
        self._audit = AuditRecorder(session, clock)

    async def create_organization(self, user: User, slug: str, name: str) -> OrganizationView:
        """조직과 만든 사람의 ADMIN 멤버를 한 트랜잭션에 넣는다. 예약 slug 는 받지 않는다."""
        if slug in self._reserved_slugs:
            raise OrganizationSlugReservedError
        now = self._clock.now()
        organization = Organization(
            slug=slug,
            name=name,
            personal=False,
            created_by_user_id=user.id,
            created_at=now,
            updated_at=now,
        )
        async with self._session.begin():
            self._organizations.add(organization)
            try:
                await self._session.flush()
            except IntegrityError as exc:
                if violated_constraint_name(exc) == SLUG_UNIQUE_CONSTRAINT:
                    raise OrganizationSlugConflictError from None
                raise
            self._members.add(
                OrganizationMember(
                    organization_id=organization.id,
                    user_id=user.id,
                    role=OrganizationRole.ADMIN,
                    created_at=now,
                    updated_at=now,
                )
            )
            await self._session.flush()
            self._audit.record(
                organization_id=organization.id,
                actor=AuditActor.user(user.id),
                action=AuditAction.ORGANIZATION_CREATED,
                target=AuditTarget(AuditTargetType.ORGANIZATION, organization.id, name),
            )
        logger.info(
            "organization created",
            extra={
                "action": "create_organization",
                "organization_id": str(organization.id),
                "user_id": str(user.id),
            },
        )
        return OrganizationView(organization=organization, role=OrganizationRole.ADMIN)

    async def update_settings(
        self,
        organization_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        role: OrganizationRole | None,
        allow_self_approval: bool,
        live_feed: LiveFeedThresholdChange | None = None,
    ) -> OrganizationView:
        """조직 행을 `FOR NO KEY UPDATE` 로 잠그고 자기 승인 스위치(와 피드 기준)를 바꾼다.

        어드민 검사는 라우트의 검사기가 한다. 값이 그대로면 감사 줄을 쓰지 않는다.
        """
        now = self._clock.now()
        async with self._session.begin():
            organization = await self._organizations.find_by_id(organization_id, lock=True)
            if organization is None:
                raise OrganizationNotFoundError
            changes: dict[str, Any] = {}
            previous = organization.allow_self_approval
            if previous != allow_self_approval:
                changes["allowSelfApproval"] = {"from": previous, "to": allow_self_approval}
            organization.allow_self_approval = allow_self_approval
            if live_feed is not None and live_feed.min_magnitude is not None:
                before = organization.live_feed_min_magnitude
                if before != live_feed.min_magnitude:
                    changes["liveFeedMinMagnitude"] = {
                        "from": float(before),
                        "to": float(live_feed.min_magnitude),
                    }
                organization.live_feed_min_magnitude = live_feed.min_magnitude
            if live_feed is not None and live_feed.radius_km is not None:
                before_radius = organization.live_feed_radius_km
                if before_radius != live_feed.radius_km:
                    changes["liveFeedRadiusKm"] = {"from": before_radius, "to": live_feed.radius_km}
                organization.live_feed_radius_km = live_feed.radius_km
            organization.updated_at = now
            await self._session.flush()
            if changes:
                self._audit.record(
                    organization_id=organization_id,
                    actor=AuditActor.user(actor_user_id),
                    action=AuditAction.ORGANIZATION_SETTINGS_CHANGED,
                    target=AuditTarget(
                        AuditTargetType.ORGANIZATION, organization_id, organization.name
                    ),
                    details=changes,
                )
        logger.info(
            "organization settings changed",
            extra={
                "action": "update_organization_settings",
                "organization_id": str(organization_id),
                "allow_self_approval": allow_self_approval,
            },
        )
        return OrganizationView(organization=organization, role=role)

    async def search_my_organizations(
        self, user_id: uuid.UUID, page: int, size: int
    ) -> OrganizationSearchResult:
        async with self._session.begin():
            rows = await self._organizations.search_by_member(
                user_id, offset=page * size, limit=size
            )
            total = await self._organizations.count_by_member(user_id)
        return OrganizationSearchResult(
            items=[OrganizationView(organization=org, role=role) for org, role in rows],
            total=total,
        )

    async def search_members(
        self, organization_id: uuid.UUID, page: int, size: int
    ) -> MemberSearchResult:
        async with self._session.begin():
            rows = await self._members.search_with_users(
                organization_id, offset=page * size, limit=size
            )
            total = await self._members.count(organization_id)
        return MemberSearchResult(
            items=[MemberView(member=member, user=user) for member, user in rows], total=total
        )

    async def update_member_role(
        self,
        organization_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        target_user_id: uuid.UUID,
        role: OrganizationRole,
    ) -> MemberView:
        """어드민이 다른 멤버의 역할을 바꾼다. 자기 역할은 못 바꾸고, 어드민은 1명 이상 남긴다."""
        if target_user_id == actor_user_id:
            raise CannotChangeOwnRoleError
        now = self._clock.now()
        async with self._session.begin():
            actor = await self._lock_and_read_actor(organization_id, actor_user_id)
            if actor.role != OrganizationRole.ADMIN:
                raise RoleForbiddenError
            member = await self._get_member(organization_id, target_user_id)
            if (
                member.role == OrganizationRole.ADMIN
                and role != OrganizationRole.ADMIN
                and await self._members.count_admins(organization_id) <= 1
            ):
                raise LastAdminError
            previous = member.role
            member.role = role
            member.updated_at = now
            await self._session.flush()
            user = await self._users.find_by_id(target_user_id)
            assert user is not None
            if previous != role:
                self._audit.record(
                    organization_id=organization_id,
                    actor=AuditActor.user(actor_user_id),
                    action=AuditAction.MEMBER_ROLE_CHANGED,
                    target=AuditTarget(AuditTargetType.USER, user.id, user.github_login),
                    details={"role": {"from": str(previous), "to": str(role)}},
                )
        logger.info(
            "organization member role changed",
            extra={
                "action": "update_member_role",
                "organization_id": str(organization_id),
                "user_id": str(target_user_id),
                "role": role,
            },
        )
        return MemberView(member=member, user=user)

    async def remove_member(
        self, organization_id: uuid.UUID, actor_user_id: uuid.UUID, target_user_id: uuid.UUID
    ) -> None:
        """어드민은 다른 멤버를 내보내고, 누구나 자기 자신은 탈퇴한다. 어드민은 1명 이상 남긴다."""
        async with self._session.begin():
            actor = await self._lock_and_read_actor(organization_id, actor_user_id)
            if target_user_id != actor_user_id and actor.role != OrganizationRole.ADMIN:
                raise RoleForbiddenError
            member = await self._get_member(organization_id, target_user_id)
            if (
                member.role == OrganizationRole.ADMIN
                and await self._members.count_admins(organization_id) <= 1
            ):
                raise LastAdminError
            removed_role = member.role
            await self._members.delete(member)
            user = await self._users.find_by_id(target_user_id)
            assert user is not None
            self._audit.record(
                organization_id=organization_id,
                actor=AuditActor.user(actor_user_id),
                action=AuditAction.MEMBER_REMOVED,
                target=AuditTarget(AuditTargetType.USER, user.id, user.github_login),
                details={"role": str(removed_role)},
            )
        logger.info(
            "organization member removed",
            extra={
                "action": "remove_member",
                "organization_id": str(organization_id),
                "user_id": str(target_user_id),
                "is_self": target_user_id == actor_user_id,
            },
        )

    async def _lock_and_read_actor(
        self, organization_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> OrganizationMember:
        """조직 행을 잠근 뒤 행위자의 멤버 행을 다시 읽는다. 그사이 멤버가 아니게 됐으면 403."""
        if await self._organizations.find_by_id(organization_id, lock=True) is None:
            raise OrganizationNotFoundError
        actor = await self._members.find(organization_id, actor_user_id)
        if actor is None:
            raise RoleForbiddenError
        return actor

    async def _get_member(
        self, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> OrganizationMember:
        member = await self._members.find(organization_id, user_id)
        if member is None:
            raise MemberNotFoundError("member not found", user_id=str(user_id))
        return member
