"""고객 앱 한 연결의 쿠버네티스 객체(순수 함수). 계약은 docs/executors.md 의 "쿠버네티스 직접 반영".

한 연결 = 한 네임스페이스 `{서비스}-{suffix}`(대표 주소 첫 레이블). 객체 이름은 모두 `app`.
라벨 `app.kubernetes.io/managed-by=echo-was`·`echo.service-id` 를 붙이고, 배포 기록 번호는
Deployment 어노테이션과 파드 라벨 `echo.deployment-id` 에 둔다.
"""

import base64
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "echo-was"
NAME_LABEL = "app.kubernetes.io/name"
SERVICE_ID_LABEL = "echo.service-id"
DEPLOYMENT_ID_KEY = "echo.deployment-id"
PULL_EXPIRES_AT_ANNOTATION = "echo.pull-expires-at"
APP_NAME = "app"
CONTAINER_NAME = "app"
PULL_SECRET_NAME = "echo-pull"
PULL_SECRET_USERNAME = "oauth2accesstoken"
APP_ROLE_BINDING_NAME = "echo-app-deployer"
APP_CLUSTER_ROLE = "echo-app-deployer"
SERVICE_PORT = 80
CPU_REQUEST = "100m"
MEMORY_REQUEST = "128Mi"
MEMORY_LIMIT = "512Mi"

DEFAULT_INGRESS_CLASS = {"aws": "alb", "gcp": "nginx", "onprem": "traefik"}

# AWS Load Balancer Controller 어노테이션(ALB 그룹 공유).
ALB_PREFIX = "alb.ingress.kubernetes.io/"


@dataclass(frozen=True)
class AppSpec:
    """연결 하나의 반영 값. 비밀값은 없다(앱 비밀값은 미리 둔 `{ns}-env` Secret 을 읽기만 한다)."""

    namespace: str
    service_id: uuid.UUID
    deployment_id: uuid.UUID
    image: str
    container_port: int
    health_check_path: str
    environment_name: str
    hosts: tuple[str, ...]
    ingress_class: str
    ingress_annotations: dict[str, str]
    uses_pull_secret: bool


def labels(service_id: uuid.UUID) -> dict[str, str]:
    return {MANAGED_BY_LABEL: MANAGED_BY_VALUE, SERVICE_ID_LABEL: str(service_id)}


def is_managed(namespace: dict[str, Any]) -> bool:
    metadata = namespace.get("metadata")
    found = metadata.get("labels") if isinstance(metadata, dict) else None
    return isinstance(found, dict) and found.get(MANAGED_BY_LABEL) == MANAGED_BY_VALUE


def namespace_manifest(namespace: str, service_id: uuid.UUID) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": namespace, "labels": labels(service_id)},
    }


def app_secret_name(namespace: str) -> str:
    """환경 담당이 미리 두는 앱 비밀값 Secret 이름."""
    return f"{namespace}-env"


def deployment_manifest(spec: AppSpec) -> dict[str, Any]:
    deployment_id = str(spec.deployment_id)
    pod_spec: dict[str, Any] = {
        "containers": [
            {
                "name": CONTAINER_NAME,
                "image": spec.image,
                "ports": [{"name": "http", "containerPort": spec.container_port}],
                # env 가 envFrom 보다 우선한다. 고객 Secret 이 이 두 값을 덮지 못한다.
                "env": [
                    {"name": "PORT", "value": str(spec.container_port)},
                    {"name": "ECHO_ENVIRONMENT", "value": spec.environment_name},
                ],
                "envFrom": [
                    {"secretRef": {"name": app_secret_name(spec.namespace), "optional": True}}
                ],
                "readinessProbe": {
                    "httpGet": {"path": spec.health_check_path, "port": spec.container_port},
                    "periodSeconds": 5,
                    "failureThreshold": 3,
                },
                "resources": {
                    "requests": {"cpu": CPU_REQUEST, "memory": MEMORY_REQUEST},
                    "limits": {"memory": MEMORY_LIMIT},
                },
            }
        ],
    }
    if spec.uses_pull_secret:
        pod_spec["imagePullSecrets"] = [{"name": PULL_SECRET_NAME}]
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": APP_NAME,
            "namespace": spec.namespace,
            "labels": labels(spec.service_id),
            "annotations": {DEPLOYMENT_ID_KEY: deployment_id},
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {NAME_LABEL: APP_NAME}},
            "template": {
                "metadata": {
                    "labels": {
                        **labels(spec.service_id),
                        NAME_LABEL: APP_NAME,
                        DEPLOYMENT_ID_KEY: deployment_id,
                    }
                },
                "spec": pod_spec,
            },
        },
    }


