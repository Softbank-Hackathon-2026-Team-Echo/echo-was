"""실제 배포·내리기 실행기: 쿠버네티스 직접 반영 (`executor_mode = real`, API 서버 3-2).

계약은 docs/executors.md 의 "쿠버네티스 직접 반영". 요약:

- apply: (온프레) 앱 주소 DNS → 네임스페이스(없으면 만듦, echo-was 라벨이 없는 곳은 쓰지 않음) →
  (온프레) RoleBinding·이미지 받기 자격 → Deployment·Service·Ingress 서버 측 apply.
  external_ref = `k8s:{환경 id}:{네임스페이스}:{배포 기록 id}`.
- poll: Deployment 어노테이션이 이 배포 번호이고 새 파드가 모두 준비되면 DeployApplied. 이미지 받기
  실패·CrashLoopBackOff 는 바로 실패 코드. 401·403 은 CLUSTER_UNAUTHORIZED, 닿지 못하면 진행 중.
- revert: previous 가 있으면 그 배포 기록으로 다시 apply, 없으면
  Deployment·Service·Ingress 를 지운다.
- 내리기: 네임스페이스 삭제(+온프레 DNS 레코드), poll 은 네임스페이스가 사라지면 TeardownRemoved.

일시 오류는 시작 계열에서 1·2·4초 3번 재시도 뒤 ExecutorUnavailableError, 조회 계열에서 진행 중.
external_ref·실패 코드·로그에 토큰·비밀값을 넣지 않는다.
"""

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.adapters.aws import AwsEnvironmentConfig
from app.adapters.onprem import OnpremEnvironmentConfig
from app.core.clock import Clock
from app.core.log_masking import mask_secrets
from app.enums import DeployRequestErrorCode, LogLevel, TeardownErrorCode
from app.executors.base import (
    ClusterEnvironment,
    DeployApplied,
    DeployApplyError,
    DeployFailed,
    DeploymentSpec,
    DeployPending,
    DeployPollResult,
    DeployTarget,
    EnvironmentLookup,
    ExecutorUnavailableError,
    InvalidLogCursor,
    LogChunk,
    LogLine,
    Reverted,
    RevertFailed,
    RevertResult,
    TeardownFailed,
    TeardownPending,
    TeardownPollResult,
    TeardownRemoved,
    cluster_environment,
)
from app.executors.cloudflare_dns import (
    CloudflareDnsClient,
    DnsRecordConflictError,
    DnsUnauthorizedError,
    record_comment,
)
from app.executors.google_metadata import GoogleMetadataClient, MetadataAuthError
from app.executors.kubernetes import manifests
from app.executors.kubernetes.access import (
    ClusterAccessProvider,
    ClusterAuthError,
    ClusterConfigError,
    service_account_from_token,
)
from app.executors.kubernetes.api import KubeApi, KubeHttpClients, KubeRequestError
from app.executors.retry import Sleep, TransientError, retry_transient

logger = logging.getLogger(__name__)

DEPLOY_REF_PREFIX = "k8s:"
REVERT_REF_PREFIX = "k8s-revert:"
TEARDOWN_REF_PREFIX = "k8s-teardown:"
TEARDOWN_INVALID_REF_PREFIX = "k8s-teardown-invalid:"

# 경고 로그 코드.
ENVIRONMENT_NOT_CONFIGURED = "ENVIRONMENT_NOT_CONFIGURED"
CLUSTER_UNAUTHORIZED = "CLUSTER_UNAUTHORIZED"
CLUSTER_UNREACHABLE = "CLUSTER_UNREACHABLE"
CLUSTER_REQUEST_REJECTED = "CLUSTER_REQUEST_REJECTED"
NAMESPACE_NOT_MANAGED = "NAMESPACE_NOT_MANAGED"
DNS_RECORD_CONFLICT = "DNS_RECORD_CONFLICT"
DNS_UNAUTHORIZED = "DNS_UNAUTHORIZED"
DEPLOY_REF_UNKNOWN = "DEPLOY_REF_UNKNOWN"
DEPLOYMENT_REPLACED = "DEPLOYMENT_REPLACED"
PULL_SECRET_REFRESH_FAILED = "PULL_SECRET_REFRESH_FAILED"

