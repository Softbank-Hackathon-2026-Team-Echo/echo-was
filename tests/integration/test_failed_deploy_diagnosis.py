"""배포 실패 AI 진단 → 승인 → 반영 → 재배포 통합 시험(SPEC api-98, 이슈 #98).

migration 픽스처: shop-api(aws-seoul 서비스 중 + gcp-seoul 예비), 개발자 A(author_client)가 진단을
만들고 개발자 B(approver_client)가 승인한다. 실패 배포는 가짜 실행기 simulate.failAt 로 만든다.
"""

import re
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import Response
from sqlalchemy import select

from app.audit.actions import AuditAction
from app.core.exceptions import ServiceDeletingError
from app.enums import AgentRunKind, AgentRunStatus
from app.models import AgentRun, AuditLog
from app.repositories.agent_run_repository import AgentRunRepository
from app.services.deploy_request_service import DeployRequestService
from app.services.diagnosis_service import SIMULATED_CAUSE, SIMULATED_FILE, SIMULATED_LINE
from tests.integration.auth_support import org_path
from tests.integration.migration_support import Migration
from tests.integration.rollback_support import deploy_and_finish
from tests.integration.runner_support import (
    ClaimedRunFixture,
    RunnerFixture,
    add_runner,
    claim,
    event,
    prechecks_path,
    run_patch,
    run_row,
)

SHA40 = re.compile(r"^[0-9a-f]{40}$")
APPLIED_SHA = "0123456789abcdef0123456789abcdef01234567"
DIAGNOSIS_CAPABILITIES = ["PRECHECK", "APPLY_PATCH", "CHANGE_PROPOSAL"]


@pytest.fixture
async def runner(migration: Migration) -> RunnerFixture:
    return await add_runner(migration.harness.session_factory)


def _data(response: Response) -> dict[str, Any]:
    data: dict[str, Any] = response.json()["data"]
    return data


def _diagnosis_path(migration: Migration, deploy_request_id: str) -> str:
    return org_path(
        migration.harness.organization.id, f"/deploy-requests/{deploy_request_id}/diagnosis"
    )


async def _failed_deploy(migration: Migration) -> dict[str, Any]:
    failed = await deploy_and_finish(
        migration.harness, migration.service_id, migration.aws.id, simulate={"failAt": "DEPLOYING"}
    )
    assert failed["status"] == "FAILED"
    assert failed["failure"] is not None
    assert failed["diagnosis"] is None
    return failed


async def _request_diagnosis(
    migration: Migration, deploy_request_id: str, body: dict[str, Any] | None = None
) -> Response:
    path = _diagnosis_path(migration, deploy_request_id)
    client = migration.author_client
    return await (client.post(path, json=body) if body is not None else client.post(path))


async def _detail(migration: Migration, run_id: str) -> dict[str, Any]:
    response = await migration.author_client.get(prechecks_path(migration, f"/{run_id}"))
    assert response.status_code == 200, response.text
    return _data(response)


async def _approve(migration: Migration, run_id: str, patch: dict[str, Any]) -> Response:
    return await migration.approver_client.post(
        prechecks_path(migration, f"/{run_id}/patches/{patch['id']}/approve"),
        json={"patchHash": patch["patchHash"]},
    )


async def _claim(runner: RunnerFixture, capabilities: list[str]) -> ClaimedRunFixture:
    response = await claim(runner, capabilities=capabilities)
    assert response.status_code == 200, response.text
    data = _data(response)
    assert data["run"] is not None
    return ClaimedRunFixture(data["run"]["id"], data["runToken"], data["run"])


# --- 가짜 진단(러너 스위치 꺼짐, 기본) 끝까지 ---


