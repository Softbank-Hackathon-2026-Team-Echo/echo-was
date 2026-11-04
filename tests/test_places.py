"""환경 좌표 표와 찾는 규칙(SPEC api-5 AC-6·AC-7, DB 없음)."""

import pytest
from pydantic import ValidationError

from app.adapters.aws import AwsEnvironmentConfig
from app.adapters.gcp import GcpEnvironmentConfig
from app.adapters.onprem import OnpremEnvironmentConfig
from app.core.places import (
    AWS_REGION_COORDINATES,
    GCP_REGION_COORDINATES,
    ONPREM_CITY_COORDINATES,
    Coordinates,
)


def _aws(region: str) -> AwsEnvironmentConfig:
    return AwsEnvironmentConfig(region=region, cluster_name="echo-eks")


def _gcp(region: str, cluster_location: str | None = None) -> GcpEnvironmentConfig:
    return GcpEnvironmentConfig(
        project_id="echo-hack-123",
        region=region,
        cluster_name="echo-gke",
        cluster_location=cluster_location,
    )


def _onprem(location: str) -> OnpremEnvironmentConfig:
    return OnpremEnvironmentConfig(location=location, kube_context="k3d-echo")


def test_coordinate_tables_match_spec_values_exactly() -> None:
    assert dict(AWS_REGION_COORDINATES) == {
        "ap-northeast-1": (35.68, 139.69),
        "ap-northeast-2": (37.57, 126.98),
        "ap-northeast-3": (34.69, 135.5),
        "us-east-1": (38.9, -77.4),
        "us-west-2": (45.6, -121.2),
        "eu-west-1": (53.3, -6.3),
    }
    assert dict(GCP_REGION_COORDINATES) == {
        "asia-northeast1": (35.68, 139.69),
        "asia-northeast2": (34.69, 135.5),
        "asia-northeast3": (37.57, 126.98),
    }
    assert dict(ONPREM_CITY_COORDINATES) == {
        "tokyo": (35.68, 139.69),
        "osaka": (34.69, 135.5),
        "fukuoka": (33.59, 130.4),
        "seoul": (37.57, 126.98),
        "busan": (35.18, 129.08),
    }


def test_coordinates_known_location_returns_table_value() -> None:
    assert _aws("ap-northeast-1").coordinates() == Coordinates(lat=35.68, lng=139.69)
    assert _gcp("asia-northeast3").coordinates() == Coordinates(lat=37.57, lng=126.98)
    assert _onprem("seoul").coordinates() == Coordinates(lat=37.57, lng=126.98)


def test_coordinates_unknown_location_returns_none() -> None:
    assert _aws("ap-southeast-1").coordinates() is None
    assert _gcp("us-central1").coordinates() is None
    assert _onprem("team-laptop").coordinates() is None


@pytest.mark.parametrize("location", ["seoul", "Seoul", "SEOUL"])
def test_onprem_coordinates_ignores_case(location: str) -> None:
    config = _onprem(location)

    assert config.coordinates() == Coordinates(lat=37.57, lng=126.98)
    assert config.display_location() == location


def test_gcp_coordinates_uses_region_not_cluster_location() -> None:
    config = _gcp("asia-northeast1", cluster_location="asia-northeast3-a")

    assert config.coordinates() == Coordinates(lat=35.68, lng=139.69)


def test_aws_region_pattern_rejects_city_key() -> None:
    with pytest.raises(ValidationError):
        _aws("tokyo")


@pytest.mark.parametrize("location", ["ap-northeast-1", "asia-northeast3"])
def test_onprem_coordinates_does_not_look_up_region_keys(location: str) -> None:
    assert _onprem(location).coordinates() is None


def test_onprem_location_schema_adds_hint_and_keeps_validation() -> None:
    schema = OnpremEnvironmentConfig.model_json_schema(by_alias=True)
    location = schema["properties"]["location"]

    assert all(city in location["description"] for city in ONPREM_CITY_COORDINATES)
    assert location["examples"] == ["seoul", "team-laptop"]
    assert location["minLength"] == 1
    assert location["pattern"] == r"^\S(?:.*\S)?$"
    assert "location" in schema["required"]
    for invalid in ("", " seoul", "seoul "):
        with pytest.raises(ValidationError):
            _onprem(invalid)
