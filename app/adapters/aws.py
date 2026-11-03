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


@register_environment_adapter("aws")
class AwsEnvironmentAdapter(SimulatedEnvironmentAdapter):
    """AWS(EKS) 동작 클래스 — **가짜(simulated)**. 네트워크 호출 없이 고정 값을 돌려준다.

    실제 EKS 접속은 인프라 파트가 클러스터를 만든 뒤 별도 작업에서 이 클래스를 바꾼다.
    """

    simulated_server_version = "v1.31.0"
    simulated_node_count = 3