async def test_simulated_diagnosis_approve_apply_and_redeploy(migration: Migration) -> None:
    harness = migration.harness
    failed = await _failed_deploy(migration)

    created = await _request_diagnosis(migration, failed["id"])

    assert created.status_code == 201, created.text
    run = _data(created)
    run_id = run["id"]
    assert (run["kind"], run["status"], run["simulated"]) == (
        "CHANGE_PROPOSAL",
        "SUCCEEDED",
        True,
    )
    assert (run["serviceId"], run["targetEnvironmentId"]) == (
        migration.service_id,
        str(migration.aws.id),
    )
    assert [step["name"] for step in run["steps"]] == [
        "INPUT_LOAD",
        "ANALYZE",
        "PATCH_PROPOSE",
        "PATCH_VERIFY",
    ]
    diagnosis = run["diagnosis"]
    assert diagnosis["result"] == "DIAGNOSED"
    assert (diagnosis["category"], diagnosis["cause"]) == ("CODE", SIMULATED_CAUSE)
    assert (diagnosis["fixable"], diagnosis["unfixableReason"]) == (True, None)
    assert diagnosis["evidence"][1] == {
        "source": "FILE",
        "line": SIMULATED_LINE,
        "path": SIMULATED_FILE,
        "text": "      renderStatus(data;",
    }
    assert diagnosis["evidence"][0]["source"] == "LOG"
    assert (diagnosis["deployRequestId"], diagnosis["environmentId"]) == (
        failed["id"],
        str(migration.aws.id),
    )
    assert diagnosis["sourceCommitSha"] == failed["commitSha"]
    [patch] = run["patches"]
    assert (patch["origin"], patch["status"], patch["files"]) == (
        "AI",
        "PROPOSED",
        [SIMULATED_FILE],
    )
    assert patch["apply"] is None
    assert run == await _detail(migration, run_id)

    # 배포 상세의 연결 칸
    detail = await harness.get_request(failed["id"])
    assert detail["diagnosis"] == {"runId": run_id, "status": "SUCCEEDED"}

    # 같은 배포에서 다시 누르면 200 으로 같은 실행
    again = await _request_diagnosis(migration, failed["id"])
    assert again.status_code == 200, again.text
    assert _data(again)["id"] == run_id

    # 승인 → 가짜 반영(러너 없이 바로 성공) → 같은 환경 재배포
    approved = await _approve(migration, run_id, patch)

    assert approved.status_code == 200, approved.text
    decided = _data(approved)
    assert decided["status"] == "APPLIED"
    apply = decided["apply"]
    assert apply["status"] == "SUCCEEDED"
    assert apply["simulated"] is True
    assert apply["failureCode"] is None
    assert SHA40.match(apply["appliedCommitSha"])
    assert apply["branch"] == f"applied/{apply['runId']}"
    assert apply["pullRequestUrl"] is None
    assert apply["redeployRejectedCode"] is None
    redeploy_id = apply["redeployRequestId"]
    assert redeploy_id is not None
    assert (await _detail(migration, run_id))["patches"][0] == decided

    redeploy = await harness.get_request(redeploy_id)
    assert redeploy["environment"]["id"] == str(migration.aws.id)
    assert (redeploy["trigger"], redeploy["ref"]["type"]) == ("MANUAL", "COMMIT")
    assert redeploy["commitSha"] == failed["commitSha"]
    assert redeploy["diagnosis"] is None
    await harness.run_until_idle()
    assert (await harness.get_request(redeploy_id))["status"] == "SUCCEEDED"

    # 재배포 접수 감사 줄은 승인한 개발자 이름이고 반영 실행을 가리킨다.
    async with harness.session_factory() as session:
        rows = (
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.target_id == uuid.UUID(redeploy_id),
                    AuditLog.action == AuditAction.DEPLOY_REQUESTED,
                )
            )
        ).all()
    assert [(row.actor_user_id, row.details["diagnosisApplyRunId"]) for row in rows] == [
        (migration.approver.user.id, apply["runId"])
    ]

    # 다시 승인해도 재배포를 또 만들지 않는다.
    repeated = await _approve(migration, run_id, patch)
    assert repeated.status_code == 200, repeated.text
    assert _data(repeated)["apply"]["redeployRequestId"] == redeploy_id


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ServiceDeletingError("service is being deleted"), "SERVICE_DELETING"),
        (RuntimeError("github down"), "INTERNAL_ERROR"),
    ],
)
async def test_redeploy_rejection_is_recorded_without_failing_approval(
    migration: Migration, monkeypatch: pytest.MonkeyPatch, error: Exception, code: str
) -> None:
    failed = await _failed_deploy(migration)
    run = _data(await _request_diagnosis(migration, failed["id"]))

    async def reject(*_: Any, **__: Any) -> Any:
        raise error

    monkeypatch.setattr(DeployRequestService, "create_deploy_request", reject)
    approved = await _approve(migration, run["id"], run["patches"][0])

    assert approved.status_code == 200, approved.text
    apply = _data(approved)["apply"]
    assert (apply["status"], apply["redeployRequestId"], apply["redeployRejectedCode"]) == (
        "SUCCEEDED",
        None,
        code,
    )
    assert (await _detail(migration, run["id"]))["patches"][0]["apply"] == apply


