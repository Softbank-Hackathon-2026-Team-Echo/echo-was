"""실제 배포·내리기 실행기(쿠버네티스 직접 반영) 시험(SPEC api-3-2 AC-1·AC-3·AC-4).

메타데이터·STS·EKS·GKE·쿠버네티스 창구·Cloudflare 는 모두 가짜 전송이다.
"""

import base64
import json
import uuid
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

import pytest

from app.adapters.gcp import GcpEnvironmentConfig
from app.adapters.onprem import OnpremEnvironmentConfig
from app.core.config import ExecutorMode, HealthCheckerMode, Settings, SourceClientMode
from app.enums import DeployRequestErrorCode, TeardownErrorCode
from app.executors import ExecutorSet, RealExecutorTransports, build_executor_set
from app.executors.base import (
    CONTAINER_LOG_LINE_MAX_LENGTH,
    CONTAINER_LOG_TAIL_LINES,
    ClusterEnvironment,
    ContainerDiagnostics,
    DeployApplied,
    DeployApplyError,
    DeployFailed,
    DeploymentSpec,
    DeployPending,
    DeployTarget,
    ExecutorUnavailableError,
    Reverted,
    RevertFailed,
    TeardownFailed,
    TeardownPending,
    TeardownRemoved,
    cluster_environment,
)
from app.executors.kubernetes.executors import KubernetesDeployExecutor
from tests.executor_contract import (
    DEPLOYMENT,
    TARGET,
    assert_deploy_executor_contract,
    assert_runtime_log_contract,
    assert_teardown_executor_contract,
)
from tests.real_executor_fakes import (
    AR_PREFIX,
    AR_REPOSITORY,
    CLOUDFLARE_BASE,
    CLOUDFLARE_TOKEN,
    CLOUDFLARE_TOKEN_ENV,
    CLOUDFLARE_ZONE,
    DIGEST,
    ECR_PREFIX,
    METADATA_BASE,
    ONPREM_API_URL,
    ONPREM_TOKEN_ENV,
    ONPREM_TUNNEL,
    DictSecretClient,
    FakeCloud,
    FakeCloudflare,
    FakeCluster,
    RecordingSleep,
    TestClock,
    onprem_service_account_token,
)

NAMESPACE = "shop-api-ab12cd34"
KEY_ENV = "ECHO_SECRET_GITHUB_APP_PRIVATE_KEY"

AWS_DEPLOYMENT = replace(DEPLOYMENT, image_repository=AR_REPOSITORY, image_digest=DIGEST)
GCP_TARGET = replace(
    TARGET,
    environment_kind="gcp",
    environment_name="gcp-seoul",
    public_host="shop-api-ab12cd34.gcp-seoul.apps.echo.test",
    environment_config=GcpEnvironmentConfig(
        project_id="team-echo-final",
        region="asia-northeast3",
        cluster_name="echo-dev-gke",
        cluster_location="asia-northeast3-a",
        ingress_domain="gcp-seoul.apps.echo.test",
    ),
)
ONPREM_TARGET = replace(
    TARGET,
    environment_kind="onprem",
    environment_name="onprem-seoul",
    public_host="shop-api-onprem-seoul-ab12cd34.apps.echo.test",
    environment_config=OnpremEnvironmentConfig(
        location="aws-seoul-ec2",
        kube_context="default",
        api_url=ONPREM_API_URL,
        tunnel_target=ONPREM_TUNNEL,
    ),
    environment_secret_ref=f"env:{ONPREM_TOKEN_ENV}",
)


@dataclass
class World:
    clock: TestClock
    cloud: FakeCloud
    cluster: FakeCluster
    cloudflare: FakeCloudflare
    sleep: RecordingSleep
    executors: ExecutorSet
    lookup: dict[uuid.UUID, ClusterEnvironment]

    @property
    def deploy(self) -> KubernetesDeployExecutor:
        assert isinstance(self.executors.deploy, KubernetesDeployExecutor)
        return self.executors.deploy


class _Lookup:
    def __init__(self, environments: dict[uuid.UUID, ClusterEnvironment]) -> None:
        self.environments = environments

    async def find_environment(self, environment_id: uuid.UUID) -> ClusterEnvironment | None:
        return self.environments.get(environment_id)


