from typing import Annotated

from pydantic import StringConstraints

from app.adapters.base import (
    ClusterBackedEnvironmentAdapter,
    DomainName,
    EnvironmentConfig,
    KubernetesName,
    NonEmptyStr,
    register_environment_adapter,
    register_environment_kind,
)
from app.core.places import Coordinates, find_gcp_region_coordinates

# GCP 프로젝트 ID 규칙: 6~30자, 소문자로 시작, 소문자·숫자·하이픈, 하이픈으로 끝나지 않음.
GcpProjectId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")]
# 예: asia-northeast3, us-central1, europe-west1
GcpRegion = Annotated[str, StringConstraints(pattern=r"^[a-z]+-[a-z]+\d{1,2}$")]
# GKE 위치: 리전(asia-northeast3) 또는 존(asia-northeast3-a).
GcpLocation = Annotated[str, StringConstraints(pattern=r"^[a-z]+-[a-z]+\d{1,2}(?:-[a-z])?$")]


@register_environment_kind("gcp")
class GcpEnvironmentConfig(EnvironmentConfig):
    """GCP(GKE) 환경. 접속 자격 증명은 두지 않는다(실행기는 워크로드 신원 토큰을 쓴다).

    선택 칸(실제 실행기): cluster_location(존 클러스터면 존, 비면 region), ingress_domain(환경별
    주소의 하위 도메인), ingress_class(비면 nginx).
    """

    project_id: GcpProjectId
    region: GcpRegion
    cluster_name: NonEmptyStr
    cluster_location: GcpLocation | None = None
    ingress_domain: DomainName | None = None
    ingress_class: KubernetesName | None = None

    def display_location(self) -> str:
        return self.region

    def environment_host_domain(self) -> str | None:
        return self.ingress_domain

    def coordinates(self) -> Coordinates | None:
        return find_gcp_region_coordinates(self.region)


@register_environment_adapter("gcp")
class GcpEnvironmentAdapter(ClusterBackedEnvironmentAdapter):
    """GCP(GKE) 동작 클래스(API 서버 12).

    `ENVIRONMENT_ADAPTER_MODE=real` 이면 reader 가 주입되어 배포 실행기와 같은 접근 경로(워크로드
    신원 액세스 토큰 + GKE API)로 `/version`·노드 수를 읽는다. 아니면 **가짜(simulated)** 고정
    값이다. secretRef 의 값은 읽지 않는다.
    """

    simulated_server_version = "v1.31.1-gke.1000"
    simulated_node_count = 3
