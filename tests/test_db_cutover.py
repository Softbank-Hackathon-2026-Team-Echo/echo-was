"""DB 쓰기 전환 단계(API 서버 17) 단위 시험: Job 모양(VAP), 쿠버네티스 실행기, 판정 규칙, 설정."""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import DbCutoverExecutorMode, Settings
from app.enums import CutoverKind, MigrationErrorCode, MigrationPlanMode
from app.executors import build_executor_set
from app.executors.base import (
    CutoverRun,
    CutoverRunNotFound,
    CutoverRunTarget,
    CutoverRunUnavailable,
    DbCutoverFinished,
    DbCutoverJobKind,
    DbCutoverLost,
    DbCutoverRunning,
    DbCutoverStarted,
    DbCutoverStartFailed,
    DisabledDbCutoverExecutor,
)
from app.executors.fake import FakeDbCutoverExecutor
from app.executors.kubernetes.cutover_job import (
    JOB_CREATE_REJECTED,
    JOB_CREATE_UNAUTHORIZED,
    JOB_CREATE_UNREACHABLE,
    JOB_NAME_CONFLICT,
    JOB_NOT_FOUND,
    InClusterKubeClient,
    KubernetesDbCutoverExecutor,
    cutover_job_manifest,
    cutover_job_name,
    log_reason,
)
from app.services.migration_db_cutover import (
    CutoverRecovery,
    db_cutover_request,
    judge_db_cutover,
    run_from_json,
    run_to_json,
)
from tests.real_executor_fakes import TestClock

DATABASE_URL = "postgresql+asyncpg://x:y@127.0.0.1:1/z"
IMAGE = "asia-northeast3-docker.pkg.dev/p/echo/cutover@sha256:" + "a" * 64
PLAN_ID = uuid.UUID("0f1e2d3c-4b5a-4968-8776-655443322110")
NAMESPACE = "echo-ops"
JOBS = f"/apis/batch/v1/namespaces/{NAMESPACE}/jobs"
TOKEN = "sa-token-value"


def _request(mode: MigrationPlanMode = MigrationPlanMode.PLANNED) -> Any:
    return db_cutover_request(PLAN_ID, mode, uuid.uuid4(), "gcp")


def _run(**changes: Any) -> CutoverRun:
    values: dict[str, Any] = {
        "kind": CutoverKind.CUTOVER,
        "paused_at": datetime(2026, 11, 7, 1, 0, tzinfo=UTC),
        "resumed_at": datetime(2026, 11, 7, 1, 0, 17, tzinfo=UTC),
        "write_pause_ms": 17500,
        "drain_ms": 100,
        "catchup_ms": 200,
        "switch_ms": 300,
        "rows_total": 120,
        "missing_rows": 0,
        "duplicate_rows": 0,
        "mode": "NORMAL",
        "rpo_estimate_ms": None,
    }
    values.update(changes)
    return CutoverRun(**values)


# --- Job 모양: echo-infra VAP(echo-cutover-job-template)가 받는 것만 ---


def test_manifest_matches_infra_vap_rules() -> None:
    manifest = cutover_job_manifest(_request(), namespace=NAMESPACE, image=IMAGE)
    pod = manifest["spec"]["template"]["spec"]
    [container] = pod["containers"]

    assert manifest["metadata"]["name"] == "echo-cutover-0f1e2d3c-1"
    assert manifest["metadata"]["namespace"] == NAMESPACE
    assert pod["serviceAccountName"] == "echo-cutover"
    assert container["image"] == IMAGE
    assert "command" not in container and "args" not in container and "envFrom" not in container
    assert container["env"] == [
        {"name": "KIND", "value": "cutover"},
        {"name": "PLAN_ID", "value": str(PLAN_ID)},
        {"name": "ALLOW_PROMOTE", "value": "false"},
    ]
    assert "volumes" not in pod and "initContainers" not in pod
    assert not container["securityContext"].get("privileged", False)
    assert (manifest["spec"]["backoffLimit"], manifest["spec"]["activeDeadlineSeconds"]) == (0, 120)