def _settings(**changes: Any) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://x:y@127.0.0.1:1/z",
        executor_mode=ExecutorMode.REAL,
        github_app_id="123",
        github_app_private_key_ref=f"env:{KEY_ENV}",
        source_client_mode=SourceClientMode.GITHUB,
        health_checker_mode=HealthCheckerMode.HTTP,
        google_metadata_base_url=METADATA_BASE,
        image_registry_mirrors=f"{AR_PREFIX}={ECR_PREFIX}",
        cloudflare_api_token_ref=f"env:{CLOUDFLARE_TOKEN_ENV}",
        cloudflare_zone_id=CLOUDFLARE_ZONE,
        cloudflare_api_base_url=CLOUDFLARE_BASE,
        **changes,
    )


def _world(**settings_changes: Any) -> World:
    clock = TestClock()
    cloud = FakeCloud(clock)
    cluster = FakeCluster(clock)
    cloudflare = FakeCloudflare()
    sleep = RecordingSleep()
    lookup: dict[uuid.UUID, ClusterEnvironment] = {}
    executors = build_executor_set(
        _settings(**settings_changes),
        clock,
        DictSecretClient(
            {
                ONPREM_TOKEN_ENV: onprem_service_account_token(),
                CLOUDFLARE_TOKEN_ENV: CLOUDFLARE_TOKEN,
            }
        ),
        environment_lookup=_Lookup(lookup),
        transports=RealExecutorTransports(
            cloud=cloud.transport(),
            metadata=cloud.transport(),
            cloudflare=cloudflare.transport(),
            kube_http_factory=cluster.factory(),
            sleep=sleep,
        ),
    )
    return World(clock, cloud, cluster, cloudflare, sleep, executors, lookup)


def _paths(cluster: FakeCluster) -> dict[str, dict[str, Any]]:
    return {
        path.rsplit("/", 2)[-2]: body
        for path, body in cluster.objects.items()
        if path.endswith("/app") or path.endswith("/echo-pull")
    }


# --- AC-1 공통 계약 ---


async def test_kubernetes_deploy_executor_satisfies_contract() -> None:
    world = _world()

    def finish() -> None:
        world.cluster.rolled_out = True

    await assert_deploy_executor_contract(world.executors.deploy, finish)


async def test_kubernetes_teardown_executor_satisfies_contract() -> None:
    world = _world()
    world.cluster.namespaces[NAMESPACE] = {
        "metadata": {"name": NAMESPACE, "labels": {"app.kubernetes.io/managed-by": "echo-was"}}
    }

    await assert_teardown_executor_contract(
        world.executors.teardown, world.cluster.finish_namespace_deletion
    )


async def test_kubernetes_runtime_log_reader_satisfies_contract() -> None:
    world = _world()
    world.cluster.add_pod(
        DEPLOYMENT.deployment_id, name="app-old", created_at="2026-11-03T05:00:00Z"
    )
    world.cluster.add_pod(
        DEPLOYMENT.deployment_id,
        name="app-new",
        log=(
            "2026-11-03T06:00:01.000000001Z listening on :8080\n"
            "2026-11-03T06:00:01.000000001Z same instant line\n"
            "2026-11-03T06:00:01.5Z GET /healthz 200\n"
            "2026-11-03T06:00:02Z token ghs_" + "A1b2C3d4E5f6G7h8I9j0" * 2 + "\n"
        ),
    )

    lines = await assert_runtime_log_contract(world.executors.deploy)

    assert [line.message for line in lines] == [
        "listening on :8080",
        "same instant line",
        "GET /healthz 200",
        "token ***",
    ]


# --- AC-3 매니페스트 ---


