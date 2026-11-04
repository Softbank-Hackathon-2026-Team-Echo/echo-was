"""실제 실행기(API 서버 3-2) 시험용 가짜 전송(httpx.MockTransport). 실제 외부 시스템에 닿지 않는다.

- FakeActions: GitHub App 설치 토큰·workflow_dispatch·실행·아티팩트·잡 로그.
- FakeCloud: GCE 메타데이터 서버·AWS STS·EKS DescribeCluster·GKE clusters.get.
- FakeCluster: 쿠버네티스 창구(네임스페이스·서버 측 apply·파드·로그·삭제).
- FakeCloudflare: DNS 레코드 목록·만들기·지우기.

토큰 값은 시험 안 상수다. 응답·로그에 나오지 않는지 시험이 확인한다.
"""

import base64
import io
import json
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx

from app.core.secrets import SecretRef, SecretValue
from app.executors.kubernetes.api import KubeHttpFactory

API_BASE = "https://api.github.test"
BUILD_REPOSITORY = "Softbank-Hackathon-2026-Team-Echo/echo-was"
WORKFLOW = "customer-build.yml"
ACTIONS_TOKEN = "ghs_actions_write_token_for_tests_9c1e7b"
AR_REPOSITORY = "asia-northeast3-docker.pkg.dev/team-echo-final/echo/app"
ECR_PREFIX = "706878591119.dkr.ecr.ap-northeast-1.amazonaws.com/echo"
AR_PREFIX = "asia-northeast3-docker.pkg.dev/team-echo-final/echo"
DIGEST = "sha256:" + "b" * 64
BLOB_HOST = "blob.actions.test"

METADATA_BASE = "http://metadata.test"
GOOGLE_ACCESS_TOKEN = "google-access-token-for-tests-"
GOOGLE_ID_TOKEN = "eyJhbGciOiJSUzI1NiJ9.google-identity-token-for-tests.sig"
AWS_ACCESS_KEY = "TESTACCESSKEYID00001"
AWS_SESSION_TOKEN = "aws-session-token-for-tests"
EKS_ENDPOINT = "https://abc123.gr7.ap-northeast-1.eks.amazonaws.com"
GKE_ENDPOINT = "34.64.0.10"
FAKE_CA_PEM = "-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----\n"
ONPREM_API_URL = "https://k3s-k2f6xje5.echoport.test"
ONPREM_TUNNEL = "c446e0fe-0000-0000-0000-000000000000.cfargotunnel.com"
ONPREM_TOKEN_ENV = "ECHO_SECRET_TEAM_ECHO__ONPREM_TOKEN"
CLOUDFLARE_TOKEN_ENV = "ECHO_SECRET_CLOUDFLARE_DNS_TOKEN"
CLOUDFLARE_TOKEN = "cloudflare_dns_token_for_tests"
CLOUDFLARE_BASE = "https://api.cloudflare.test/client/v4"
CLOUDFLARE_ZONE = "zone123"


def onprem_service_account_token(
    namespace: str = "echo-system", name: str = "echo-executor"
) -> str:
    """서명 없는 모양만 맞춘 서비스 계정 JWT(시험용)."""
    payload = {"sub": f"system:serviceaccount:{namespace}:{name}"}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{body}.c2lnbmF0dXJl"


