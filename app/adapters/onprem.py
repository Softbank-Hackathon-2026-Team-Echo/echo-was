from typing import Annotated

from pydantic import Field, StringConstraints

from app.adapters.base import (
    ClusterBackedEnvironmentAdapter,
    DomainName,
    EnvironmentConfig,
    KubernetesName,
    NonEmptyStr,
    register_environment_adapter,
    register_environment_kind,
)
from app.core.places import ONPREM_CITY_COORDINATES, Coordinates, find_onprem_city_coordinates

# 공개 인증서가 붙은 https 주소(경로 없음). 예: https://k3s-k2f6xje5.echoport.uk
HttpsUrl = Annotated[
    str,
    StringConstraints(
        max_length=200,
        pattern=r"^https://[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)+(?::\d{1,5})?$",
    ),
]


@register_environment_kind("onprem")
class OnpremEnvironmentConfig(EnvironmentConfig):
    """온프레미스(k3s 등) 환경. location 은 사람이 읽는 설치 위치(예: seoul, team-laptop)다.

    location 이 도시 표에 있는 코드면 좌표가 나오고, 그 밖의 글자는 좌표 없는 이름표로 남는다.
    선택 칸(실제 실행기): api_url(터널로 연 쿠버네티스 창구), tunnel_target(앱 주소 CNAME 대상
    `<터널 id>.cfargotunnel.com`), ingress_class(비면 traefik). 창구 토큰은 환경 행의 secret_ref 다.
    """

    location: Annotated[
        NonEmptyStr,
        Field(
            description=(
                "Install location. A city code "
                f"({', '.join(ONPREM_CITY_COORDINATES)}; case-insensitive) "
                "places the environment on the map. "
                "Any other text is kept as a label without coordinates."
            ),
            examples=["seoul", "team-laptop"],
        ),
    ]
    kube_context: NonEmptyStr
    api_url: HttpsUrl | None = None
    tunnel_target: DomainName | None = None
    ingress_class: KubernetesName | None = None

    def display_location(self) -> str:
        return self.location

    def coordinates(self) -> Coordinates | None:
        return find_onprem_city_coordinates(self.location)


@register_environment_adapter("onprem")
class OnpremEnvironmentAdapter(ClusterBackedEnvironmentAdapter):
    """온프레미스(k3s) 동작 클래스(API 서버 12).

    `ENVIRONMENT_ADAPTER_MODE=real` 이면 reader 가 주입되어 `apiUrl` 로 붙고, secretRef 에서
    서비스가 조회해 넘긴 서비스 계정 토큰을 쓴다. 아니면 **가짜(simulated)** 고정 값이다.
    """

    uses_credentials_as_token = True

    simulated_server_version = "v1.31.4+k3s1"
    simulated_node_count = 1