async def test_aws_apply_writes_four_objects_with_ecr_image_and_alb_annotations() -> None:
    world = _world()

    external_ref = await world.deploy.apply(AWS_DEPLOYMENT, TARGET, "key")

    assert external_ref == f"k8s:{TARGET.environment_id}:{NAMESPACE}:{DEPLOYMENT.deployment_id}"
    assert world.cluster.namespaces[NAMESPACE]["metadata"]["labels"] == {
        "app.kubernetes.io/managed-by": "echo-was",
        "echo.service-id": str(TARGET.service_id),
    }
    objects = _paths(world.cluster)
    assert set(objects) == {"deployments", "services", "ingresses"}
    deployment = objects["deployments"]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert deployment["metadata"]["annotations"] == {
        "echo.deployment-id": str(DEPLOYMENT.deployment_id)
    }
    assert deployment["spec"]["replicas"] == 1
    assert container["image"] == f"{ECR_PREFIX}/app@{DIGEST}"
    assert container["env"] == [
        {"name": "PORT", "value": "8080"},
        {"name": "ECHO_ENVIRONMENT", "value": "aws-tokyo"},
    ]
    assert container["envFrom"] == [{"secretRef": {"name": f"{NAMESPACE}-env", "optional": True}}]
    assert container["readinessProbe"]["httpGet"] == {"path": "/healthz", "port": 8080}
    assert container["resources"] == {
        "requests": {"cpu": "100m", "memory": "128Mi"},
        "limits": {"memory": "512Mi"},
    }
    assert "imagePullSecrets" not in deployment["spec"]["template"]["spec"]
    assert objects["services"]["spec"]["ports"] == [
        {"name": "http", "port": 80, "targetPort": 8080}
    ]
    ingress = objects["ingresses"]
    assert ingress["spec"]["ingressClassName"] == "alb"
    assert [rule["host"] for rule in ingress["spec"]["rules"]] == [
        TARGET.public_host,
        TARGET.main_public_host,
    ]
    assert ingress["metadata"]["annotations"] == {
        "alb.ingress.kubernetes.io/group.name": "echo-apps",
        "alb.ingress.kubernetes.io/scheme": "internet-facing",
        "alb.ingress.kubernetes.io/listen-ports": '[{"HTTP": 80}, {"HTTPS": 443}]',
        "alb.ingress.kubernetes.io/certificate-arn": (
            "arn:aws:acm:ap-northeast-1:123456789012:certificate/abc-123"
        ),
        "alb.ingress.kubernetes.io/target-type": "ip",
        "alb.ingress.kubernetes.io/healthcheck-path": "/healthz",
    }
    # EKS 창구에는 presigned 토큰으로 붙었다.
    assert all(token.startswith("Bearer k8s-aws-v1.") for token in world.cluster.tokens)


async def test_gcp_apply_keeps_artifact_registry_image_and_nginx_class() -> None:
    world = _world()

    await world.deploy.apply(AWS_DEPLOYMENT, GCP_TARGET, "key")

    objects = _paths(world.cluster)
    container = objects["deployments"]["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == f"{AR_REPOSITORY}@{DIGEST}"
    assert objects["ingresses"]["spec"]["ingressClassName"] == "nginx"
    assert "annotations" not in objects["ingresses"]["metadata"]
    assert world.cloud.gke_calls == [
        "/v1/projects/team-echo-final/locations/asia-northeast3-a/clusters/echo-dev-gke"
    ]


async def test_onprem_apply_adds_dns_role_binding_and_pull_secret() -> None:
    world = _world()

    await world.deploy.apply(AWS_DEPLOYMENT, ONPREM_TARGET, "key")

    assert world.cloudflare.records == [
        {
            "id": "rec1",
            "type": "CNAME",
            "name": ONPREM_TARGET.public_host,
            "content": ONPREM_TUNNEL,
            "proxied": True,
            "ttl": 1,
            "comment": f"echo-was:{ONPREM_TARGET.service_environment_id}",
        }
    ]
    binding = world.cluster.role_bindings[f"{NAMESPACE}/echo-app-deployer"]
    assert binding["roleRef"]["name"] == "echo-app-deployer"
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "echo-executor", "namespace": "echo-system"}
    ]
    objects = _paths(world.cluster)
    pod_spec = objects["deployments"]["spec"]["template"]["spec"]
    assert pod_spec["imagePullSecrets"] == [{"name": "echo-pull"}]
    assert pod_spec["containers"][0]["image"] == f"{AR_REPOSITORY}@{DIGEST}"
    assert objects["ingresses"]["spec"]["ingressClassName"] == "traefik"
    secret = world.cluster.objects[f"/api/v1/namespaces/{NAMESPACE}/secrets/echo-pull"]
    assert secret["type"] == "kubernetes.io/dockerconfigjson"
    expires_at = world.clock.now() + timedelta(seconds=3599)
    assert secret["metadata"]["annotations"] == {"echo.pull-expires-at": expires_at.isoformat()}
    config = json.loads(base64.b64decode(secret["data"][".dockerconfigjson"]))
    assert set(config["auths"]) == {"asia-northeast3-docker.pkg.dev"}
    assert config["auths"]["asia-northeast3-docker.pkg.dev"]["username"] == "oauth2accesstoken"


