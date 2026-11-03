"""조직 만들기·개인 조직 slug·초대·멤버(SPEC api-6 AC-6~AC-8)."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import Response
from sqlalchemy import func, select

from app.clients.github_user import GithubUser
from app.enums import InvitationStatus, OrganizationRole
from app.models import AuditLog, Invitation, Organization, OrganizationMember, User
from app.services.organization_service import create_personal_organization
from tests.integration.auth_support import (
    Member,
    create_member,
    create_organization,
    create_session,
    create_user,
    make_client,
    org_path,
)
from tests.integration.deploy_harness import DeployHarness

ORGS = "/api/v1/orgs"


def _data(response: Response) -> Any:
    assert response.is_success, response.text
    return response.json()["data"]


async def _member_role(harness: DeployHarness, organization_id: Any, user_id: Any) -> str | None:
    async with harness.session_factory() as session:
        return await session.scalar(
            select(OrganizationMember.role).where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == user_id,
            )
        )


async def _audit_reasons(harness: DeployHarness) -> list[str]:
    async with harness.session_factory() as session:
        return list((await session.scalars(select(AuditLog.reason))).all())


async def _member(harness: DeployHarness, role: OrganizationRole, login: str) -> Member:
    return await create_member(
        harness.session_factory, harness.organization, role, harness.clock.now(), login=login
    )


# --- AC-6: 조직 ---


async def test_create_organization_makes_creator_admin(harness: DeployHarness) -> None:
    response = await harness.client.post(ORGS, json={"slug": "acme-corp", "name": "ACME 주식회사"})

    data = _data(response)
    assert response.status_code == 201
    assert (data["slug"], data["name"], data["personal"], data["role"]) == (
        "acme-corp",
        "ACME 주식회사",
        False,
        "ADMIN",
    )
    assert await _member_role(harness, data["id"], harness.admin.user.id) == "ADMIN"


@pytest.mark.parametrize(
    "slug",
    ["Acme", "acme--corp", "-acme", "acme-", "a" * 40, "acme_corp", "", "acme.corp"],
)
async def test_create_organization_invalid_slug_is_validation_error(
    harness: DeployHarness, slug: str
) -> None:
    response = await harness.client.post(ORGS, json={"slug": slug, "name": "x"})

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


async def test_create_organization_duplicate_and_reserved_slug_conflict(
    harness: DeployHarness,
) -> None:
    first = await harness.client.post(ORGS, json={"slug": "acme", "name": "ACME"})
    duplicate = await harness.client.post(ORGS, json={"slug": "acme", "name": "other"})
    reserved = await harness.client.post(ORGS, json={"slug": "team-echo", "name": "fake"})

    assert first.status_code == 201
    assert (duplicate.status_code, duplicate.json()["code"]) == (409, "ORGANIZATION_SLUG_CONFLICT")
    assert (reserved.status_code, reserved.json()["code"]) == (409, "ORGANIZATION_SLUG_RESERVED")


async def test_concurrent_same_slug_creates_one_and_conflicts_other(
    harness: DeployHarness,
) -> None:
    responses = await asyncio.gather(
        *(harness.client.post(ORGS, json={"slug": "race", "name": "r"}) for _ in range(2))
    )

    assert sorted(response.status_code for response in responses) == [201, 409]


async def test_search_organizations_lists_only_mine(harness: DeployHarness) -> None:
    await create_organization(harness.session_factory, "someone-else")
    # 하네스의 team-echo 는 DB now()(실제 시각)로, API 가 만든 조직은 주입 시계로 created_at 을
    # 적는다. 실제 시각이 시계 시작값을 지나도 순서가 날짜에 흔들리지 않게 시계를 그 뒤로 옮긴다.
    harness.clock.advance((datetime.now(UTC) - harness.clock.now()).total_seconds() + 60)
    await harness.client.post(ORGS, json={"slug": "acme", "name": "ACME"})

    response = await harness.client.get(ORGS)

    data = _data(response)
    assert [(item["slug"], item["role"]) for item in data["items"]] == [
        ("team-echo", "ADMIN"),
        ("acme", "ADMIN"),
    ]
    assert data["total"] == 2


async def test_get_organization_returns_my_role(harness: DeployHarness) -> None:
    viewer = await _member(harness, OrganizationRole.VIEWER, "vic")
    async with make_client(session_token=viewer.session_token) as client:
        response = await client.get(org_path(harness.organization.id))

    assert _data(response)["role"] == "VIEWER"


async def _personal_slug(harness: DeployHarness, login: str, github_id: int) -> str:
    user = await create_user(harness.session_factory, login, github_id)
    async with harness.session_factory() as session, session.begin():
        organization_id = await create_personal_organization(
            session,
            user=user,
            reserved_slugs=harness.settings.reserved_org_slugs,
            clock=harness.clock,
        )
        organization = await session.get_one(Organization, organization_id)
        assert organization.personal
        return organization.slug


async def test_personal_org_slug_skips_taken_and_reserved(harness: DeployHarness) -> None:
    await create_organization(harness.session_factory, "kim")

    taken = await _personal_slug(harness, "Kim", 501)
    reserved = await _personal_slug(harness, "team-echo", 502)

    assert taken == "kim-2"
    assert reserved == "team-echo-2"


async def test_personal_org_slug_long_login_and_collision_stays_valid(
    harness: DeployHarness,
) -> None:
    login = "a" * 37 + "-b"  # 39자, 줄이면 끝이 하이픈이 된다
    await create_organization(harness.session_factory, login)

    slug = await _personal_slug(harness, login, 503)
    odd = await _personal_slug(harness, "old--style", 504)

    assert slug == "a" * 37 + "-2"
    assert len(slug) <= 39
    # 하이픈 연속(옛 GitHub 아이디)은 규칙 밖이라 숫자 번호로 넘어간다.
    assert odd == "user-504"


# --- AC-7: 초대 ---


BOB = GithubUser(id=7007, login="Bob-Ops", name="Bob", avatar_url=None)


async def _invite(harness: DeployHarness, login: str = "bob-ops", role: str = "DEVELOPER") -> Any:
    return await harness.client.post(
        org_path(harness.organization.id, "/invitations"),
        json={"githubLogin": login, "role": role},
    )


async def test_invitation_applies_on_login(harness: DeployHarness) -> None:
    harness.github_users.add_user("code-bob", BOB)

    invited = await _invite(harness)
    async with make_client() as client:
        start = await client.get("/api/v1/auth/github/login")
        state = start.headers["location"].split("state=")[1]
        await client.get(
            "/api/v1/auth/github/callback", params={"code": "code-bob", "state": state}
        )
        me = await client.get("/api/v1/me")

    data = _data(invited)
    assert invited.status_code == 201
    assert (data["githubLogin"], data["githubId"], data["role"], data["status"]) == (
        "Bob-Ops",
        BOB.id,
        "DEVELOPER",
        "PENDING",
    )
    assert data["expiresAt"].startswith(
        (harness.clock.now() + timedelta(days=7)).date().isoformat()
    )
    roles = {org["slug"]: org["role"] for org in _data(me)["organizations"]}
    assert roles == {"bob-ops": "ADMIN", "team-echo": "DEVELOPER"}
    async with harness.session_factory() as session:
        invitation = await session.get_one(Invitation, data["id"])
    assert (invitation.status, invitation.accepted_at) == ("ACCEPTED", harness.clock.now())


async def _signed_in(harness: DeployHarness, github_user: GithubUser) -> tuple[User, str]:
    user = await create_user(harness.session_factory, github_user.login, github_user.id)
    # 시계를 7일 넘게 돌리는 시험도 있어 세션을 길게 둔다.
    token = await create_session(
        harness.session_factory, user, harness.clock.now(), ttl_seconds=30 * 24 * 60 * 60
    )
    harness.github_users.directory[github_user.login.lower()] = github_user
    return user, token


async def test_invitation_applies_on_me_for_signed_in_user_once_under_concurrency(
    harness: DeployHarness,
) -> None:
    user, token = await _signed_in(harness, BOB)
    await _invite(harness, role="VIEWER")

    async with make_client(session_token=token) as client:
        responses = await asyncio.gather(client.get("/api/v1/me"), client.get("/api/v1/me"))

    assert [response.status_code for response in responses] == [200, 200]
    async with harness.session_factory() as session:
        members = await session.scalar(
            select(func.count())
            .select_from(OrganizationMember)
            .where(OrganizationMember.user_id == user.id)
        )
    assert members == 1
    assert await _member_role(harness, harness.organization.id, user.id) == "VIEWER"


async def test_expired_invitation_is_not_applied(harness: DeployHarness) -> None:
    user, token = await _signed_in(harness, BOB)
    invited = _data(await _invite(harness))

    harness.clock.advance(7 * 24 * 60 * 60 + 1)
    async with make_client(session_token=token) as client:
        await client.get("/api/v1/me")

    async with harness.session_factory() as session:
        invitation = await session.get_one(Invitation, invited["id"])
    assert invitation.status == InvitationStatus.EXPIRED
    assert await _member_role(harness, harness.organization.id, user.id) is None


async def test_list_invitations_closes_expired_ones(harness: DeployHarness) -> None:
    harness.github_users.directory["bob-ops"] = BOB
    await _invite(harness)
    harness.clock.advance(7 * 24 * 60 * 60)
    # 시험 도구의 어드민 세션도 7일이라 같이 끝난다. 새 세션으로 본다.
    token = await create_session(harness.session_factory, harness.admin.user, harness.clock.now())

    async with make_client(session_token=token) as client:
        listed = await client.get(org_path(harness.organization.id, "/invitations"))

    assert [item["status"] for item in _data(listed)["items"]] == ["EXPIRED"]


async def test_revoke_and_reinvite(harness: DeployHarness) -> None:
    harness.github_users.directory["bob-ops"] = BOB
    first = _data(await _invite(harness))
    second = _data(await _invite(harness, role="ADMIN"))
    path = org_path(harness.organization.id, "/invitations")

    revoked = await harness.client.delete(f"{path}/{second['id']}")
    again = await harness.client.delete(f"{path}/{second['id']}")
    listed = _data(await harness.client.get(path))

    assert revoked.status_code == 204
    assert (again.status_code, again.json()["code"]) == (409, "INVITATION_NOT_PENDING")
    statuses = {item["id"]: item["status"] for item in listed["items"]}
    assert statuses == {first["id"]: "REVOKED", second["id"]: "REVOKED"}


async def test_invite_existing_member_unknown_login_and_github_down(
    harness: DeployHarness,
) -> None:
    await _signed_in(harness, BOB)
    await _invite(harness)
    member = await _member(harness, OrganizationRole.VIEWER, "carol")
    harness.github_users.directory["carol"] = GithubUser(
        id=member.user.github_id, login="carol", name=None, avatar_url=None
    )

    already = await _invite(harness, login="carol")
    unknown = await _invite(harness, login="nobody-here")
    harness.github_users.unavailable = True
    down = await _invite(harness)

    assert (already.status_code, already.json()["code"]) == (409, "ALREADY_MEMBER")
    assert (unknown.status_code, unknown.json()["code"]) == (422, "GITHUB_USER_NOT_FOUND")
    assert (down.status_code, down.json()["code"]) == (502, "GITHUB_UNAVAILABLE")


async def test_invite_by_developer_is_forbidden_and_recorded(harness: DeployHarness) -> None:
    developer = await _member(harness, OrganizationRole.DEVELOPER, "dev")
    async with make_client(session_token=developer.session_token) as client:
        response = await client.post(
            org_path(harness.organization.id, "/invitations"),
            json={"githubLogin": "bob-ops", "role": "ADMIN"},
        )

    assert (response.status_code, response.json()["code"]) == (403, "ROLE_FORBIDDEN")
    assert await _audit_reasons(harness) == ["ROLE_FORBIDDEN"]
    assert harness.github_users.calls == []


# --- AC-8: 멤버 ---


async def _members_path(harness: DeployHarness, user: User | None = None) -> str:
    suffix = "/members" if user is None else f"/members/{user.id}"
    return org_path(harness.organization.id, suffix)


async def test_admin_changes_role_lists_members_and_cannot_change_own(
    harness: DeployHarness,
) -> None:
    viewer = await _member(harness, OrganizationRole.VIEWER, "vic")

    changed = await harness.client.patch(
        await _members_path(harness, viewer.user), json={"role": "DEVELOPER"}
    )
    own = await harness.client.patch(
        await _members_path(harness, harness.admin.user), json={"role": "VIEWER"}
    )
    listed = await harness.client.get(await _members_path(harness))

    assert _data(changed)["role"] == "DEVELOPER"
    assert (own.status_code, own.json()["code"]) == (403, "CANNOT_CHANGE_OWN_ROLE")
    assert await _audit_reasons(harness) == ["CANNOT_CHANGE_OWN_ROLE"]
    items = _data(listed)["items"]
    assert [(item["githubLogin"], item["role"]) for item in items] == [
        ("team-echo-admin", "ADMIN"),
        ("vic", "DEVELOPER"),
    ]
    assert set(items[0]) == {"id", "githubLogin", "name", "avatarUrl", "role", "joinedAt"}


async def test_leave_remove_and_last_admin(harness: DeployHarness) -> None:
    viewer = await _member(harness, OrganizationRole.VIEWER, "vic")
    other = await _member(harness, OrganizationRole.VIEWER, "olga")
    async with make_client(session_token=viewer.session_token) as client:
        removes_other = await client.delete(await _members_path(harness, other.user))
        leaves = await client.delete(await _members_path(harness, viewer.user))
        after_leave = await client.get(org_path(harness.organization.id))

    last_admin_leaves = await harness.client.delete(
        await _members_path(harness, harness.admin.user)
    )
    removed = await harness.client.delete(await _members_path(harness, other.user))
    missing = await harness.client.delete(await _members_path(harness, other.user))

    assert (removes_other.status_code, removes_other.json()["code"]) == (403, "ROLE_FORBIDDEN")
    assert leaves.status_code == 204
    assert (after_leave.status_code, after_leave.json()["code"]) == (404, "ORGANIZATION_NOT_FOUND")
    assert (last_admin_leaves.status_code, last_admin_leaves.json()["code"]) == (409, "LAST_ADMIN")
    assert removed.status_code == 204
    assert (missing.status_code, missing.json()["code"]) == (404, "MEMBER_NOT_FOUND")


async def test_demoting_last_admin_conflicts(harness: DeployHarness) -> None:
    second = await _member(harness, OrganizationRole.ADMIN, "second-admin")
    async with make_client(session_token=second.session_token) as client:
        demote_first = await client.patch(
            await _members_path(harness, harness.admin.user), json={"role": "VIEWER"}
        )
    demote_second_by_viewer_first = await harness.client.patch(
        await _members_path(harness, second.user), json={"role": "VIEWER"}
    )

    assert demote_first.status_code == 200
    assert demote_second_by_viewer_first.json()["code"] == "ROLE_FORBIDDEN"


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
async def test_two_admins_removing_each_other_keep_one_admin(
    harness: DeployHarness, method: str
) -> None:
    second = await _member(harness, OrganizationRole.ADMIN, "second-admin")
    first_path = await _members_path(harness, harness.admin.user)
    second_path = await _members_path(harness, second.user)

    async def act(client: Any, path: str) -> Response:
        if method == "PATCH":
            response: Response = await client.patch(path, json={"role": "VIEWER"})
            return response
        response = await client.delete(path)
        return response

    async with make_client(session_token=second.session_token) as second_client:
        results = await asyncio.gather(
            act(harness.client, second_path), act(second_client, first_path)
        )

    async with harness.session_factory() as session:
        admins = await session.scalar(
            select(func.count())
            .select_from(OrganizationMember)
            .where(
                OrganizationMember.organization_id == harness.organization.id,
                OrganizationMember.role == OrganizationRole.ADMIN,
            )
        )
    winners = [response for response in results if response.is_success]
    losers = [response for response in results if not response.is_success]
    assert admins == 1
    assert len(winners) == 1 and len(losers) == 1
    allowed = {(403, "ROLE_FORBIDDEN"), (409, "LAST_ADMIN")}
    if method == "DELETE":
        # 먼저 지워진 쪽은 이미 조직 멤버가 아니다. API 는 멤버가 아닌 사람에게
        # 조직의 존재를 숨기려고 404 를 주므로 이 응답은 설계대로다.
        allowed.add((404, "ORGANIZATION_NOT_FOUND"))
    assert (losers[0].status_code, losers[0].json()["code"]) in allowed
