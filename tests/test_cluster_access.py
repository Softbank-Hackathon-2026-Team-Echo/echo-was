"""클러스터 접속기 시험(SPEC api-3-2 AC-5). 메타데이터·STS·EKS·GKE 는 가짜 전송이다."""

import base64
import uuid
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from app.adapters.aws import AwsEnvironmentConfig
from app.adapters.gcp import GcpEnvironmentConfig
from app.adapters.onprem import OnpremEnvironmentConfig
from app.core.secrets import SecretValue
from app.executors.base import ClusterEnvironment
from app.executors.google_metadata import GoogleMetadataClient
from app.executors.kubernetes import access as access_module
from app.executors.kubernetes.access import (
    ClusterAccessProvider,
    ClusterAuthError,
    ClusterConfigError,
    presign_eks_token,
    service_account_from_token,
)
from tests.real_executor_fakes import (
    AWS_ACCESS_KEY,
    EKS_ENDPOINT,
    FAKE_CA_PEM,
    GKE_ENDPOINT,
    GOOGLE_ACCESS_TOKEN,
    METADATA_BASE,
    ONPREM_API_URL,
    ONPREM_TOKEN_ENV,
    DictSecretClient,
    FakeCloud,
    TestClock,
    onprem_service_account_token,
)

ROLE_ARN = "arn:aws:iam::123456789012:role/echo-dev-executor"
AWS = ClusterEnvironment(
    environment_id=uuid.uuid4(),
    kind="aws",
    name="aws-tokyo",
    config=AwsEnvironmentConfig(
        region="ap-northeast-1", cluster_name="echo-dev-eks", role_arn=ROLE_ARN
    ),
    secret_ref=None,
)
GCP = ClusterEnvironment(
    environment_id=uuid.uuid4(),
    kind="gcp",
    name="gcp-seoul",
    config=GcpEnvironmentConfig(
        project_id="team-echo-final", region="asia-northeast3", cluster_name="echo-dev-gke"
    ),
    secret_ref=None,
)
ONPREM = ClusterEnvironment(
    environment_id=uuid.uuid4(),
    kind="onprem",
    name="onprem",
    config=OnpremEnvironmentConfig(location="ec2", kube_context="default", api_url=ONPREM_API_URL),
    secret_ref=f"env:{ONPREM_TOKEN_ENV}",
)


def _provider(
    cloud: FakeCloud, clock: TestClock, *, default_role: str | None = None
) -> tuple[ClusterAccessProvider, DictSecretClient]:
    secrets = DictSecretClient({ONPREM_TOKEN_ENV: onprem_service_account_token()})
    metadata = GoogleMetadataClient(METADATA_BASE, clock, transport=cloud.transport())
    provider = ClusterAccessProvider(
        metadata=metadata,
        secret_client=secrets,
        clock=clock,
        default_aws_role_arn=default_role,
        transport=cloud.transport(),
    )
    return provider, secrets


def _decode_eks_token(token: SecretValue) -> str:
    value = token.get_secret_value()
    assert value.startswith("k8s-aws-v1.")
    encoded = value.removeprefix("k8s-aws-v1.")
    assert "=" not in encoded
    return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()


async def test_eks_token_is_presigned_get_caller_identity_with_cluster_header() -> None:
    clock = TestClock()
    cloud = FakeCloud(clock)
    provider, _ = _provider(cloud, clock)

    access = await provider.access(AWS)

    url = _decode_eks_token(access.token)
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert parts.netloc == "sts.ap-northeast-1.amazonaws.com"
    assert query["Action"] == ["GetCallerIdentity"]
    assert query["X-Amz-Expires"] == ["60"]
    assert query["X-Amz-SignedHeaders"] == ["host;x-k8s-aws-id"]
    assert query["X-Amz-Credential"][0].startswith(AWS_ACCESS_KEY)
    assert (access.server, access.ca_pem) == (EKS_ENDPOINT, FAKE_CA_PEM)
    assert cloud.sts_calls[0]["RoleArn"] == [ROLE_ARN]
    assert cloud.sts_calls[0]["Action"] == ["AssumeRoleWithWebIdentity"]