@pytest.mark.parametrize(
    ("mode", "kind", "promote", "result_kind"),
    [
        (MigrationPlanMode.PLANNED, DbCutoverJobKind.CUTOVER, False, CutoverKind.CUTOVER),
        (MigrationPlanMode.EMERGENCY, DbCutoverJobKind.CUTOVER, True, CutoverKind.CUTOVER),
        (MigrationPlanMode.FAILBACK, DbCutoverJobKind.ROLLBACK, False, CutoverKind.ROLLBACK),
    ],
)
def test_request_by_plan_mode(
    mode: MigrationPlanMode, kind: DbCutoverJobKind, promote: bool, result_kind: CutoverKind
) -> None:
    request = _request(mode)

    assert (request.job_kind, request.allow_promote, request.result_kind) == (
        kind,
        promote,
        result_kind,
    )


# --- 쿠버네티스 실행기(httpx.MockTransport) ---


class _Kube:
    def __init__(self, routes: dict[tuple[str, str], httpx.Response]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.routes.get((request.method, request.url.path))
        if response is None:
            return httpx.Response(404, json={})
        return response


class _Reader:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[tuple[CutoverRunTarget, CutoverKind, uuid.UUID]] = []

    async def find_by_plan(
        self, target: CutoverRunTarget, kind: CutoverKind, plan_id: uuid.UUID
    ) -> Any:
        self.calls.append((target, kind, plan_id))
        return self.result


def _executor(
    tmp_path: Path, kube: _Kube, reader: _Reader | None = None
) -> KubernetesDbCutoverExecutor:
    (tmp_path / "token").write_text(TOKEN + "\n")
    client = InClusterKubeClient(
        "https://kubernetes.default.svc", str(tmp_path), 5.0, transport=httpx.MockTransport(kube)
    )
    return KubernetesDbCutoverExecutor(client, namespace=NAMESPACE, image=IMAGE, reader=reader)


def _job(env_plan: str = str(PLAN_ID), kind: str = "cutover", **status: Any) -> dict[str, Any]:
    return {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "env": [
                                {"name": "KIND", "value": kind},
                                {"name": "PLAN_ID", "value": env_plan},
                            ]
                        }
                    ]
                }
            }
        },
        "status": status,
    }


async def test_start_creates_job_with_sa_token(tmp_path: Path) -> None:
    kube = _Kube({("POST", JOBS): httpx.Response(201, json={})})

    result = await _executor(tmp_path, kube).start(_request())

    assert result == DbCutoverStarted("echo-cutover-0f1e2d3c-1")
    [request] = kube.requests
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    body = json.loads(request.content)
    assert body["spec"]["template"]["spec"]["containers"][0]["image"] == IMAGE


async def test_start_existing_job_of_same_plan_is_idempotent(tmp_path: Path) -> None:
    name = cutover_job_name(_request())
    kube = _Kube(
        {
            ("POST", JOBS): httpx.Response(409, json={}),
            ("GET", f"{JOBS}/{name}"): httpx.Response(200, json=_job()),
        }
    )

    assert await _executor(tmp_path, kube).start(_request()) == DbCutoverStarted(name)


async def test_start_existing_job_of_other_plan_is_conflict(tmp_path: Path) -> None:
    name = cutover_job_name(_request())
    kube = _Kube(
        {
            ("POST", JOBS): httpx.Response(409, json={}),
            ("GET", f"{JOBS}/{name}"): httpx.Response(200, json=_job(env_plan=str(uuid.uuid4()))),
        }
    )

    assert await _executor(tmp_path, kube).start(_request()) == DbCutoverStartFailed(
        JOB_NAME_CONFLICT
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (403, DbCutoverStartFailed(JOB_CREATE_UNAUTHORIZED)),
        (422, DbCutoverStartFailed(JOB_CREATE_REJECTED)),
        (503, DbCutoverStartFailed(JOB_CREATE_UNREACHABLE, retryable=True)),
    ],
)
async def test_start_failures_map_to_codes(
    tmp_path: Path, status: int, expected: DbCutoverStartFailed
) -> None:
    kube = _Kube({("POST", JOBS): httpx.Response(status, json={})})

    assert await _executor(tmp_path, kube).start(_request()) == expected


async def test_start_transport_error_is_retryable(tmp_path: Path) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    (tmp_path / "token").write_text(TOKEN)
    client = InClusterKubeClient(
        "https://kubernetes.default.svc", str(tmp_path), 5.0, transport=httpx.MockTransport(broken)
    )
    executor = KubernetesDbCutoverExecutor(client, namespace=NAMESPACE, image=IMAGE, reader=None)

    assert await executor.start(_request()) == DbCutoverStartFailed(
        JOB_CREATE_UNREACHABLE, retryable=True
    )


