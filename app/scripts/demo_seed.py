"""데모 준비 명령: 데모 조직·멤버·에이전트 토큰을 만든다.

    python -m app.scripts.demo_seed --org team-echo --admin <GitHub 아이디> --viewer <GitHub 아이디>

- 두 사람 모두 GitHub 로그인을 한 번 해 둔 상태여야 한다(GitHub 숫자 번호를 로그인으로 얻는다).
  로그인한 적 없는 아이디면 분명한 메시지로 실패한다.
- 조직이 없으면 만든다. 예약 slug(team-echo)도 이 명령만은 만들 수 있다. 조직이 이미 있는데 만든
  사람이 --admin 이 아니면 실패한다(남이 먼저 만든 같은 이름 조직에 팀을 넣지 않는다).
- --admin 은 ADMIN, --viewer 는 VIEWER 로 맞춘다. 두 번 돌려도 조직·멤버는 늘지 않는다.
- 에이전트 토큰을 하나 발급해 원문을 표준 출력에 한 번 쓴다(돌릴 때마다 새 토큰).
  로그에는 쓰지 않는다.
"""

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock, SystemClock
from app.core.config import get_settings
from app.core.database import get_engine, get_session_factory
from app.core.logging import configure_logging
from app.enums import OrganizationRole
from app.models import Organization, User
from app.repositories.organization_member_repository import OrganizationMemberRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.user_repository import UserRepository
from app.services.agent_token_service import IssuedAgentToken, issue_agent_token
from app.services.organization_service import is_valid_slug

DEMO_AGENT_TOKEN_NAME = "demo-agent"


class DemoSeedError(Exception):
    """데모 준비를 할 수 없다. 메시지는 그대로 사용자에게 보여 준다(비밀값 없음)."""


@dataclass(frozen=True)
class DemoSeedResult:
    organization: Organization
    organization_created: bool
    admin: User
    viewer: User
    agent_token: IssuedAgentToken


async def seed_demo(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    *,
    org_slug: str,
    admin_login: str,
    viewer_login: str,
    agent_token_days: int,
) -> DemoSeedResult:
    if not is_valid_slug(org_slug):
        raise DemoSeedError(f"invalid organization slug: {org_slug}")
    if admin_login.lower() == viewer_login.lower():
        raise DemoSeedError("--admin and --viewer must be different GitHub users")
    now = clock.now()
    async with session_factory() as session, session.begin():
        users = UserRepository(session)
        organizations = OrganizationRepository(session)
        members = OrganizationMemberRepository(session)
        admin = await _signed_in_user(users, admin_login)
        viewer = await _signed_in_user(users, viewer_login)

        created_id = None
        if await organizations.find_by_slug(org_slug) is None:
            created_id = await organizations.insert_if_slug_free(
                slug=org_slug,
                name=org_slug,
                personal=False,
                created_by_user_id=admin.id,
                now=now,
            )
        organization = await organizations.find_by_slug(org_slug)
        assert organization is not None
        if organization.created_by_user_id != admin.id:
            raise DemoSeedError(
                f"organization {org_slug} exists and was not created by {admin_login}"
            )
        # 멤버 변경과 같은 잠금(조직 FOR NO KEY UPDATE)을 잡고 역할을 맞춘다.
        await organizations.find_by_id(organization.id, lock=True)
        await _ensure_role(members, organization.id, admin.id, OrganizationRole.ADMIN, now)
        await _ensure_role(members, organization.id, viewer.id, OrganizationRole.VIEWER, now)
        token = await issue_agent_token(
            session,
            organization_id=organization.id,
            created_by_user_id=admin.id,
            name=DEMO_AGENT_TOKEN_NAME,
            expires_in_days=agent_token_days,
            clock=clock,
        )
    return DemoSeedResult(
        organization=organization,
        organization_created=created_id is not None,
        admin=admin,
        viewer=viewer,
        agent_token=token,
    )


async def _signed_in_user(users: UserRepository, github_login: str) -> User:
    user = await users.find_by_github_login(github_login)
    if user is None:
        raise DemoSeedError(
            f"GitHub user {github_login} has not signed in to Echo yet; sign in once first"
        )
    return user


async def _ensure_role(
    members: OrganizationMemberRepository,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    role: OrganizationRole,
    now: datetime,
) -> None:
    member = await members.find(organization_id, user_id)
    if member is None:
        await members.insert_if_absent(
            organization_id=organization_id, user_id=user_id, role=role, now=now
        )
        return
    if member.role != role:
        member.role = role
        member.updated_at = now


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m app.scripts.demo_seed")
    parser.add_argument("--org", required=True, help="조직 slug (예: team-echo)")
    parser.add_argument(
        "--admin", required=True, help="어드민이 될 GitHub 아이디(로그인한 적 있음)"
    )
    parser.add_argument("--viewer", required=True, help="뷰어가 될 GitHub 아이디(로그인한 적 있음)")
    return parser.parse_args(argv)


async def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    try:
        result = await seed_demo(
            get_session_factory(),
            SystemClock(),
            org_slug=args.org,
            admin_login=args.admin,
            viewer_login=args.viewer,
            agent_token_days=settings.agent_token_default_days,
        )
    except DemoSeedError as exc:
        sys.stderr.write(f"demo_seed: {exc}\n")
        return 1
    finally:
        await get_engine().dispose()
    organization = result.organization
    token = result.agent_token
    sys.stdout.write(
        f"organization: {organization.slug} ({organization.id})"
        f"{' created' if result.organization_created else ' already existed'}\n"
        f"admin: {result.admin.github_login} ADMIN\n"
        f"viewer: {result.viewer.github_login} VIEWER\n"
        f"agent token {token.token.id} (expires {token.token.expires_at.isoformat()}), "
        "shown once:\n"
        f"{token.raw.get_secret_value()}\n"
    )
    return 0


if __name__ == "__main__":
    configure_logging("echo-demo-seed", get_settings().log_level)
    sys.exit(asyncio.run(main()))