# 온프레에서 RoleBinding 을 막 만든 직후의 403 은 권한 캐시 반영 지연이다. 2초 간격 5번(10초).
ROLE_BINDING_RETRY_DELAYS_SECONDS = (2.0, 2.0, 2.0, 2.0, 2.0)
_IMAGE_PULL_REASONS = frozenset({"ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull"})
_CRASH_REASONS = frozenset({"CrashLoopBackOff"})
_NAMESPACE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_DEPLOY_REF = re.compile(rf"^k8s:({_UUID}):([-a-z0-9]{{1,63}}):({_UUID})$")
_TEARDOWN_REF = re.compile(rf"^k8s-teardown:({_UUID}):([-a-z0-9]{{1,63}}):(.+)$")
_LOG_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z$")
_LOG_CURSOR = re.compile(
    r"^([a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?)\|(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z)\|(\d{1,9})$"
)
_LOG_LIMIT_BYTES = "1048576"


class _NamespaceNotManagedError(Exception):
    """같은 이름의 네임스페이스가 있는데 echo-was 가 만든 것이 아니다."""


@dataclass(frozen=True)
class _DeployRef:
    environment_id: uuid.UUID
    namespace: str
    deployment_id: uuid.UUID


class KubernetesRuntime:
    """배포·내리기 실행기가 함께 쓰는 접속기·클라이언트·환경 캐시. 프로세스당 하나."""

    def __init__(
        self,
        *,
        provider: ClusterAccessProvider,
        clients: KubeHttpClients,
        metadata: GoogleMetadataClient,
        dns: CloudflareDnsClient | None,
        environment_lookup: EnvironmentLookup | None,
        image_mirrors: tuple[tuple[str, str], ...],
        clock: Clock,
        sleep: Sleep,
        image_pull_grace_seconds: float,
    ) -> None:
        self.provider = provider
        self.clients = clients
        self.metadata = metadata
        self.dns = dns
        self.image_mirrors = image_mirrors
        self.clock = clock
        self.sleep = sleep
        self.image_pull_grace = timedelta(seconds=image_pull_grace_seconds)
        self._lookup = environment_lookup
        self._environments: dict[uuid.UUID, ClusterEnvironment] = {}

    def api(self, environment: ClusterEnvironment) -> KubeApi:
        self._environments[environment.environment_id] = environment
        return KubeApi(environment, self.provider, self.clients)

    async def find_environment(self, environment_id: uuid.UUID) -> ClusterEnvironment | None:
        """poll 은 external_ref 만 받는다. 이 프로세스가 본 환경이 아니면 DB 에서 다시 읽는다."""
        known = self._environments.get(environment_id)
        if known is not None or self._lookup is None:
            return known
        found = await self._lookup.find_environment(environment_id)
        if found is not None:
            self._environments[environment_id] = found
        return found

    async def aclose(self) -> None:
        await self.clients.aclose()
        await self.provider.aclose()
        await self.metadata.aclose()
        if self.dns is not None:
            await self.dns.aclose()


