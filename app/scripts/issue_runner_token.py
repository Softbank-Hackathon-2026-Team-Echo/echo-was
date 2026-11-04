"""AI 러너 토큰 발급·철회 명령. 어드민이 운영 파드에서 한 번 돌린다(API 로는 발급하지 않는다).

    python -m app.scripts.issue_runner_token --name <이름>
    python -m app.scripts.issue_runner_token --revoke <이름>

- 발급: 이름이 이미 있으면(철회한 이름 포함) 실패한다. 원문은 표준 출력에 한 번만 쓰고 DB 에는
  SHA-256 만 둔다. 로그에는 쓰지 않는다. 감사 줄은 남기지 않는다(조직 밖 어드민 명령).
- 철회: 그 러너의 토큰이 바로 무효가 된다(401). 없는 이름이나 이미 철회한 이름이면 실패한다.
"""

import argparse
import asyncio
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.tokens import hash_token, new_runner_token
from app.core.clock import Clock, SystemClock
from app.core.config import get_settings
from app.core.database import get_engine, get_session_factory
from app.core.logging import configure_logging
from app.core.secrets import SecretValue
from app.models import AgentRunner
from app.models.agent_runner import AGENT_RUNNER_NAME_MAX_LENGTH
from app.repositories.agent_runner_repository import AgentRunnerRepository

_NAME_PATTERN = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")


class RunnerTokenCommandError(Exception):
    """발급·철회를 할 수 없다. 메시지는 그대로 사용자에게 보여 준다(비밀값 없음)."""


@dataclass(frozen=True)
class IssuedRunnerToken:
    runner: AgentRunner
    raw: SecretValue


async def issue_runner_token(
    session_factory: async_sessionmaker[AsyncSession], name: str
) -> IssuedRunnerToken:
    if len(name) > AGENT_RUNNER_NAME_MAX_LENGTH or _NAME_PATTERN.fullmatch(name) is None:
        raise RunnerTokenCommandError(
            "name must be lowercase letters, digits and hyphens (1 to 63 characters)"
        )
    raw = new_runner_token()
    runner = AgentRunner(name=name, token_hash=hash_token(raw.get_secret_value()))
    try:
        async with session_factory() as session, session.begin():
            if await AgentRunnerRepository(session).find_by_name(name) is not None:
                raise RunnerTokenCommandError(f"runner already exists: {name}")
            AgentRunnerRepository(session).add(runner)
    except IntegrityError:
        raise RunnerTokenCommandError(f"runner already exists: {name}") from None
    return IssuedRunnerToken(runner, raw)


async def revoke_runner_token(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock, name: str
) -> AgentRunner:
    async with session_factory() as session, session.begin():
        repository = AgentRunnerRepository(session)
        runner = await repository.find_by_name(name)
        if runner is None or not await repository.revoke(runner.id, clock.now()):
            raise RunnerTokenCommandError(f"no active runner: {name}")
    return runner


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI 러너 토큰을 발급·철회한다.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--name", help="새 러너 이름. 토큰 원문을 한 번 출력한다")
    group.add_argument("--revoke", metavar="NAME", help="토큰을 철회할 러너 이름")
    return parser.parse_args(argv)


async def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.name is not None:
            issued = await issue_runner_token(get_session_factory(), args.name)
            sys.stdout.write(
                f"runner {issued.runner.name} ({issued.runner.id}) token, shown once:\n"
                f"{issued.raw.get_secret_value()}\n"
            )
        else:
            runner = await revoke_runner_token(get_session_factory(), SystemClock(), args.revoke)
            sys.stdout.write(f"runner {runner.name} ({runner.id}) revoked\n")
    except RunnerTokenCommandError as exc:
        sys.stderr.write(f"issue_runner_token: {exc}\n")
        return 1
    finally:
        await get_engine().dispose()
    return 0


if __name__ == "__main__":
    configure_logging("echo-runner-token", get_settings().log_level)
    sys.exit(asyncio.run(main()))