def service_manifest(spec: AppSpec) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": APP_NAME,
            "namespace": spec.namespace,
            "labels": labels(spec.service_id),
        },
        "spec": {
            "type": "ClusterIP",
            "selector": {NAME_LABEL: APP_NAME},
            "ports": [{"name": "http", "port": SERVICE_PORT, "targetPort": spec.container_port}],
        },
    }


def ingress_manifest(spec: AppSpec) -> dict[str, Any]:
    """규칙 = 환경별 주소 + 대표 주소(진입점이 Host 를 그대로 넘긴다). 둘 다 app:80."""
    metadata: dict[str, Any] = {
        "name": APP_NAME,
        "namespace": spec.namespace,
        "labels": labels(spec.service_id),
    }
    if spec.ingress_annotations:
        metadata["annotations"] = dict(spec.ingress_annotations)
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": metadata,
        "spec": {
            "ingressClassName": spec.ingress_class,
            "rules": [
                {
                    "host": host,
                    "http": {
                        "paths": [
                            {
                                "path": "/",
                                "pathType": "Prefix",
                                "backend": {
                                    "service": {"name": APP_NAME, "port": {"number": SERVICE_PORT}}
                                },
                            }
                        ]
                    },
                }
                for host in spec.hosts
            ],
        },
    }


def alb_annotations(
    group_name: str, certificate_arn: str, health_check_path: str
) -> dict[str, str]:
    """ALB 그룹 공유. 인증서는 명시한다(자동 찾기는 대표 주소용 인증서까지 찾다가 그룹 전체가 실패).

    80→443 강제 이동은 하지 않는다(진입점이 HTTP 로 대표 주소를 보낸다).
    """
    return {
        f"{ALB_PREFIX}group.name": group_name,
        f"{ALB_PREFIX}scheme": "internet-facing",
        f"{ALB_PREFIX}listen-ports": '[{"HTTP": 80}, {"HTTPS": 443}]',
        f"{ALB_PREFIX}certificate-arn": certificate_arn,
        f"{ALB_PREFIX}target-type": "ip",
        f"{ALB_PREFIX}healthcheck-path": health_check_path,
    }


def role_binding_manifest(
    namespace: str, service_id: uuid.UUID, account_namespace: str, account_name: str
) -> dict[str, Any]:
    """온프레: 앱 네임스페이스 안에서만 실행기 서비스 계정에 앱 배포 권한을 준다."""
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {
            "name": APP_ROLE_BINDING_NAME,
            "namespace": namespace,
            "labels": labels(service_id),
        },
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": APP_CLUSTER_ROLE,
        },
        "subjects": [
            {"kind": "ServiceAccount", "name": account_name, "namespace": account_namespace}
        ],
    }


def pull_secret_manifest(
    namespace: str,
    service_id: uuid.UUID,
    registry_host: str,
    access_token: str,
    expires_at: datetime,
) -> dict[str, Any]:
    """온프레: Artifact Registry 이미지 받기 자격(dockerconfigjson).

    만료 시각을 어노테이션에 둔다.
    """
    auth = base64.b64encode(f"{PULL_SECRET_USERNAME}:{access_token}".encode()).decode()
    config = {
        "auths": {
            registry_host: {
                "username": PULL_SECRET_USERNAME,
                "password": access_token,
                "auth": auth,
            }
        }
    }
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "kubernetes.io/dockerconfigjson",
        "metadata": {
            "name": PULL_SECRET_NAME,
            "namespace": namespace,
            "labels": labels(service_id),
            "annotations": {PULL_EXPIRES_AT_ANNOTATION: expires_at.isoformat()},
        },
        "data": {".dockerconfigjson": base64.b64encode(json.dumps(config).encode()).decode()},
    }


# --- 창구 경로 ---


def namespaces_path() -> str:
    return "/api/v1/namespaces"


def namespace_path(namespace: str) -> str:
    return f"/api/v1/namespaces/{namespace}"


def deployment_path(namespace: str) -> str:
    return f"/apis/apps/v1/namespaces/{namespace}/deployments/{APP_NAME}"


def service_path(namespace: str) -> str:
    return f"/api/v1/namespaces/{namespace}/services/{APP_NAME}"


def ingress_path(namespace: str) -> str:
    return f"/apis/networking.k8s.io/v1/namespaces/{namespace}/ingresses/{APP_NAME}"


def role_bindings_path(namespace: str) -> str:
    return f"/apis/rbac.authorization.k8s.io/v1/namespaces/{namespace}/rolebindings"


def pull_secret_path(namespace: str) -> str:
    return f"/api/v1/namespaces/{namespace}/secrets/{PULL_SECRET_NAME}"


def pods_path(namespace: str) -> str:
    return f"/api/v1/namespaces/{namespace}/pods"


def pod_log_path(namespace: str, pod: str) -> str:
    return f"/api/v1/namespaces/{namespace}/pods/{pod}/log"
