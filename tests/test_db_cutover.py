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
    DbCutoverJobAbsent,
    DbCutoverJobFound,
    DbCutoverJobKind,
    DbCutoverJobUnknown,
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
    JOB_LOOKUP_FAILED,
    JOB_LOOKUP_UNREACHABLE,
    JOB_NAME_CONFLICT,
    JOB_NOT_FOUND,
    InClusterKubeClient,
    KubernetesDbCutoverExecutor,
    cutover_job_manifest,
    cutover_job_name,
    log_reason,
)
from app.services.migration_db_cutover import (
    REASON_NO_CHANGE,
    REASON_PRECHECK_FAILED,
    WRITER_SOURCE,
    WRITER_TARGET,
    WRITER_UNKNOWN,
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
    return db_cutover_request(PLAN_ID, mode, uuid.uuid4(), "gcp", promote_consented=True)


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

    # 접속 자체가 안 됐다 = 보내지 않았다(만들었을 리 없음).
    assert await executor.start(_request()) == DbCutoverStartFailed(
        JOB_CREATE_UNREACHABLE, retryable=True, not_sent=True
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
    # 종료 1 의 뜻(인프라가 찍기로 한 표식). PRECHECK_FAILED 가 NO_CHANGE 보다 먼저 맞는다.
    assert log_reason("[실패] NO_CHANGE: 쓰기 담당 A 그대로, RESUME 완료") == "NO_CHANGE"
    assert log_reason("[실패] PRECHECK_FAILED NO_CHANGE") == "PRECHECK_FAILED"


# --- 검증 반영(P1-1·P1-2·P1-4): 생성 응답 유실·재확인·종료 1 표식 ---


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("lost"),
        httpx.RemoteProtocolError("closed"),
        httpx.ReadError("reset"),
    ],
)
async def test_start_lost_response_may_have_created(tmp_path: Path, error: Exception) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise error

    (tmp_path / "token").write_text(TOKEN)
    client = InClusterKubeClient(
        "https://kubernetes.default.svc", str(tmp_path), 5.0, transport=httpx.MockTransport(broken)
    )
    executor = KubernetesDbCutoverExecutor(client, namespace=NAMESPACE, image=IMAGE, reader=None)

    result = await executor.start(_request())

    assert result == DbCutoverStartFailed(JOB_CREATE_UNREACHABLE, retryable=True)
    assert isinstance(result, DbCutoverStartFailed) and result.maybe_created


@pytest.mark.parametrize(("status", "maybe_created"), [(500, True), (504, True), (429, False)])
async def test_start_server_errors_mark_uncertainty(
    tmp_path: Path, status: int, maybe_created: bool
) -> None:
    kube = _Kube({("POST", JOBS): httpx.Response(status, json={})})

    result = await _executor(tmp_path, kube).start(_request())

    assert isinstance(result, DbCutoverStartFailed) and result.retryable
    assert result.maybe_created is maybe_created


async def test_start_without_token_file_was_not_sent(tmp_path: Path) -> None:
    kube = _Kube({("POST", JOBS): httpx.Response(201, json={})})
    client = InClusterKubeClient(
        "https://kubernetes.default.svc", str(tmp_path), 5.0, transport=httpx.MockTransport(kube)
    )
    executor = KubernetesDbCutoverExecutor(client, namespace=NAMESPACE, image=IMAGE, reader=None)

    result = await executor.start(_request())

    assert result == DbCutoverStartFailed(JOB_CREATE_UNREACHABLE, retryable=True, not_sent=True)
    assert kube.requests == []


async def test_start_conflict_with_unreadable_job_stays_uncertain(tmp_path: Path) -> None:
    name = cutover_job_name(_request())
    kube = _Kube(
        {
            ("POST", JOBS): httpx.Response(409, json={}),
            ("GET", f"{JOBS}/{name}"): httpx.Response(403, json={}),
        }
    )

    result = await _executor(tmp_path, kube).start(_request())

    assert isinstance(result, DbCutoverStartFailed) and result.maybe_created


async def test_find_job_by_deterministic_name(tmp_path: Path) -> None:
    name = cutover_job_name(_request())
    path = f"{JOBS}/{name}"
    found = _Kube({("GET", path): httpx.Response(200, json=_job())})
    other = _Kube({("GET", path): httpx.Response(200, json=_job(env_plan=str(uuid.uuid4())))})
    failing = _Kube({("GET", path): httpx.Response(503, json={})})

    assert await _executor(tmp_path, found).find_job(_request()) == DbCutoverJobFound(name)
    assert await _executor(tmp_path, _Kube({})).find_job(_request()) == DbCutoverJobAbsent()
    assert await _executor(tmp_path, other).find_job(_request()) == DbCutoverJobUnknown(
        JOB_NAME_CONFLICT
    )
    assert await _executor(tmp_path, failing).find_job(_request()) == DbCutoverJobUnknown(
        JOB_LOOKUP_FAILED
    )
    [request] = found.requests
    assert request.url.path == path


