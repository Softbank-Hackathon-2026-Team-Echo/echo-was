"""데모 준비 명령(SPEC api-6 AC-17)."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.database import get_session_factory
from app.dependencies import get_clock
from app.enums import ActorType, AuditResult, OrganizationRole
from app.main import app
from app.models import AgentToken, AuditLog, Organization, OrganizationMember
from app.scripts import demo_seed
from app.scripts.demo_seed import DemoSeedError, seed_demo
from tests.integration.auth_support import (
    auth_settings,
    create_organization,
    create_user,
    make_client,
    org_path,
)
from tests.integration.deploy_harness import FakeClock

SessionFactory = async_sessionmaker[AsyncSession]


async def _counts(session_factory: SessionFactory) -> tuple[int, int, int]:
    async with session_factory() as session:
        return (
            await session.scalar(select(func.count()).select_from(Organization)) or 0,
            await session.scalar(select(func.count()).select_from(OrganizationMember)) or 0,
            await session.scalar(select(func.count()).select_from(AgentToken)) or 0,
        )


@pytest.fixture
async def api(session_factory: SessionFactory) -> AsyncIterator[FakeClock]:
    clock = FakeClock()
    settings = auth_settings(get_settings())
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_clock] = lambda: clock
    try:
        yield clock
    finally:
        app.dependency_overrides.clear()


async def test_demo_seed_creates_org_members_token_and_is_idempotent(
    session_factory: SessionFactory, api: FakeClock
) -> None:
    admin = await create_user(session_factory, "Admin-Kim")
    viewer = await create_user(session_factory, "viewer-lee")

    first = await seed_demo(
        session_factory,
        api,
        org_slug="team-echo",
        admin_login="admin-kim",
        viewer_login="VIEWER-LEE",
        agent_token_days=30,
    )
    counts_after_first = await _counts(session_factory)
    second = await seed_demo(
        session_factory,
        api,
        org_slug="team-echo",
        admin_login="admin-kim",
        viewer_login="viewer-lee",
        agent_token_days=30,
    )

    assert first.organization_created and not second.organization_created
    assert first.organization.id == second.organization.id
    assert first.organization.slug == "team-echo" and not first.organization.personal
    assert counts_after_first == (1, 2, 1)
    # 두 번째 실행은 조직·멤버를 늘리지 않고 토큰만 새로 낸다.
    assert await _counts(session_factory) == (1, 2, 2)
    async with session_factory() as session:
        roles = dict(
            (
                await session.execute(select(OrganizationMember.user_id, OrganizationMember.role))
            ).all()
        )
    assert roles == {admin.id: OrganizationRole.ADMIN, viewer.id: OrganizationRole.VIEWER}

    # 출력된 토큰으로 운영 변경(배포 요청) → 403 + 기록. 데모 거부 장면.
    token = first.agent_token.raw.get_secret_value()
    async with make_client(agent_token=token) as agent:
        read = await agent.get(org_path(first.organization.id, "/services"))
        denied = await agent.post(
            org_path(first.organization.id, "/services/00000000-0000-0000-0000-000000000000")
            + "/deploy-requests",
            json={"environmentId": "00000000-0000-0000-0000-000000000000", "branch": "main"},
        )
    async with session_factory() as session:
        audit = (await session.scalars(select(AuditLog))).all()
    assert read.status_code == 200
    assert (denied.status_code, denied.json()["code"]) == (403, "AGENT_READ_ONLY")
    denials = [row for row in audit if row.result == AuditResult.DENIED]
    assert [(row.actor_type, row.organization_id, row.reason) for row in denials] == [
        (ActorType.AGENT, first.organization.id, "AGENT_READ_ONLY")
    ]
    # 시드 자체는 줄을 남기지 않는다. 남은 것은 에이전트의 읽기·거부뿐이다.
    assert {row.actor_type for row in audit} == {ActorType.AGENT}
    assert sorted(row.action for row in audit) == ["DEPLOY_REQUESTED", "RESOURCE_READ"]


async def test_demo_seed_fails_clearly_for_unknown_user_or_foreign_org(
    session_factory: SessionFactory,
) -> None:
    await create_user(session_factory, "admin-kim")
    await create_user(session_factory, "viewer-lee")
    stranger = await create_user(session_factory, "stranger")
    await create_organization(session_factory, "taken-org", creator=stranger)
    clock = FakeClock()

    with pytest.raises(DemoSeedError, match="ghost has not signed in"):
        await seed_demo(
            session_factory,
            clock,
            org_slug="team-echo",
            admin_login="admin-kim",
            viewer_login="ghost",
            agent_token_days=30,
        )
    with pytest.raises(DemoSeedError, match="was not created by admin-kim"):
        await seed_demo(
            session_factory,
            clock,
            org_slug="taken-org",
            admin_login="admin-kim",
            viewer_login="viewer-lee",
            agent_token_days=30,
        )
    with pytest.raises(DemoSeedError, match="must be different"):
        await seed_demo(
            session_factory,
            clock,
            org_slug="team-echo",
            admin_login="admin-kim",
            viewer_login="Admin-Kim",
            agent_token_days=30,
        )
    # 실패한 실행은 아무것도 남기지 않는다(stranger 의 조직 하나만).
    assert await _counts(session_factory) == (1, 0, 0)


async def test_demo_seed_command_prints_token_once(
    engine: AsyncEngine,
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await create_user(session_factory, "admin-kim")
    await create_user(session_factory, "viewer-lee")
    monkeypatch.setattr(demo_seed, "get_session_factory", lambda: session_factory)

    class _Engine:
        async def dispose(self) -> None:
            return None

    monkeypatch.setattr(demo_seed, "get_engine", lambda: _Engine())
    args: list[Any] = ["--org", "team-echo", "--admin", "admin-kim", "--viewer", "viewer-lee"]

    ok = await demo_seed.main(args)
    out = capsys.readouterr()
    missing = await demo_seed.main(["--org", "team-echo", "--admin", "admin-kim", "--viewer", "x"])
    failed = capsys.readouterr()

    token_lines = [line for line in out.out.splitlines() if line.startswith("echo_agt_")]
    assert ok == 0
    assert len(token_lines) == 1
    assert "organization: team-echo" in out.out and "created" in out.out
    assert missing == 1
    assert "x has not signed in" in failed.err
    assert "echo_agt_" not in failed.out
