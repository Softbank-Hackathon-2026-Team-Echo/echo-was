"""DB 쓰기 전환 실행기(API 서버 17): 서울 GKE `echo-ops` 에 컷오버 Job 을 만들고 상태를 읽는다.

Job 모양은 echo-infra `replication/cutover-job/job.example.yaml` 과 같고, 그 네임스페이스의 VAP
(`echo-cutover-job-template`)가 받는 것만 쓴다: SA `echo-cutover`, 컨테이너 1개, 고정 digest 이미지,
command·args 없음, 환경 변수는 `KIND`·`PLAN_ID`·`ALLOW_PROMOTE` 값만, 볼륨 없음.

- 이름 = `echo-cutover-<계획 앞 8자>-<시도>`(인프라 README 규칙). 같은 이름이 이미 있으면(409)
  그 Job 의 PLAN_ID·KIND 가 이 계획과 같을 때만 같은 Job 으로 본다(멱등).
- 창구는 클러스터 안 주소(`https://kubernetes.default.svc`)이고 토큰은 파드 SA 토큰 파일을 호출마다
  읽는다(회전돼도 그대로). echo-was KSA `echo-was/echo-was` 의 권한은 echo-infra `echo-ops.yaml`
  Role `echo-was-cutover-jobs`(jobs create·get·list·watch, pods get·list, pods/log get)다.
- 상태: Job 조건 Complete → 종료 0, Failed → 파드 컨테이너 종료 코드. 종료 0 이면 파드 로그 끝에서
  건너뜀 이유(ALREADY_WRITER·ALREADY_DONE)·승격(PROMOTED)을 읽는다. 로그·토큰 원문은 남기지 않는다.
- 결과 행은 고객 DB 결과 표(`PostgresCutoverRunReader.find_by_plan`)에서 읽는다.
"""

import logging
import ssl
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.enums import CutoverKind
from app.executors.base import (
    CutoverRunResult,
    CutoverRunTarget,
    CutoverRunUnavailable,
    DbCutoverFinished,
    DbCutoverLost,
    DbCutoverPollResult,
    DbCutoverRequest,
    DbCutoverRunning,
    DbCutoverStarted,
    DbCutoverStartFailed,
    DbCutoverStartResult,
)

logger = logging.getLogger(__name__)

CUTOVER_JOB_SERVICE_ACCOUNT = "echo-cutover"
CUTOVER_JOB_CONTAINER = "cutover"
CUTOVER_JOB_LABEL = {"app.kubernetes.io/part-of": "echo-cutover"}
CUTOVER_JOB_ATTEMPT = 1
# 인프라 job.example.yaml 과 같은 값.
# activeDeadlineSeconds 를 넘으면 SIGTERM → 스크립트가 RESUME 하고 끝낸다.
CUTOVER_JOB_ACTIVE_DEADLINE_SECONDS = 120
CUTOVER_JOB_TTL_SECONDS = 86400
CUTOVER_JOB_TERMINATION_GRACE_SECONDS = 30
CUTOVER_LOG_TAIL_LINES = 200

JOB_CREATE_REJECTED = "JOB_CREATE_REJECTED"
JOB_CREATE_UNAUTHORIZED = "JOB_CREATE_UNAUTHORIZED"
JOB_CREATE_UNREACHABLE = "JOB_CREATE_UNREACHABLE"
JOB_NAME_CONFLICT = "JOB_NAME_CONFLICT"
JOB_NOT_FOUND = "JOB_NOT_FOUND"
RESULT_READER_MISSING = "CUTOVER_RESULT_READER_MISSING"

# 스크립트가 찍는 건너뜀·승격 표시(common.sh preflight, promote.sh). 로그에서 이것만 찾는다.
_LOG_REASONS = (
    ("ALREADY_WRITER", "ALREADY_WRITER"),
    ("ALREADY_DONE", "ALREADY_DONE"),
    ("승격 완료", "PROMOTED"),
)


class PlanCutoverRunReader(Protocol):
    async def find_by_plan(
        self, target: CutoverRunTarget, kind: CutoverKind, plan_id: Any
    ) -> CutoverRunResult: ...


