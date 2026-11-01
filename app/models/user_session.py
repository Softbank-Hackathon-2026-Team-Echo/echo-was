import uuid
from datetime import datetime

from sqlalchemy import CHAR, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UuidPrimaryKeyMixin

TOKEN_HASH_LENGTH = 64


class UserSession(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """브라우저 로그인 하나(`sessions` 표). 쿠키 값은 저장하지 않고 SHA-256(16진수 64자)만 둔다.

    만료 연장은 하지 않는다. 로그아웃하면 revoked_at 을 채운다.
    """

    __tablename__ = "sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(CHAR(TOKEN_HASH_LENGTH), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
