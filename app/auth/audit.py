"""요청과 따로 커밋하는 감사 줄 두 가지: 거부(403)와 에이전트의 성공한 읽기.

거부는 요청이 실패해도 남아야 하므로 요청 트랜잭션과 따로 커밋한다. 403 은 검사기에서도, 서비스(자기
역할 변경, 잠금 안 재확인)에서도 나므로 예외 처리기 한 곳에서 기록한다. 에이전트 읽기는 응답 상태를
봐야 해서 미들웨어가 응답 뒤에 부른다. 기록에 실패해도 응답은 그대로다.

세션 팩토리·행위자·조직 id·시계는 검사기가 request.state 에 둔 것을 쓴다. get_session_factory() 를
직접 부르지 않으므로 시험의 의존성 교체가 그대로 적용된다. 401·404 와 사람의 읽기는 기록하지 않는다.
"""

import logging
import uuid
from http import HTTPMethod

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.types import Scope

from app.audit.actions import WRITE_ROUTE_ACTIONS, AuditAction
from app.audit.context import RequestContext, request_context_of
from app.audit.recorder import AuditActor, AuditRecorder
from app.auth.actors import Actor, AgentActor, HumanActor
from app.core.clock import Clock, SystemClock
from app.core.exception_handlers import handle_app_error
from app.core.exceptions import AuditedForbiddenError
from app.enums import AuditResult
from app.repositories.organization_repository import OrganizationRepository

logger = logging.getLogger(__name__)

_AGENT_READ_METHODS = frozenset({HTTPMethod.GET, HTTPMethod.HEAD})


async def handle_audited_forbidden(request: Request, exc: AuditedForbiddenError) -> JSONResponse:
    await record_denial(request, exc.code)
    return await handle_app_error(request, exc)


def _audit_actor(actor: Actor) -> AuditActor:
    if isinstance(actor, HumanActor):
        return AuditActor.user(actor.user.id)
    return AuditActor.agent(actor.token.id)


async def record_denial(request: Request, reason: str) -> None:
    state = request.state
    session_factory = getattr(state, "session_factory", None)
    actor: Actor | None = getattr(state, "actor", None)
    if session_factory is None or actor is None:
        logger.error("denial not recorded: request has no actor", extra={"action": "record_denial"})
        return
    organization_id: uuid.UUID | None = getattr(state, "organization_id", None)
    clock: Clock = getattr(state, "clock", None) or SystemClock()
    route = request.scope.get("route")
    template = getattr(route, "path", request.url.path)
    method_and_route = f"{request.method} {template}"
    action = WRITE_ROUTE_ACTIONS.get((request.method, template), method_and_route)
    context = RequestContext(
        route=method_and_route,
        path=request.url.path,
        request_id=getattr(state, "request_id", None),
    )
    try:
        async with session_factory() as session, session.begin():
            # 경로의 조직이 없을 수 있다(FK). 그때는 조직 없이 남긴다.
            if organization_id is not None and not await OrganizationRepository(session).exists(
                organization_id
            ):
                organization_id = None
            AuditRecorder(session, clock).record(
                organization_id=organization_id,
                actor=_audit_actor(actor),
                action=action,
                result=AuditResult.DENIED,
                reason=reason,
                ctx=context,
            )
    except SQLAlchemyError:
        logger.exception("denial not recorded", extra={"action": "record_denial", "reason": reason})
        return
    logger.info(
        "request denied",
        extra={
            "action": "record_denial",
            "reason": reason,
            "actor_type": actor.actor_type,
            "organization_id": str(organization_id) if organization_id else None,
        },
    )


async def record_agent_read(scope: Scope, status_code: int) -> None:
    """에이전트 토큰의 성공한 GET·HEAD(2xx)면 한 줄을 쓴다. 실패해도 예외를 올리지 않는다."""
    state = scope.get("state") or {}
    actor = state.get("actor")
    if not isinstance(actor, AgentActor) or not 200 <= status_code < 300:
        return
    session_factory = state.get("session_factory")
    if session_factory is None or scope["method"] not in _AGENT_READ_METHODS:
        return
    clock: Clock = state.get("clock") or SystemClock()
    organization_id: uuid.UUID = state.get("organization_id") or actor.organization_id
    try:
        async with session_factory() as session, session.begin():
            AuditRecorder(session, clock).record(
                organization_id=organization_id,
                actor=_audit_actor(actor),
                action=AuditAction.RESOURCE_READ,
                ctx=request_context_of(scope),
            )
    except Exception:
        logger.exception(
            "agent read not recorded",
            extra={"action": "record_agent_read", "agent_token_id": str(actor.token.id)},
        )


def register_audit_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AuditedForbiddenError, handle_audited_forbidden)  # type: ignore[arg-type]