class KubernetesDeployExecutor:
    def __init__(self, runtime: KubernetesRuntime) -> None:
        self._runtime = runtime

    @property
    def simulated(self) -> bool:
        return False

    # --- apply ---

    async def apply(
        self, deployment: DeploymentSpec, target: DeployTarget, idempotency_key: str
    ) -> str:
        environment = cluster_environment(target)
        try:
            namespace = app_namespace(target)
            spec = self._app_spec(deployment, target, namespace)
            await retry_transient(
                lambda: self._apply_once(environment, target, spec), self._runtime.sleep
            )
        except TransientError as exc:
            raise ExecutorUnavailableError(exc.code) from None
        except _APPLY_ERRORS as exc:
            code, log_code = _apply_failure(exc)
            _warn("deploy apply failed", target, log_code)
            raise DeployApplyError(code) from None
        return f"{DEPLOY_REF_PREFIX}{target.environment_id}:{namespace}:{deployment.deployment_id}"

    async def _apply_once(
        self, environment: ClusterEnvironment, target: DeployTarget, spec: manifests.AppSpec
    ) -> None:
        config = environment.config
        is_onprem = isinstance(config, OnpremEnvironmentConfig)
        if isinstance(config, OnpremEnvironmentConfig):
            await self._ensure_dns(target, config)
        api = self._runtime.api(environment)
        await _ensure_namespace(api, spec.namespace, spec.service_id)
        just_bound = False
        if is_onprem:
            just_bound = await self._ensure_role_binding(api, environment, spec)

        async def apply_objects() -> None:
            if is_onprem:
                await self._apply_pull_secret(api, spec)
            await api.apply(manifests.deployment_path(spec.namespace), _deployment(spec))
            await api.apply(
                manifests.service_path(spec.namespace), manifests.service_manifest(spec)
            )
            await api.apply(
                manifests.ingress_path(spec.namespace), manifests.ingress_manifest(spec)
            )

        await self._retry_after_binding(apply_objects, just_bound)

    async def _ensure_dns(self, target: DeployTarget, config: OnpremEnvironmentConfig) -> None:
        dns = self._runtime.dns
        if dns is None or config.tunnel_target is None:
            raise ClusterConfigError
        await dns.ensure_cname(
            target.public_host,
            config.tunnel_target,
            record_comment(str(target.service_environment_id)),
        )

    async def _ensure_role_binding(
        self, api: KubeApi, environment: ClusterEnvironment, spec: manifests.AppSpec
    ) -> bool:
        """앱 네임스페이스 안에서만 자기 서비스 계정에 앱 배포 권한을 준다. 새로 만들었으면 True."""
        access = await self._runtime.provider.access(environment)
        account = service_account_from_token(access.token)
        if account is None:
            raise ClusterConfigError
        return await api.create(
            manifests.role_bindings_path(spec.namespace),
            manifests.role_binding_manifest(spec.namespace, spec.service_id, *account),
        )

    async def _apply_pull_secret(self, api: KubeApi, spec: manifests.AppSpec) -> None:
        token = await self._runtime.metadata.fresh_access_token()
        await api.apply(
            manifests.pull_secret_path(spec.namespace),
            manifests.pull_secret_manifest(
                spec.namespace,
                spec.service_id,
                registry_host(spec.image),
                token.token.get_secret_value(),
                token.expires_at,
            ),
        )

    async def _retry_after_binding(
        self, operation: Callable[[], Awaitable[None]], just_bound: bool
    ) -> None:
        if just_bound:
            for delay in ROLE_BINDING_RETRY_DELAYS_SECONDS:
                try:
                    return await operation()
                except ClusterAuthError:
                    await self._runtime.sleep(delay)
        await operation()

    def _app_spec(
        self, deployment: DeploymentSpec, target: DeployTarget, namespace: str
    ) -> manifests.AppSpec:
        config = target.environment_config
        if config is None:
            raise ClusterConfigError
        snapshot = deployment.config_snapshot
        health_check_path = str(snapshot.get("healthCheckPath") or "/")
        port = snapshot.get("containerPort")
        container_port = port if isinstance(port, int) else target.container_port
        annotations: dict[str, str] = {}
        if isinstance(config, AwsEnvironmentConfig):
            if config.certificate_arn is None or config.ingress_group is None:
                raise ClusterConfigError
            annotations = manifests.alb_annotations(
                config.ingress_group, config.certificate_arn, health_check_path
            )
        ingress_class = getattr(config, "ingress_class", None) or (
            manifests.DEFAULT_INGRESS_CLASS.get(target.environment_kind)
        )
        if ingress_class is None:
            raise ClusterConfigError
        hosts = tuple(
            dict.fromkeys(host for host in (target.public_host, target.main_public_host) if host)
        )
        return manifests.AppSpec(
            namespace=namespace,
            service_id=target.service_id,
            deployment_id=deployment.deployment_id,
            image=f"{self._repository(deployment, target)}@{deployment.image_digest}",
            container_port=container_port,
            health_check_path=health_check_path,
            environment_name=target.environment_name,
            hosts=hosts,
            ingress_class=ingress_class,
            ingress_annotations=annotations,
            uses_pull_secret=isinstance(config, OnpremEnvironmentConfig),
        )

    def _repository(self, deployment: DeploymentSpec, target: DeployTarget) -> str:
        """aws 환경은 정본(Artifact Registry) 주소를 미러 표로 ECR 주소로 바꾼다(같은 digest)."""
        repository = deployment.image_repository
        if target.environment_kind != "aws":
            return repository
        for source, mirror in self._runtime.image_mirrors:
            if repository == source or repository.startswith(f"{source}/"):
                return mirror + repository.removeprefix(source)
        return repository

    # --- poll ---

    async def poll(self, external_ref: str) -> DeployPollResult:
        ref = _parse_deploy_ref(external_ref)
        if ref is None:
            _warn_ref(DEPLOY_REF_UNKNOWN)
            return DeployFailed()
        environment = await self._runtime.find_environment(ref.environment_id)
        if environment is None:
            _warn_ref(ENVIRONMENT_NOT_CONFIGURED)
            return DeployFailed()
        api = self._runtime.api(environment)
        try:
            deployment = await api.get(manifests.deployment_path(ref.namespace))
            if deployment is None or _annotation(deployment) != str(ref.deployment_id):
                # 그사이 다른 반영(또는 되돌리기)이 덮었다.
                _warn_ref(DEPLOYMENT_REPLACED)
                return DeployFailed()
            if _is_rolled_out(deployment):
                return DeployApplied()
            pods = await api.get(
                manifests.pods_path(ref.namespace),
                params={"labelSelector": f"{manifests.DEPLOYMENT_ID_KEY}={ref.deployment_id}"},
            )
        except ClusterAuthError:
            _warn_ref(CLUSTER_UNAUTHORIZED)
            return DeployFailed(DeployRequestErrorCode.CLUSTER_UNAUTHORIZED)
        except ClusterConfigError:
            _warn_ref(ENVIRONMENT_NOT_CONFIGURED)
            return DeployFailed()
        except (TransientError, KubeRequestError):
            _warn_ref(CLUSTER_UNREACHABLE)
            return DeployPending()
        code = _pod_failure(pods, self._runtime.clock.now(), self._runtime.image_pull_grace)
        return DeployFailed(code) if code is not None else DeployPending()

    # --- revert ---

    async def revert(
        self, target: DeployTarget, previous: DeploymentSpec | None, idempotency_key: str
    ) -> RevertResult:
        environment = cluster_environment(target)
        try:
            namespace = app_namespace(target)
            if previous is not None:
                spec = self._app_spec(previous, target, namespace)
                await retry_transient(
                    lambda: self._apply_once(environment, target, spec), self._runtime.sleep
                )
                kept = str(previous.deployment_id)
            else:
                api = self._runtime.api(environment)
                await retry_transient(lambda: _delete_app(api, namespace), self._runtime.sleep)
                kept = "none"
        except TransientError:
            _warn("deploy revert failed", target, CLUSTER_UNREACHABLE)
            return RevertFailed()
        except _APPLY_ERRORS as exc:
            code, log_code = _apply_failure(exc)
            _warn("deploy revert failed", target, log_code)
            return RevertFailed(code)
        return Reverted(
            external_ref=f"{REVERT_REF_PREFIX}{target.environment_id}:{namespace}:{kept}"
        )

    # --- 실행 로그 ---

    async def read_runtime_logs(
        self,
        target: DeployTarget,
        deployment: DeploymentSpec,
        external_ref: str,
        cursor: str | None,
        limit: int,
    ) -> LogChunk:
        """그 배포 번호 파드의 app 컨테이너 로그.

        cursor = `{파드}|{마지막 줄 시각}|{같은 시각 줄 수}`.

        cursor 가 없으면 가장 최근 파드를 고른다. 파드가 없으면 빈 청크다.
        """
        position = _parse_log_cursor(cursor)
        try:
            namespace = app_namespace(target)
            api = self._runtime.api(cluster_environment(target))
            pod = (
                position[0]
                if position is not None
                else await _latest_pod(api, namespace, deployment.deployment_id)
            )
            text = (
                await api.get_text(
                    manifests.pod_log_path(namespace, pod),
                    params={
                        "container": manifests.CONTAINER_NAME,
                        "timestamps": "true",
                        "limitBytes": _LOG_LIMIT_BYTES,
                    },
                )
                if pod is not None
                else None
            )
        except (ClusterAuthError, ClusterConfigError, TransientError, KubeRequestError):
            _warn("runtime logs unavailable", target, CLUSTER_UNREACHABLE)
            return LogChunk(lines=(), next_cursor=cursor, has_more=False)
        if pod is None or text is None:
            return LogChunk(lines=(), next_cursor=cursor, has_more=False)
        return _page_logs(pod, _parse_pod_log(text), position, cursor, limit)

    # --- 온프레 이미지 받기 자격 갱신 ---

    async def refresh_pull_secret(
        self, target: DeployTarget, deployment: DeploymentSpec, min_remaining: timedelta
    ) -> bool:
        """온프레 연결의 echo-pull 남은 수명이 min_remaining 보다 짧으면 새 토큰으로 다시 넣는다.

        다시 넣었으면 True. 실패는 경고 로그만 남기고 False(다음 확인 주기에 다시 한다).
        """
        if not isinstance(target.environment_config, OnpremEnvironmentConfig):
            return False
        environment = cluster_environment(target)
        try:
            namespace = app_namespace(target)
            spec = self._app_spec(deployment, target, namespace)
            api = self._runtime.api(environment)
            current = await api.get(manifests.pull_secret_path(namespace))
            expires_at = _pull_expires_at(current)
            if expires_at is not None and expires_at - self._runtime.clock.now() >= min_remaining:
                return False
            await self._apply_pull_secret(api, spec)
        except (
            ClusterAuthError,
            ClusterConfigError,
            TransientError,
            KubeRequestError,
            MetadataAuthError,
        ):
            _warn("pull secret refresh failed", target, PULL_SECRET_REFRESH_FAILED)
            return False
        return True