async def test_apply_again_with_same_key_is_idempotent() -> None:
    world = _world()
    first = await world.deploy.apply(AWS_DEPLOYMENT, ONPREM_TARGET, "key")
    snapshot = json.dumps(_paths(world.cluster), sort_keys=True)
    generations = dict(world.cluster.generations)

    again = await world.deploy.apply(AWS_DEPLOYMENT, ONPREM_TARGET, "key")

    after = _paths(world.cluster)
    after["secrets"] = json.loads(snapshot)["secrets"]  # 이미지 받기 토큰은 새로 받는다
    assert again == first
    assert json.dumps(after, sort_keys=True) == snapshot
    app_paths = [path for path in generations if not path.endswith("/echo-pull")]
    assert {path: world.cluster.generations[path] for path in app_paths} == {
        path: generations[path] for path in app_paths
    }
    assert len(world.cloudflare.records) == 1
    assert len(world.cluster.role_bindings) == 1


async def test_executor_writes_only_inside_app_namespace() -> None:
    world = _world()

    await world.deploy.apply(AWS_DEPLOYMENT, ONPREM_TARGET, "key")
    await world.deploy.revert(ONPREM_TARGET, None, "key")
    await world.executors.teardown.remove(ONPREM_TARGET, "conn:0")

    for request in world.cluster.writes():
        path = request.url.path
        assert path == "/api/v1/namespaces" or f"/namespaces/{NAMESPACE}" in path, path


# --- AC-3 poll 판정 ---


async def _applied(world: World, target: DeployTarget = TARGET) -> str:
    return await world.deploy.apply(AWS_DEPLOYMENT, target, "key")


async def test_poll_ready_rollout_is_applied() -> None:
    world = _world()
    external_ref = await _applied(world)

    pending = await world.deploy.poll(external_ref)
    world.cluster.rolled_out = True
    applied = await world.deploy.poll(external_ref)

    assert (pending, applied) == (DeployPending(), DeployApplied())


@pytest.mark.parametrize(
    ("reason", "age_seconds", "expected"),
    [
        ("ImagePullBackOff", 0, DeployFailed(DeployRequestErrorCode.IMAGE_PULL_FAILED)),
        ("ErrImagePull", 10, DeployPending()),
        ("ErrImagePull", 31, DeployFailed(DeployRequestErrorCode.IMAGE_PULL_FAILED)),
        ("CrashLoopBackOff", 0, DeployFailed(DeployRequestErrorCode.CONTAINER_START_FAILED)),
        ("ContainerCreating", 120, DeployPending()),
    ],
)
async def test_poll_pod_waiting_reasons(reason: str, age_seconds: float, expected: object) -> None:
    world = _world()
    external_ref = await _applied(world)
    world.cluster.add_pod(DEPLOYMENT.deployment_id, waiting_reason=reason)
    world.clock.advance(age_seconds)

    assert await world.deploy.poll(external_ref) == expected


# --- API 서버 4-1 실패 진단 ---

_CRASH_LOG = "\n".join(
    [
        "Starting shap-api",
        "Traceback (most recent call last):",
        "pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings",
        "database_url",
        "  Field required [type=missing, input_value={}, input_type=dict]",
    ]
)