class DictSecretClient:
    """env 위치 → 값 사전. 없으면 SECRET_NOT_FOUND."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.reads: list[str] = []

    async def get_secret(self, ref: SecretRef) -> SecretValue:
        from app.clients.secret_client import SecretLookupError
        from app.enums import ConnectionErrorCode

        self.reads.append(ref.location)
        value = self.values.get(ref.location)
        if value is None:
            raise SecretLookupError(ConnectionErrorCode.SECRET_NOT_FOUND)
        return SecretValue(value)


class TestClock:
    __test__ = False

    def __init__(self, start: datetime | None = None) -> None:
        self.value = start or datetime(2026, 11, 3, 6, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


@dataclass
class RecordingSleep:
    delays: list[float] = field(default_factory=list)
    on_sleep: Callable[[float], None] | None = None

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep(seconds)


def result_zip(build_id: str, digest: str = DIGEST, repository: str = AR_REPOSITORY) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(
            "build-result.json",
            json.dumps(
                {
                    "build_id": build_id,
                    "image_digest": digest,
                    "ar_repository": repository,
                    "ecr_repository": f"{ECR_PREFIX}/app",
                }
            ),
        )
    return buffer.getvalue()


def scan_zip(
    build_id: str,
    digest: str = DIGEST,
    *,
    trivy: object | None = None,
    gitleaks: object | None = None,
    meta: dict[str, Any] | None = None,
    raw_files: dict[str, bytes] | None = None,
) -> bytes:
    """customer-build.yml 이 남기는 security-scan 아티팩트(보안·거버넌스 11) 모양."""
    body: dict[str, Any] = {
        "schema_version": 1,
        "build_id": build_id,
        "image_digest": digest,
        "ar_repository": AR_REPOSITORY,
        "scanned_at": "2026-11-04T08:00:00Z",
        "trivy": {"version": "0.75.0", "outcome": "success"},
        "gitleaks": {"version": "8.30.1", "outcome": "success"},
        **(meta or {}),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        if "meta.json" not in (raw_files or {}):
            bundle.writestr("meta.json", json.dumps(body))
        if trivy is not None:
            bundle.writestr("trivy-image.json", json.dumps(trivy))
        if gitleaks is not None:
            bundle.writestr("gitleaks.json", json.dumps(gitleaks))
        for name, content in (raw_files or {}).items():
            bundle.writestr(name, content)
    return buffer.getvalue()


# security-scan 아티팩트 id = 실행 id + 이 값(build-result 아티팩트 id = 실행 id).
SCAN_ARTIFACT_ID_OFFSET = 100_000


@dataclass
class FakeRun:
    id: int
    build_id: str
    created_at: str
    status: str = "queued"
    conclusion: str | None = None
    artifact: bytes | None = None
    scan_artifact: bytes | None = None
    log: str = ""
    visible_after_list_calls: int = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            # name 은 워크플로 이름이다. 찾기는 display_title(run-name)로 해야 한다.
            "name": "Customer build",
            "display_title": f"build {self.build_id}",
            "status": self.status,
            "conclusion": self.conclusion,
            "created_at": self.created_at,
        }


@dataclass
class FakeActions:
    """GitHub Actions REST 흉내. dispatch_status 로 dispatch 응답을 바꾼다."""

    clock: TestClock
    dispatch_status: int = 200
    runs: dict[int, FakeRun] = field(default_factory=dict)
    dispatches: list[dict[str, Any]] = field(default_factory=list)
    token_requests: list[dict[str, Any]] = field(default_factory=list)
    list_calls: int = 0
    requests: list[httpx.Request] = field(default_factory=list)
    override: Callable[[httpx.Request], httpx.Response | None] | None = None
    new_run_visible_after_list_calls: int = 0
    _next_id: int = 9000

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def add_run(self, build_id: str, **fields: Any) -> FakeRun:
        self._next_id += 1
        run = FakeRun(
            id=self._next_id,
            build_id=build_id,
            created_at=self.clock.now().isoformat().replace("+00:00", "Z"),
            **fields,
        )
        self.runs[run.id] = run
        return run

    def finish(self, run: FakeRun, *, success: bool = True, artifact: bytes | None = None) -> None:
        run.status = "completed"
        run.conclusion = "success" if success else "failure"
        run.artifact = artifact if artifact is not None else result_zip(run.build_id)

    def finish_all(self) -> None:
        for run in self.runs.values():
            self.finish(run)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.override is not None:
            overridden = self.override(request)
            if overridden is not None:
                return overridden
        if request.url.host == BLOB_HOST:
            return self._blob(request)
        auth = request.headers.get("authorization", "")
        parts = request.url.path.strip("/").split("/")
        if parts[:2] == ["app", "installations"]:
            self.token_requests.append(json.loads(request.content))
            expires = (self.clock.now() + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
            return httpx.Response(201, json={"token": ACTIONS_TOKEN, "expires_at": expires})
        repo = "/".join(parts[1:3])
        rest = parts[3:]
        if repo != BUILD_REPOSITORY:
            return httpx.Response(404)
        if rest == ["installation"]:
            return httpx.Response(200, json={"id": 77})
        if auth != f"Bearer {ACTIONS_TOKEN}":
            return httpx.Response(401)
        if rest == ["actions", "workflows", WORKFLOW, "runs"]:
            self.list_calls += 1
            visible = [
                run.as_json()
                for run in self.runs.values()
                if self.list_calls > run.visible_after_list_calls
            ]
            return httpx.Response(200, json={"total_count": len(visible), "workflow_runs": visible})
        if rest == ["actions", "workflows", WORKFLOW, "dispatches"]:
            body = json.loads(request.content)
            self.dispatches.append(body)
            if self.dispatch_status >= 300:
                return httpx.Response(self.dispatch_status, json={"message": "nope"})
            run = self.add_run(
                body["inputs"]["build_id"],
                visible_after_list_calls=self.list_calls + self.new_run_visible_after_list_calls,
            )
            if self.dispatch_status == 204:
                return httpx.Response(204)
            return httpx.Response(200, json={"workflow_run_id": run.id, "run_url": "x"})
        if rest[:2] == ["actions", "runs"] and len(rest) >= 3:
            run = self.runs.get(int(rest[2]))
            if run is None:
                return httpx.Response(404)
            if len(rest) == 3:
                return httpx.Response(200, json=run.as_json())
            if rest[3] == "artifacts":
                wanted = request.url.params.get("name")
                stored = (
                    (run.id, "build-result", run.artifact),
                    (run.id + SCAN_ARTIFACT_ID_OFFSET, "security-scan", run.scan_artifact),
                )
                artifacts = [
                    {
                        "id": artifact_id,
                        "name": name,
                        "expired": False,
                        "archive_download_url": (
                            f"{API_BASE}/repos/{BUILD_REPOSITORY}/actions/artifacts/"
                            f"{artifact_id}/zip"
                        ),
                    }
                    for artifact_id, name, content in stored
                    if content is not None and wanted in (None, name)
                ]
                return httpx.Response(200, json={"artifacts": artifacts})
            if rest[3] == "jobs":
                return httpx.Response(
                    200,
                    json={"jobs": [{"id": run.id * 10, "started_at": run.created_at}]},
                )
        if rest[:2] == ["actions", "artifacts"]:
            return httpx.Response(
                302, headers={"location": f"https://{BLOB_HOST}/artifact/{rest[2]}"}
            )
        if rest[:2] == ["actions", "jobs"]:
            return httpx.Response(
                302, headers={"location": f"https://{BLOB_HOST}/log/{int(rest[2]) // 10}"}
            )
        return httpx.Response(404)

    def _blob(self, request: httpx.Request) -> httpx.Response:
        # 서명된 저장소 주소에는 우리 토큰이 실리면 안 된다.
        assert "authorization" not in request.headers
        kind, blob_id = request.url.path.strip("/").split("/")
        if kind == "artifact" and int(blob_id) > SCAN_ARTIFACT_ID_OFFSET:
            scanned = self.runs[int(blob_id) - SCAN_ARTIFACT_ID_OFFSET]
            return httpx.Response(200, content=scanned.scan_artifact or b"")
        run = self.runs[int(blob_id)]
        if kind == "artifact":
            return httpx.Response(200, content=run.artifact or b"")
        return httpx.Response(200, text="﻿" + run.log)


def sts_response(expires_at: datetime) -> str:
    expiration = expires_at.isoformat().replace("+00:00", "Z")
    return (
        '<AssumeRoleWithWebIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
        "<AssumeRoleWithWebIdentityResult><Credentials>"
        f"<AccessKeyId>{AWS_ACCESS_KEY}</AccessKeyId>"
        "<SecretAccessKey>aws-secret-key-for-tests</SecretAccessKey>"
        f"<SessionToken>{AWS_SESSION_TOKEN}</SessionToken>"
        f"<Expiration>{expiration}</Expiration>"
        "</Credentials></AssumeRoleWithWebIdentityResult></AssumeRoleWithWebIdentityResponse>"
    )


@dataclass
class FakeCloud:
    """메타데이터 서버 + AWS STS·EKS + GKE API."""

    clock: TestClock
    token_expires_in: int = 3599
    sts_status: int = 200
    metadata_token_calls: int = 0
    identity_calls: int = 0
    sts_calls: list[dict[str, list[str]]] = field(default_factory=list)
    eks_calls: int = 0
    gke_calls: list[str] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if str(url).startswith(METADATA_BASE):
            assert request.headers.get("metadata-flavor") == "Google"
            if url.path.endswith("/token"):
                self.metadata_token_calls += 1
                return httpx.Response(
                    200,
                    json={
                        "access_token": f"{GOOGLE_ACCESS_TOKEN}{self.metadata_token_calls}",
                        "expires_in": self.token_expires_in,
                        "token_type": "Bearer",
                    },
                )
            if url.path.endswith("/identity"):
                self.identity_calls += 1
                assert url.params["audience"] == "sts.amazonaws.com"
                return httpx.Response(200, text=GOOGLE_ID_TOKEN)
        if url.host.startswith("sts."):
            self.sts_calls.append(parse_qs(request.content.decode()))
            if self.sts_status != 200:
                return httpx.Response(self.sts_status, text="<Error>denied</Error>")
            return httpx.Response(200, text=sts_response(self.clock.now() + timedelta(hours=1)))
        if url.host.startswith("eks."):
            self.eks_calls += 1
            assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256")
            return httpx.Response(
                200,
                json={
                    "cluster": {
                        "endpoint": EKS_ENDPOINT,
                        "certificateAuthority": {
                            "data": base64.b64encode(FAKE_CA_PEM.encode()).decode()
                        },
                    }
                },
            )
        if url.host == "container.googleapis.com":
            self.gke_calls.append(url.path)
            return httpx.Response(
                200,
                json={
                    "endpoint": GKE_ENDPOINT,
                    "masterAuth": {
                        "clusterCaCertificate": base64.b64encode(FAKE_CA_PEM.encode()).decode()
                    },
                },
            )
        return httpx.Response(404)


@dataclass
class FakePod:
    name: str
    deployment_id: str
    created_at: str
    waiting_reason: str | None = None
    log: str = ""


@dataclass
class FakeCluster:
    """쿠버네티스 창구 흉내(모든 서버 주소를 같은 상태로 받는다).

    - objects: 경로 → apply 한 매니페스트. namespaces: 이름 → 객체.
    - rolled_out: True 면 Deployment 상태가 준비 완료.
    - fail: (메서드, 경로 앞부분) → 상태 코드(한 번 쓰면 count 만큼 줄어든다).
    """

    clock: TestClock
    namespaces: dict[str, dict[str, Any]] = field(default_factory=dict)
    deleting: set[str] = field(default_factory=set)
    objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    generations: dict[str, int] = field(default_factory=dict)
    role_bindings: dict[str, dict[str, Any]] = field(default_factory=dict)
    pods: list[FakePod] = field(default_factory=list)
    rolled_out: bool = False
    fail: dict[tuple[str, str], tuple[int, int]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)

    def factory(self) -> KubeHttpFactory:
        def build(server: str, ca_pem: str | None, timeout: float) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                base_url=server, transport=httpx.MockTransport(self.handle), timeout=timeout
            )

        return build

    def fail_next(self, method: str, path_prefix: str, status: int, count: int = 1) -> None:
        self.fail[(method, path_prefix)] = (status, count)

    def finish_namespace_deletion(self) -> None:
        for name in self.deleting:
            self.namespaces.pop(name, None)
        self.deleting.clear()

    def writes(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.method != "GET"]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.tokens.append(request.headers.get("authorization", ""))
        path = request.url.path
        for (method, prefix), (status, count) in list(self.fail.items()):
            if request.method == method and path.startswith(prefix) and count > 0:
                self.fail[(method, prefix)] = (status, count - 1)
                return httpx.Response(status, json={"kind": "Status", "code": status})
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return httpx.Response(401)
        parts = path.strip("/").split("/")
        if path == "/api/v1/namespaces" and request.method == "POST":
            body = json.loads(request.content)
            name = body["metadata"]["name"]
            if name in self.namespaces:
                return httpx.Response(409, json={"kind": "Status", "code": 409})
            self.namespaces[name] = body
            return httpx.Response(201, json=body)
        if len(parts) == 4 and parts[:3] == ["api", "v1", "namespaces"]:
            name = parts[3]
            if request.method == "DELETE":
                if name not in self.namespaces:
                    return httpx.Response(404)
                self.deleting.add(name)
                return httpx.Response(200, json={"kind": "Namespace"})
            if name not in self.namespaces:
                return httpx.Response(404)
            namespace = dict(self.namespaces[name])
            if name in self.deleting:
                namespace["status"] = {"phase": "Terminating"}
            return httpx.Response(200, json=namespace)
        if path.endswith("/rolebindings") and request.method == "POST":
            body = json.loads(request.content)
            key = f"{body['metadata']['namespace']}/{body['metadata']['name']}"
            if key in self.role_bindings:
                return httpx.Response(409)
            self.role_bindings[key] = body
            return httpx.Response(201, json=body)
        if path.endswith("/pods") and request.method == "GET":
            selector = request.url.params.get("labelSelector", "")
            wanted = selector.split("=", 1)[1]
            items = [self._pod_json(pod) for pod in self.pods if pod.deployment_id == wanted]
            return httpx.Response(200, json={"items": items})
        if path.endswith("/log"):
            pod = next((pod for pod in self.pods if pod.name == parts[-2]), None)
            if pod is None:
                return httpx.Response(404)
            assert request.url.params["timestamps"] == "true"
            return httpx.Response(200, text=pod.log)
        if request.method == "PATCH":
            assert request.headers["content-type"] == "application/apply-patch+yaml"
            assert request.url.params["fieldManager"] == "echo-was"
            body = json.loads(request.content)
            namespace = body["metadata"].get("namespace")
            if namespace is not None and namespace not in self.namespaces:
                return httpx.Response(404)
            previous = self.objects.get(path)
            if previous is None or previous.get("spec") != body.get("spec"):
                self.generations[path] = self.generations.get(path, 0) + 1
            self.objects[path] = body
            return httpx.Response(200, json=self._with_status(path, body))
        if request.method == "DELETE":
            if self.objects.pop(path, None) is None:
                return httpx.Response(404)
            return httpx.Response(200, json={"kind": "Status"})
        if request.method == "GET":
            body = self.objects.get(path)
            if body is None:
                return httpx.Response(404)
            return httpx.Response(200, json=self._with_status(path, body))
        return httpx.Response(404)

    def _with_status(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        result = json.loads(json.dumps(body))
        generation = self.generations.get(path, 1)
        result["metadata"]["generation"] = generation
        if body.get("kind") == "Deployment":
            ready = 1 if self.rolled_out else 0
            result["status"] = {
                "observedGeneration": generation,
                "replicas": 1,
                "updatedReplicas": ready,
                "availableReplicas": ready,
            }
        return result

    @staticmethod
    def _pod_json(pod: FakePod) -> dict[str, Any]:
        state: dict[str, Any] = (
            {"waiting": {"reason": pod.waiting_reason}} if pod.waiting_reason else {"running": {}}
        )
        return {
            "metadata": {
                "name": pod.name,
                "creationTimestamp": pod.created_at,
                "labels": {"echo.deployment-id": pod.deployment_id},
            },
            "status": {"containerStatuses": [{"name": "app", "state": state}]},
        }

    def deployment(self, namespace: str) -> dict[str, Any]:
        return self.objects[f"/apis/apps/v1/namespaces/{namespace}/deployments/app"]

    def add_pod(self, deployment_id: uuid.UUID, **fields: Any) -> FakePod:
        pod = FakePod(
            name=fields.pop("name", f"app-{len(self.pods)}"),
            deployment_id=str(deployment_id),
            created_at=fields.pop(
                "created_at", self.clock.now().isoformat().replace("+00:00", "Z")
            ),
            **fields,
        )
        self.pods.append(pod)
        return pod


@dataclass
class FakeCloudflare:
    records: list[dict[str, Any]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {CLOUDFLARE_TOKEN}":
            return httpx.Response(403, json={"success": False})
        parts = request.url.path.split("/dns_records")
        if request.method == "GET":
            name = request.url.params.get("name")
            found = [record for record in self.records if record["name"] == name]
            return httpx.Response(200, json={"success": True, "result": found})
        if request.method == "POST":
            body = json.loads(request.content)
            record = {**body, "id": f"rec{len(self.records) + 1}"}
            self.records.append(record)
            return httpx.Response(200, json={"success": True, "result": record})
        if request.method == "DELETE":
            record_id = parts[1].strip("/")
            before = len(self.records)
            self.records = [record for record in self.records if record["id"] != record_id]
            return httpx.Response(200 if len(self.records) < before else 404, json={})
        return httpx.Response(404)
