"""실행기의 앱 Secret `{ns}-env` 쓰기(SPEC api-10 AC-5, 5-4 표·AD-6·AD-7, 5-6 호환성)."""

import base64
import logging
from dataclasses import replace
from typing import Any

import pytest

from app.core.secrets import SecretValue
from app.enums import DeployRequestErrorCode
from app.executors.base import (
    DeployApplyError,
    DeploymentSpec,
    ExecutorUnavailableError,
    Reverted,
    RevertFailed,
)
from app.executors.kubernetes import manifests
from tests.executor_contract import DEPLOYMENT, TARGET
from tests.real_executor_fakes import FakeCluster
from tests.test_kubernetes_executors import NAMESPACE, ONPREM_TARGET, World, _world

SECRET_PATH = f"/api/v1/namespaces/{NAMESPACE}/secrets/{NAMESPACE}-env"
SECRET_COLLECTION = f"/api/v1/namespaces/{NAMESPACE}/secrets"
VALUE = "sv-5b1c2d3e-0000-4000-8000-000000000001"
SNAPSHOT = dict(DEPLOYMENT.config_snapshot)
MANAGED_BY = "app.kubernetes.io/managed-by"


def _spec(values: dict[str, str] | None, versions: dict[str, int] | None = None) -> DeploymentSpec:
    if values is None:
        return DEPLOYMENT
    refs = {
        key: f"gcp-sm:projects/p/secrets/s-{key.lower()}/versions/{(versions or {}).get(key, 1)}"
        for key in values
    }
    return replace(
        DEPLOYMENT,
        config_snapshot={**SNAPSHOT, "envVars": refs},
        env_values={key: SecretValue(value) for key, value in values.items()},
    )


def _hand_secret(cluster: FakeCluster, managed_by: str = "jimin-manual") -> None:
    cluster.add_secret(
        SECRET_PATH,
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "metadata": {
                "name": f"{NAMESPACE}-env",
                "namespace": NAMESPACE,
                "labels": {MANAGED_BY: managed_by},
            },
            "data": {"DATABASE_URL": "aGFuZA==", "AUTO_MIGRATE": "ZmFsc2U="},
        },
    )


def _ensure_namespace(world: World) -> None:
    world.cluster.namespaces[NAMESPACE] = {
        "metadata": {"name": NAMESPACE, "labels": {MANAGED_BY: "echo-was"}}
    }


def _secret_requests(cluster: FakeCluster) -> list[tuple[str, str]]:
    return [
        (request.method, request.url.path)
        for request in cluster.requests
        if "/secrets" in request.url.path
    ]


def _decoded(secret: dict[str, Any]) -> dict[str, str]:
    return {key: base64.b64decode(value).decode() for key, value in secret["data"].items()}


def _template(world: World) -> dict[str, Any]:
    template: dict[str, Any] = world.cluster.deployment(NAMESPACE)["spec"]["template"]
    return template


async def _apply(world: World, spec: DeploymentSpec, target: Any = TARGET) -> None:
    await world.deploy.apply(spec, target, str(spec.deployment_id))


# --- 값 사전 → 통째로 교체 ---


async def test_values_create_secret_with_label_and_annotation_when_missing() -> None:
    world = _world()

    await _apply(world, _spec({"DATABASE_URL": VALUE, "API_KEY": "two\nlines"}))

    secret = world.cluster.objects[SECRET_PATH]
    assert _decoded(secret) == {"DATABASE_URL": VALUE, "API_KEY": "two\nlines"}
    assert secret["type"] == "Opaque"
    assert secret["metadata"]["labels"][MANAGED_BY] == "echo-was"
    assert secret["metadata"]["labels"]["echo.service-id"] == str(TARGET.service_id)
    assert ("POST", SECRET_COLLECTION) in _secret_requests(world.cluster)
    annotation = _template(world)["metadata"]["annotations"]["echo.env-revision"]
    assert len(annotation) == 16
    assert VALUE not in annotation


async def test_values_replace_hand_made_secret_entirely() -> None:
    world = _world()
    _ensure_namespace(world)
    _hand_secret(world.cluster)
    before = world.cluster.objects[SECRET_PATH]["metadata"]["resourceVersion"]

    await _apply(world, _spec({"DATABASE_URL": VALUE}))

    secret = world.cluster.objects[SECRET_PATH]
    put = next(request for request in world.cluster.requests if request.method == "PUT")
    assert _decoded(secret) == {"DATABASE_URL": VALUE}  # AUTO_MIGRATE 는 남지 않는다
    assert secret["metadata"]["labels"][MANAGED_BY] == "echo-was"
    assert before.encode() in put.content
    assert put.url.params["fieldManager"] == "echo-was"
    assert put.headers["content-type"] == "application/json"
    # 서버 측 apply(PATCH)로 쓰지 않는다. 다른 manager 의 키가 남기 때문이다.
    assert ("PATCH", SECRET_PATH) not in _secret_requests(world.cluster)


