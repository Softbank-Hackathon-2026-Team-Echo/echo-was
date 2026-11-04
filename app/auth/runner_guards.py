"""AI 러너 경로 검사기. 사람·에이전트 경로와 자격 증명을 섞지 않는다.

- require_runner: 러너 토큰(`echo_rnr_…`). 실행 가져가기만 부를 수 있다.
- require_run_token: 실행 토큰(`echo_run_…`). 토큰의 실행과 경로의 실행 id 가 같아야 한다.

자격 증명이 없거나 무효면 401 RUNNER_TOKEN_INVALID, 사람 쿠키·에이전트 토큰이면 403
RUNNER_FORBIDDEN(거부 줄을 남기지 않는 일반 403)이다. 끝난 실행의 토큰은 여기서 막지 않는다.
해시를 지우지 않으므로 서비스가 상태를 보고 409 로 답한다. 검사기는 짧은 세션을 따로 연다.
"""

import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, Path, Request

from app.auth.guards import AUTH_GUARDS, ClockDep, FactoryDep, SettingsDep
from app.auth.tokens import AGENT_TOKEN_PREFIX, RUN_TOKEN_PREFIX, RUNNER_TOKEN_PREFIX, hash_token
from app.core.config import Settings
from app.core.exceptions import RunnerForbiddenError, RunnerTokenInvalidError
from app.models import AgentRunner
from app.repositories.agent_run_repository import AgentRunRepository
from app.repositories.agent_runner_repository import AgentRunnerRepository

RUNNER_LAST_SEEN_INTERVAL = timedelta(seconds=60)
_BEARER_PREFIX = "bearer "


def _read_bearer_token(request: Request, settings: Settings) -> str:
    authorization = request.headers.get("authorization")
    if authorization is None:
        if settings.session_cookie_name in request.cookies:
            raise RunnerForbiddenError
        raise RunnerTokenInvalidError
    if not authorization.lower().startswith(_BEARER_PREFIX):
        raise RunnerTokenInvalidError
    raw = authorization[len(_BEARER_PREFIX) :].strip()
    if raw.startswith(AGENT_TOKEN_PREFIX):
        raise RunnerForbiddenError
    return raw


async def require_runner(
    request: Request, settings: SettingsDep, clock: ClockDep, session_factory: FactoryDep
) -> AgentRunner:
    raw = _read_bearer_token(request, settings)
    if not raw.startswith(RUNNER_TOKEN_PREFIX):
        raise RunnerTokenInvalidError
    async with session_factory() as session, session.begin():
        repository = AgentRunnerRepository(session)
        runner = await repository.find_active_by_hash(hash_token(raw))
        if runner is not None:
            await repository.touch_last_seen(runner.id, clock.now(), RUNNER_LAST_SEEN_INTERVAL)
    if runner is None:
        raise RunnerTokenInvalidError
    return runner


async def require_run_token(
    request: Request,
    run_id: Annotated[str, Path(description="AI 실행 id(uuid)")],
    settings: SettingsDep,
    session_factory: FactoryDep,
) -> uuid.UUID:
    raw = _read_bearer_token(request, settings)
    if not raw.startswith(RUN_TOKEN_PREFIX):
        raise RunnerTokenInvalidError
    async with session_factory() as session, session.begin():
        token_run_id = await AgentRunRepository(session).find_id_by_token_hash(hash_token(raw))
    if token_run_id is None or str(token_run_id) != run_id.lower():
        raise RunnerTokenInvalidError
    return token_run_id


AUTH_GUARDS.update({require_runner, require_run_token})

RunnerIdentity = Annotated[AgentRunner, Depends(require_runner)]
RunTokenRunId = Annotated[uuid.UUID, Depends(require_run_token)]
