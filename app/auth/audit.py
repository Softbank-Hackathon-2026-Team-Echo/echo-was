"""거부 기록: 403(ROLE_FORBIDDEN·AGENT_READ_ONLY·CANNOT_CHANGE_OWN_ROLE·ORIGIN_NOT_ALLOWED)마다
audit_logs 에 한 줄을 남긴다.

요청 트랜잭션과 따로 커밋한다(요청이 실패해도 기록은 남는다). 403 은 검사기에서도,
서비스(자기 역할 변경, 잠금 안 재확인)에서도 나므로 예외 처리기 한 곳에서 기록한다.
세션 팩토리·행위자·조직 id·시계는 검사기가 request.state 에 둔 것을 쓴다.
get_session_factory() 를 직접 부르지 않으므로 시험의 의존성 교체가 그대로 적용된다.
401·404 는 기록하지 않는다.
"""

import logging
import uuid
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.auth.actors import Actor, AgentActor, HumanActor
from app.core.exception_handlers import handle_app_error
from app.core.exceptions import AuditedForbiddenError
from app.models import AuditLog
from app.models.audit_log import AUDIT_ACTION_MAX_LENGTH, REQUEST_ID_MAX_LENGTH
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.organization_repository import OrganizationRepository

logger = logging.getLogger(__name__)


async def handle_audited_forbidden(request: Request, exc: AuditedForbiddenError) -> JSONResponse:
    await record_denial(request, exc.code)
    return await handle_app_error(request, exc)


async def record_denial(request: Request, reason: str) -> None:
    state = request.state
    session_factory = getattr(state, "session_factory", None)
    actor: Actor | None = getattr(state, "actor", None)
    if session_factory is None or actor is None:
        logger.error("denial not recorded: request has no actor", extra={"action": "record_denial"})
        return
    organization_id: uuid.UUID | None = getattr(state, "organization_id", None)
    clock = getattr(state, "clock", None)
    now = clock.now() if clock is not None else datetime.now(UTC)
    route = request.scope.get("route")
    template = getattr(route, "path", request.url.path)
    request_id = getattr(state, "request_id", None)
    try:
        async with session_factory() as session, session.begin():
            # 경로의 조직이 없을 수 있다(FK). 그때는 조직 없이 남긴다.
            if organization_id is not None and not await OrganizationRepository(session).exists(
                organization_id
            ):
                organization_id = None
            AuditLogRepository(session).add(
                AuditLog(
                    organization_id=organization_id,
                    actor_type=actor.actor_type,
                    actor_user_id=actor.user.id if isinstance(actor, HumanActor) else None,
                    actor_agent_token_id=actor.token.id if isinstance(actor, AgentActor) else None,
                    action=f"{request.method} {template}"[:AUDIT_ACTION_MAX_LENGTH],
                    path=request.url.path,
                    reason=reason,
                    request_id=request_id[:REQUEST_ID_MAX_LENGTH] if request_id else None,
                    created_at=now,
                )
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


def register_audit_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AuditedForbiddenError, handle_audited_forbidden)  # type: ignore[arg-type]
