"""클러스터 접속기: 환경 종류별로 쿠버네티스 창구 주소·CA·토큰을 얻는다.

- gcp: GKE API `projects/{p}/locations/{위치}/clusters/{이름}` 의 endpoint·CA,
  토큰은 메타데이터 액세스 토큰.
- aws: STS 임시 키로 서명한 `eks:DescribeCluster` 의 endpoint·CA,
  토큰은 presigned GetCallerIdentity.
- onprem: config `apiUrl`(공개 인증서), 토큰은 환경 secret_ref 의 서비스 계정 토큰.

- aws: 메타데이터 ID 토큰(audience sts.amazonaws.com) → AssumeRoleWithWebIdentity(역할 = config
  roleArn 또는 설정 EXECUTOR_AWS_ROLE_ARN) → 임시 키(만료 5분 전까지 재사용) → presigned URL 을
  `k8s-aws-v1.` + base64url(패딩 없음)로 감싼 토큰(10분, 만료 5분 전 재발급).
- 토큰은 SecretValue 로만 다루고 로그·예외·external_ref 에 넣지 않는다.
- 실패: 권한(401·403·자격 발급 거절) ClusterAuthError, 설정 누락 ClusterConfigError,
  닿지 못함·5xx TransientError.
"""

import base64
import json
import logging
import re
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from botocore.auth import SigV4Auth, SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from app.adapters.aws import AwsEnvironmentConfig
from app.adapters.base import EnvironmentConfig
from app.adapters.gcp import GcpEnvironmentConfig
from app.adapters.onprem import OnpremEnvironmentConfig
from app.clients.secret_client import SecretClient, SecretLookupError
from app.core.clock import Clock
from app.core.secrets import InvalidSecretRefError, SecretValue, parse_secret_ref
from app.executors.base import ClusterEnvironment
from app.executors.google_metadata import GoogleMetadataClient, MetadataAuthError
from app.executors.retry import TransientError

logger = logging.getLogger(__name__)

CLUSTER_UNREACHABLE = "CLUSTER_UNREACHABLE"
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)
EKS_TOKEN_LIFETIME = timedelta(minutes=10)
EKS_TOKEN_PREFIX = "k8s-aws-v1."
EKS_PRESIGN_EXPIRES_SECONDS = 60
EKS_CLUSTER_ID_HEADER = "x-k8s-aws-id"
STS_AUDIENCE = "sts.amazonaws.com"
STS_SESSION_NAME = "echo-executor"
STS_VERSION = "2011-06-15"
GKE_API_BASE_URL = "https://container.googleapis.com/v1"


def sts_url(region: str) -> str:
    return f"https://sts.{region}.amazonaws.com/"


def eks_url(region: str) -> str:
    return f"https://eks.{region}.amazonaws.com"


class ClusterAuthError(Exception):
    """클러스터·자격 발급이 거절했다(401·403 등). 실패 코드 CLUSTER_UNAUTHORIZED."""


class ClusterConfigError(Exception):
    """환경 설정이 비어 실제 반영을 할 수 없다.

    실패 코드 DEPLOY_FAILED, 경고 로그 ENVIRONMENT_NOT_CONFIGURED.
    """


@dataclass(frozen=True)
class ClusterAccess:
    server: str
    ca_pem: str | None
    token: SecretValue


@dataclass(frozen=True)
class _ClusterEndpoint:
    server: str
    ca_pem: str


@dataclass(frozen=True)
class _AwsCredentials:
    credentials: Credentials
    expires_at: datetime


@dataclass(frozen=True)
class _EksToken:
    token: SecretValue
    expires_at: datetime


# STS 가 HTTP 400 으로 주지만 다시 시도하면 되는 오류(AWS STS 공통 오류 문서).
STS_TRANSIENT_ERROR_CODES = frozenset(
    {
        "IDPCommunicationError",
        "Throttling",
        "ThrottlingException",
        "RequestLimitExceeded",
        "ServiceUnavailable",
        "InternalFailure",
    }
)
_STS_ERROR_CODE = re.compile(r"<Code>\s*([A-Za-z]{1,64})\s*</Code>")


