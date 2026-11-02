import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import DeployLogStage, LogLevel
from app.models.base import Base, CreatedAtMixin, UuidPrimaryKeyMixin

_LEVEL_VALUES = ", ".join(f"'{value.value}'" for value in LogLevel)
DEPLOY_LOG_MESSAGE_MAX_LENGTH = 500
DEPLOY_LOG_CODE_MAX_LENGTH = 48
PREVENT_DELETE_TRIGGER = "trg_deploy_request_logs_prevent_delete"
SEQ_UNIQUE_CONSTRAINT = "uq_deploy_request_logs_deploy_request_id_seq"


class DeployRequestLog(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """배포 작업 로그 한 줄. 지우지 않는다(DELETE 금지 트리거).

    - 그 줄을 일으킨 상태 전환(또는 요청 삽입)과 같은 트랜잭션에서, 반영됐을 때만 쓴다.
    - seq 는 요청마다 1 부터 빈틈없이 늘어난다. 서비스 행 잠금을 쥔 채 max(seq) + 1 로 정한다.
    - logged_at 은 주입한 시계 값이다(DB now() 를 쓰지 않는다).
    - message 는 코드별 고정 문장이다. 외부 응답 원문·토큰을 넣지 않는다.
    """

    __tablename__ = "deploy_request_logs"
    __table_args__ = (
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint(f"level IN ({_LEVEL_VALUES})", name="level_valid"),
        UniqueConstraint("deploy_request_id", "seq"),
    )

    deploy_request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deploy_requests.id", ondelete="RESTRICT"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    logged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    level: Mapped[LogLevel] = mapped_column(String(8), nullable=False)
    stage: Mapped[DeployLogStage] = mapped_column(String(16), nullable=False)
    code: Mapped[str] = mapped_column(String(DEPLOY_LOG_CODE_MAX_LENGTH), nullable=False)
    message: Mapped[str] = mapped_column(String(DEPLOY_LOG_MESSAGE_MAX_LENGTH), nullable=False)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False)
