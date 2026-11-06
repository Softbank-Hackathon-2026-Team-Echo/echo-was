"""실제 재난 피드 순수 규칙(API 서버 18): USGS 읽기·거리·임계·중복 제거."""

import math
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.core.places import Coordinates
from app.services.disaster_rules import EnvironmentLocation, distance_km
from app.services.live_disaster_feed_rules import (
    BELOW_THRESHOLD,
    MAX_ALIASES,
    MAX_FEED_FEATURES,
    NO_AFFECTED_ENVIRONMENT,
    FeedEvent,
    FeedFormatError,
    evaluate_event,
    parse_usgs_feed,
    read_usgs_feed,
    remember,
    to_magnitude,
    unseen_events,
)

TOKYO = Coordinates(35.68, 139.69)
SEOUL = Coordinates(37.57, 126.98)
# 2026-11-06 실제 USGS 지진 us7000twg9: M6.1, 144 km SE of Hasaki, Japan(도쿄에서 약 234km).
HASAKI = (142.1175, 34.9677, 29.0)


def feature(
    event_id: str,
    mag: float | None,
    lng_lat_depth: tuple[float, float, float],
    *,
    time_ms: int = 1793961567869,
    kind: str = "earthquake",
    ids: str | None = None,
    place: str = "144 km SE of Hasaki, Japan",
) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": event_id,
        "properties": {
            "mag": mag,
            "place": place,
            "time": time_ms,
            "url": f"https://earthquake.usgs.gov/earthquakes/eventpage/{event_id}",
            "type": kind,
            "ids": ids if ids is not None else f",{event_id},",
        },
        "geometry": {"type": "Point", "coordinates": list(lng_lat_depth)},
    }


def collection(*features: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "metadata": {"count": len(features)},
        "features": list(features),
    }


def event(
    event_id: str = "us1", magnitude: str = "6.1", aliases: tuple[str, ...] = ()
) -> FeedEvent:
    return FeedEvent(
        provider="usgs",
        event_id=event_id,
        aliases=aliases,
        magnitude=Decimal(magnitude),
        epicenter=Coordinates(HASAKI[1], HASAKI[0]),
        depth_km=HASAKI[2],
        place=None,
        occurred_at=datetime(2026, 11, 6, tzinfo=UTC),
        url=None,
    )


def test_parse_usgs_reads_earthquakes_and_skips_others() -> None:
    payload = collection(
        feature("us7000twg9", 6.14, HASAKI, ids=",us7000twg9,at00abc,"),
        feature("ci1", 0.71, (-116.75, 33.58, 12.0), time_ms=1793961000000),
        feature("blast1", 2.0, (-110.0, 40.0, 0.0), kind="quarry blast"),
        feature("nomag", None, (0.0, 0.0, 0.0)),
        feature("micro", -0.4, (0.0, 0.0, 0.0)),
        {"type": "Feature", "id": "broken"},
    )

    events = parse_usgs_feed(payload)

    assert [item.event_id for item in events] == ["ci1", "us7000twg9"]  # 시각 오름차순
    hasaki = events[1]
    assert hasaki.key == "usgs:us7000twg9"
    assert hasaki.aliases == ("at00abc",)
    assert hasaki.magnitude == Decimal("6.1")
    assert hasaki.epicenter == Coordinates(34.9677, 142.1175)
    assert hasaki.depth_km == 29.0
    assert hasaki.occurred_at == datetime.fromtimestamp(1793961567.869, tz=UTC)
    assert hasaki.place == "144 km SE of Hasaki, Japan"


@pytest.mark.parametrize("payload", [None, [], {"type": "Feature"}, {"type": "FeatureCollection"}])
def test_parse_usgs_rejects_non_feature_collection(payload: Any) -> None:
    with pytest.raises(FeedFormatError):
        parse_usgs_feed(payload)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(6.05, Decimal("6.1")), (6.04, Decimal("6.0")), (0, Decimal("0.0")), (10.04, Decimal("10.0"))],
)
def test_to_magnitude_rounds_half_up_to_one_decimal(value: float, expected: Decimal) -> None:
    assert to_magnitude(value) == expected


@pytest.mark.parametrize(
    "value", [-0.1, 10.1, None, "6.0", True, float("nan"), float("inf"), 1e100, -1e100, 10**400]
)
def test_to_magnitude_rejects_out_of_range_and_non_numbers(value: Any) -> None:
    assert to_magnitude(value) is None


def test_haversine_distance_tokyo_to_real_hasaki_quake_is_about_234km() -> None:
    assert round(distance_km(TOKYO, Coordinates(HASAKI[1], HASAKI[0]))) == 234


def test_evaluate_event_matches_when_magnitude_and_radius_met() -> None:
    tokyo_id, seoul_id, unknown_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    locations = [
        EnvironmentLocation(tokyo_id, TOKYO),
        EnvironmentLocation(seoul_id, SEOUL),
        EnvironmentLocation(unknown_id, None),
    ]

    decision = evaluate_event(event(), locations, Decimal("6.0"), 300)

    assert decision.matched and decision.reason is None
    assert decision.classification.affected == (tokyo_id,)
    assert decision.classification.safe == (seoul_id,)
    assert decision.classification.unknown == (unknown_id,)
    assert decision.nearest_distance_km == pytest.approx(234, abs=1)