def _sts_error_code(body: str) -> str | None:
    """STS 오류 본문에서 <Code> 이름 하나만 꺼낸다(나머지 본문은 쓰지 않는다)."""
    match = _STS_ERROR_CODE.search(body)
    return match.group(1) if match else None


class ClusterAccessProvider:
    """환경마다 창구·토큰을 얻고 기억한다. 401 을 받은 호출은 refresh=True 로 다시 얻는다."""

    def __init__(
        self,
        *,
        metadata: GoogleMetadataClient,
        secret_client: SecretClient,
        clock: Clock,
        default_aws_role_arn: str | None,
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._metadata = metadata
        self._secret_client = secret_client
        self._clock = clock
        self._default_aws_role_arn = default_aws_role_arn
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._endpoints: dict[str, _ClusterEndpoint] = {}
        self._aws_credentials: dict[str, _AwsCredentials] = {}
        self._eks_tokens: dict[str, _EksToken] = {}
        self._onprem_tokens: dict[str, SecretValue] = {}

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def access(
        self, environment: ClusterEnvironment, *, refresh: bool = False
    ) -> ClusterAccess:
        config = environment.config
        try:
            match config:
                case GcpEnvironmentConfig():
                    return await self._gke_access(config, refresh=refresh)
                case AwsEnvironmentConfig():
                    return await self._eks_access(config, refresh=refresh)
                case OnpremEnvironmentConfig():
                    return await self._onprem_access(environment, config, refresh=refresh)
                case _:
                    raise ClusterConfigError
        except MetadataAuthError:
            raise ClusterAuthError from None

    async def access_for_config(
        self,
        config: EnvironmentConfig,
        *,
        onprem_token: SecretValue | None = None,
        refresh: bool = False,
    ) -> ClusterAccess:
        """환경 설정만으로 창구를 얻는다(환경 접속 확인 어댑터용, API 서버 12).

        aws·gcp 는 `access()` 와 같은 경로·같은 캐시를 쓴다. onprem 은 서비스가 이미 조회해 넘긴
        서비스 계정 토큰을 그대로 쓰고(비밀을 다시 조회하지 않고, 기억하지도 않는다) `apiUrl` 이
        없으면 설정 오류다.
        """
        try:
            match config:
                case GcpEnvironmentConfig():
                    return await self._gke_access(config, refresh=refresh)
                case AwsEnvironmentConfig():
                    return await self._eks_access(config, refresh=refresh)
                case OnpremEnvironmentConfig():
                    if config.api_url is None or onprem_token is None:
                        raise ClusterConfigError
                    return ClusterAccess(server=config.api_url, ca_pem=None, token=onprem_token)
                case _:
                    raise ClusterConfigError
        except MetadataAuthError:
            raise ClusterAuthError from None

    # --- gcp ---

    async def _gke_access(self, config: GcpEnvironmentConfig, *, refresh: bool) -> ClusterAccess:
        token = (await self._metadata.access_token(force=refresh)).token
        location = config.cluster_location or config.region
        cache_key = f"gke:{config.project_id}:{location}:{config.cluster_name}"
        endpoint = None if refresh else self._endpoints.get(cache_key)
        if endpoint is None:
            response = await self._send(
                "GET",
                f"{GKE_API_BASE_URL}/projects/{config.project_id}/locations/{location}"
                f"/clusters/{config.cluster_name}",
                headers={"Authorization": f"Bearer {token.get_secret_value()}"},
            )
            body = _json_or_raise(response)
            master_auth = body.get("masterAuth")
            endpoint = _endpoint(
                body.get("endpoint"),
                master_auth.get("clusterCaCertificate") if isinstance(master_auth, dict) else None,
            )
            self._endpoints[cache_key] = endpoint
        return ClusterAccess(server=endpoint.server, ca_pem=endpoint.ca_pem, token=token)

    # --- aws ---

    async def credentials_for_security_read(self, config: AwsEnvironmentConfig) -> Credentials:
        """설정된 실행 역할의 임시 신원. 수집기는 읽기 API만 호출한다."""
        role_arn = config.role_arn or self._default_aws_role_arn
        if not role_arn:
            raise ClusterConfigError
        return await self._aws_role_credentials(role_arn, config.region, refresh=False)

    async def _eks_access(self, config: AwsEnvironmentConfig, *, refresh: bool) -> ClusterAccess:
        role_arn = config.role_arn or self._default_aws_role_arn
        if not role_arn:
            raise ClusterConfigError
        credentials = await self._aws_role_credentials(role_arn, config.region, refresh=refresh)
        cache_key = f"eks:{role_arn}:{config.region}:{config.cluster_name}"
        endpoint = None if refresh else self._endpoints.get(cache_key)
        if endpoint is None:
            request = AWSRequest(
                method="GET", url=f"{eks_url(config.region)}/clusters/{config.cluster_name}"
            )
            SigV4Auth(credentials, "eks", config.region).add_auth(request)
            response = await self._send("GET", str(request.url), headers=dict(request.headers))
            cluster = _json_or_raise(response).get("cluster")
            if not isinstance(cluster, dict):
                raise TransientError(CLUSTER_UNREACHABLE)
            authority = cluster.get("certificateAuthority")
            endpoint = _endpoint(
                cluster.get("endpoint"),
                authority.get("data") if isinstance(authority, dict) else None,
            )
            self._endpoints[cache_key] = endpoint
        token = self._eks_tokens.get(cache_key)
        now = self._clock.now()
        if refresh or token is None or now >= token.expires_at - TOKEN_REFRESH_MARGIN:
            token = _EksToken(
                token=presign_eks_token(credentials, config.region, config.cluster_name),
                expires_at=now + EKS_TOKEN_LIFETIME,
            )
            self._eks_tokens[cache_key] = token
        return ClusterAccess(server=endpoint.server, ca_pem=endpoint.ca_pem, token=token.token)

    async def _aws_role_credentials(
        self, role_arn: str, region: str, *, refresh: bool
    ) -> Credentials:
        cached = self._aws_credentials.get(role_arn)
        if (
            not refresh
            and cached is not None
            and self._clock.now() < cached.expires_at - TOKEN_REFRESH_MARGIN
        ):
            return cached.credentials
        identity = await self._metadata.identity_token(STS_AUDIENCE)
        response = await self._send(
            "POST",
            sts_url(region),
            data={
                "Action": "AssumeRoleWithWebIdentity",
                "Version": STS_VERSION,
                "RoleArn": role_arn,
                "RoleSessionName": STS_SESSION_NAME,
                "WebIdentityToken": identity.get_secret_value(),
            },
        )
        if response.status_code >= 400:
            # STS 는 일시 오류 일부를 HTTP 400 으로 준다. 코드 이름만 보고 본문은 버린다.
            if _sts_error_code(response.text) in STS_TRANSIENT_ERROR_CODES:
                raise TransientError(CLUSTER_UNREACHABLE)
            # 신뢰 정책·역할 문제다(원문에는 토큰 일부가 들어 있을 수 있어 남기지 않는다).
            raise ClusterAuthError
        parsed = _parse_sts_credentials(response.text)
        if parsed is None:
            raise TransientError(CLUSTER_UNREACHABLE)
        self._aws_credentials[role_arn] = parsed
        return parsed.credentials

    # --- onprem ---

    async def _onprem_access(
        self, environment: ClusterEnvironment, config: OnpremEnvironmentConfig, *, refresh: bool
    ) -> ClusterAccess:
        if config.api_url is None or environment.secret_ref is None:
            raise ClusterConfigError
        cache_key = f"{environment.environment_id}:{environment.secret_ref}"
        token = None if refresh else self._onprem_tokens.get(cache_key)
        if token is None:
            try:
                token = await self._secret_client.get_secret(
                    parse_secret_ref(environment.secret_ref)
                )
            except (InvalidSecretRefError, SecretLookupError):
                raise ClusterConfigError from None
            self._onprem_tokens[cache_key] = token
        return ClusterAccess(server=config.api_url, ca_pem=None, token=token)

    # --- 공통 ---

    async def _send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> httpx.Response:
        if self._http is None:
            self._http = httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout_seconds, follow_redirects=False
            )
        try:
            response = await self._http.request(method, url, headers=headers, data=data)
        except httpx.TransportError:
            raise TransientError(CLUSTER_UNREACHABLE) from None
        if response.status_code >= 500 or response.status_code == 429:
            raise TransientError(CLUSTER_UNREACHABLE)
        return response


