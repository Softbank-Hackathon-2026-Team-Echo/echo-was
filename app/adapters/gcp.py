from typing import Annotated

from pydantic import StringConstraints

from app.adapters.base import EnvironmentConfig, NonEmptyStr, register_environment_kind

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
