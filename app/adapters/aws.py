from typing import Annotated

from pydantic import StringConstraints

from app.adapters.base import EnvironmentConfig, NonEmptyStr, register_environment_kind

# 예: ap-northeast-2, us-east-1, us-gov-west-1
AwsRegion = Annotated[str, StringConstraints(pattern=r"^[a-z]{2}(?:-[a-z]+)+-\d{1,2}$")]


@register_environment_kind("aws")
class AwsEnvironmentConfig(EnvironmentConfig):
    """AWS(EKS) 환경. 접속 자격 증명은 여기 두지 않고 환경 행의 secret_ref 로 찾는다."""

    region: AwsRegion
    cluster_name: NonEmptyStr

    def display_location(self) -> str:
        return self.region
