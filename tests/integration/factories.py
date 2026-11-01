"""통합 테스트용 데이터. docs/schema.md ③의 행 예시(shop-api 가 AWS 90%·GCP 10%)와 같은 모양이다."""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import DeploymentStatus
from app.models import Deployment, Environment, Service, ServiceEnvironment

SHOP_API_IMAGE_REPOSITORY = "ghcr.io/team-echo/shop-api"
SHOP_API_DIGEST = "sha256:" + "4f" * 32

ENVIRONMENT_CONFIGS: dict[str, tuple[str, dict[str, object]]] = {
    "aws-seoul": ("aws", {"region": "ap-northeast-2", "cluster_name": "echo-eks"}),
    "gcp-seoul": (
        "gcp",
        {"project_id": "echo-hack-123", "region": "asia-northeast3", "cluster_name": "echo-gke"},
    ),
    "onprem-k3s": ("onprem", {"location": "team-laptop", "kube_context": "k3d-echo"}),
}


def make_digest(fill: str) -> str:
    return "sha256:" + (fill * 64)[:64]


@dataclass(frozen=True)
class ShopApiExample:
    service: Service
    environments: dict[str, Environment]
    aws_link: ServiceEnvironment
    gcp_link: ServiceEnvironment
    aws_deployment: Deployment
    gcp_deployment: Deployment


PUBLIC_BASE_DOMAIN = "apps.echo.test"


def make_service(name: str, description: str | None = None) -> Service:
    """서비스 행. 원터치 배포 칸(저장소·대표 주소)을 채운다."""
    return Service(
        name=name,
        description=description,
        repo_url=f"https://github.com/team-echo/{name}",
        repo_private=False,
        public_host=f"{name}.{PUBLIC_BASE_DOMAIN}",
        simulated=True,
    )


def make_link(
    service: Service, environment: Environment, traffic_weight: int = 0
) -> ServiceEnvironment:
    """서비스-환경 연결 행. 환경별 주소를 채운다."""
    return ServiceEnvironment(
        service_id=service.id,
        environment_id=environment.id,
        traffic_weight=traffic_weight,
        public_host=f"{service.name}.{environment.name}.{PUBLIC_BASE_DOMAIN}",
    )


def secret_ref_for(name: str) -> str:
    """env 저장소 형식의 비밀 참조. 예: aws-seoul → env:ECHO_SECRET_AWS_SEOUL_KUBECONFIG"""
    return f"env:ECHO_SECRET_{name.upper().replace('-', '_')}_KUBECONFIG"


def make_environment(name: str) -> Environment:
    kind, config = ENVIRONMENT_CONFIGS[name]
    return Environment(name=name, kind=kind, config=dict(config), secret_ref=secret_ref_for(name))


async def add_succeeded_deployment(
    session: AsyncSession, link: ServiceEnvironment, digest: str = SHOP_API_DIGEST
) -> Deployment:
    now = datetime.now(UTC)
    deployment = Deployment(
        service_environment_id=link.id,
        image_repository=SHOP_API_IMAGE_REPOSITORY,
        image_digest=digest,
        config_snapshot={"replicas": 2, "env": {"DB_PASSWORD": {"secret_ref": "shop-api/db"}}},
        status=DeploymentStatus.SUCCEEDED,
        started_at=now,
        finished_at=now,
    )
    session.add(deployment)
    await session.flush()
    return deployment


async def seed_shop_api_example(session: AsyncSession) -> ShopApiExample:
    """shop-api 가 같은 이미지를 AWS 90%·GCP 10%로 돌리는 순간. 온프레 환경은 등록만 돼 있다."""
    service = make_service("shop-api", description="데모 쇼핑몰 API")
    environments = {name: make_environment(name) for name in ENVIRONMENT_CONFIGS}
    session.add_all([service, *environments.values()])
    await session.flush()

    applied_at = datetime.now(UTC)
    aws_link = make_link(service, environments["aws-seoul"], traffic_weight=90)
    aws_link.traffic_applied_at = applied_at
    gcp_link = make_link(service, environments["gcp-seoul"], traffic_weight=10)
    gcp_link.traffic_applied_at = applied_at
    session.add_all([aws_link, gcp_link])
    await session.flush()

    aws_deployment = await add_succeeded_deployment(session, aws_link)
    gcp_deployment = await add_succeeded_deployment(session, gcp_link)
    aws_link.current_deployment_id = aws_deployment.id
    gcp_link.current_deployment_id = gcp_deployment.id
    await session.flush()

    return ShopApiExample(
        service=service,
        environments=environments,
        aws_link=aws_link,
        gcp_link=gcp_link,
        aws_deployment=aws_deployment,
        gcp_deployment=gcp_deployment,
    )