async def test_poll_crash_reads_previous_container_tail_and_exit_code() -> None:
    world = _world()
    external_ref = await _applied(world)
    world.cluster.add_pod(
        DEPLOYMENT.deployment_id,
        name="app-crash",
        waiting_reason="CrashLoopBackOff",
        previous_log=_CRASH_LOG,
        restart_count=3,
        exit_code=1,
        termination_reason="Error",
    )

    result = await world.deploy.poll(external_ref)

    assert result == DeployFailed(DeployRequestErrorCode.CONTAINER_START_FAILED)
    assert isinstance(result, DeployFailed) and result.diagnostics is not None
    assert result.diagnostics == ContainerDiagnostics(
        container="app",
        reason="CrashLoopBackOff",
        termination_reason="Error",
        exit_code=1,
        restart_count=3,
        log_tail=tuple(_CRASH_LOG.splitlines()),
        log_truncated=False,
    )
    log_requests = [r for r in world.cluster.requests if r.url.path.endswith("/log")]
    assert [dict(r.url.params) for r in log_requests] == [
        {"container": "app", "tailLines": "51", "limitBytes": "262144", "previous": "true"}
    ]


async def test_poll_crash_tail_keeps_last_50_lines_and_masks_tokens() -> None:
    world = _world()
    external_ref = await _applied(world)
    token = "ghs_" + "A" * 36
    lines = [f"line {index}" for index in range(80)] + [f"auth Bearer {token}", "x" * 1000]
    world.cluster.add_pod(
        DEPLOYMENT.deployment_id,
        waiting_reason="CrashLoopBackOff",
        previous_log="\n".join(lines),
        restart_count=5,
        exit_code=137,
        termination_reason="OOMKilled",
    )

    result = await world.deploy.poll(external_ref)

    assert isinstance(result, DeployFailed) and result.diagnostics is not None
    tail = result.diagnostics.log_tail
    assert len(tail) == CONTAINER_LOG_TAIL_LINES
    assert result.diagnostics.log_truncated is True
    assert tail[0] == "line 32"
    assert tail[-2] == "auth Bearer ***"
    assert len(tail[-1]) == CONTAINER_LOG_LINE_MAX_LENGTH
    assert token not in "\n".join(tail)
    assert (result.diagnostics.termination_reason, result.diagnostics.exit_code) == (
        "OOMKilled",
        137,
    )


async def test_poll_crash_without_previous_log_reads_current_container() -> None:
    world = _world()
    external_ref = await _applied(world)
    world.cluster.add_pod(
        DEPLOYMENT.deployment_id,
        waiting_reason="CrashLoopBackOff",
        log="boot\nfatal: missing DATABASE_URL",
        restart_count=1,
    )

    result = await world.deploy.poll(external_ref)

    assert isinstance(result, DeployFailed) and result.diagnostics is not None
    assert result.diagnostics.log_tail == ("boot", "fatal: missing DATABASE_URL")
    assert result.diagnostics.exit_code is None