class KubernetesTeardownExecutor:
    def __init__(self, runtime: KubernetesRuntime) -> None:
        self._runtime = runtime

    @property
    def simulated(self) -> bool:
        return False

    async def remove(self, target: DeployTarget, idempotency_key: str) -> str:
        """네임스페이스 삭제를 요청한다(+온프레 DNS 레코드). 설정이 비면 호출 없이 실패 ref."""
        invalid = f"{TEARDOWN_INVALID_REF_PREFIX}{idempotency_key}"
        environment = cluster_environment(target)
        try:
            namespace = app_namespace(target)
            if environment.config is None:
                raise ClusterConfigError
            await retry_transient(
                lambda: self._remove_once(environment, target, namespace), self._runtime.sleep
            )
        except TransientError as exc:
            raise ExecutorUnavailableError(exc.code) from None
        except ClusterAuthError:
            # poll 이 같은 창구에서 401·403 을 받아 TEARDOWN_FAILED 로 끝낸다.
            _warn("teardown remove failed", target, CLUSTER_UNAUTHORIZED)
        except (ClusterConfigError, _NamespaceNotManagedError, KubeRequestError) as exc:
            _warn("teardown remove failed", target, _apply_failure(exc)[1])
            return invalid
        return f"{TEARDOWN_REF_PREFIX}{target.environment_id}:{namespace}:{idempotency_key}"

    async def _remove_once(
        self, environment: ClusterEnvironment, target: DeployTarget, namespace: str
    ) -> None:
        api = self._runtime.api(environment)
        existing = await api.get(manifests.namespace_path(namespace))
        if existing is not None:
            if not manifests.is_managed(existing):
                raise _NamespaceNotManagedError
            await api.delete(manifests.namespace_path(namespace))
        if isinstance(environment.config, OnpremEnvironmentConfig):
            await self._remove_dns(target)

    async def _remove_dns(self, target: DeployTarget) -> None:
        """comment 가 이 연결인 레코드만 지운다. 실패는 경고만(네임스페이스 삭제는 계속)."""
        dns = self._runtime.dns
        if dns is None:
            _warn("teardown dns skipped", target, ENVIRONMENT_NOT_CONFIGURED)
            return
        try:
            await dns.remove(target.public_host, record_comment(str(target.service_environment_id)))
        except DnsUnauthorizedError:
            _warn("teardown dns failed", target, DNS_UNAUTHORIZED)

    async def poll(self, external_ref: str) -> TeardownPollResult:
        match = _TEARDOWN_REF.fullmatch(external_ref)
        if match is None:
            _warn_ref(DEPLOY_REF_UNKNOWN)
            return TeardownFailed()
        environment = await self._runtime.find_environment(uuid.UUID(match[1]))
        if environment is None:
            _warn_ref(ENVIRONMENT_NOT_CONFIGURED)
            return TeardownFailed()
        try:
            existing = await self._runtime.api(environment).get(manifests.namespace_path(match[2]))
        except ClusterAuthError:
            _warn_ref(CLUSTER_UNAUTHORIZED)
            return TeardownFailed(TeardownErrorCode.TEARDOWN_FAILED)
        except ClusterConfigError:
            _warn_ref(ENVIRONMENT_NOT_CONFIGURED)
            return TeardownFailed()
        except (TransientError, KubeRequestError):
            _warn_ref(CLUSTER_UNREACHABLE)
            return TeardownPending()
        return TeardownRemoved() if existing is None else TeardownPending()


