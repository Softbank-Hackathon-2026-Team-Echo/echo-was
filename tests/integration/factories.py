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


def make_environment(name: str) -> Environment:
    kind, config = ENVIRONMENT_CONFIGS[name]
    return Environment(
        name=name, kind=kind, config=dict(config), secret_ref=f"echo/environments/{name}"
    )


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
    service = Service(name="shop-api", description="데모 쇼핑몰 API")
    environments = {name: make_environment(name) for name in ENVIRONMENT_CONFIGS}
    session.add_all([service, *environments.values()])
    await session.flush()

    applied_at = datetime.now(UTC)
    aws_link = ServiceEnvironment(
        service_id=service.id,
        environment_id=environments["aws-seoul"].id,
        traffic_weight=90,
        traffic_applied_at=applied_at,
    )
    gcp_link = ServiceEnvironment(
        service_id=service.id,
        environment_id=environments["gcp-seoul"].id,
        traffic_weight=10,
        traffic_applied_at=applied_at,
    )
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
