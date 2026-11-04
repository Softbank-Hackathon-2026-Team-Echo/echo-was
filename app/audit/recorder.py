"""감사 로그 한 줄을 쓰는 곳. 모든 경로(사람·일꾼·거부·에이전트 읽기)가 이 함수를 쓴다.

- session.add 만 한다. 커밋은 부른 쪽의 트랜잭션이 한다. 그래서 작업이 되돌려지면 줄도 함께
  사라지고, 줄을 쓰지 못하면 작업도 저장되지 않는다.
- 값은 명시적으로 받는다(`OrganizationAccess` 를 모른다). 일꾼·초대 적용처럼 요청 행위자가 없는
  호출자도 같은 함수를 쓰고, AI 러너 행동도 AuditActor.system("ai-runner") 로 쓴다.
- 시각은 주입한 시계로 정한다(DB now() 에 기대지 않는다).
- 문자열은 칸 길이로 자른다. 긴 이름 때문에 작업 자체가 실패하면 안 된다.
- details 에 비밀값·토큰 원문을 넣지 않는다.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.actions import AuditTargetType
from app.audit.context import RequestContext, current_request_context
from app.core.clock import Clock
from app.enums import ActorType, AuditResult
from app.models.audit_log import (
    AUDIT_ACTION_MAX_LENGTH,
    AUDIT_COMPONENT_MAX_LENGTH,
    AUDIT_REASON_MAX_LENGTH,
    AUDIT_ROUTE_MAX_LENGTH,
    AUDIT_TARGET_LABEL_MAX_LENGTH,
    AUDIT_TARGET_TYPE_MAX_LENGTH,
    REQUEST_ID_MAX_LENGTH,
    AuditLog,
)
from app.repositories.audit_log_repository import AuditLogRepository


@dataclass(frozen=True)
class AuditActor:
    actor_type: ActorType
    user_id: uuid.UUID | None = None
    agent_token_id: uuid.UUID | None = None
    component: str | None = None

    @classmethod
    def user(cls, user_id: uuid.UUID) -> "AuditActor":
        return cls(ActorType.USER, user_id=user_id)

    @classmethod
    def agent(cls, agent_token_id: uuid.UUID) -> "AuditActor":
        return cls(ActorType.AGENT, agent_token_id=agent_token_id)

    @classmethod
    def system(cls, component: str) -> "AuditActor":
        return cls(ActorType.SYSTEM, component=component)


@dataclass(frozen=True)
class AuditTarget:
    """무엇에 한 행동인가. label 은 기록할 때 복사해 두는 사람이 읽는 이름이다(FK 없음)."""

    type: AuditTargetType
    id: uuid.UUID
    label: str | None = None


def truncate(value: str | None, max_length: int) -> str | None:
    return value[:max_length] if value is not None else None


class AuditRecorder:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._repository = AuditLogRepository(session)
        self._clock = clock

    def record(
        self,
        *,
        organization_id: uuid.UUID | None,
        actor: AuditActor,
        action: str,
        result: AuditResult = AuditResult.SUCCEEDED,
        target: AuditTarget | None = None,
        details: dict[str, Any] | None = None,
        reason: str | None = None,
        ctx: RequestContext | None = None,
    ) -> AuditLog:
        """ctx 를 주지 않으면 지금 요청의 값을 쓴다(요청 밖이면 route·path·request_id 는 NULL)."""
        context = ctx if ctx is not None else current_request_context()
        audit_log = AuditLog(
            organization_id=organization_id,
            actor_type=actor.actor_type,
            actor_user_id=actor.user_id,
            actor_agent_token_id=actor.agent_token_id,
            component=truncate(actor.component, AUDIT_COMPONENT_MAX_LENGTH),
            action=action[:AUDIT_ACTION_MAX_LENGTH],
            route=truncate(context.route, AUDIT_ROUTE_MAX_LENGTH) if context else None,
            path=context.path if context else None,
            target_type=(
                target.type.value[:AUDIT_TARGET_TYPE_MAX_LENGTH] if target is not None else None
            ),
            target_id=target.id if target is not None else None,
            target_label=(
                truncate(target.label, AUDIT_TARGET_LABEL_MAX_LENGTH)
                if target is not None
                else None
            ),
            details=details if details is not None else {},
            reason=truncate(reason, AUDIT_REASON_MAX_LENGTH),
            result=result,
            request_id=truncate(context.request_id, REQUEST_ID_MAX_LENGTH) if context else None,
            created_at=self._clock.now(),
        )
        self._repository.add(audit_log)
        return audit_log