async def test_eks_cluster_token_is_reissued_five_minutes_before_ten_minute_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = TestClock()
    cloud = FakeCloud(clock)
    provider, _ = _provider(cloud, clock)
    issued: list[str] = []

    def counting_presign(credentials: Any, region: str, cluster_name: str) -> SecretValue:
        # 서명 시각은 botocore 가 실제 시계로 정해 같은 초면 같은 값이다. 발급 횟수로 본다.
        issued.append(cluster_name)
        return presign_eks_token(credentials, region, cluster_name)

    monkeypatch.setattr(access_module, "presign_eks_token", counting_presign)

    await provider.access(AWS)
    clock.advance(4 * 60)
    await provider.access(AWS)
    assert issued == ["echo-dev-eks"]
    clock.advance(61)
    await provider.access(AWS)

    assert issued == ["echo-dev-eks", "echo-dev-eks"]
    # 임시 키(1시간)는 그대로 재사용하고 DescribeCluster 도 다시 부르지 않는다.
    assert (len(cloud.sts_calls), cloud.eks_calls, cloud.identity_calls) == (1, 1, 1)


async def test_aws_role_credentials_are_renewed_five_minutes_before_expiry() -> None:
    clock = TestClock()
    cloud = FakeCloud(clock)
    provider, _ = _provider(cloud, clock)

    await provider.access(AWS)
    clock.advance(timedelta(minutes=56).total_seconds())
    await provider.access(AWS)

    assert len(cloud.sts_calls) == 2


async def test_aws_without_role_uses_default_or_fails_as_config_error() -> None:
    clock = TestClock()
    config = AwsEnvironmentConfig(region="ap-northeast-1", cluster_name="echo-dev-eks")
    environment = ClusterEnvironment(AWS.environment_id, "aws", "aws-tokyo", config, None)
    cloud = FakeCloud(clock)
    with_default, _ = _provider(cloud, clock, default_role="arn:aws:iam::1:role/x")
    without_default, _ = _provider(FakeCloud(clock), clock)

    await with_default.access(environment)
    with pytest.raises(ClusterConfigError):
        await without_default.access(environment)

    assert cloud.sts_calls[0]["RoleArn"] == ["arn:aws:iam::1:role/x"]


async def test_sts_rejection_is_cluster_auth_error() -> None:
    clock = TestClock()
    cloud = FakeCloud(clock, sts_status=403)
    provider, _ = _provider(cloud, clock)

    with pytest.raises(ClusterAuthError):
        await provider.access(AWS)


async def test_gke_access_uses_metadata_token_cached_until_five_minutes_before_expiry() -> None:
    clock = TestClock()
    cloud = FakeCloud(clock)
    provider, _ = _provider(cloud, clock)

    first = await provider.access(GCP)
    clock.advance(timedelta(minutes=54).total_seconds())
    cached = await provider.access(GCP)
    clock.advance(timedelta(minutes=1, seconds=1).total_seconds())
    renewed = await provider.access(GCP)

    assert first.server == f"https://{GKE_ENDPOINT}" and first.ca_pem == FAKE_CA_PEM
    assert first.token.get_secret_value() == f"{GOOGLE_ACCESS_TOKEN}1"
    assert cached.token.get_secret_value() == f"{GOOGLE_ACCESS_TOKEN}1"
    assert renewed.token.get_secret_value() == f"{GOOGLE_ACCESS_TOKEN}2"
    # 존 위치가 없으면 region 으로 찾는다. 클러스터 정보는 한 번만 읽는다.
    assert cloud.gke_calls == [
        "/v1/projects/team-echo-final/locations/asia-northeast3/clusters/echo-dev-gke"
    ]


async def test_refresh_forces_new_gke_token() -> None:
    clock = TestClock()
    cloud = FakeCloud(clock)
    provider, _ = _provider(cloud, clock)

    await provider.access(GCP)
    refreshed = await provider.access(GCP, refresh=True)

    assert refreshed.token.get_secret_value() == f"{GOOGLE_ACCESS_TOKEN}2"


async def test_onprem_token_comes_from_secret_ref_and_is_kept() -> None:
    clock = TestClock()
    provider, secrets = _provider(FakeCloud(clock), clock)

    first = await provider.access(ONPREM)
    again = await provider.access(ONPREM)

    assert first.server == ONPREM_API_URL and first.ca_pem is None
    assert first.token.get_secret_value() == onprem_service_account_token()
    assert again.token.get_secret_value() == first.token.get_secret_value()
    assert secrets.reads == [ONPREM_TOKEN_ENV]


async def test_onprem_missing_secret_is_config_error() -> None:
    clock = TestClock()
    provider, secrets = _provider(FakeCloud(clock), clock)
    secrets.values.clear()

    with pytest.raises(ClusterConfigError):
        await provider.access(ONPREM)


def test_service_account_from_token_reads_subject() -> None:
    assert service_account_from_token(SecretValue(onprem_service_account_token("a", "b"))) == (
        "a",
        "b",
    )
    assert service_account_from_token(SecretValue("not-a-jwt")) is None