# --- 도움 함수 ---

_APPLY_ERRORS = (
    ClusterAuthError,
    ClusterConfigError,
    MetadataAuthError,
    _NamespaceNotManagedError,
    DnsRecordConflictError,
    DnsUnauthorizedError,
    KubeRequestError,
)


def _apply_failure(exc: Exception) -> tuple[DeployRequestErrorCode, str]:
    """(실패 코드, 경고 로그 코드). 권한은 CLUSTER_UNAUTHORIZED, 나머지는 DEPLOY_FAILED."""
    if isinstance(exc, ClusterAuthError | MetadataAuthError):
        return DeployRequestErrorCode.CLUSTER_UNAUTHORIZED, CLUSTER_UNAUTHORIZED
    log_code = {
        ClusterConfigError: ENVIRONMENT_NOT_CONFIGURED,
        _NamespaceNotManagedError: NAMESPACE_NOT_MANAGED,
        DnsRecordConflictError: DNS_RECORD_CONFLICT,
        DnsUnauthorizedError: DNS_UNAUTHORIZED,
    }.get(type(exc), CLUSTER_REQUEST_REJECTED)
    return DeployRequestErrorCode.DEPLOY_FAILED, log_code


def app_namespace(target: DeployTarget) -> str:
    """앱 네임스페이스 = 대표 주소의 첫 레이블(`{서비스}-{suffix}`)."""
    namespace = target.main_public_host.split(".", 1)[0]
    if not _NAMESPACE.fullmatch(namespace):
        raise ClusterConfigError
    return namespace


