"""통합 테스트용 데이터. docs/schema.md ③의 행 예시(shop-api 가 AWS 90%·GCP 10%)와 같은 모양이다."""

import itertools
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.secrets import organization_env_secret_prefix
from app.enums import DeploymentStatus
from app.models import Deployment, Environment, Organization, Service, ServiceEnvironment, User

DEFAULT_ORG_SLUG = "team-echo"
_github_ids = itertools.count(9_000_001)


def next_github_id() -> int:
    """시험마다 겹치지 않는 GitHub 숫자 번호."""
    return next(_github_ids)


async def add_user(session: AsyncSession, login: str, github_id: int | None = None) -> User:
    user = User(github_id=github_id or next_github_id(), github_login=login, name=login)
    session.add(user)
    await session.flush()
    return user


async def add_organization(
    session: AsyncSession, slug: str = DEFAULT_ORG_SLUG, creator: User | None = None
) -> Organization:
    """조직 행. creator 가 없으면 `{slug}-owner` 사용자를 만든다(멤버 행은 넣지 않는다)."""
    if creator is None:
        # GitHub 아이디는 39자까지다.
        creator = await add_user(session, f"{slug[:33]}-owner")
    organization = Organization(slug=slug, name=slug, created_by_user_id=creator.id)
    session.add(organization)
    await session.flush()
    return organization


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


def make_service(name: str, organization: Organization, description: str | None = None) -> Service:
    """서비스 행. 원터치 배포 칸(저장소·대표 주소)을 채운다."""
    return Service(
        organization_id=organization.id,
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


def secret_env_name(name: str, organization_slug: str = DEFAULT_ORG_SLUG) -> str:
    """조직 열쇠 이름으로 묶은 환경변수 이름.

    예: (aws-seoul, team-echo) → ECHO_SECRET_TEAM_ECHO__AWS_SEOUL_KUBECONFIG
    """
    key = name.upper().replace("-", "_")
    return f"{organization_env_secret_prefix(organization_slug)}{key}_KUBECONFIG"


def secret_ref_for(name: str, organization_slug: str = DEFAULT_ORG_SLUG) -> str:
    """env 저장소 형식의 비밀 참조. 예: env:ECHO_SECRET_TEAM_ECHO__AWS_SEOUL_KUBECONFIG"""
    return f"env:{secret_env_name(name, organization_slug)}"


def make_environment(name: str, organization: Organization) -> Environment:
    kind, config = ENVIRONMENT_CONFIGS[name]
    return Environment(
        organization_id=organization.id,
        name=name,
        kind=kind,
        config=dict(config),
        secret_ref=secret_ref_for(name, organization.slug),
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


async def seed_shop_api_example(
    session: AsyncSession, organization: Organization | None = None
) -> ShopApiExample:
    """shop-api 가 같은 이미지를 AWS 90%·GCP 10%로 돌리는 순간. 온프레 환경은 등록만 돼 있다.

    organization 이 없으면 team-echo 조직을 만든다.
    """
    if organization is None:
        organization = await add_organization(session)
    service = make_service("shop-api", organization, description="데모 쇼핑몰 API")
    environments = {name: make_environment(name, organization) for name in ENVIRONMENT_CONFIGS}
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
