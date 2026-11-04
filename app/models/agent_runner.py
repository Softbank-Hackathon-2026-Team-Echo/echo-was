from datetime import datetime

from sqlalchemy import CHAR, CheckConstraint, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UuidPrimaryKeyMixin

AGENT_RUNNER_NAME_MAX_LENGTH = 63


class AgentRunner(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """샌드박스의 AI 러너 신원(WAS ↔ 러너 계약 v1 §3.2). 조직에 묶이지 않는 플랫폼 신원이다.

    - 러너 토큰(`echo_rnr_…`)은 어드민 명령으로만 발급하고 SHA-256 해시만 둔다.
    - 이 신원은 실행 가져가기만 한다. 실행 하나의 제출은 그 실행 토큰으로 한다.
    """

    __tablename__ = "agent_runners"
    __table_args__ = (
        CheckConstraint("name ~ '^[a-z0-9]([-a-z0-9]*[a-z0-9])?$'", name="name_format"),
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_hash_format"),
    )

    name: Mapped[str] = mapped_column(
        String(AGENT_RUNNER_NAME_MAX_LENGTH), nullable=False, unique=True
    )
    token_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False, unique=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