def presign_eks_token(credentials: Credentials, region: str, cluster_name: str) -> SecretValue:
    """aws-iam-authenticator 방식 토큰.

    GetCallerIdentity presigned URL(60초)을 base64url(패딩 없음)로 감싼다.
    """
    request = AWSRequest(
        method="GET",
        url=f"{sts_url(region)}?Action=GetCallerIdentity&Version={STS_VERSION}",
        headers={EKS_CLUSTER_ID_HEADER: cluster_name},
    )
    SigV4QueryAuth(credentials, "sts", region, expires=EKS_PRESIGN_EXPIRES_SECONDS).add_auth(
        request
    )
    encoded = base64.urlsafe_b64encode(str(request.url).encode()).decode().rstrip("=")
    return SecretValue(f"{EKS_TOKEN_PREFIX}{encoded}")


def _parse_sts_credentials(text: str) -> _AwsCredentials | None:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return None
    node = root.find(".//{*}Credentials")
    if node is None:
        return None
    values = {child.tag.rsplit("}", 1)[-1]: (child.text or "") for child in node}
    try:
        expires_at = datetime.fromisoformat(values["Expiration"].replace("Z", "+00:00"))
        credentials = Credentials(
            values["AccessKeyId"], values["SecretAccessKey"], values["SessionToken"]
        )
    except (KeyError, ValueError):
        return None
    return _AwsCredentials(credentials=credentials, expires_at=expires_at)