def registry_host(image: str) -> str:
    return image.split("/", 1)[0]


def _deployment(spec: manifests.AppSpec) -> dict[str, Any]:
    return manifests.deployment_manifest(spec)


async def _ensure_namespace(api: KubeApi, namespace: str, service_id: uuid.UUID) -> None:
    """없으면 만든다(create 권한만으로). 이미 있으면 echo-was 라벨이 있어야 쓴다."""
    created = await api.create(
        manifests.namespaces_path(), manifests.namespace_manifest(namespace, service_id)
    )
    if created:
        return
    existing = await api.get(manifests.namespace_path(namespace))
    if existing is None:
        raise TransientError(CLUSTER_UNREACHABLE)
    if not manifests.is_managed(existing):
        raise _NamespaceNotManagedError


async def _delete_app(api: KubeApi, namespace: str) -> None:
    """되돌릴 직전 버전이 없을 때: 앱 객체만 지운다(네임스페이스·Secret 은 남긴다)."""
    await api.delete(manifests.ingress_path(namespace))
    await api.delete(manifests.service_path(namespace))
    await api.delete(manifests.deployment_path(namespace))


def _annotation(deployment: dict[str, Any]) -> str | None:
    metadata = deployment.get("metadata") or {}
    annotations = metadata.get("annotations") or {}
    value = annotations.get(manifests.DEPLOYMENT_ID_KEY)
    return value if isinstance(value, str) else None


def _is_rolled_out(deployment: dict[str, Any]) -> bool:
    """새 파드가 모두 준비됐다(kubectl rollout status 와 같은 판정)."""
    metadata = deployment.get("metadata") or {}
    spec = deployment.get("spec") or {}
    status = deployment.get("status") or {}
    desired = _int(spec.get("replicas"), 1)
    updated = _int(status.get("updatedReplicas"))
    return (
        _int(status.get("observedGeneration")) >= _int(metadata.get("generation"))
        and updated == desired
        and _int(status.get("replicas")) == updated
        and _int(status.get("availableReplicas")) == updated
    )


def _pod_failure(
    pods: dict[str, Any] | None, now: datetime, image_pull_grace: timedelta
) -> DeployRequestErrorCode | None:
    for pod in (pods or {}).get("items") or []:
        if not isinstance(pod, dict):
            continue
        created_at = _parse_time((pod.get("metadata") or {}).get("creationTimestamp"))
        status = pod.get("status") or {}
        for container in [
            *(status.get("initContainerStatuses") or []),
            *(status.get("containerStatuses") or []),
        ]:
            waiting = ((container or {}).get("state") or {}).get("waiting") or {}
            reason = waiting.get("reason")
            if reason in _IMAGE_PULL_REASONS:
                return DeployRequestErrorCode.IMAGE_PULL_FAILED
            if (
                reason == "ErrImagePull"
                and created_at is not None
                and now - created_at > image_pull_grace
            ):
                return DeployRequestErrorCode.IMAGE_PULL_FAILED
            if reason in _CRASH_REASONS:
                return DeployRequestErrorCode.CONTAINER_START_FAILED
    return None