def _pods(exit_code: int | None = None) -> dict[str, Any]:
    state = {"terminated": {"exitCode": exit_code}} if exit_code is not None else {"running": {}}
    return {
        "items": [
            {
                "metadata": {"name": "echo-cutover-0f1e2d3c-1-abcde"},
                "status": {"containerStatuses": [{"name": "cutover", "state": state}]},
            }
        ]
    }


async def test_poll_complete_reads_skip_reason_from_log(tmp_path: Path) -> None:
    name = "echo-cutover-0f1e2d3c-1"
    pods = f"/api/v1/namespaces/{NAMESPACE}/pods"
    kube = _Kube(
        {
            ("GET", f"{JOBS}/{name}"): httpx.Response(
                200, json=_job(conditions=[{"type": "Complete", "status": "True"}])
            ),
            ("GET", pods): httpx.Response(200, json=_pods(0)),
            ("GET", f"{pods}/echo-cutover-0f1e2d3c-1-abcde/log"): httpx.Response(
                200, text="[건너뜀] ALREADY_WRITER: 쓰기 담당이 이미 B 다\n"
            ),
        }
    )

    assert await _executor(tmp_path, kube).poll(name) == DbCutoverFinished(0, "ALREADY_WRITER")


@pytest.mark.parametrize("code", [1, 2, 143])
async def test_poll_failed_job_reports_container_exit_code(tmp_path: Path, code: int) -> None:
    name = "echo-cutover-0f1e2d3c-1"
    kube = _Kube(
        {
            ("GET", f"{JOBS}/{name}"): httpx.Response(
                200, json=_job(conditions=[{"type": "Failed", "status": "True"}])
            ),
            ("GET", f"/api/v1/namespaces/{NAMESPACE}/pods"): httpx.Response(200, json=_pods(code)),
        }
    )

    assert await _executor(tmp_path, kube).poll(name) == DbCutoverFinished(code)


async def test_poll_running_missing_and_unreachable(tmp_path: Path) -> None:
    name = "echo-cutover-0f1e2d3c-1"
    running = _Kube({("GET", f"{JOBS}/{name}"): httpx.Response(200, json=_job(active=1))})
    missing = _Kube({})
    unreachable = _Kube({("GET", f"{JOBS}/{name}"): httpx.Response(503, json={})})

    assert await _executor(tmp_path, running).poll(name) == DbCutoverRunning()
    assert await _executor(tmp_path, missing).poll(name) == DbCutoverLost(JOB_NOT_FOUND)
    assert await _executor(tmp_path, unreachable).poll(name) == DbCutoverRunning()


async def test_find_run_reads_by_plan_id_on_target(tmp_path: Path) -> None:
    reader = _Reader(_run())
    request = _request(MigrationPlanMode.FAILBACK)

    result = await _executor(tmp_path, _Kube({}), reader).find_run(request)

    assert result == _run()
    assert reader.calls == [(request.target, CutoverKind.ROLLBACK, PLAN_ID)]


def test_log_reason_markers() -> None:
    assert log_reason("[건너뜀] ALREADY_DONE: 이 계획") == "ALREADY_DONE"
    assert log_reason("승격 완료. 쓰기 담당 = B") == "PROMOTED"
    assert log_reason("컷오버 완료. max(n)=3") is None


# --- 판정 규칙 ---


def test_judge_exit_zero_with_clean_row_passes() -> None:
    verdict = judge_db_cutover(
        exit_code=0, reason=None, finished=True, lost=False, timed_out=False, run_result=_run()
    )

    assert verdict.passed and verdict.run == _run()


