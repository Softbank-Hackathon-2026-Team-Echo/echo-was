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


class DisasterResponse(ApiModel):
    """재난 한 건. 재난은 늘 시뮬레이션(simulated=true)이고, 이전 계획의 simulated 는 따로다."""

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
    created_by: UserRefResponse
    ended_by: UserRefResponse | None
    started_at: datetime
    ended_at: datetime | None
    plans: list[DisasterPlanResponse]

    @classmethod
    def from_view(cls, view: DisasterView) -> Self:
        disaster = view.disaster
        created_by = UserRefResponse.from_model(view.user(disaster.created_by_user_id))
        assert created_by is not None
        return cls(
            id=disaster.id,
            type=disaster.type,
            source=disaster.source,
            simulated=disaster.source == DisasterSource.SIMULATED,
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
        )
