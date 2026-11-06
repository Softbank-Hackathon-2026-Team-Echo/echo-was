"""C-SEC-1 의 "참조로만 전달" 부분: 앱 Deployment 는 비밀을 Secret 참조로만 받는다(sec-1-2 AC-4).

C-SEC-1 판정(키가 도착에 모두 있는가)은 전달 방식을 다시 보지 않는다. 그 대신 이 시험이 매니페스트
모양을 고정한다. 값이 들어 있는 곳은 `{ns}-env` Secret 하나뿐이다
(실제 배포 경로 전체는 `test_secret_value_appears_nowhere_after_full_deploy` 가 본다).
"""

import json
import uuid

from app.core.secrets import SecretValue
from app.executors.kubernetes.manifests import (
    AppSpec,
    app_env_secret_manifest,
    app_secret_name,
    deployment_manifest,
)

NAMESPACE = "shop-api-a1b2c3d4"
SERVICE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
VALUE = "invariant-secret-value-71d0"


def _spec() -> AppSpec:
    return AppSpec(
        namespace=NAMESPACE,
        service_id=SERVICE_ID,
        deployment_id=uuid.UUID("00000000-0000-0000-0000-0000000000d1"),
        image="registry.example/app@sha256:" + "a" * 64,
        container_port=8080,
        health_check_path="/healthz",
        environment_name="gcp-seoul",
        hosts=("shop-api-a1b2c3d4.gcp-seoul.example.com",),
        ingress_class="nginx",
        ingress_annotations={},
        uses_pull_secret=False,
        env_revision="0123456789ab",
    )


def test_deployment_receives_app_secrets_only_by_secret_reference() -> None:
    manifest = deployment_manifest(_spec())

    containers = manifest["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    container = containers[0]
    # 값이 적힌 env 는 Echo 가 정한 두 개뿐이고, 개별 키 참조(valueFrom)도 없다.
    assert [entry["name"] for entry in container["env"]] == ["PORT", "ECHO_ENVIRONMENT"]
    assert all(set(entry) == {"name", "value"} for entry in container["env"])
    assert container["envFrom"] == [
        {"secretRef": {"name": app_secret_name(NAMESPACE), "optional": True}}
    ]
    assert "volumes" not in manifest["spec"]["template"]["spec"]


def test_secret_value_is_only_in_the_app_env_secret() -> None:
    secret = app_env_secret_manifest(NAMESPACE, SERVICE_ID, {"API_KEY": SecretValue(VALUE)})
    deployment = deployment_manifest(_spec())

    assert VALUE not in json.dumps(deployment)
    assert secret["metadata"]["name"] == app_secret_name(NAMESPACE)
    assert set(secret["data"]) == {"API_KEY"}
