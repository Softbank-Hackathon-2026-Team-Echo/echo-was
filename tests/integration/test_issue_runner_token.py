"""러너 토큰 발급·철회 명령(SPEC api-runner-prechecks AC-13). 시험 DB 안에서만 부른다."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.tokens import RUNNER_TOKEN_PREFIX, hash_token
from app.models import AgentRunner, AuditLog
from app.scripts import issue_runner_token
from tests.integration.auth_support import make_client
from tests.integration.migration_support import Migration
from tests.integration.runner_support import CLAIM_PATH, RUNNER_NAME

SessionFactory = async_sessionmaker[AsyncSession]


class _Engine:
    async def dispose(self) -> None:
        return None


@pytest.fixture
def command(session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch) -> SessionFactory:
    monkeypatch.setattr(issue_runner_token, "get_session_factory", lambda: session_factory)
    monkeypatch.setattr(issue_runner_token, "get_engine", lambda: _Engine())
    return session_factory


async def _runner(session_factory: SessionFactory, name: str) -> AgentRunner | None:
    async with session_factory() as session:
        return await session.scalar(select(AgentRunner).where(AgentRunner.name == name))


async def test_issue_runner_token_prints_raw_once_and_stores_hash_only(
    command: SessionFactory, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    code = await issue_runner_token.main(["--name", RUNNER_NAME])

    out = capsys.readouterr().out
    token_lines = [line for line in out.splitlines() if line.startswith(RUNNER_TOKEN_PREFIX)]
    assert code == 0
    assert len(token_lines) == 1
    raw = token_lines[0]
    row = await _runner(command, RUNNER_NAME)
    assert row is not None
    assert row.token_hash == hash_token(raw)
    assert raw not in row.token_hash
    assert row.revoked_at is None
    assert raw not in caplog.text
    async with command() as session:
        assert await session.scalar(select(func.count()).select_from(AuditLog)) == 0


async def test_issue_runner_token_same_name_twice_fails(
    command: SessionFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await issue_runner_token.main(["--name", RUNNER_NAME]) == 0
    first = await _runner(command, RUNNER_NAME)
    capsys.readouterr()

    code = await issue_runner_token.main(["--name", RUNNER_NAME])

    captured = capsys.readouterr()
    assert code == 1
    assert RUNNER_TOKEN_PREFIX not in captured.out
    assert "already exists" in captured.err
    second = await _runner(command, RUNNER_NAME)
    assert first is not None and second is not None
    assert first.token_hash == second.token_hash


@pytest.mark.parametrize("name", ["Bad_Name", "-edge", "edge-", "x" * 64, ""])
async def test_issue_runner_token_rejects_bad_name(
    command: SessionFactory, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    code = await issue_runner_token.main([f"--name={name}"])

    assert code == 1
    assert "name must be" in capsys.readouterr().err


async def test_issue_runner_token_revoke_makes_claim_unauthorized(
    migration: Migration,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session_factory = migration.harness.session_factory
    monkeypatch.setattr(issue_runner_token, "get_session_factory", lambda: session_factory)
    monkeypatch.setattr(issue_runner_token, "get_engine", lambda: _Engine())
    assert await issue_runner_token.main(["--name", RUNNER_NAME]) == 0
    raw = next(
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith(RUNNER_TOKEN_PREFIX)
    )
    body = {"runnerId": RUNNER_NAME, "capabilities": ["PRECHECK"]}
    async with make_client(agent_token=raw, origin=None) as client:
        before = await client.post(CLAIM_PATH, json=body)

        revoked = await issue_runner_token.main(["--revoke", RUNNER_NAME])
        after = await client.post(CLAIM_PATH, json=body)

    assert before.status_code == 200, before.text
    assert revoked == 0
    assert (after.status_code, after.json()["code"]) == (401, "RUNNER_TOKEN_INVALID")
    assert raw not in capsys.readouterr().out


async def test_issue_runner_token_revoke_unknown_or_revoked_fails(
    command: SessionFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await issue_runner_token.main(["--revoke", "nobody"]) == 1
    assert await issue_runner_token.main(["--name", RUNNER_NAME]) == 0
    assert await issue_runner_token.main(["--revoke", RUNNER_NAME]) == 0
    capsys.readouterr()

    code = await issue_runner_token.main(["--revoke", RUNNER_NAME])

    assert code == 1
    assert "no active runner" in capsys.readouterr().err


async def test_issue_runner_token_requires_exactly_one_action() -> None:
    with pytest.raises(SystemExit):
        await issue_runner_token.main([])
    with pytest.raises(SystemExit):
        await issue_runner_token.main(["--name", "a", "--revoke", "b"])