async def test_simulated_insufficient_evidence_has_reason_and_no_patch(
    migration: Migration,
) -> None:
    failed = await _failed_deploy(migration)

    created = await _request_diagnosis(
        migration, failed["id"], {"simulate": {"result": "INSUFFICIENT_EVIDENCE"}}
    )

    assert created.status_code == 201, created.text
    run = _data(created)
    assert run["patches"] == []
    assert run["diagnosis"]["result"] == "INSUFFICIENT_EVIDENCE"
    assert run["diagnosis"]["fixable"] is False
    assert run["diagnosis"]["unfixableReason"]
    assert run["diagnosis"]["evidence"] == []


# --- 거절 ---


async def test_diagnosis_rejects_not_failed_unknown_and_viewer(migration: Migration) -> None:
    harness = migration.harness
    succeeded = await deploy_and_finish(harness, migration.service_id, migration.aws.id)
    assert succeeded["status"] == "SUCCEEDED"

    not_failed = await _request_diagnosis(migration, succeeded["id"])
    unknown = await _request_diagnosis(migration, str(uuid.uuid4()))
    unknown_field = await _request_diagnosis(migration, succeeded["id"], {"extra": 1})

    assert (not_failed.status_code, not_failed.json()["code"]) == (409, "DEPLOY_NOT_FAILED")
    assert (unknown.status_code, unknown.json()["code"]) == (404, "DEPLOY_REQUEST_NOT_FOUND")
    assert unknown_field.status_code == 422
    assert (await harness.get_request(succeeded["id"]))["diagnosis"] is None


async def test_simulate_body_rejected_when_switch_off(migration: Migration) -> None:
    failed = await _failed_deploy(migration)
    migration.harness.use_settings(ai_diagnosis_simulate_enabled=False)

    response = await _request_diagnosis(migration, failed["id"], {"simulate": {}})

    assert (response.status_code, response.json()["code"]) == (422, "SIMULATE_NOT_ALLOWED")


# --- C-AI-1·레지던시에 세지 않는다 ---


async def test_diagnosis_is_not_precheck_evidence_or_residency(migration: Migration) -> None:
    harness = migration.harness
    failed = await _failed_deploy(migration)
    created = await _request_diagnosis(migration, failed["id"])
    assert created.status_code == 201, created.text

    async with harness.session_factory() as session:
        evidence = await AgentRunRepository(session).find_latest_succeeded_precheck(
            uuid.UUID(migration.service_id),
            migration.aws.id,
            datetime(2000, 1, 1, tzinfo=UTC),
        )
    residency = await migration.author_client.get(
        org_path(harness.organization.id, "/ai/residency"), params={"days": 30}
    )

    assert evidence is None
    assert residency.status_code == 200, residency.text
    assert _data(residency)["runs"]["total"] == 0


# --- 러너 진단(스위치 켬): 입력에 failure, 결과에 diagnosis, 반영 뒤 재배포 ---


