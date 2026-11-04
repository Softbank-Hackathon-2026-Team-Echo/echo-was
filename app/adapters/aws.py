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
from app.core.places import Coordinates, find_aws_region_coordinates

# 예: ap-northeast-2, us-east-1, us-gov-west-1
AwsRegion = Annotated[str, StringConstraints(pattern=r"^[a-z]{2}(?:-[a-z]+)+-\d{1,2}$")]
# 예: arn:aws:iam::123456789012:role/echo-dev-executor
IamRoleArn = Annotated[
    str, StringConstraints(pattern=r"^arn:aws[-a-z]*:iam::\d{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}$")
]
# 예: arn:aws:acm:ap-northeast-1:123456789012:certificate/<uuid>
AcmCertificateArn = Annotated[
    str,
    StringConstraints(pattern=r"^arn:aws[-a-z]*:acm:[a-z0-9-]+:\d{12}:certificate/[A-Za-z0-9-]+$"),
]


@register_environment_kind("aws")
class AwsEnvironmentConfig(EnvironmentConfig):
    """AWS(EKS) 환경. 접속 자격 증명은 두지 않는다(실행기는 워크로드 신원으로 역할을 넘겨받는다).

    선택 칸(실제 실행기): role_arn(비면 설정 EXECUTOR_AWS_ROLE_ARN), ingress_domain(환경별
    주소의 하위 도메인), ingress_group(ALB 그룹), certificate_arn(ALB HTTPS 인증서),
    ingress_class(비면 alb).
    """

    region: AwsRegion
    cluster_name: NonEmptyStr
    role_arn: IamRoleArn | None = None
    ingress_domain: DomainName | None = None
    ingress_group: KubernetesName | None = None
    certificate_arn: AcmCertificateArn | None = None
    ingress_class: KubernetesName | None = None

    def display_location(self) -> str:
        return self.region

    def environment_host_domain(self) -> str | None:
        return self.ingress_domain

    def coordinates(self) -> Coordinates | None:
        return find_aws_region_coordinates(self.region)


@register_environment_adapter("aws")
class AwsEnvironmentAdapter(ClusterBackedEnvironmentAdapter):
    """AWS(EKS) 동작 클래스(API 서버 12).

    `ENVIRONMENT_ADAPTER_MODE=real` 이면 reader 가 주입되어 배포 실행기와 같은 접근 경로(구글 신분증
    → STS 역할 → EKS 토큰)로 `/version`·노드 수를 읽는다. 아니면 **가짜(simulated)** 고정 값이다.
    secretRef 의 값(운영은 자리 표시 비밀)은 읽지 않는다.
    """

    simulated_server_version = "v1.31.0"
    simulated_node_count = 3
