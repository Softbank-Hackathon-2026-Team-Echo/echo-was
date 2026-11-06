"""실제 재난 피드 순수 규칙: USGS GeoJSON 읽기·중복 제거·조직 기준 판정(API 서버 18).

DB·시계·HTTP 를 모른다.

- 피드: USGS Earthquake Hazards Program 요약 피드(GeoJSON FeatureCollection, 인증 없음, 1분 갱신).
  `properties.type == "earthquake"` 이고 규모가 있는 지진만 읽는다(채석 발파·폭발은 뺀다).
  좌표는 `[경도, 위도, 깊이 km]` 순서다.
- 중복 제거: 지진 한 건은 여러 관측망 id 를 가질 수 있다(`properties.ids` = ",us7000abcd,ci123,").
  대표 id 가 나중에 바뀌어도 같은 지진으로 보도록 모든 id 를 기억하고 하나라도 본 적 있으면
  건너뛴다.
- 판정: 규모 ≥ 조직 기준 규모 ∧ 조직 환경 하나 이상이 진원지에서 조직 기준 반경 안(하버사인, 경계
  포함).
  피해·안전·모름 분류는 버튼 재난과 같은 함수(classify_environments)를 반경만 바꿔 쓴다.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.core.places import Coordinates
from app.services.disaster_rules import (
    EnvironmentClassification,
    EnvironmentLocation,
    classify_environments,
    distance_km,
)

USGS_PROVIDER = "usgs"
INJECT_PROVIDER = "inject"
# 기억할 최근 피드 지진 id 수. 지난 1시간 피드는 보통 수십 건이라 넉넉하다.
SEEN_EVENT_ID_LIMIT = 2000
MIN_MAGNITUDE = Decimal("0.0")
MAX_MAGNITUDE = Decimal("10.0")
_MAGNITUDE_STEP = Decimal("0.1")
_PLACE_MAX_LENGTH = 200
_URL_MAX_LENGTH = 500
_DISTANCE_DIGITS = 1

NO_AFFECTED_ENVIRONMENT = "NO_AFFECTED_ENVIRONMENT"
BELOW_THRESHOLD = "BELOW_THRESHOLD"


class FeedFormatError(ValueError):
    """피드 응답이 GeoJSON FeatureCollection 이 아니다."""


@dataclass(frozen=True)
class FeedEvent:
    """피드 지진 한 건. key 는 `<provider>:<id>` 이고 aliases 는 같은 지진의 다른 id 다."""

    provider: str
    event_id: str
    aliases: tuple[str, ...]
    magnitude: Decimal
    epicenter: Coordinates
    depth_km: float | None
    place: str | None
    occurred_at: datetime
    url: str | None

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.event_id}"

    @property
    def keys(self) -> tuple[str, ...]:
        """대표 id 와 다른 id 모두(중복 제거용)."""
        ordered = dict.fromkeys([self.key, *(f"{self.provider}:{alias}" for alias in self.aliases)])
        return tuple(ordered)

    def summary(self, nearest_distance_km: float | None) -> dict[str, Any]:
        """disasters.feed_event·응답 feedEvent 에 그대로 싣는 요약."""
        return {
            "provider": self.provider,
            "eventId": self.event_id,
            "place": self.place,
            "occurredAt": self.occurred_at.isoformat(),
            "depthKm": self.depth_km,
            "url": self.url,
            "nearestDistanceKm": nearest_distance_km,
        }


@dataclass(frozen=True)
class FeedDecision:
    """조직 하나에 대한 판정. matched 면 재난을 만든다."""

    matched: bool
    reason: str | None
    classification: EnvironmentClassification
    nearest_distance_km: float | None


def parse_usgs_feed(payload: Any) -> list[FeedEvent]:
    """USGS GeoJSON → 지진 목록(일어난 시각 오름차순). 읽을 수 없는 지진 한 건은 건너뛴다."""
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise FeedFormatError("feed is not a GeoJSON FeatureCollection")
    features = payload.get("features")
    if not isinstance(features, list):
        raise FeedFormatError("feed has no features list")
    events = [event for event in (_parse_feature(item) for item in features) if event is not None]
    return sorted(events, key=lambda event: (event.occurred_at, event.event_id))


def _parse_feature(feature: Any) -> FeedEvent | None:
    if not isinstance(feature, dict):
        return None
    properties = feature.get("properties")
    geometry = feature.get("geometry")
    event_id = feature.get("id")
    if not isinstance(properties, dict) or not isinstance(geometry, dict):
        return None
    if not isinstance(event_id, str) or not event_id:
        return None
    if properties.get("type", "earthquake") != "earthquake":
        return None
    magnitude = to_magnitude(properties.get("mag"))
    coordinates = geometry.get("coordinates")
    occurred_ms = properties.get("time")
    if magnitude is None or not isinstance(coordinates, list) or len(coordinates) < 2:
        return None
    if not isinstance(occurred_ms, int | float) or isinstance(occurred_ms, bool):
        return None
    lng, lat = coordinates[0], coordinates[1]
    if not _is_number(lng) or not _is_number(lat):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    depth = coordinates[2] if len(coordinates) > 2 and _is_number(coordinates[2]) else None
    return FeedEvent(
        provider=USGS_PROVIDER,
        event_id=event_id,
        aliases=_aliases(properties.get("ids"), event_id),
        magnitude=magnitude,
        epicenter=Coordinates(float(lat), float(lng)),
        depth_km=float(depth) if depth is not None else None,
        place=_text(properties.get("place"), _PLACE_MAX_LENGTH),
        occurred_at=datetime.fromtimestamp(occurred_ms / 1000, tz=UTC),
        url=_text(properties.get("url"), _URL_MAX_LENGTH),
    )


def to_magnitude(value: Any) -> Decimal | None:
    """소수 한 자리로 반올림(0.05 는 올림). 0 미만(미소 지진)·10 초과·숫자 아님은 None."""
    if not _is_number(value):
        return None
    magnitude = Decimal(str(value)).quantize(_MAGNITUDE_STEP, rounding=ROUND_HALF_UP)
    if magnitude < MIN_MAGNITUDE or magnitude > MAX_MAGNITUDE:
        return None
    return magnitude


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _aliases(ids: Any, event_id: str) -> tuple[str, ...]:
    if not isinstance(ids, str):
        return ()
    return tuple(item for item in dict.fromkeys(ids.split(",")) if item and item != event_id)


def _text(value: Any, max_length: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:max_length]


def unseen_events(events: Iterable[FeedEvent], seen: Iterable[str]) -> list[FeedEvent]:
    """id 하나라도 본 적 있는 지진은 뺀다. 같은 응답 안의 겹침도 하나만 남긴다."""
    remembered = set(seen)
    fresh: list[FeedEvent] = []
    for event in events:
        if remembered.intersection(event.keys):
            continue
        fresh.append(event)
        remembered.update(event.keys)
    return fresh


def remember(
    seen: Sequence[str], events: Iterable[FeedEvent], limit: int = SEEN_EVENT_ID_LIMIT
) -> list[str]:
    """기억 목록에 지진 id 를 더하고 최근 limit 개만 남긴다(오래된 것부터 버린다)."""
    merged = list(dict.fromkeys([*seen, *(key for event in events for key in event.keys)]))
    return merged[-limit:]


def evaluate_event(
    event: FeedEvent,
    locations: Iterable[EnvironmentLocation],
    min_magnitude: Decimal,
    radius_km: int,
) -> FeedDecision:
    """조직 기준으로 지진 하나를 판정한다. 규모 미달이어도 분류는 계산해 돌려준다(로그용)."""
    location_list = list(locations)
    classification = classify_environments(location_list, event.epicenter, radius_km)
    distances = [
        distance_km(location.coordinates, event.epicenter)
        for location in location_list
        if location.coordinates is not None
    ]
    nearest = round(min(distances), _DISTANCE_DIGITS) if distances else None
    if event.magnitude < min_magnitude:
        return FeedDecision(False, BELOW_THRESHOLD, classification, nearest)
    if not classification.affected:
        return FeedDecision(False, NO_AFFECTED_ENVIRONMENT, classification, nearest)
    return FeedDecision(True, None, classification, nearest)
