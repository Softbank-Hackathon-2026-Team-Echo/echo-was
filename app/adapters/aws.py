from typing import Annotated

from pydantic import StringConstraints

from app.adapters.base import (
    EnvironmentConfig,
    NonEmptyStr,
    SimulatedEnvironmentAdapter,
    register_environment_adapter,
    register_environment_kind,
)

# 예: ap-northeast-2, us-east-1, us-gov-west-1
AwsRegion = Annotated[str, StringConstraints(pattern=r"^[a-z]{2}(?:-[a-z]+)+-\d{1,2}$")]


@register_environment_kind("aws")
class AwsEnvironmentConfig(EnvironmentConfig):
    """AWS(EKS) 환경. 접속 자격 증명은 여기 두지 않고 환경 행의 secret_ref 로 찾는다."""

    region: AwsRegion
    cluster_name: NonEmptyStr

    def display_location(self) -> str:
        return self.region


@register_environment_adapter("aws")
class AwsEnvironmentAdapter(SimulatedEnvironmentAdapter):
    """AWS(EKS) 동작 클래스 — **가짜(simulated)**. 네트워크 호출 없이 고정 값을 돌려준다.

    실제 EKS 접속은 인프라 파트가 클러스터를 만든 뒤 별도 작업에서 이 클래스를 바꾼다.
    """

    simulated_server_version = "v1.31.0"
    simulated_node_count = 3