@pytest.mark.parametrize("status", [403, 503])
async def test_poll_crash_still_fails_when_log_read_fails(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    world = _world()
    external_ref = await _applied(world)
    pod = world.cluster.add_pod(
        DEPLOYMENT.deployment_id,
        waiting_reason="CrashLoopBackOff",
        previous_log=_CRASH_LOG,
        restart_count=2,
        exit_code=1,
    )
    world.cluster.fail_next(
        "GET", f"/api/v1/namespaces/{NAMESPACE}/pods/{pod.name}/log", status, count=10
    )

    result = await world.deploy.poll(external_ref)

    assert result == DeployFailed(DeployRequestErrorCode.CONTAINER_START_FAILED)
    assert isinstance(result, DeployFailed) and result.diagnostics is not None
    assert result.diagnostics.log_tail == ()
    assert (result.diagnostics.reason, result.diagnostics.exit_code) == ("CrashLoopBackOff", 1)
    assert "CONTAINER_LOGS_UNAVAILABLE" in [getattr(r, "error_code", None) for r in caplog.records]


async def test_poll_image_pull_failure_reports_reason_without_reading_logs() -> None:
    world = _world()
    external_ref = await _applied(world)
    world.cluster.add_pod(DEPLOYMENT.deployment_id, waiting_reason="ImagePullBackOff")

    result = await world.deploy.poll(external_ref)

    assert isinstance(result, DeployFailed) and result.diagnostics is not None
    assert result.code is DeployRequestErrorCode.IMAGE_PULL_FAILED
    assert result.diagnostics.reason == "ImagePullBackOff"
    assert not [r for r in world.cluster.requests if r.url.path.endswith("/log")]


async def test_poll_annotation_from_other_deployment_fails() -> None:
    world = _world()
    external_ref = await _applied(world)
    await world.deploy.apply(replace(AWS_DEPLOYMENT, deployment_id=uuid.uuid4()), TARGET, "other")
    world.cluster.rolled_out = True

    assert await world.deploy.poll(external_ref) == DeployFailed(
        DeployRequestErrorCode.DEPLOY_FAILED
    )


@pytest.mark.parametrize("status", [401, 403])
async def test_poll_unauthorized_returns_cluster_unauthorized(status: int) -> None:
    world = _world()
    external_ref = await _applied(world)
    world.cluster.fail_next("GET", f"/apis/apps/v1/namespaces/{NAMESPACE}", status, count=2)

    assert await world.deploy.poll(external_ref) == DeployFailed(
        DeployRequestErrorCode.CLUSTER_UNAUTHORIZED
    )


async def test_poll_401_once_refreshes_token_and_succeeds() -> None:
    world = _world()
    external_ref = await _applied(world)
    world.cluster.rolled_out = True
    world.cluster.fail_next("GET", f"/apis/apps/v1/namespaces/{NAMESPACE}", 401, count=1)

    assert await world.deploy.poll(external_ref) == DeployApplied()


async def test_poll_unreachable_cluster_is_pending_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = _world()
    external_ref = await _applied(world)
    world.cluster.fail_next("GET", f"/apis/apps/v1/namespaces/{NAMESPACE}", 503)

    assert await world.deploy.poll(external_ref) == DeployPending()
    assert "CLUSTER_UNREACHABLE" in [getattr(r, "error_code", None) for r in caplog.records]


async def test_poll_in_another_process_reads_environment_through_lookup() -> None:
    world = _world()
    external_ref = await _applied(world)
    # 다른 일꾼 프로세스: 이 환경을 본 적이 없어 환경 조회기(DB)로 접속 정보를 다시 읽는다.
    other = _world()
    other.cluster.objects = world.cluster.objects
    other.cluster.namespaces = world.cluster.namespaces
    other.cluster.rolled_out = True
    other.lookup[TARGET.environment_id] = cluster_environment(TARGET)

    assert await other.deploy.poll(external_ref) == DeployApplied()
    # 조회기도 모르는 환경(지워진 기록 등)은 실패로 끝낸다.
    assert await _world().deploy.poll(external_ref) == DeployFailed()


async def test_poll_unknown_ref_fails() -> None:
    assert await _world().deploy.poll("fake-deploy|k|0|x") == DeployFailed()


# --- AC-3 apply 실패 ---


@pytest.mark.parametrize("status", [401, 403])
async def test_apply_unauthorized_raises_cluster_unauthorized(status: int) -> None:
    world = _world()
    world.cluster.fail_next("POST", "/api/v1/namespaces", status, count=5)

    with pytest.raises(DeployApplyError) as exc_info:
        await _applied(world)

    assert exc_info.value.code is DeployRequestErrorCode.CLUSTER_UNAUTHORIZED


async def test_apply_sts_denied_raises_cluster_unauthorized() -> None:
    world = _world()
    world.cloud.sts_status = 403

    with pytest.raises(DeployApplyError) as exc_info:
        await _applied(world)

    assert exc_info.value.code is DeployRequestErrorCode.CLUSTER_UNAUTHORIZED


@pytest.mark.parametrize(
    "target",
    [
        replace(
            ONPREM_TARGET,
            environment_config=OnpremEnvironmentConfig(location="x", kube_context="y"),
        ),
        replace(ONPREM_TARGET, environment_secret_ref=None),
        replace(
            TARGET,
            environment_config=TARGET.environment_config.model_copy(  # type: ignore[union-attr]
                update={"certificate_arn": None}
            ),
        ),
        replace(TARGET, environment_config=None),
        replace(TARGET, main_public_host=""),
    ],
    ids=["onprem-no-api-url", "onprem-no-secret", "aws-no-certificate", "no-config", "no-host"],
)
async def test_apply_without_required_settings_fails_with_deploy_failed(
    target: DeployTarget, caplog: pytest.LogCaptureFixture
) -> None:
    world = _world()

    with pytest.raises(DeployApplyError) as exc_info:
        await world.deploy.apply(AWS_DEPLOYMENT, target, "key")

    assert exc_info.value.code is DeployRequestErrorCode.DEPLOY_FAILED
    assert "ENVIRONMENT_NOT_CONFIGURED" in [getattr(r, "error_code", None) for r in caplog.records]
    assert world.cluster.writes() == []


async def test_apply_aws_without_role_uses_default_setting() -> None:
    world = _world(executor_aws_role_arn="arn:aws:iam::123456789012:role/default-executor")
    config = TARGET.environment_config
    assert config is not None
    target = replace(TARGET, environment_config=config.model_copy(update={"role_arn": None}))

    await world.deploy.apply(AWS_DEPLOYMENT, target, "key")

    assert world.cloud.sts_calls[0]["RoleArn"] == [
        "arn:aws:iam::123456789012:role/default-executor"
    ]
    assert world.cloud.sts_calls[0]["RoleSessionName"] == ["echo-executor"]


async def test_apply_onprem_dns_conflict_fails_without_touching_cluster() -> None:
    world = _world()
    world.cloudflare.records.append(
        {"id": "x", "type": "A", "name": ONPREM_TARGET.public_host, "content": "1.2.3.4"}
    )

    with pytest.raises(DeployApplyError) as exc_info:
        await world.deploy.apply(AWS_DEPLOYMENT, ONPREM_TARGET, "key")

    assert exc_info.value.code is DeployRequestErrorCode.DEPLOY_FAILED
    assert world.cluster.requests == []


async def test_apply_refuses_namespace_not_created_by_echo() -> None:
    world = _world()
    world.cluster.namespaces[NAMESPACE] = {"metadata": {"name": NAMESPACE, "labels": {}}}

    with pytest.raises(DeployApplyError):
        await _applied(world)

    assert [r for r in world.cluster.writes() if r.method == "PATCH"] == []


async def test_apply_server_errors_retry_then_raise_unavailable() -> None:
    world = _world()
    world.cluster.fail_next("POST", "/api/v1/namespaces", 503, count=10)

    with pytest.raises(ExecutorUnavailableError):
        await _applied(world)

    assert world.sleep.delays == [1.0, 2.0, 4.0]


async def test_apply_onprem_retries_forbidden_right_after_role_binding() -> None:
    world = _world()
    world.cluster.fail_next("PATCH", f"/api/v1/namespaces/{NAMESPACE}/secrets", 403, count=2)

    await world.deploy.apply(AWS_DEPLOYMENT, ONPREM_TARGET, "key")

    assert world.sleep.delays == [2.0, 2.0]
    assert "deployments" in _paths(world.cluster)


# --- AC-4 되돌리기 ---


async def test_revert_with_previous_reapplies_previous_digest() -> None:
    world = _world()
    previous = DeploymentSpec(
        deployment_id=uuid.uuid4(),
        image_repository=AR_REPOSITORY,
        image_digest="sha256:" + "c" * 64,
        config_snapshot={"containerPort": 8080, "healthCheckPath": "/ready"},
    )
    await _applied(world)

    result = await world.deploy.revert(TARGET, previous, str(DEPLOYMENT.deployment_id))

    deployment = world.cluster.deployment(NAMESPACE)
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert result == Reverted(
        external_ref=f"k8s-revert:{TARGET.environment_id}:{NAMESPACE}:{previous.deployment_id}"
    )
    assert container["image"] == f"{ECR_PREFIX}/app@sha256:{'c' * 64}"
    assert container["readinessProbe"]["httpGet"]["path"] == "/ready"
    assert deployment["metadata"]["annotations"]["echo.deployment-id"] == str(
        previous.deployment_id
    )


async def test_revert_without_previous_deletes_app_objects_but_keeps_namespace() -> None:
    world = _world()
    await _applied(world)

    result = await world.deploy.revert(TARGET, None, "key")

    assert isinstance(result, Reverted)
    assert _paths(world.cluster) == {}
    assert NAMESPACE in world.cluster.namespaces


async def test_revert_unauthorized_returns_revert_failed() -> None:
    world = _world()
    await _applied(world)
    world.cluster.fail_next("DELETE", f"/apis/networking.k8s.io/v1/namespaces/{NAMESPACE}", 403, 2)

    result = await world.deploy.revert(TARGET, None, "key")

    assert result == RevertFailed(DeployRequestErrorCode.CLUSTER_UNAUTHORIZED)


# --- AC-4 내리기 ---


async def test_teardown_deletes_namespace_and_only_matching_dns_record() -> None:
    world = _world()
    await world.deploy.apply(AWS_DEPLOYMENT, ONPREM_TARGET, "key")
    world.cloudflare.records.append(
        {
            "id": "other",
            "type": "CNAME",
            "name": ONPREM_TARGET.public_host,
            "content": ONPREM_TUNNEL,
            "proxied": True,
            "comment": "someone-else",
        }
    )
    key = f"{ONPREM_TARGET.service_environment_id}:0"

    external_ref = await world.executors.teardown.remove(ONPREM_TARGET, key)
    pending = await world.executors.teardown.poll(external_ref)
    world.cluster.finish_namespace_deletion()
    removed = await world.executors.teardown.poll(external_ref)

    assert external_ref == f"k8s-teardown:{ONPREM_TARGET.environment_id}:{NAMESPACE}:{key}"
    assert (pending, removed) == (TeardownPending(), TeardownRemoved())
    assert [record["id"] for record in world.cloudflare.records] == ["other"]


async def test_teardown_without_settings_returns_invalid_ref_that_fails() -> None:
    world = _world()
    target = replace(ONPREM_TARGET, environment_config=None)

    external_ref = await world.executors.teardown.remove(target, "conn:3")

    assert external_ref == "k8s-teardown-invalid:conn:3"
    assert await world.executors.teardown.poll(external_ref) == TeardownFailed(
        TeardownErrorCode.TEARDOWN_FAILED
    )
    assert world.cluster.requests == []


async def test_teardown_refuses_unmanaged_namespace() -> None:
    world = _world()
    world.cluster.namespaces[NAMESPACE] = {"metadata": {"name": NAMESPACE}}

    external_ref = await world.executors.teardown.remove(TARGET, "conn:0")

    assert external_ref.startswith("k8s-teardown-invalid:")
    assert NAMESPACE in world.cluster.namespaces and not world.cluster.deleting


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, TeardownFailed()), (403, TeardownFailed()), (503, TeardownPending())],
)
async def test_teardown_poll_errors(status: int, expected: object) -> None:
    world = _world()
    world.cluster.namespaces[NAMESPACE] = {
        "metadata": {"name": NAMESPACE, "labels": {"app.kubernetes.io/managed-by": "echo-was"}}
    }
    external_ref = await world.executors.teardown.remove(TARGET, "conn:0")
    world.cluster.fail_next("GET", f"/api/v1/namespaces/{NAMESPACE}", status, count=2)

    assert await world.executors.teardown.poll(external_ref) == expected


