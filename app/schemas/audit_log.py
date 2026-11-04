import uuid
from datetime import datetime
from typing import Any

from app.enums import ActorType, AuditResult
from app.schemas.response import ApiModel
from app.services.audit_log_service import AuditLogView


class AuditActorResponse(ApiModel):
    type: ActorType
    user_id: uuid.UUID | None
    github_login: str | None
    agent_token_id: uuid.UUID | None
    agent_token_name: str | None
    component: str | None


class AuditTargetResponse(ApiModel):
    type: str
    id: uuid.UUID
    label: str | None


class AuditLogResponse(ApiModel):
    id: uuid.UUID
    at: datetime
    actor: AuditActorResponse
    # 행동 이름. API 서버 6 때 쌓인 거부 줄은 `METHOD 경로 틀` 문자열이라 Enum 으로 묶지 않는다.
    action: str
    route: str | None
    path: str | None
    target: AuditTargetResponse | None
    result: AuditResult
    reason: str | None
    details: dict[str, Any]
    request_id: str | None

    @classmethod
    def from_view(cls, view: AuditLogView) -> "AuditLogResponse":
        row = view.audit_log
        return cls(
            id=row.id,
            at=row.created_at,
            actor=AuditActorResponse(
                type=row.actor_type,
                user_id=row.actor_user_id,
                github_login=view.github_login,
                agent_token_id=row.actor_agent_token_id,
                agent_token_name=view.agent_token_name,
                component=row.component,
            ),
            action=row.action,
            route=row.route,
            path=row.path,
            target=(
                AuditTargetResponse(type=row.target_type, id=row.target_id, label=row.target_label)
                if row.target_type is not None and row.target_id is not None
                else None
            ),
            result=row.result,
            reason=row.reason,
            details=row.details,
            request_id=row.request_id,
        )
