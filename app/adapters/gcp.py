from typing import Annotated

from pydantic import StringConstraints

from app.adapters.base import (
    EnvironmentConfig,
    NonEmptyStr,
    SimulatedEnvironmentAdapter,
    register_environment_adapter,
    register_environment_kind,
)

# GCP 프로젝트 ID 규칙: 6~30자, 소문자로 시작, 소문자·숫자·하이픈, 하이픈으로 끝나지 않음.
GcpProjectId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")]
# 예: asia-northeast3, us-central1, europe-west1
GcpRegion = Annotated[str, StringConstraints(pattern=r"^[a-z]+-[a-z]+\d{1,2}$")]


@register_environment_kind("gcp")
class GcpEnvironmentConfig(EnvironmentConfig):
    """GCP(GKE) 환경. 접속 자격 증명은 여기 두지 않고 환경 행의 secret_ref 로 찾는다."""

    project_id: GcpProjectId
    region: GcpRegion
    cluster_name: NonEmptyStr

    def display_location(self) -> str:
        return self.region


@register_environment_adapter("gcp")
class GcpEnvironmentAdapter(SimulatedEnvironmentAdapter):
    """GCP(GKE) 동작 클래스 — **가짜(simulated)**. 네트워크 호출 없이 고정 값을 돌려준다.

    실제 GKE 접속은 인프라 파트가 클러스터를 만든 뒤 별도 작업에서 이 클래스를 바꾼다.
    """

    simulated_server_version = "v1.31.1-gke.1000"
    simulated_node_count = 3