def cutover_job_name(request: DbCutoverRequest) -> str:
    return f"echo-cutover-{request.plan_id.hex[:8]}-{CUTOVER_JOB_ATTEMPT}"


def cutover_job_manifest(
    request: DbCutoverRequest, *, namespace: str, image: str
) -> dict[str, Any]:
    """VAP 가 받는 모양 그대로의 Job(job.example.yaml)."""
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": cutover_job_name(request),
            "namespace": namespace,
            "labels": dict(CUTOVER_JOB_LABEL),
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": CUTOVER_JOB_ACTIVE_DEADLINE_SECONDS,
            "ttlSecondsAfterFinished": CUTOVER_JOB_TTL_SECONDS,
            "template": {
                "metadata": {"labels": dict(CUTOVER_JOB_LABEL)},
                "spec": {
                    "serviceAccountName": CUTOVER_JOB_SERVICE_ACCOUNT,
                    "restartPolicy": "Never",
                    "terminationGracePeriodSeconds": CUTOVER_JOB_TERMINATION_GRACE_SECONDS,
                    "containers": [
                        {
                            "name": CUTOVER_JOB_CONTAINER,
                            "image": image,
                            "env": [
                                {"name": "KIND", "value": request.job_kind.value},
                                {"name": "PLAN_ID", "value": str(request.plan_id)},
                                {
                                    "name": "ALLOW_PROMOTE",
                                    "value": "true" if request.allow_promote else "false",
                                },
                            ],
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "64Mi"},
                                "limits": {"memory": "256Mi"},
                            },
                            "securityContext": {
                                "runAsNonRoot": True,
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                },
            },
        },
    }