async def test_replace_keeps_existing_secret_type() -> None:
    world = _world()
    _ensure_namespace(world)
    _hand_secret(world.cluster)
    world.cluster.objects[SECRET_PATH]["type"] = "example.com/custom"

    await _apply(world, _spec({"A": "x"}))

    assert world.cluster.objects[SECRET_PATH]["type"] == "example.com/custom"


async def test_secret_is_written_before_deployment_rolls() -> None:
    world = _world()

    await _apply(world, _spec({"A": "x"}))

    writes = [request.url.path for request in world.cluster.writes()]
    assert writes.index(SECRET_COLLECTION) < writes.index(
        f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/app"
    )


async def test_onprem_writes_secret_after_role_binding_and_retries_cache_delay() -> None:
    world = _world()
    world.cluster.fail_next("GET", SECRET_PATH, 403, count=2)

    await _apply(world, _spec({"A": "x"}), ONPREM_TARGET)

    assert _decoded(world.cluster.objects[SECRET_PATH]) == {"A": "x"}
    assert world.sleep.delays[:2] == [2.0, 2.0]


async def test_env_revision_depends_on_keys_and_versions_only() -> None:
    first = manifests.env_revision({"A": "1", "B": "2"})

    assert first == manifests.env_revision({"B": "2", "A": "1"})
    assert first != manifests.env_revision({"A": "1", "B": "3"})
    assert first != manifests.env_revision({"A": "1"})


async def test_annotation_changes_when_version_changes_not_when_value_changes() -> None:
    world = _world()
    await _apply(world, _spec({"A": "one"}))
    first = _template(world)["metadata"]["annotations"]["echo.env-revision"]

    await _apply(world, _spec({"A": "other"}))
    same_version = _template(world)["metadata"]["annotations"]["echo.env-revision"]
    await _apply(world, _spec({"A": "other"}, {"A": 2}))
    new_version = _template(world)["metadata"]["annotations"]["echo.env-revision"]

    assert first == same_version
    assert new_version != first


# --- {} ---


async def test_empty_values_empty_echo_owned_secret() -> None:
    world = _world()
    _ensure_namespace(world)
    _hand_secret(world.cluster, managed_by="echo-was")

    await _apply(world, _spec({}))

    assert world.cluster.objects[SECRET_PATH]["data"] == {}
    assert "annotations" not in _template(world)["metadata"]


async def test_empty_values_leave_hand_made_secret_alone() -> None:
    world = _world()
    _ensure_namespace(world)
    _hand_secret(world.cluster)
    before = dict(world.cluster.objects[SECRET_PATH])

    await _apply(world, _spec({}))

    assert world.cluster.objects[SECRET_PATH] == before
    assert _secret_requests(world.cluster) == [("GET", SECRET_PATH)]


async def test_empty_values_do_not_create_secret() -> None:
    world = _world()

    await _apply(world, _spec({}))

    assert SECRET_PATH not in world.cluster.objects
    assert _secret_requests(world.cluster) == [("GET", SECRET_PATH)]


# --- None ---


async def test_none_does_not_even_read_secret() -> None:
    world = _world()
    _ensure_namespace(world)
    _hand_secret(world.cluster)
    before = dict(world.cluster.objects[SECRET_PATH])

    await _apply(world, _spec(None))

    assert _secret_requests(world.cluster) == []
    assert world.cluster.objects[SECRET_PATH] == before
    assert "annotations" not in _template(world)["metadata"]


async def test_env_vars_key_in_snapshot_without_values_changes_nothing() -> None:
    """옛 파드처럼 env_values 를 모르는 실행 경로: 스냅샷의 새 키는 매니페스트를 바꾸지 않는다."""
    plain = _world()
    with_key = _world()
    new_snapshot = {**SNAPSHOT, "envVars": {"A": "gcp-sm:projects/p/secrets/s/versions/1"}}

    await _apply(plain, DEPLOYMENT)
    await _apply(with_key, replace(DEPLOYMENT, config_snapshot=new_snapshot))

    assert with_key.cluster.objects == plain.cluster.objects
    assert _secret_requests(with_key.cluster) == []