async def test_runner_diagnosis_round_trip(migration: Migration, runner: RunnerFixture) -> None:
    harness = migration.harness
    harness.use_settings(ai_diagnosis_runner_enabled=True, patch_apply_enabled=True)
    failed = await _failed_deploy(migration)

    created = await _request_diagnosis(migration, failed["id"])

    assert created.status_code == 201, created.text
    run_id = _data(created)["id"]
    assert (_data(created)["status"], _data(created)["simulated"]) == ("QUEUED", False)
    assert _data(created)["diagnosis"]["result"] is None
    assert _data(created)["diagnosis"]["deployRequestId"] == failed["id"]
    assert (await harness.get_request(failed["id"]))["diagnosis"] == {
        "runId": run_id,
        "status": "QUEUED",
    }

    # 진단을 밝히지 않은 러너(지금 러너)는 가져가지 않는다.
    precheck_only = await claim(runner, capabilities=["PRECHECK", "APPLY_PATCH"])
    assert _data(precheck_only) == {"run": None, "runToken": None}

    claimed = await _claim(runner, DIAGNOSIS_CAPABILITIES)
    assert claimed.run_id == run_id
    assert claimed.run["kind"] == "CHANGE_PROPOSAL"
    snapshot = claimed.run["inputSnapshot"]
    assert snapshot["schemaVersion"] == 1
    assert snapshot["deployRequest"]["id"] == failed["id"]
    assert snapshot["deployRequest"]["commitSha"] == failed["commitSha"]
    assert snapshot["deployRequest"]["repo"] == "team-echo/shop-api"
    assert snapshot["environment"]["id"] == str(migration.aws.id)
    # #97 failure 칸(배포 상세와 같은 값)이 그대로 들어 있다.
    assert snapshot["failure"] == {
        key: failed["failure"][key]
        for key in (
            "code",
            "message",
            "container",
            "containerReason",
            "terminationReason",
            "exitCode",
            "restartCount",
            "logTail",
            "logTruncated",
            "previousVersionRunning",
        )
    }
    assert snapshot["failure"]["logTail"]

    events = await claimed.post("events", {"events": [event(1, "INPUT_LOAD"), event(2, "ANALYZE")]})
    assert events.status_code == 200, events.text
    patch = run_patch("fix-1", origin="AI")
    result = {
        "status": "SUCCEEDED",
        "failureCode": None,
        "simulated": False,
        "sandbox": None,
        "model": None,
        "configDiff": [],
        "featureGaps": [],
        "findings": [],
        "patches": [patch],
        "blockedAttempts": [],
        "diagnosis": {
            "result": "DIAGNOSED",
            "category": "CONFIG",
            "cause": "DATABASE_URL is missing",
            "fixable": True,
            "unfixableReason": None,
            "evidence": [{"source": "LOG", "line": 3, "text": "ValidationError: database_url"}],
        },
    }
    submitted = await claimed.post("result", result)
    assert submitted.status_code == 200, submitted.text

    detail = await _detail(migration, run_id)
    assert detail["status"] == "SUCCEEDED"
    assert detail["diagnosis"]["category"] == "CONFIG"
    assert detail["diagnosis"]["evidence"] == [
        {"source": "LOG", "line": 3, "path": None, "text": "ValidationError: database_url"}
    ]
    assert [item["origin"] for item in detail["patches"]] == ["AI"]

    approved = await _approve(migration, run_id, patch)
    assert approved.status_code == 200, approved.text
    apply = _data(approved)["apply"]
    assert (apply["status"], apply["redeployRequestId"]) == ("QUEUED", None)

    apply_claim = await _claim(runner, DIAGNOSIS_CAPABILITIES)
    assert apply_claim.run_id == apply["runId"]
    apply_snapshot = apply_claim.run["inputSnapshot"]
    assert apply_snapshot["repo"] == "team-echo/shop-api"
    assert apply_snapshot["diagnosis"] == {
        "deployRequestId": failed["id"],
        "environmentId": str(migration.aws.id),
        "sourceCommitSha": failed["commitSha"],
    }
    done = await apply_claim.post(
        "result",
        {
            "status": "SUCCEEDED",
            "failureCode": None,
            "simulated": False,
            "appliedCommitSha": APPLIED_SHA,
            "branch": f"applied/{apply['runId']}",
        },
    )
    assert done.status_code == 200, done.text

    after = (await _detail(migration, run_id))["patches"][0]["apply"]
    assert after["status"] == "SUCCEEDED"
    assert after["appliedCommitSha"] == APPLIED_SHA
    redeploy_id = after["redeployRequestId"]
    assert redeploy_id is not None
    redeploy = await harness.get_request(redeploy_id)
    assert (redeploy["environment"]["id"], redeploy["commitSha"]) == (
        str(migration.aws.id),
        failed["commitSha"],
    )


async def test_runner_result_without_diagnosis_is_insufficient_evidence(
    migration: Migration, runner: RunnerFixture
) -> None:
    migration.harness.use_settings(ai_diagnosis_runner_enabled=True)
    failed = await _failed_deploy(migration)
    run_id = _data(await _request_diagnosis(migration, failed["id"]))["id"]
    claimed = await _claim(runner, DIAGNOSIS_CAPABILITIES)
    base = {
        "status": "SUCCEEDED",
        "failureCode": None,
        "simulated": False,
        "sandbox": None,
        "model": None,
        "configDiff": [],
        "featureGaps": [],
        "findings": [],
        "patches": [],
        "blockedAttempts": [],
    }

    with_patch = await claimed.post("result", base | {"patches": [run_patch("p1")]})
    accepted = await claimed.post("result", base)

    assert (with_patch.status_code, with_patch.json()["code"]) == (422, "RESULT_INVALID")
    assert accepted.status_code == 200, accepted.text
    detail = await _detail(migration, run_id)
    assert detail["diagnosis"]["result"] == "INSUFFICIENT_EVIDENCE"
    assert detail["diagnosis"]["fixable"] is False
    assert detail["patches"] == []
    row: AgentRun = await run_row(migration, run_id)
    assert (row.kind, row.status) == (AgentRunKind.CHANGE_PROPOSAL, AgentRunStatus.SUCCEEDED)
