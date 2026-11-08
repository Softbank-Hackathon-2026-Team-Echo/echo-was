"""환경 보안 수집기의 실제 읽기 전용 어댑터."""

import logging

import httpx

from app.adapters.aws import AwsEnvironmentConfig
from app.adapters.gcp import GcpEnvironmentConfig
from app.clients.environment_cloud_iam import inspect_aws_subject, inspect_gcp_subject
from app.clients.environment_iam import CLOUD, KUBE, check, inspect_kubernetes_subject
from app.clients.environment_transport import (
    DATA,
    TLS,
    inspect_database,
    inspect_entrypoint,
    inspect_http,
)
from app.clients.secret_client import EnvVarSecretClient, SecretClient
from app.core.clock import Clock
from app.core.config import Settings
from app.enums import SecurityControlId, SecurityResult
from app.executors.base import ClusterEnvironment
from app.executors.google_metadata import GoogleMetadataClient
from app.executors.kubernetes.access import ClusterAccessProvider
from app.executors.kubernetes.api import KubeApi, KubeHttpClients, default_kube_http_factory
from app.security_environment.access import reader_environment
from app.security_environment.types import EnvironmentSecurityProfile, EvidenceCheck, unknown

logger = logging.getLogger(__name__)


class LiveEnvironmentSecurityReader:
    def __init__(
        self, settings: Settings, clock: Clock, secrets: SecretClient | None = None
    ) -> None:
        self._clock = clock
        self._executor_role = settings.executor_aws_role_arn
        self._secrets = secrets or EnvVarSecretClient()
        self._metadata = GoogleMetadataClient(settings.google_metadata_base_url, clock)
        self._provider = ClusterAccessProvider(
            metadata=self._metadata,
            secret_client=self._secrets,
            clock=clock,
            default_aws_role_arn=None,
            timeout_seconds=5,
        )
        self._clients = KubeHttpClients(default_kube_http_factory, 5)
        self._http = httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False)

    async def inspect(
        self, profile: EnvironmentSecurityProfile, environment: ClusterEnvironment
    ) -> dict[SecurityControlId, list[EvidenceCheck]]:
        environment = reader_environment(profile, environment, self._executor_role)
        if isinstance(environment.config, (AwsEnvironmentConfig, GcpEnvironmentConfig)):
            assert profile.reader is not None
            if (
                await self._metadata.service_account_email()
                != profile.reader.google_service_account
            ):
                raise ValueError("unexpected collector workload identity")
        api = KubeApi(environment, self._provider, self._clients)
        iam: list[EvidenceCheck] = []
        transport: list[EvidenceCheck] = []
        for subject in profile.subjects:
            now = self._clock.now()
            try:
                if subject.kind == "kubernetes":
                    iam.extend(await inspect_kubernetes_subject(api, subject, now))
                elif subject.kind == "aws":
                    if not isinstance(environment.config, AwsEnvironmentConfig):
                        raise ValueError("AWS subject requires AWS environment")
                    credentials = await self._provider.credentials_for_security_read(
                        environment.config
                    )
                    iam.extend(
                        await inspect_aws_subject(credentials, environment.config, subject, now)
                    )
                else:
                    token = await self._metadata.access_token()
                    iam.extend(
                        await inspect_gcp_subject(
                            self._http, token.token.get_secret_value(), subject, now
                        )
                    )
                for other_id in subject.must_be_distinct_from:
                    other = next(s for s in profile.subjects if s.id == other_id)
                    if (subject.kind, subject.name, subject.namespace) == (
                        other.kind,
                        other.name,
                        other.namespace,
                    ):
                        iam.append(
                            check(
                                subject.id,
                                "SHARED_EXECUTION_IDENTITY",
                                SecurityResult.WARN,
                                now,
                                KUBE if subject.kind == "kubernetes" else CLOUD,
                            )
                        )
            except Exception as exc:
                logger.warning(
                    "environment identity read failed",
                    extra={
                        "action": "collect_environment_security",
                        "subject_id": subject.id,
                        "error_type": type(exc).__name__,
                    },
                )
                iam.append(
                    unknown(
                        subject.id,
                        "IDENTITY_READ_FAILED",
                        now,
                        KUBE if subject.kind == "kubernetes" else CLOUD,
                    )
                )
        for target in profile.transports:
            now = self._clock.now()
            try:
                if target.kind == "database":
                    transport.extend(await inspect_database(api, target, self._clock))
                elif target.kind == "entrypoint":
                    transport.extend(await inspect_entrypoint(target, self._secrets, self._clock))
                else:
                    transport.extend(await inspect_http(target, now))
            except Exception as exc:
                logger.warning(
                    "environment transport read failed",
                    extra={
                        "action": "collect_environment_security",
                        "target_id": target.id,
                        "error_type": type(exc).__name__,
                    },
                )
                transport.append(
                    unknown(
                        target.id,
                        "TRANSPORT_READ_FAILED",
                        now,
                        DATA if target.kind == "database" else TLS,
                    )
                )
        return {SecurityControlId.IAM_1: iam, SecurityControlId.TLS_2: transport}

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._clients.aclose()
        await self._provider.aclose()
        await self._metadata.aclose()
