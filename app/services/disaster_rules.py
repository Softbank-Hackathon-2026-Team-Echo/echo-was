"""재난 판정 순수 규칙: 규모 기준·반경·거리·피해/안전/모름 분류·도착 고르기(API 서버 9).

DB·시계·설정을 모른다. 숫자는 이 파일의 상수이고 환경 변수로 바꾸지 않는다.

- 지진은 규모 7.0 이상이면 이전 대상, 태풍은 규모와 상관없이 대상이다.
- 반경은 `50 · e^(0.9 · (M − 5.2))` km 를 정수 km 로 반올림한 값이다(M7.0 → 253km, M7.2 → 302km).
  0.5 는 올린다. 프론트 `Math.round` 와 같은 값이 나와야 화면 원과 판정이 같다.
- 거리는 하버사인(지구 반지름 6371km, 프론트 `geo.ts` 의 distanceKm 와 같은 식)이고, 거리가 반경과
  같으면 피해다. 좌표를 모르는 환경은 피해·안전 어느 쪽도 아니다.
"""

import math
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from app.core.places import Coordinates
from app.enums import DisasterPlanOutcome, DisasterType, MigrationPlanStatus

EARTHQUAKE_MIN_MAGNITUDE = Decimal("7.0")
EARTH_RADIUS_KM = 6371.0
_RADIUS_BASE_KM = 50.0
_RADIUS_GROWTH = 0.9
_RADIUS_BASE_MAGNITUDE = 5.2
DISTANCE_LOG_DIGITS = 1


def meets_threshold(disaster_type: DisasterType, magnitude: Decimal) -> bool:
    """이전을 시작할 재난인가. 태풍은 기준이 없다(항상 대상)."""
    if disaster_type is DisasterType.TYPHOON:
        return True
    return magnitude >= EARTHQUAKE_MIN_MAGNITUDE


def radius_km(magnitude: Decimal) -> int:
    raw = _RADIUS_BASE_KM * math.exp(_RADIUS_GROWTH * (float(magnitude) - _RADIUS_BASE_MAGNITUDE))
    return round_half_up(raw)


def round_half_up(value: float) -> int:
    """JavaScript `Math.round` 와 같은 반올림. 파이썬 round() 는 0.5 를 짝수 쪽으로 보낸다."""
    return math.floor(value + 0.5)


def distance_km(a: Coordinates, b: Coordinates) -> float:
    delta_lat = math.radians(b.lat - a.lat)
    delta_lng = math.radians(b.lng - a.lng)
    h = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(math.radians(a.lat))
        * math.cos(math.radians(b.lat))
        * math.sin(delta_lng / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


@dataclass(frozen=True)
class EnvironmentLocation:
    environment_id: uuid.UUID
    coordinates: Coordinates | None


@dataclass(frozen=True)
class EnvironmentClassification:
    affected: tuple[uuid.UUID, ...]
    safe: tuple[uuid.UUID, ...]
    unknown: tuple[uuid.UUID, ...]


def classify_environments(
    locations: Iterable[EnvironmentLocation], epicenter: Coordinates, radius: float
) -> EnvironmentClassification:
    """반경 안은 피해, 밖은 안전, 좌표를 모르면 모름. 입력 순서를 유지한다."""
    affected: list[uuid.UUID] = []
    safe: list[uuid.UUID] = []
    unknown: list[uuid.UUID] = []
    for location in locations:
        if location.coordinates is None:
            unknown.append(location.environment_id)
        elif distance_km(location.coordinates, epicenter) <= radius:
            affected.append(location.environment_id)
        else:
            safe.append(location.environment_id)
    return EnvironmentClassification(tuple(affected), tuple(safe), tuple(unknown))


@dataclass(frozen=True)
class TargetCandidate:
    """서비스의 예비 연결 하나와 그 환경의 값. 안전 여부는 부르는 쪽이 이미 걸렀다."""

    environment_id: uuid.UUID
    coordinates: Coordinates
    image_digest: str
    is_connection_failed: bool
    has_entrypoint_name: bool


def choose_target(
    candidates: Iterable[TargetCandidate], source_image_digest: str, epicenter: Coordinates
) -> TargetCandidate | None:
    """접속 실패가 아니고 진입점 이름이 있는 후보 중 출발과 같은 이미지에서 진원지에서 가장 먼 곳.

    같은 이미지가 하나도 없으면 가장 먼 후보를 돌려준다. 계획 만들기가 이유(TARGET_NOT_READY·
    TARGET_SYNCING)를 판정하게 하기 위해서다. 후보가 없으면 None(NO_SAFE_TARGET). 거리가 같으면
    환경 id 가 작은 쪽이다.
    """
    eligible = [
        candidate
        for candidate in candidates
        if not candidate.is_connection_failed and candidate.has_entrypoint_name
    ]
    same_image = [item for item in eligible if item.image_digest == source_image_digest]
    pool = same_image or eligible
    if not pool:
        return None
    return min(
        pool, key=lambda item: (-distance_km(item.coordinates, epicenter), item.environment_id)
    )


def summarize_plan(status: MigrationPlanStatus, is_approved: bool) -> DisasterPlanOutcome:
    """계획 상태 → plans[].outcome. 승인 없이 닫힌 계획(거절·취소·만료·낡음)은 SKIPPED 다."""
    if status is MigrationPlanStatus.PENDING_APPROVAL:
        return DisasterPlanOutcome.PENDING_APPROVAL
    return DisasterPlanOutcome.APPROVED if is_approved else DisasterPlanOutcome.SKIPPED
