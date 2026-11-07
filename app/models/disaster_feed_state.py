from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

FEED_PROVIDER_MAX_LENGTH = 32
FEED_ERROR_CODE_MAX_LENGTH = 64


class DisasterFeedState(TimestampMixin, Base):
    """실제 재난 피드 하나의 조회 상태와 이미 본 지진 id(API 서버 18). 피드마다 한 행이다.

    - 일꾼이 조회할 때마다 고친다. API(`GET /disasters/feed`)는 읽기만 한다(일꾼과 API 는 다른
      프로세스).
    - seen_event_ids 는 최근에 처리한 피드 지진 id 목록(중복 제거 기억, 최근 것만 남김)이다.
      같은 지진이 두 조직·두 일꾼에서 겹쳐도 재난은 disasters 의 부분 유니크 인덱스가 하나로 막는다.
    - 시연 주입은 이 행을 바꾸지 않는다(실제 피드 상태와 섞지 않는다).
    """

    __tablename__ = "disaster_feed_states"
    __table_args__ = (
        CheckConstraint("jsonb_typeof(seen_event_ids) = 'array'", name="seen_event_ids_is_array"),
        CheckConstraint(
            "last_feed_count >= 0 AND last_new_event_count >= 0 AND last_skipped_count >= 0",
            name="counts_non_negative",
        ),
    )

    provider: Mapped[str] = mapped_column(String(FEED_PROVIDER_MAX_LENGTH), primary_key=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(
        String(FEED_ERROR_CODE_MAX_LENGTH), nullable=True
    )
    last_feed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_new_event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 마지막 응답에서 이상해서 건너뛴 지진 수(시각·규모·id·좌표가 범위 밖, 처리 실패).
    last_skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    seen_event_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