def _json_or_raise(response: httpx.Response) -> dict[str, Any]:
    if response.status_code in (401, 403):
        raise ClusterAuthError
    if response.status_code == 404:
        raise ClusterConfigError
    if not response.is_success:
        raise ClusterConfigError
    try:
        body = response.json()
    except ValueError:
        raise TransientError(CLUSTER_UNREACHABLE) from None
    if not isinstance(body, dict):
        raise TransientError(CLUSTER_UNREACHABLE)
    return body


def _endpoint(endpoint: object, ca_base64: object) -> _ClusterEndpoint:
    if not isinstance(endpoint, str) or not endpoint or not isinstance(ca_base64, str):
        raise TransientError(CLUSTER_UNREACHABLE)
    try:
        ca_pem = base64.b64decode(ca_base64, validate=True).decode()
    except ValueError:
        raise TransientError(CLUSTER_UNREACHABLE) from None
    server = endpoint if endpoint.startswith("https://") else f"https://{endpoint}"
    return _ClusterEndpoint(server=server.rstrip("/"), ca_pem=ca_pem)


def service_account_from_token(token: SecretValue) -> tuple[str, str] | None:
    """서비스 계정 토큰(JWT)의 sub → (네임스페이스, 이름).

    sub 모양은 `system:serviceaccount:<네임스페이스>:<이름>` 이다.

    서명은 확인하지 않는다(우리가 보관한 토큰에서 자기 이름을 읽을 뿐이다). 토큰 값은 남기지 않는다.
    """
    parts = token.get_secret_value().split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except ValueError:
        return None
    subject = payload.get("sub") if isinstance(payload, dict) else None
    if not isinstance(subject, str):
        return None
    prefix, _, rest = subject.partition("system:serviceaccount:")
    namespace, _, name = rest.partition(":")
    if prefix or not namespace or not name:
        return None
    return namespace, name
