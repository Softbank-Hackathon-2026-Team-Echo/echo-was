from typing import Annotated

from pydantic import StringConstraints

from app.adapters.base import (
    DomainName,
    EnvironmentConfig,
    KubernetesName,
    NonEmptyStr,
    SimulatedEnvironmentAdapter,
    register_environment_adapter,
    register_environment_kind,
)

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
    """온프레미스(k3s 등) 환경. location 은 사람이 읽는 설치 위치(예: team-laptop)다.

    선택 칸(실제 실행기): api_url(터널로 연 쿠버네티스 창구), tunnel_target(앱 주소 CNAME 대상
    `<터널 id>.cfargotunnel.com`), ingress_class(비면 traefik). 창구 토큰은 환경 행의 secret_ref 다.
    """

    location: NonEmptyStr
    kube_context: NonEmptyStr
    api_url: HttpsUrl | None = None
    tunnel_target: DomainName | None = None
    ingress_class: KubernetesName | None = None

    def display_location(self) -> str:
        return self.location


@register_environment_adapter("onprem")
class OnpremEnvironmentAdapter(SimulatedEnvironmentAdapter):
    """온프레미스(k3s) 동작 클래스 — **가짜(simulated)**. 네트워크 호출 없이 고정 값을 돌려준다.

    실제 k3s 접속은 인프라 파트가 클러스터를 만든 뒤 별도 작업에서 이 클래스를 바꾼다.
    """

    simulated_server_version = "v1.31.4+k3s1"
    simulated_node_count = 1