class InClusterKubeClient:
    """파드 SA 토큰으로 같은 클러스터 창구를 부른다. 토큰은 호출마다 파일에서 읽는다."""

    def __init__(
        self,
        api_url: str,
        service_account_dir: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
        read_text: Callable[[Path], str] = lambda path: path.read_text(),
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._dir = Path(service_account_dir)
        self._timeout = timeout_seconds
        self._transport = transport
        self._read_text = read_text
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            verify: ssl.SSLContext | bool = True
            if self._transport is None:
                ca_path = self._dir / "ca.crt"
                verify = (
                    ssl.create_default_context(cafile=str(ca_path)) if ca_path.exists() else True
                )
            self._client = httpx.AsyncClient(
                base_url=self._api_url,
                verify=verify,
                timeout=self._timeout,
                follow_redirects=False,
                transport=self._transport,
            )
        return self._client

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """창구 응답. 닿지 못하면 httpx.TransportError 를 그대로 올린다."""
        token = self._read_text(self._dir / "token").strip()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        return await self._http().request(
            method, path, params=params, json=json_body, headers=headers
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class KubernetesDbCutoverExecutor:
    def __init__(
        self,
        client: InClusterKubeClient,
        *,
        namespace: str,
        image: str,
        reader: PlanCutoverRunReader | None,
    ) -> None:
        self._client = client
        self._namespace = namespace
        self._image = image
        self._reader = reader

    @property
    def enabled(self) -> bool:
        return True

    @property
    def _jobs_path(self) -> str:
        return f"/apis/batch/v1/namespaces/{self._namespace}/jobs"

    async def start(self, request: DbCutoverRequest) -> DbCutoverStartResult:
        name = cutover_job_name(request)
        manifest = cutover_job_manifest(request, namespace=self._namespace, image=self._image)
        try:
            response = await self._client.request("POST", self._jobs_path, json_body=manifest)
            if response.status_code == 409:
                existing = await self._client.request("GET", f"{self._jobs_path}/{name}")
                if existing.status_code == 200 and _same_job(_json(existing), request):
                    return DbCutoverStarted(name)
                if existing.status_code >= 500:
                    return DbCutoverStartFailed(JOB_CREATE_UNREACHABLE, retryable=True)
                return DbCutoverStartFailed(JOB_NAME_CONFLICT)
        except httpx.TransportError:
            return DbCutoverStartFailed(JOB_CREATE_UNREACHABLE, retryable=True)
        if response.is_success:
            return DbCutoverStarted(name)
        if response.status_code in (401, 403):
            return DbCutoverStartFailed(JOB_CREATE_UNAUTHORIZED)
        if response.status_code >= 500 or response.status_code == 429:
            return DbCutoverStartFailed(JOB_CREATE_UNREACHABLE, retryable=True)
        # 400·422: VAP 거부(이미지 digest 가 다름 등). 다시 불러도 같다.
        _log_rejected(name, response.status_code)
        return DbCutoverStartFailed(JOB_CREATE_REJECTED)

    async def poll(self, job_name: str) -> DbCutoverPollResult:
        try:
            response = await self._client.request("GET", f"{self._jobs_path}/{job_name}")
        except httpx.TransportError:
            return DbCutoverRunning()
        if response.status_code == 404:
            return DbCutoverLost(JOB_NOT_FOUND)
        if not response.is_success:
            return DbCutoverRunning()
        conditions = _conditions(_json(response))
        if conditions.get("Complete") == "True":
            return DbCutoverFinished(0, await self._reason(job_name))
        if conditions.get("Failed") == "True":
            return DbCutoverFinished(await self._exit_code(job_name))
        return DbCutoverRunning()

    async def find_run(self, request: DbCutoverRequest) -> CutoverRunResult:
        if self._reader is None:
            return CutoverRunUnavailable(RESULT_READER_MISSING)
        return await self._reader.find_by_plan(request.target, request.result_kind, request.plan_id)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _pod(self, job_name: str) -> dict[str, Any] | None:
        try:
            response = await self._client.request(
                "GET",
                f"/api/v1/namespaces/{self._namespace}/pods",
                params={"labelSelector": f"job-name={job_name}"},
            )
        except httpx.TransportError:
            return None
        if not response.is_success:
            return None
        items = _json(response).get("items") or []
        return items[-1] if items and isinstance(items[-1], dict) else None

    async def _exit_code(self, job_name: str) -> int | None:
        pod = await self._pod(job_name)
        if pod is None:
            return None
        for status in (pod.get("status") or {}).get("containerStatuses") or []:
            if status.get("name") != CUTOVER_JOB_CONTAINER:
                continue
            terminated = (status.get("state") or {}).get("terminated") or {}
            code = terminated.get("exitCode")
            return int(code) if isinstance(code, int) else None
        return None

    async def _reason(self, job_name: str) -> str | None:
        pod = await self._pod(job_name)
        name = ((pod or {}).get("metadata") or {}).get("name")
        if not name:
            return None
        try:
            response = await self._client.request(
                "GET",
                f"/api/v1/namespaces/{self._namespace}/pods/{name}/log",
                params={
                    "container": CUTOVER_JOB_CONTAINER,
                    "tailLines": str(CUTOVER_LOG_TAIL_LINES),
                },
            )
        except httpx.TransportError:
            return None
        if not response.is_success:
            return None
        return log_reason(response.text)


def log_reason(text: str) -> str | None:
    """컷오버 스크립트 로그에서 건너뜀·승격 이유를 찾는다. 없으면 None."""
    for marker, reason in _LOG_REASONS:
        if marker in text:
            return reason
    return None


def _same_job(body: dict[str, Any], request: DbCutoverRequest) -> bool:
    try:
        containers = body["spec"]["template"]["spec"]["containers"]
        env = {item["name"]: item.get("value") for item in containers[0].get("env") or []}
    except (KeyError, IndexError, TypeError):
        return False
    return env.get("PLAN_ID") == str(request.plan_id) and env.get("KIND") == request.job_kind.value


def _conditions(body: dict[str, Any]) -> dict[str, str]:
    items = (body.get("status") or {}).get("conditions") or []
    return {
        str(item.get("type")): str(item.get("status")) for item in items if isinstance(item, dict)
    }


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _log_rejected(name: str, status_code: int) -> None:
    logger.warning(
        "cutover job rejected",
        extra={"action": "create_cutover_job", "job_name": name, "status_code": status_code},
    )
