import uuid
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Identity, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import ActorType, AuditResult
from app.models.base import APPENDED_COLUMN_SORT_ORDER, Base, CreatedAtMixin, UuidPrimaryKeyMixin

_ACTOR_TYPE_VALUES = ", ".join(f"'{value.value}'" for value in ActorType)
_RESULT_VALUES = ", ".join(f"'{value.value}'" for value in AuditResult)
AUDIT_ACTION_MAX_LENGTH = 300
AUDIT_ROUTE_MAX_LENGTH = 300
AUDIT_REASON_MAX_LENGTH = 64
AUDIT_COMPONENT_MAX_LENGTH = 32
AUDIT_TARGET_TYPE_MAX_LENGTH = 32
AUDIT_TARGET_LABEL_MAX_LENGTH = 200
REQUEST_ID_MAX_LENGTH = 128


class AuditLog(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """행동 한 건(성공·실패·거부·에이전트 읽기). 지우지도 고치지도 않는다(DELETE·UPDATE 트리거).

    - 행위자는 사람(actor_user_id)·에이전트 토큰(actor_agent_token_id)·서버(component) 중 하나다.
    - action: 행동 이름(AuditAction). API 서버 6 때 쌓인 거부 줄은 `METHOD 경로 틀` 문자열 그대로다.
    - route: `METHOD 경로 틀`, path: 실제 경로. 요청 밖(일꾼)에서 쓴 줄은 NULL.
    - target_*: 대상 종류·id·기록할 때 복사한 이름(FK 없음. 대상이 지워져도 읽힌다).
    - reason: 거부 이유 코드(ROLE_FORBIDDEN 등). 거부 줄에만 있다.
    - seq: 정렬 보조 순번. 같은 시각 줄의 순서를 고정한다(id 는 uuid 라 무작위다).
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint(f"actor_type IN ({_ACTOR_TYPE_VALUES})", name="actor_type_valid"),
        CheckConstraint(f"result IN ({_RESULT_VALUES})", name="result_valid"),
        CheckConstraint(
            f"(actor_type = '{ActorType.USER.value}' AND actor_user_id IS NOT NULL "
            "AND actor_agent_token_id IS NULL AND component IS NULL) OR "
            f"(actor_type = '{ActorType.AGENT.value}' AND actor_agent_token_id IS NOT NULL "
            "AND actor_user_id IS NULL AND component IS NULL) OR "
            f"(actor_type = '{ActorType.SYSTEM.value}' AND actor_user_id IS NULL "
            "AND actor_agent_token_id IS NULL AND component IS NOT NULL)",
            name="actor_consistency",
        ),
        CheckConstraint(
            "((target_type IS NULL) = (target_id IS NULL)) "
            "AND (target_label IS NULL OR target_type IS NOT NULL)",
            name="target_consistency",
        ),
        Index("ix_audit_logs_organization_id_created_at", "organization_id", "created_at"),
        Index("ix_audit_logs_organization_id_target_id", "organization_id", "target_id"),
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True
    )
    actor_type: Mapped[ActorType] = mapped_column(String(16), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    actor_agent_token_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_tokens.id", ondelete="RESTRICT"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(AUDIT_ACTION_MAX_LENGTH), nullable=False)
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(AUDIT_REASON_MAX_LENGTH), nullable=True)
    # 옛 파드의 거부 기록 INSERT 가 result 를 넣지 않아도 되도록 서버 기본값을 남긴다.
    result: Mapped[AuditResult] = mapped_column(
        String(16),
        nullable=False,
        default=AuditResult.DENIED,
        server_default=text(f"'{AuditResult.DENIED.value}'"),
    )
    request_id: Mapped[str | None] = mapped_column(String(REQUEST_ID_MAX_LENGTH), nullable=True)
    seq: Mapped[int] = mapped_column(
        BigInteger,
        Identity(always=False),
        nullable=False,
        sort_order=APPENDED_COLUMN_SORT_ORDER,
    )
    component: Mapped[str | None] = mapped_column(
        String(AUDIT_COMPONENT_MAX_LENGTH), nullable=True, sort_order=APPENDED_COLUMN_SORT_ORDER
    )
    route: Mapped[str | None] = mapped_column(
        String(AUDIT_ROUTE_MAX_LENGTH), nullable=True, sort_order=APPENDED_COLUMN_SORT_ORDER
    )
    target_type: Mapped[str | None] = mapped_column(
        String(AUDIT_TARGET_TYPE_MAX_LENGTH), nullable=True, sort_order=APPENDED_COLUMN_SORT_ORDER
    )
    target_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True, sort_order=APPENDED_COLUMN_SORT_ORDER
    )
    target_label: Mapped[str | None] = mapped_column(
        String(AUDIT_TARGET_LABEL_MAX_LENGTH), nullable=True, sort_order=APPENDED_COLUMN_SORT_ORDER
    )
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
        sort_order=APPENDED_COLUMN_SORT_ORDER,
    )