# --- 온프레 이미지 받기 자격 갱신(5.8) ---


async def test_refresh_pull_secret_only_when_remaining_lifetime_is_short() -> None:
    world = _world()
    await world.deploy.apply(AWS_DEPLOYMENT, ONPREM_TARGET, "key")
    calls_after_apply = world.cloud.metadata_token_calls
    minimum = timedelta(minutes=30)

    world.clock.advance(timedelta(minutes=20).total_seconds())
    fresh = await world.deploy.refresh_pull_secret(ONPREM_TARGET, AWS_DEPLOYMENT, minimum)
    world.clock.advance(timedelta(minutes=15).total_seconds())
    stale = await world.deploy.refresh_pull_secret(ONPREM_TARGET, AWS_DEPLOYMENT, minimum)

    secret = world.cluster.objects[f"/api/v1/namespaces/{NAMESPACE}/secrets/echo-pull"]
    expected_expiry = world.clock.now() + timedelta(seconds=3599)
    assert (fresh, stale) == (False, True)
    assert world.cloud.metadata_token_calls == calls_after_apply + 1
    assert secret["metadata"]["annotations"]["echo.pull-expires-at"] == expected_expiry.isoformat()


async def test_refresh_pull_secret_skips_cloud_targets() -> None:
    world = _world()

    assert not await world.deploy.refresh_pull_secret(TARGET, AWS_DEPLOYMENT, timedelta(hours=1))
    assert world.cluster.requests == []