# --- 되돌리기 ---


async def test_revert_to_record_before_this_feature_leaves_echo_secret_alone() -> None:
    world = _world()
    await _apply(world, _spec({"A": "new"}))
    before = dict(world.cluster.objects[SECRET_PATH])
    world.cluster.requests.clear()
    previous = replace(
        DEPLOYMENT, deployment_id=DEPLOYMENT.deployment_id, image_digest="sha256:" + "c" * 64
    )

    result = await world.deploy.revert(TARGET, previous, "key")

    assert isinstance(result, Reverted)
    assert world.cluster.objects[SECRET_PATH] == before
    assert _secret_requests(world.cluster) == []
    assert "sha256:" + "c" * 64 in _template(world)["spec"]["containers"][0]["image"]


async def test_revert_with_previous_values_restores_them() -> None:
    world = _world()
    await _apply(world, _spec({"A": "new", "ONLY_NEW": "x"}))

    result = await world.deploy.revert(TARGET, _spec({"A": "old"}), "key")

    assert isinstance(result, Reverted)
    assert _decoded(world.cluster.objects[SECRET_PATH]) == {"A": "old"}


async def test_revert_without_previous_keeps_secret() -> None:
    world = _world()
    await _apply(world, _spec({"A": "x"}))

    await world.deploy.revert(TARGET, None, "key")

    assert SECRET_PATH in world.cluster.objects


# --- 충돌·실패 ---


async def test_conflict_rereads_and_retries() -> None:
    world = _world()
    _ensure_namespace(world)
    _hand_secret(world.cluster)
    world.cluster.fail_next("PUT", SECRET_PATH, 409, count=2)

    await _apply(world, _spec({"A": "x"}))

    assert _decoded(world.cluster.objects[SECRET_PATH]) == {"A": "x"}
    assert _secret_requests(world.cluster).count(("GET", SECRET_PATH)) == 3


async def test_create_race_falls_back_to_replace() -> None:
    world = _world()
    _ensure_namespace(world)
    original = world.cluster.handle

    def racing(request: Any) -> Any:
        if request.method == "POST" and request.url.path == SECRET_COLLECTION:
            _hand_secret(world.cluster)
        return original(request)

    world.cluster.handle = racing  # type: ignore[method-assign]

    await _apply(world, _spec({"A": "x"}))

    assert _decoded(world.cluster.objects[SECRET_PATH]) == {"A": "x"}


async def test_persistent_conflict_fails_with_deploy_failed() -> None:
    world = _world()
    _ensure_namespace(world)
    _hand_secret(world.cluster)
    world.cluster.fail_next("PUT", SECRET_PATH, 409, count=10)

    with pytest.raises(DeployApplyError) as failure:
        await _apply(world, _spec({"A": "x"}))

    assert failure.value.code is DeployRequestErrorCode.DEPLOY_FAILED


async def test_forbidden_secret_write_is_cluster_unauthorized() -> None:
    world = _world()
    world.cluster.fail_next("POST", SECRET_COLLECTION, 403)

    with pytest.raises(DeployApplyError) as failure:
        await _apply(world, _spec({"A": "x"}))

    assert failure.value.code is DeployRequestErrorCode.CLUSTER_UNAUTHORIZED


async def test_server_errors_retry_then_unavailable() -> None:
    world = _world()
    world.cluster.fail_next("GET", SECRET_PATH, 503, count=10)

    with pytest.raises(ExecutorUnavailableError):
        await _apply(world, _spec({"A": "x"}))

    assert world.sleep.delays == [1.0, 2.0, 4.0]


async def test_revert_secret_failure_is_revert_failed() -> None:
    world = _world()
    world.cluster.fail_next("GET", SECRET_PATH, 403)

    result = await world.deploy.revert(TARGET, _spec({"A": "x"}), "key")

    assert isinstance(result, RevertFailed)
    assert result.code is DeployRequestErrorCode.CLUSTER_UNAUTHORIZED


async def test_values_never_appear_in_logs_or_repr(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    world = _world()
    spec = _spec({"DATABASE_URL": VALUE})

    await _apply(world, spec)
    world.cluster.fail_next("GET", SECRET_PATH, 403)
    with pytest.raises(DeployApplyError):
        await _apply(world, spec)

    assert VALUE not in caplog.text
    assert base64.b64encode(VALUE.encode()).decode() not in caplog.text
    assert VALUE not in repr(spec)