def test_evaluate_event_below_magnitude_threshold() -> None:
    locations = [EnvironmentLocation(uuid.uuid4(), TOKYO)]

    decision = evaluate_event(event(magnitude="5.9"), locations, Decimal("6.0"), 300)

    assert not decision.matched
    assert decision.reason == BELOW_THRESHOLD


def test_evaluate_event_no_environment_within_radius() -> None:
    locations = [EnvironmentLocation(uuid.uuid4(), TOKYO), EnvironmentLocation(uuid.uuid4(), None)]

    decision = evaluate_event(event(), locations, Decimal("6.0"), 200)

    assert not decision.matched
    assert decision.reason == NO_AFFECTED_ENVIRONMENT


def test_evaluate_event_boundary_distance_counts_as_affected() -> None:
    epicenter = Coordinates(HASAKI[1], HASAKI[0])
    exact = distance_km(TOKYO, epicenter)
    tokyo = EnvironmentLocation(uuid.uuid4(), TOKYO)

    assert evaluate_event(event(), [tokyo], Decimal("6.0"), int(exact) + 1).matched
    assert not evaluate_event(event(), [tokyo], Decimal("6.0"), int(exact)).matched


def test_unseen_events_skips_seen_ids_aliases_and_in_feed_duplicates() -> None:
    first = event("us1")
    renamed = event("us2", aliases=("us1",))  # 대표 id 가 바뀐 같은 지진
    fresh = event("us3")
    again = event("us3")

    assert unseen_events([first, renamed, fresh, again], ["usgs:us0"]) == [first, fresh]
    assert unseen_events([renamed], ["usgs:us1"]) == []


def test_remember_keeps_order_without_duplicates_and_trims_oldest() -> None:
    seen = ["usgs:a", "usgs:b"]

    assert remember(seen, [event("c", aliases=("a",))]) == ["usgs:a", "usgs:b", "usgs:c"]
    assert remember(seen, [event("c"), event("d")], limit=3) == ["usgs:b", "usgs:c", "usgs:d"]


# --- 입력 내성(PR #103 검증 반영): 이상한 지진 한 건은 그 건만 건너뛰고 센다 ---


@pytest.mark.parametrize(
    "bad",
    [
        feature("badtime", 6.0, HASAKI, time_ms=10**100),
        feature("negtime", 6.0, HASAKI, time_ms=-1),
        feature("hugemag", 1e100, HASAKI),
        feature("a" * 200, 6.0, HASAKI),  # feed_event_id 128자를 넘는다
        feature("badlat", 6.0, (139.69, 91.0, 2.0)),
        feature("badlng", 6.0, (181.0, 35.0, 2.0)),
        feature("nanlng", 6.0, (float("nan"), 35.0, 2.0)),
    ],
)
def test_read_usgs_skips_malicious_feature_and_keeps_valid_one(bad: dict[str, Any]) -> None:
    parsed = read_usgs_feed(collection(bad, feature("good", 6.0, HASAKI)))

    assert [item.event_id for item in parsed.events] == ["good"]
    assert parsed.skipped_count == 1


def test_read_usgs_counts_broken_features_but_not_non_earthquakes() -> None:
    parsed = read_usgs_feed(
        collection(
            feature("blast", 2.0, (-110.0, 40.0, 0.0), kind="quarry blast"),
            feature("nomag", None, HASAKI),
            {"type": "Feature", "id": "broken"},
            "not-an-object",  # type: ignore[arg-type]
            feature("good", 6.0, HASAKI),
        )
    )

    assert [item.event_id for item in parsed.events] == ["good"]
    assert parsed.skipped_count == 3


def test_read_usgs_drops_too_long_aliases_and_caps_alias_count() -> None:
    many = ",".join(f"a{index}" for index in range(MAX_ALIASES + 10))
    ids = f",good,{'x' * 200},{many},"

    (parsed,) = parse_usgs_feed(collection(feature("good", 6.0, HASAKI, ids=ids)))

    assert len(parsed.aliases) == MAX_ALIASES
    assert all(len(key) <= 128 for key in parsed.keys)


def test_read_usgs_rejects_too_many_features() -> None:
    payload = collection(*[feature(f"e{index}", 1.0, HASAKI) for index in range(2)])
    payload["features"] = payload["features"] * (MAX_FEED_FEATURES // 2 + 1)

    with pytest.raises(FeedFormatError):
        read_usgs_feed(payload)


def test_evaluate_event_includes_environment_exactly_on_radius() -> None:
    """반경 위의 환경은 부동소수 오차(300.00000000000006)가 있어도 피해다(1m 여유)."""
    quake = replace(event(magnitude="6.0"), epicenter=Coordinates(0, 0))
    on_edge = EnvironmentLocation(uuid.uuid4(), Coordinates(0, math.degrees(300 / 6371)))
    beyond = EnvironmentLocation(uuid.uuid4(), Coordinates(0, math.degrees(300.01 / 6371)))

    assert distance_km(on_edge.coordinates, quake.epicenter) > 300  # type: ignore[arg-type]
    assert evaluate_event(quake, [on_edge], Decimal("6.0"), 300).matched
    assert not evaluate_event(quake, [beyond], Decimal("6.0"), 300).matched
