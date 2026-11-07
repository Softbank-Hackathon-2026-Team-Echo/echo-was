from datetime import datetime
from typing import Literal

from app.schemas.response import ApiModel


class HistoricalEarthquakeResponse(ApiModel):
    event_id: str
    lat: float
    lng: float
    magnitude: float
    origin_source: Literal["REAL_HISTORICAL"]
    source: Literal["USGS_FDSN"]
    occurred_at: datetime
    collected_at: datetime
    source_url: str
    raw_sha256: str
    shake_map_url: str | None
    detail_source_url: str
    detail_raw_sha256: str


class DemoEnvironmentResponse(ApiModel):
    id: str
    label: str
    lat: float
    lng: float
    role: str


class DemoTimelineResponse(ApiModel):
    stage: str
    offset_seconds: int
    source_weight: int
    target_weight: int


class DisasterHistoryItemResponse(ApiModel):
    id: str
    is_simulated: Literal[True]
    source: Literal["DEMO"]
    collected_at: datetime
    occurred_at: datetime
    scenario_label: str
    live_state: Literal["DEMO"]
    enabled: bool
    block_reason: str | None
    radius_km: int
    radius_source: Literal["DEMO_ASSUMPTION"]
    event: HistoricalEarthquakeResponse
    environments: list[DemoEnvironmentResponse]
    timeline: list[DemoTimelineResponse]
    mock_migration_seconds: int | None


class DisasterHistoryResponse(ApiModel):
    enabled: bool
    is_simulated: Literal[True] = True
    source: Literal["DEMO"] = "DEMO"
    items: list[DisasterHistoryItemResponse]