@pytest.mark.parametrize(
    ("kwargs", "failure", "recovery"),
    [
        (
            {"exit_code": 1, "finished": True, "run_result": None},
            MigrationErrorCode.DB_CUTOVER_FAILED,
            CutoverRecovery.ROLLBACK_TRAFFIC,
        ),
        (
            {"exit_code": 2, "finished": True, "run_result": _run()},
            MigrationErrorCode.DB_CUTOVER_PARTIAL,
            CutoverRecovery.MANUAL,
        ),
        (
            {"exit_code": 0, "finished": True, "run_result": _run(missing_rows=3)},
            MigrationErrorCode.DATA_INTEGRITY_FAILED,
            CutoverRecovery.MANUAL,
        ),
        (
            {"exit_code": 0, "finished": True, "run_result": CutoverRunUnavailable("X")},
            MigrationErrorCode.DATA_INTEGRITY_FAILED,
            CutoverRecovery.MANUAL,
        ),
        (
            {"exit_code": None, "timed_out": True, "run_result": CutoverRunNotFound()},
            MigrationErrorCode.DB_CUTOVER_TIMEOUT,
            CutoverRecovery.MANUAL,
        ),
        (
            {"exit_code": None, "lost": True, "run_result": CutoverRunNotFound()},
            MigrationErrorCode.DB_CUTOVER_PARTIAL,
            CutoverRecovery.MANUAL,
        ),
        (
            {"exit_code": 143, "finished": True, "run_result": CutoverRunNotFound()},
            MigrationErrorCode.DB_CUTOVER_PARTIAL,
            CutoverRecovery.MANUAL,
        ),
    ],
)
def test_judge_failures(
    kwargs: dict[str, Any], failure: MigrationErrorCode, recovery: CutoverRecovery
) -> None:
    values: dict[str, Any] = {"reason": None, "finished": False, "lost": False, "timed_out": False}
    values.update(kwargs)

    verdict = judge_db_cutover(**values)

    assert (verdict.failure, verdict.recovery) == (failure, recovery)


def test_judge_timed_out_with_row_passes_on_row() -> None:
    verdict = judge_db_cutover(
        exit_code=None, reason=None, finished=False, lost=False, timed_out=True, run_result=_run()
    )

    assert verdict.passed


def test_run_json_round_trip() -> None:
    run = _run(mode="PROMOTE", rpo_estimate_ms=800)

    assert run_from_json(run_to_json(run)) == run


# --- 실행기 묶음·설정 ---


async def test_fake_executor_finishes_after_step_and_reports_clean_row() -> None:
    clock = TestClock()
    fake = FakeDbCutoverExecutor(clock, 2.0)
    started = await fake.start(_request())
    assert isinstance(started, DbCutoverStarted)

    assert await fake.poll(started.job_name) == DbCutoverRunning()
    clock.advance(2.0)
    assert await fake.poll(started.job_name) == DbCutoverFinished(0, None)
    run = await fake.find_run(_request())
    assert isinstance(run, CutoverRun) and (run.missing_rows, run.duplicate_rows) == (0, 0)


def test_default_executor_set_has_disabled_db_cutover() -> None:
    executors = build_executor_set(Settings(database_url=DATABASE_URL), TestClock())

    assert isinstance(executors.db_cutover, DisabledDbCutoverExecutor)
    assert not executors.db_cutover.enabled


def test_kubernetes_mode_requires_digest_image_and_result_reader() -> None:
    with pytest.raises(ValidationError, match="digest"):
        Settings(
            database_url=DATABASE_URL,
            db_cutover_executor_mode=DbCutoverExecutorMode.KUBERNETES,
            db_cutover_job_image="asia-northeast3-docker.pkg.dev/p/echo/cutover:latest",
        )
    with pytest.raises(ValidationError, match="data_sync_checker_mode postgres"):
        Settings(
            database_url=DATABASE_URL,
            db_cutover_executor_mode=DbCutoverExecutorMode.KUBERNETES,
            db_cutover_job_image=IMAGE,
        )


def test_kubernetes_mode_builds_job_executor() -> None:
    settings = Settings(
        database_url=DATABASE_URL,
        db_cutover_executor_mode=DbCutoverExecutorMode.KUBERNETES,
        db_cutover_job_image=IMAGE,
        data_sync_checker_mode="postgres",
        data_sync_databases=["gcp=echo-pg.echo-data.svc.cluster.local:5432/echo_sub"],
        data_sync_password_ref="env:ECHO_SECRET_DATA_SYNC_PASSWORD",
    )

    executors = build_executor_set(settings, TestClock())

    assert isinstance(executors.db_cutover, KubernetesDbCutoverExecutor)
    assert executors.db_cutover.enabled
