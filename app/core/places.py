"""환경 위치 글자(리전·도시 코드) → 좌표 표. 좌표는 저장하지 않고 응답 때 이 표에서 찾는다.

표를 환경 종류별로 나눈다. aws 설정이 도시 키를, onprem 설정이 리전 키를 찾지 않게 하기 위해서다.
새 리전·도시는 해당 표에 한 줄 더한다. 프론트 지도 표(echo-frontend `src/lib/geo.ts` PLACES)와
어긋나면 서버 값이 이긴다(프론트가 서버 값을 먼저 쓴다).
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import NamedTuple


class Coordinates(NamedTuple):
    lat: float
    lng: float


AWS_REGION_COORDINATES: Mapping[str, Coordinates] = MappingProxyType(
    {
        "ap-northeast-1": Coordinates(35.68, 139.69),
        "ap-northeast-2": Coordinates(37.57, 126.98),
        "ap-northeast-3": Coordinates(34.69, 135.5),
        "us-east-1": Coordinates(38.9, -77.4),
        "us-west-2": Coordinates(45.6, -121.2),
        "eu-west-1": Coordinates(53.3, -6.3),
    }
)

GCP_REGION_COORDINATES: Mapping[str, Coordinates] = MappingProxyType(
    {
        "asia-northeast1": Coordinates(35.68, 139.69),
        "asia-northeast2": Coordinates(34.69, 135.5),
        "asia-northeast3": Coordinates(37.57, 126.98),
    }
)

# 키는 소문자다. 찾을 때 입력을 소문자로 바꾼다.
ONPREM_CITY_COORDINATES: Mapping[str, Coordinates] = MappingProxyType(
    {
        "tokyo": Coordinates(35.68, 139.69),
        "osaka": Coordinates(34.69, 135.5),
        "fukuoka": Coordinates(33.59, 130.4),
        "seoul": Coordinates(37.57, 126.98),
        "busan": Coordinates(35.18, 129.08),
    }
)


def find_aws_region_coordinates(region: str) -> Coordinates | None:
    return AWS_REGION_COORDINATES.get(region)


def find_gcp_region_coordinates(region: str) -> Coordinates | None:
    return GCP_REGION_COORDINATES.get(region)


def find_onprem_city_coordinates(location: str) -> Coordinates | None:
    """온프레 location 은 자유 문구라 대소문자를 가리지 않고 도시 표에서만 찾는다."""
    return ONPREM_CITY_COORDINATES.get(location.lower())