async def _latest_pod(api: KubeApi, namespace: str, deployment_id: uuid.UUID) -> str | None:
    pods = await api.get(
        manifests.pods_path(namespace),
        params={"labelSelector": f"{manifests.DEPLOYMENT_ID_KEY}={deployment_id}"},
    )
    candidates: list[tuple[str, str]] = []
    for pod in (pods or {}).get("items") or []:
        metadata = (pod or {}).get("metadata") or {}
        name = metadata.get("name")
        if isinstance(name, str):
            candidates.append((str(metadata.get("creationTimestamp") or ""), name))
    return max(candidates)[1] if candidates else None


@dataclass(frozen=True)
class _PodLogLine:
    stamp: str
    nanos: int
    line: LogLine


def _parse_pod_log(text: str) -> list[_PodLogLine]:
    """`<RFC3339Nano> <문장>` 줄들(timestamps=true). 시각이 없는 줄은 버린다."""
    lines: list[_PodLogLine] = []
    for raw in text.splitlines():
        stamp, _, message = raw.partition(" ")
        nanos = _nanos(stamp)
        if nanos is None:
            continue
        at = _parse_time(stamp)
        assert at is not None
        lines.append(
            _PodLogLine(
                stamp, nanos, LogLine(at=at, level=LogLevel.INFO, message=mask_secrets(message))
            )
        )
    return lines


def _page_logs(
    pod: str,
    lines: list[_PodLogLine],
    position: tuple[str, str, int] | None,
    cursor: str | None,
    limit: int,
) -> LogChunk:
    start = 0
    if position is not None:
        cursor_nanos = _nanos(position[1])
        assert cursor_nanos is not None
        same_seen = 0
        while start < len(lines) and (
            lines[start].nanos < cursor_nanos
            or (lines[start].nanos == cursor_nanos and same_seen < position[2])
        ):
            if lines[start].nanos == cursor_nanos:
                same_seen += 1
            start += 1
    page = lines[start : start + limit]
    if not page:
        return LogChunk(lines=(), next_cursor=cursor, has_more=False)
    end = start + len(page)
    last = page[-1]
    same_count = sum(1 for item in lines[:end] if item.nanos == last.nanos)
    return LogChunk(
        lines=tuple(item.line for item in page),
        next_cursor=f"{pod}|{last.stamp}|{same_count}",
        has_more=end < len(lines),
    )


def _parse_log_cursor(cursor: str | None) -> tuple[str, str, int] | None:
    if cursor is None:
        return None
    match = _LOG_CURSOR.fullmatch(cursor)
    if match is None:
        raise InvalidLogCursor
    return match[1], match[2], int(match[3])


def _parse_deploy_ref(external_ref: str) -> _DeployRef | None:
    match = _DEPLOY_REF.fullmatch(external_ref)
    if match is None:
        return None
    return _DeployRef(uuid.UUID(match[1]), match[2], uuid.UUID(match[3]))


def _nanos(stamp: str) -> int | None:
    match = _LOG_TIME.fullmatch(stamp)
    if match is None:
        return None
    seconds = _parse_time(f"{match[1]}Z")
    assert seconds is not None
    return int(seconds.timestamp()) * 1_000_000_000 + int((match[2] or "0").ljust(9, "0"))


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    match = _LOG_TIME.fullmatch(value)
    text = value
    if match is not None:
        fraction = (match[2] or "")[:6]
        text = f"{match[1]}{'.' + fraction if fraction else ''}Z"
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _pull_expires_at(secret: dict[str, Any] | None) -> datetime | None:
    if secret is None:
        return None
    annotations = (secret.get("metadata") or {}).get("annotations") or {}
    return _parse_time(annotations.get(manifests.PULL_EXPIRES_AT_ANNOTATION))


def _int(value: object, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _warn(message: str, target: DeployTarget, code: str) -> None:
    logger.warning(
        message,
        extra={
            "action": "kubernetes_executor",
            "service_environment_id": str(target.service_environment_id),
            "environment_id": str(target.environment_id),
            "error_code": code,
        },
    )


def _warn_ref(code: str) -> None:
    logger.warning(
        "kubernetes executor poll failed",
        extra={"action": "kubernetes_executor", "error_code": code},
    )