async def test_find_job_unreachable_or_token_missing_is_unknown(tmp_path: Path) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lost")

    client = InClusterKubeClient(
        "https://kubernetes.default.svc", str(tmp_path), 5.0, transport=httpx.MockTransport(broken)
    )
    executor = KubernetesDbCutoverExecutor(client, namespace=NAMESPACE, image=IMAGE, reader=None)
    # 토큰 파일 없음(OSError).
    assert await executor.find_job(_request()) == DbCutoverJobUnknown(JOB_LOOKUP_UNREACHABLE)
    (tmp_path / "token").write_text(TOKEN)
    assert await executor.find_job(_request()) == DbCutoverJobUnknown(JOB_LOOKUP_UNREACHABLE)


async def test_poll_failed_exit_one_reads_no_change_marker(tmp_path: Path) -> None:
    name = "echo-cutover-0f1e2d3c-1"
    pods = f"/api/v1/namespaces/{NAMESPACE}/pods"
    kube = _Kube(
        {
            ("GET", f"{JOBS}/{name}"): httpx.Response(
                200, json=_job(conditions=[{"type": "Failed", "status": "True"}])
            ),
            ("GET", pods): httpx.Response(200, json=_pods(1)),
            ("GET", f"{pods}/echo-cutover-0f1e2d3c-1-abcde/log"): httpx.Response(
                200, text="[실패] NO_CHANGE: 쓰기 담당 A 그대로\n"
            ),
        }
    )

    assert await _executor(tmp_path, kube).poll(name) == DbCutoverFinished(1, REASON_NO_CHANGE)


async def test_poll_token_missing_raises_for_processor(tmp_path: Path) -> None:
    client = InClusterKubeClient(
        "https://kubernetes.default.svc",
        str(tmp_path),
        5.0,
        transport=httpx.MockTransport(_Kube({})),
    )
    executor = KubernetesDbCutoverExecutor(client, namespace=NAMESPACE, image=IMAGE, reader=None)

    with pytest.raises(FileNotFoundError):
        await executor.poll("echo-cutover-0f1e2d3c-1")


async def test_fake_find_job_only_confirms_jobs_it_started() -> None:
    clock = TestClock()
    fake = FakeDbCutoverExecutor(clock, 1.0)
    assert isinstance(await fake.find_job(_request()), DbCutoverJobUnknown)
    assert await fake.find_run(_request()) == CutoverRunNotFound()
    started = await fake.start(_request())
    assert isinstance(started, DbCutoverStarted)
    assert await fake.find_job(_request()) == DbCutoverJobFound(started.job_name)
    absent = FakeDbCutoverExecutor(clock, 1.0, job_lookup=DbCutoverJobAbsent())
    assert await absent.find_job(_request()) == DbCutoverJobAbsent()


async def test_disabled_executor_reports_no_job() -> None:
    assert await DisabledDbCutoverExecutor().find_job(_request()) == DbCutoverJobAbsent()


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
            {"exit_code": 1, "finished": True, "run_result": None, "writer": WRITER_SOURCE},
            MigrationErrorCode.DB_CUTOVER_FAILED,
            CutoverRecovery.ROLLBACK_TRAFFIC,
        ),
        (
            {"exit_code": 1, "finished": True, "run_result": None, "reason": REASON_NO_CHANGE},
            MigrationErrorCode.DB_CUTOVER_FAILED,
            CutoverRecovery.ROLLBACK_TRAFFIC,
        ),
        # 종료 1 만으로는 원복하지 않는다(검증 P1-2): 쓰기 담당을 모르거나 도착이면 사람 확인.
        (
            {"exit_code": 1, "finished": True, "run_result": None},
            MigrationErrorCode.DB_CUTOVER_UNKNOWN,
            CutoverRecovery.MANUAL,
        ),
        (
            {"exit_code": 1, "finished": True, "run_result": None, "writer": WRITER_TARGET},
            MigrationErrorCode.DB_CUTOVER_UNKNOWN,
            CutoverRecovery.MANUAL,
        ),
        (
            {
                "exit_code": 1,
                "finished": True,
                "run_result": None,
                "writer": WRITER_UNKNOWN,
                "reason": REASON_PRECHECK_FAILED,
            },
            MigrationErrorCode.DB_CUTOVER_UNKNOWN,
            CutoverRecovery.MANUAL,
        ),
        (
            {"exit_code": None, "run_result": CutoverRunNotFound(), "poll_failed": True},
            MigrationErrorCode.DB_CUTOVER_UNKNOWN,
            CutoverRecovery.MANUAL,
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
