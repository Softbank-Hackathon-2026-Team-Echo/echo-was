import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.enums import DisasterSource, DisasterStatus, DisasterType
from app.models.base import (
    APPENDED_COLUMN_SORT_ORDER,
    Base,
    TimestampMixin,
    UuidPrimaryKeyMixin,
)

PREVENT_DELETE_TRIGGER = "trg_disasters_prevent_delete"
MIN_MAGNITUDE = 0
MAX_MAGNITUDE = 10
FEED_EVENT_ID_MAX_LENGTH = 128


def _values(enum: type[StrEnum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum)


class Disaster(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """재난 이벤트 한 건: 진원지·규모와 그 순간 서버가 계산한 반경·피해/안전/모름 환경.

    - 사람이 버튼으로 낸 시뮬레이션(source = SIMULATED)과 실제 지진 피드가 만든 재난(LIVE_FEED),
      시연용으로 피드에 넣은 가짜 지진(FEED_INJECTED)이 있다(API 서버 18). 이전 자체는 실제일 수
      있다.
    - 피드 재난은 만든 사람이 없고(created_by_user_id NULL) 피드 원본
      id·요약(feed_event_id·feed_event)이
      있다. 같은 조직에 같은 피드 지진의 재난은 하나뿐이다(부분 유니크 인덱스).
    - 환경 id 목록은 만든 순간 스냅숏이다. 환경이 나중에 바뀌어도 고치지 않는다.
    - 상태는 조건부 UPDATE 로만 바꾼다(ACTIVE → ENDED). 지우지 않는다(DELETE 금지 트리거).
    - 이 재난으로 만든 이전 계획은 migration_plans.disaster_id 로 찾는다.
    """

    __tablename__ = "disasters"
    __table_args__ = (
        CheckConstraint(f"type IN ({_values(DisasterType)})", name="type_valid"),
        CheckConstraint(f"source IN ({_values(DisasterSource)})", name="source_valid"),
        CheckConstraint(f"status IN ({_values(DisasterStatus)})", name="status_valid"),
        CheckConstraint(
            f"magnitude BETWEEN {MIN_MAGNITUDE} AND {MAX_MAGNITUDE}", name="magnitude_range"
        ),
        CheckConstraint("epicenter_lat BETWEEN -90 AND 90", name="epicenter_lat_range"),
        CheckConstraint("epicenter_lng BETWEEN -180 AND 180", name="epicenter_lng_range"),
        CheckConstraint("radius_km >= 0", name="radius_km_non_negative"),
        CheckConstraint(
            f"(status = '{DisasterStatus.ENDED.value}') = (ended_at IS NOT NULL)",
            name="ended_at_consistency",
        ),
        CheckConstraint(
            "jsonb_typeof(affected_environment_ids) = 'array'",
            name="affected_environment_ids_is_array",
        ),
        CheckConstraint(
            "jsonb_typeof(safe_environment_ids) = 'array'", name="safe_environment_ids_is_array"
        ),
        CheckConstraint(
            "jsonb_typeof(unknown_environment_ids) = 'array'",
            name="unknown_environment_ids_is_array",
        ),
        Index(
            "ix_disasters_organization_id_status_created_at",
            "organization_id",
            "status",
            "created_at",
        ),
        # API 서버 18: 버튼 재난만 사람이 만들고 피드 원본이 없다. 피드 재난은 그 반대다.
        CheckConstraint(
            f"(source = '{DisasterSource.SIMULATED.value}') = (created_by_user_id IS NOT NULL)",
            name="creator_matches_source",
        ),
        CheckConstraint(
            f"(source = '{DisasterSource.SIMULATED.value}') = (feed_event_id IS NULL)",
            name="feed_event_matches_source",
        ),
        CheckConstraint(
            "feed_event IS NULL OR jsonb_typeof(feed_event) = 'object'",
            name="feed_event_is_object",
        ),
        # 같은 피드 지진으로 한 조직에 재난이 두 번 생기지 않는다(일꾼이 여럿이어도).
        Index(
            "uq_disasters_organization_id_feed_event_id",
            "organization_id",
            "feed_event_id",
            unique=True,
            postgresql_where=text("feed_event_id IS NOT NULL"),
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    type: Mapped[DisasterType] = mapped_column(String(16), nullable=False)
    source: Mapped[DisasterSource] = mapped_column(
        String(16),
        nullable=False,
        default=DisasterSource.SIMULATED,
        server_default=DisasterSource.SIMULATED.value,
    )
    magnitude: Mapped[Decimal] = mapped_column(Numeric(3, 1), nullable=False)
    epicenter_lat: Mapped[float] = mapped_column(Double, nullable=False)
    epicenter_lng: Mapped[float] = mapped_column(Double, nullable=False)
    radius_km: Mapped[int] = mapped_column(Integer, nullable=False)
    meets_threshold: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[DisasterStatus] = mapped_column(String(16), nullable=False)
    affected_environment_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    safe_environment_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    unknown_environment_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    ended_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # API 서버 18: 피드 원본(예 `usgs:us7000twg9`, 시연 주입은 `inject:<hex>`)과 그 요약
    # {provider, eventId, place, occurredAt, depthKm, url, distanceKm}. 버튼 재난은 둘 다 NULL.
    feed_event_id: Mapped[str | None] = mapped_column(
        String(FEED_EVENT_ID_MAX_LENGTH), nullable=True, sort_order=APPENDED_COLUMN_SORT_ORDER
    )
    feed_event: Mapped[dict[str, object] | None] = mapped_column(
        JSONB, nullable=True, sort_order=APPENDED_COLUMN_SORT_ORDER
    )
