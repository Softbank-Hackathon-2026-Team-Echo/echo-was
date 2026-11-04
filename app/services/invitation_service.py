"""초대 보내기·목록·취소와 적용(로그인 콜백·GET /me 때).

초대는 GitHub 아이디로 받아 GitHub 숫자 번호로 저장한다(아이디는 바뀔 수 있다). 그 번호의 사람이
로그인하거나 /me 를 부르면 만료 전 PENDING 초대가 멤버가 된다. 두 요청이 동시에 와도 한 번만
적용된다: `PENDING 일 때만 ACCEPTED` 조건부 UPDATE(0행이면 건너뜀) + 멤버
`ON CONFLICT DO NOTHING`.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.actions import AuditAction, AuditTargetType
from app.audit.recorder import AuditActor, AuditRecorder, AuditTarget
from app.clients.github_user import GithubUserClient
from app.core.clock import Clock
from app.core.exceptions import (
    AlreadyMemberError,
    GithubUserNotFoundError,
    InvitationNotFoundError,
    InvitationNotPendingError,
    OrganizationNotFoundError,
)
from app.enums import InvitationStatus, OrganizationRole
from app.models import Invitation, User
from app.repositories.invitation_repository import InvitationRepository
from app.repositories.organization_member_repository import OrganizationMemberRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.user_repository import UserRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InvitationSearchResult:
    items: list[Invitation]
    total: int


async def apply_pending_invitations(session: AsyncSession, user: User, clock: Clock) -> int:
    """그 사람 github_id 의 PENDING 초대를 적용한다. 호출하는 쪽이 트랜잭션을 연다.

    만료 전이면 멤버로 넣고 ACCEPTED(이미 멤버면 역할은 그대로 두고 ACCEPTED), 만료됐으면 EXPIRED.
    적용해 새로 멤버가 된 조직 수를 돌려준다. 멤버 행을 실제로 만든 초대마다 MEMBER_JOINED 한 줄을
    같은 트랜잭션에 쓴다(/me·로그인 콜백 두 경로가 모두 이 함수를 지난다).
    """
    invitations = InvitationRepository(session)
    members = OrganizationMemberRepository(session)
    now = clock.now()
    joined = 0
    for invitation in await invitations.search_pending_by_github_id(user.github_id):
        if invitation.expires_at <= now:
            await invitations.close_if_pending(
                invitation.id, {"status": InvitationStatus.EXPIRED, "updated_at": now}
            )
            continue
        accepted = await invitations.close_if_pending(
            invitation.id,
            {"status": InvitationStatus.ACCEPTED, "accepted_at": now, "updated_at": now},
        )
        if not accepted:
            continue
        if await members.insert_if_absent(
            organization_id=invitation.organization_id,
            user_id=user.id,
            role=OrganizationRole(invitation.role),
            now=now,
        ):
            joined += 1
            AuditRecorder(session, clock).record(
                organization_id=invitation.organization_id,
                actor=AuditActor.user(user.id),
                action=AuditAction.MEMBER_JOINED,
                target=AuditTarget(AuditTargetType.USER, user.id, user.github_login),
                details={"role": str(invitation.role), "invitationId": str(invitation.id)},
            )
        logger.info(
            "invitation accepted",
            extra={
                "action": "apply_invitation",
                "invitation_id": str(invitation.id),
                "organization_id": str(invitation.organization_id),
                "user_id": str(user.id),
            },
        )
    return joined


class InvitationService:
    def __init__(
        self,
        session: AsyncSession,
        clock: Clock,
        github_client: GithubUserClient,
        invitation_ttl_seconds: int,
    ) -> None:
        self._session = session
        self._clock = clock
        self._github_client = github_client
        self._ttl = timedelta(seconds=invitation_ttl_seconds)
        self._invitations = InvitationRepository(session)
        self._organizations = OrganizationRepository(session)
        self._members = OrganizationMemberRepository(session)
        self._users = UserRepository(session)
        self._audit = AuditRecorder(session, clock)

    async def create_invitation(
        self,
        organization_id: uuid.UUID,
        invited_by_user_id: uuid.UUID,
        github_login: str,
        role: OrganizationRole,
    ) -> Invitation:
        """GitHub 에서 숫자 번호를 찾고(트랜잭션 밖) 초대를 만든다. 같은 사람의 대기 초대는 닫는다.

        조직 행을 잠가 같은 조직의 초대·멤버 변경을 줄 세운다(부분 unique 충돌 방지).
        """
        github_user = await self._github_client.find_user(github_login)
        if github_user is None:
            raise GithubUserNotFoundError
        now = self._clock.now()
        async with self._session.begin():
            if await self._organizations.find_by_id(organization_id, lock=True) is None:
                raise OrganizationNotFoundError
            existing_user = await self._users.find_by_github_id(github_user.id)
            if (
                existing_user is not None
                and await self._members.find(organization_id, existing_user.id) is not None
            ):
                raise AlreadyMemberError("already a member", user_id=str(existing_user.id))
            await self._invitations.revoke_pending(organization_id, github_user.id, now)
            invitation = Invitation(
                organization_id=organization_id,
                github_id=github_user.id,
                github_login=github_user.login,
                role=role,
                status=InvitationStatus.PENDING,
                invited_by_user_id=invited_by_user_id,
                expires_at=now + self._ttl,
                created_at=now,
                updated_at=now,
            )
            self._invitations.add(invitation)
            await self._session.flush()
            self._audit.record(
                organization_id=organization_id,
                actor=AuditActor.user(invited_by_user_id),
                action=AuditAction.INVITATION_CREATED,
                target=AuditTarget(
                    AuditTargetType.INVITATION, invitation.id, invitation.github_login
                ),
                details={"githubLogin": invitation.github_login, "role": str(role)},
            )
        logger.info(
            "invitation created",
            extra={
                "action": "create_invitation",
                "invitation_id": str(invitation.id),
                "organization_id": str(organization_id),
                "role": role,
            },
        )
        return invitation

    async def search_invitations(
        self, organization_id: uuid.UUID, page: int, size: int
    ) -> InvitationSearchResult:
        """기한이 지난 대기 초대를 EXPIRED 로 닫은 뒤 최신순으로 돌려준다."""
        now = self._clock.now()
        async with self._session.begin():
            await self._invitations.expire_overdue(organization_id, now)
            items = await self._invitations.search_by_organization(
                organization_id, offset=page * size, limit=size
            )
            total = await self._invitations.count_by_organization(organization_id)
        return InvitationSearchResult(items=items, total=total)

    async def revoke_invitation(
        self, organization_id: uuid.UUID, invitation_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> None:
        now = self._clock.now()
        async with self._session.begin():
            invitation = await self._invitations.find_in_organization(
                organization_id, invitation_id
            )
            if invitation is None:
                raise InvitationNotFoundError(
                    "invitation not found", invitation_id=str(invitation_id)
                )
            revoked = await self._invitations.close_if_pending(
                invitation.id, {"status": InvitationStatus.REVOKED, "updated_at": now}
            )
            if not revoked:
                raise InvitationNotPendingError(
                    "invitation is not pending", invitation_id=str(invitation_id)
                )
            self._audit.record(
                organization_id=organization_id,
                actor=AuditActor.user(actor_user_id),
                action=AuditAction.INVITATION_REVOKED,
                target=AuditTarget(
                    AuditTargetType.INVITATION, invitation.id, invitation.github_login
                ),
            )
        logger.info(
            "invitation revoked",
            extra={
                "action": "revoke_invitation",
                "invitation_id": str(invitation_id),
                "organization_id": str(organization_id),
            },
        )
