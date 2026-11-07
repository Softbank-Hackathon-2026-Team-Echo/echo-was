"""재난 API 요청·응답 스키마.

JSON 이름은 camelCase, 모든 응답 칸은 항상 있고 값이 없으면 null.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Self

from pydantic import Field

from app.core.places import Coordinates
from app.enums import (
    DisasterPlanOutcome,
    DisasterSource,
    DisasterStatus,
    DisasterType,
    LiveFeedConnectionStatus,
    MigrationPlanStatus,
)
from app.models.disaster import MAX_MAGNITUDE, MIN_MAGNITUDE
from app.schemas.environment import CoordinatesResponse
from app.schemas.migration_plan import UserRefResponse
from app.schemas.response import ApiModel, ApiRequestModel
from app.services.disaster_service import (
    DisasterInput,
    DisasterPlanResult,
    DisasterPreview,
    DisasterView,
)
from app.services.live_disaster_feed import FEED_NAME, FeedStatusView, InjectResult

# numeric(3,1) 칸에 그대로 들어가는 값만 받는다(소수 한 자리).
MAGNITUDE_MAX_DIGITS = 3
MAGNITUDE_DECIMAL_PLACES = 1
MIN_LAT, MAX_LAT = -90.0, 90.0
MIN_LNG, MAX_LNG = -180.0, 180.0


class EpicenterRequest(ApiRequestModel):
    lat: float = Field(ge=MIN_LAT, le=MAX_LAT)
    lng: float = Field(ge=MIN_LNG, le=MAX_LNG)


class CreateDisasterRequest(ApiRequestModel):
    """재난 버튼 한 번. 서버가 반경·피해 환경을 계산하고 긴급 이전 계획을 만든다."""

    type: DisasterType
    epicenter: EpicenterRequest
    magnitude: Annotated[
        Decimal,
        Field(
            ge=MIN_MAGNITUDE,
            le=MAX_MAGNITUDE,
            max_digits=MAGNITUDE_MAX_DIGITS,
            decimal_places=MAGNITUDE_DECIMAL_PLACES,
        ),
    ]

    def to_input(self) -> DisasterInput:
        return DisasterInput(
            type=self.type,
            magnitude=self.magnitude,
            epicenter=Coordinates(self.epicenter.lat, self.epicenter.lng),
        )


class DisasterPreviewResponse(ApiModel):
    """저장하지 않는 계산 결과. unknown 은 좌표를 몰라 판정하지 못한 환경이다."""

    radius_km: int
    meets_threshold: bool
    affected: list[uuid.UUID]
    safe: list[uuid.UUID]
    unknown: list[uuid.UUID]

    @classmethod
    def from_preview(cls, preview: DisasterPreview) -> Self:
        classification = preview.classification
        return cls(
            radius_km=preview.radius_km,
            meets_threshold=preview.meets_threshold,
            affected=list(classification.affected),
            safe=list(classification.safe),
            unknown=list(classification.unknown),
        )


class DisasterPlanResponse(ApiModel):
    """서비스 하나의 트리거 결과. planStatus 는 그 계획의 실제 상태다(계획이 없으면 null).

    만들기 응답은 그 순간 결과다. 조회는 이 재난이 만든 계획만 싣는다(건너뛴 서비스는 저장되지
    않는다).
    """

    service_id: uuid.UUID
    plan_id: uuid.UUID | None
    outcome: DisasterPlanOutcome
    reason: str | None
    target_environment_id: uuid.UUID | None
    plan_status: MigrationPlanStatus | None

    @classmethod
    def from_result(cls, result: DisasterPlanResult) -> Self:
        return cls(
            service_id=result.service_id,
            plan_id=result.plan_id,
            outcome=result.outcome,
            reason=result.reason,
            target_environment_id=result.target_environment_id,
            plan_status=result.plan_status,
        )


class FeedEventResponse(ApiModel):
    """피드 재난의 원본 지진 요약. provider 는 usgs(실제) 또는 inject(시연 주입)다."""

    provider: str
    event_id: str
    place: str | None
    occurred_at: datetime | None
    depth_km: float | None
    url: str | None
    nearest_distance_km: float | None
    auto_dr_attempts: dict[str, dict[str, object]] = Field(default_factory=dict)
    collected_at: datetime | None = None

    @classmethod
    def from_summary(cls, summary: dict[str, object] | None) -> Self | None:
        if summary is None:
            return None
        return cls.model_validate(summary)


class DisasterResponse(ApiModel):
    """재난 한 건. simulated 는 source 가 LIVE_FEED 가 아니면 true 다(버튼·시연 주입).

    피드 재난(LIVE_FEED·FEED_INJECTED)은 createdBy 가 null 이고 feedEvent 가 있다. 이전 계획의
    simulated 는
    따로다.
    """

    id: uuid.UUID
    type: DisasterType
    source: DisasterSource
    simulated: bool
    magnitude: float
    epicenter: CoordinatesResponse
    radius_km: int
    meets_threshold: bool
    status: DisasterStatus
    affected_environment_ids: list[uuid.UUID]
    safe_environment_ids: list[uuid.UUID]
    unknown_environment_ids: list[uuid.UUID]
    created_by: UserRefResponse | None
    ended_by: UserRefResponse | None
    started_at: datetime
    ended_at: datetime | None
    plans: list[DisasterPlanResponse]
    feed_event: FeedEventResponse | None
    origin_source: str
    radius_source: str

    @classmethod
    def from_view(cls, view: DisasterView) -> Self:
        disaster = view.disaster
        created_by = UserRefResponse.from_model(view.user(disaster.created_by_user_id))
        return cls(
            id=disaster.id,
            type=disaster.type,
            source=disaster.source,
            simulated=disaster.source != DisasterSource.LIVE_FEED,
            magnitude=float(disaster.magnitude),
            epicenter=CoordinatesResponse(lat=disaster.epicenter_lat, lng=disaster.epicenter_lng),
            radius_km=disaster.radius_km,
            meets_threshold=disaster.meets_threshold,
            status=disaster.status,
            affected_environment_ids=[
                uuid.UUID(item) for item in disaster.affected_environment_ids
            ],
            safe_environment_ids=[uuid.UUID(item) for item in disaster.safe_environment_ids],
            unknown_environment_ids=[uuid.UUID(item) for item in disaster.unknown_environment_ids],
            created_by=created_by,
            ended_by=UserRefResponse.from_model(view.user(disaster.ended_by_user_id)),
            started_at=disaster.started_at,
            ended_at=disaster.ended_at,
            plans=[DisasterPlanResponse.from_result(result) for result in view.plans],
            feed_event=FeedEventResponse.from_summary(disaster.feed_event),
            origin_source=(
                "LIVE_USGS" if disaster.source == DisasterSource.LIVE_FEED else "SYNTHETIC"
            ),
            radius_source=(
                "ORGANIZATION_POLICY"
                if disaster.source != DisasterSource.SIMULATED
                else "DEMO_ASSUMPTION"
            ),
        )


class LiveFeedThresholdResponse(ApiModel):
    """조직 기준: 규모 minMagnitude 이상 ∧ 환경이 진원지에서 radiusKm 안이면 재난을 만든다."""

    min_magnitude: float
    radius_km: int


class LiveFeedStatusResponse(ApiModel):
    """실제 재난 피드 상태(API 서버 18). 프론트 LIVE_FEED({name, ready})를 그대로 바꿀 수 있는
    모양이다.

    ready = connectionStatus 가 CONNECTED. evacuation 은 피드 재난의 대피 계획 처리 방식이다(지금은
    SUGGESTED 하나 — 사람이 evacuate 를 불러야 계획이 생긴다).
    """

    name: str
    provider: str
    ready: bool
    enabled: bool
    connection_status: LiveFeedConnectionStatus
    feed_url: str
    poll_interval_seconds: int
    last_polled_at: datetime | None
    last_success_at: datetime | None
    last_error_code: str | None
    last_feed_count: int
    last_new_event_count: int
    last_skipped_count: int
    threshold: LiveFeedThresholdResponse
    evacuation: str
    automatic_enabled: bool = False

    @classmethod
    def from_view(cls, view: FeedStatusView) -> Self:
        return cls(
            name=FEED_NAME,
            provider="USGS",
            ready=view.connection_status is LiveFeedConnectionStatus.CONNECTED,
            enabled=view.enabled,
            connection_status=view.connection_status,
            feed_url=view.feed_url,
            poll_interval_seconds=view.poll_interval_seconds,
            last_polled_at=view.last_polled_at,
            last_success_at=view.last_success_at,
            last_error_code=view.last_error_code,
            last_feed_count=view.last_feed_count,
            last_new_event_count=view.last_new_event_count,
            last_skipped_count=view.last_skipped_count,
            threshold=LiveFeedThresholdResponse(
                min_magnitude=float(view.min_magnitude), radius_km=view.radius_km
            ),
            evacuation="PREAUTHORIZED" if view.automatic_enabled else "SUGGESTED",
            automatic_enabled=view.automatic_enabled,
        )


class InjectFeedEventRequest(ApiRequestModel):
    """시연용 가짜 지진 한 건. 피드와 같은 경로로 이 조직에만 흐른다(source = FEED_INJECTED)."""

    epicenter: EpicenterRequest
    magnitude: Annotated[
        Decimal,
        Field(
            ge=MIN_MAGNITUDE,
            le=MAX_MAGNITUDE,
            max_digits=MAGNITUDE_MAX_DIGITS,
            decimal_places=MAGNITUDE_DECIMAL_PLACES,
        ),
    ]
    place: Annotated[str | None, Field(default=None, min_length=1, max_length=120)] = None


class InjectFeedEventResponse(ApiModel):
    """matched=false 면 재난을 만들지 않았고 reason(BELOW_THRESHOLD·NO_AFFECTED_ENVIRONMENT)이
    있다."""

    feed_event_id: str
    simulated: bool
    matched: bool
    reason: str | None
    disaster: DisasterResponse | None

    @classmethod
    def build(cls, result: InjectResult, disaster: DisasterView | None) -> Self:
        return cls(
            feed_event_id=result.event.key,
            simulated=True,
            matched=result.outcome.disaster_id is not None,
            reason=result.outcome.reason,
            disaster=DisasterResponse.from_view(disaster) if disaster is not None else None,
        )
