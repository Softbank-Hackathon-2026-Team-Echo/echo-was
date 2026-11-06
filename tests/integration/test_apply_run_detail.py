"""반영 실행(APPLY_PATCH) 상세·이벤트를 사람 쪽 조회 경로로 읽는다(SPEC api-10-3 AC-1~5).

기존 `GET /prechecks/{id}`·`/logs` 가 반영 실행 id(= 부모 상세의 patches[].apply.runId)도 받는다.
이벤트 모양은 echo-ai-agent `apply_runner.py` 가 내는 줄(빌드 시간·trivy 수정 가능 수·healthz 시도
수·빌드 오류 tail)을 흉내 낸다. migration 픽스처: 개발자 A 가 만들고 개발자 B 가 승인한다.
"""

from typing import Any

import pytest
from httpx import Response

from app.enums import AgentRunStatus, OrganizationRole
from app.main import app
from tests.integration.auth_support import (
    create_agent_token,
    create_member,
    create_organization,
    make_client,
    org_path,
)
from tests.integration.migration_support import Migration
from tests.integration.runner_support import (
    ClaimedRunFixture,
    RunnerFixture,
    add_runner,
    claim,
    event,
    finish_precheck,
    prechecks_path,
    run_patch,
    run_row,
)

COMMIT_SHA = "fedcba9876543210fedcba9876543210fedcba98"
APPLY_REPO = "Softbank-Hackathon-2026-Team-Echo/echo-ai-agent"
# 실제 모양의 GitHub 토큰은 아니지만 가리기 규칙(gh[pousr]_ + 20자 이상)에 걸리는 값.
FAKE_GITHUB_TOKEN = "ghp_" + "A1" * 18


@pytest.fixture
async def runner(migration: Migration) -> RunnerFixture:
    return await add_runner(migration.harness.session_factory)


def _error(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["code"]


async def _approved_patch(migration: Migration, runner: RunnerFixture) -> tuple[str, str]:
    """스위치 켬 → 사전 점검 성공(규칙 패치 p1) → 개발자 B 승인. (부모 id, 반영 실행 id)."""
    detail = await finish_precheck(migration, runner, patches=[run_patch("p1", origin="RULE")])
    parent_id: str = detail["id"]
    migration.harness.use_settings(patch_apply_enabled=True)
    response = await migration.approver_client.post(
        prechecks_path(migration, f"/{parent_id}/patches/p1/approve"),
        json={"patchHash": run_patch("p1")["patchHash"]},
    )
    assert response.status_code == 200, response.text
    apply_run_id: str = response.json()["data"]["apply"]["runId"]
    return parent_id, apply_run_id


async def _claim_apply(runner: RunnerFixture) -> ClaimedRunFixture:
    response = await claim(runner, capabilities=["PRECHECK", "APPLY_PATCH"])
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["run"] is not None
    return ClaimedRunFixture(data["run"]["id"], data["runToken"], data["run"])


async def _get(migration: Migration, run_id: str, suffix: str = "") -> dict[str, Any]:
    response = await migration.author_client.get(prechecks_path(migration, f"/{run_id}{suffix}"))
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


def _at(seconds: int) -> str:
    return f"2026-11-06T08:00:{seconds:02d}Z"


def _applied_events(apply_run_id: str) -> list[dict[str, Any]]:
    branch = f"applied/{apply_run_id}"
    return [
        event(1, "INPUT_LOAD", at=_at(0), message="apply input loaded", data={"branch": branch}),
        event(2, "SANDBOX_CREATE", at=_at(1), message="apply workspace created"),
        event(
            3,
            "PATCH_VERIFY",
            at=_at(2),
            message="patch applied",
            data={"appliedCommitSha": COMMIT_SHA, "committed": True, "pushed": False},
        ),
        event(
            4,
            "PATCH_VERIFY",
            at=_at(40),
            message="image built",
            data={"seconds": 37.5, "pushed": False},
        ),
        event(
            5,
            "PATCH_VERIFY",
            at=_at(50),
            message="image scanned",
            data={"fixableHigh": 0, "fixableCritical": 0},
        ),
        event(
            6,
            "PATCH_VERIFY",
            at=_at(55),
            message="healthz passed",
            data={"path": "/healthz", "attempts": 2, "network": "none"},
        ),
        event(7, "SANDBOX_DELETE", at=_at(56), message="apply workspace deleted"),
    ]


def _apply_result(apply_run_id: str, **changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "status": "SUCCEEDED",
        "failureCode": None,
        "simulated": False,
        "appliedCommitSha": COMMIT_SHA,
        "branch": f"applied/{apply_run_id}",
    }
    return body | changes


# --- AC-1: 성공(APPLIED) 반영 실행 ---


async def test_apply_run_detail_and_events_for_applied_run(
    migration: Migration, runner: RunnerFixture
) -> None:
    parent_id, apply_run_id = await _approved_patch(migration, runner)
    branch = f"applied/{apply_run_id}"
    patch_hash = run_patch("p1")["patchHash"]

    # 결과 전(QUEUED): 입력 사본 값은 이미 보이고 결과 값은 null 이다.
    queued = await _get(migration, apply_run_id)
    assert (queued["kind"], queued["status"]) == ("APPLY_PATCH", "QUEUED")
    assert queued["apply"] == {
        "parentRunId": parent_id,
        "patchId": "p1",
        "patchHash": patch_hash,
        "repo": APPLY_REPO,
        "branch": branch,
        "appliedCommitSha": None,
    }
    assert queued["startedAt"] is None and queued["finishedAt"] is None
    assert await _get(migration, apply_run_id, "/logs") == {"items": [], "nextAfter": 0}

    claimed = await _claim_apply(runner)
    assert claimed.run_id == apply_run_id
    assert (await claimed.post("heartbeat")).status_code == 200
    events = await claimed.post("events", {"events": _applied_events(apply_run_id)})
    assert events.status_code == 200, events.text
    migration.harness.clock.advance(60)
    result = await claimed.post("result", _apply_result(apply_run_id))
    assert result.status_code == 200, result.text

    detail = await _get(migration, apply_run_id)

    assert detail["id"] == apply_run_id
    assert detail["kind"] == "APPLY_PATCH"
    assert (detail["status"], detail["failureCode"], detail["simulated"]) == (
        "SUCCEEDED",
        None,
        False,
    )
    assert detail["apply"] == {
        "parentRunId": parent_id,
        "patchId": "p1",
        "patchHash": patch_hash,
        "repo": APPLY_REPO,
        "branch": branch,
        "appliedCommitSha": COMMIT_SHA,
    }
    row = await run_row(migration, apply_run_id)
    assert row.started_at is not None and row.finished_at is not None
    assert detail["startedAt"] == row.started_at.isoformat().replace("+00:00", "Z")
    assert detail["finishedAt"] == row.finished_at.isoformat().replace("+00:00", "Z")
    assert detail["serviceId"] == migration.service_id
    assert [step["name"] for step in detail["steps"]] == [
        "INPUT_LOAD",
        "SANDBOX_CREATE",
        "PATCH_VERIFY",
        "SANDBOX_DELETE",
    ]
    assert {step["level"] for step in detail["steps"]} == {"INFO"}
    # 사전 점검 결과 칸은 비어 있다(반영 결과에는 그 칸이 없다).
    assert (detail["aiReviewed"], detail["model"], detail["processing"], detail["sandboxId"]) == (
        False,
        None,
        None,
        None,
    )
    for key in ("configDiff", "featureGaps", "findings", "patches", "blockedAttempts"):
        assert detail[key] == [], key

    logs = await _get(migration, apply_run_id, "/logs")
    assert logs["nextAfter"] == 7
    assert [(item["seq"], item["step"], item["message"]) for item in logs["items"]] == [
        (1, "INPUT_LOAD", "apply input loaded"),
        (2, "SANDBOX_CREATE", "apply workspace created"),
        (3, "PATCH_VERIFY", "patch applied"),
        (4, "PATCH_VERIFY", "image built"),
        (5, "PATCH_VERIFY", "image scanned"),
        (6, "PATCH_VERIFY", "healthz passed"),
        (7, "SANDBOX_DELETE", "apply workspace deleted"),
    ]
    by_message = {item["message"]: item["data"] for item in logs["items"]}
    assert by_message["image built"] == {"seconds": 37.5, "pushed": False}
    assert by_message["image scanned"] == {"fixableHigh": 0, "fixableCritical": 0}
    assert by_message["healthz passed"]["attempts"] == 2
    page = await _get(migration, apply_run_id, "/logs?after=5&size=1")
    assert [item["seq"] for item in page["items"]] == [6]
    assert page["nextAfter"] == 6

    # 부모 상세: patches[].apply.runId 가 반영 실행 id 이고, 부모는 kind PRECHECK·apply null.
    parent = await _get(migration, parent_id)
    assert (parent["kind"], parent["apply"]) == ("PRECHECK", None)
    assert parent["patches"][0]["status"] == "APPLIED"
    assert parent["patches"][0]["apply"]["runId"] == apply_run_id


# --- AC-2: 실패(APPLY_FAILED·IMAGE_BUILD_FAILED) 반영 실행 ---


async def test_apply_run_detail_shows_build_failure_tail_without_secrets(
    migration: Migration, runner: RunnerFixture
) -> None:
    parent_id, apply_run_id = await _approved_patch(migration, runner)
    claimed = await _claim_apply(runner)
    tail = (
        "#7 ERROR: failed to solve: process did not complete successfully\n"
        f"fatal: could not read from remote token={FAKE_GITHUB_TOKEN}"
    )
    events = await claimed.post(
        "events",
        {
            "events": [
                event(1, "SANDBOX_CREATE", at=_at(0), message="apply workspace created"),
                event(2, "PATCH_VERIFY", at=_at(1), message="patch applied"),
                event(
                    3,
                    "PATCH_VERIFY",
                    at=_at(30),
                    level="ERROR",
                    message="image build failed",
                    data={"exitCode": 1, "tail": tail},
                ),
                event(
                    4,
                    "PATCH_VERIFY",
                    at=_at(31),
                    level="ERROR",
                    message="apply failed; nothing pushed",
                    data={"failureCode": "IMAGE_BUILD_FAILED", "reason": "BUILD"},
                ),
                event(5, "SANDBOX_DELETE", at=_at(32), message="apply workspace deleted"),
            ]
        },
    )
    assert events.status_code == 200, events.text
    result = await claimed.post(
        "result",
        _apply_result(
            apply_run_id, status="FAILED", failureCode="IMAGE_BUILD_FAILED", appliedCommitSha=None
        ),
    )
    assert result.status_code == 200, result.text

    detail = await _get(migration, apply_run_id)

    assert (detail["kind"], detail["status"], detail["failureCode"]) == (
        "APPLY_PATCH",
        "FAILED",
        "IMAGE_BUILD_FAILED",
    )
    assert detail["apply"]["appliedCommitSha"] is None
    assert detail["apply"]["branch"] == f"applied/{apply_run_id}"
    assert detail["finishedAt"] is not None
    verify = next(step for step in detail["steps"] if step["name"] == "PATCH_VERIFY")
    assert verify["level"] == "ERROR"

    logs = await _get(migration, apply_run_id, "/logs")
    failed = next(item for item in logs["items"] if item["message"] == "image build failed")
    assert failed["level"] == "ERROR"
    assert failed["data"]["exitCode"] == 1
    assert "failed to solve: process did not complete successfully" in failed["data"]["tail"]
    # 저장 전에 가렸으므로 응답에도 토큰 원문이 없다.
    assert FAKE_GITHUB_TOKEN not in str(logs)
    assert "token=***" in failed["data"]["tail"]

    parent = await _get(migration, parent_id)
    assert parent["patches"][0]["status"] == "APPLY_FAILED"
    assert parent["patches"][0]["apply"]["runId"] == apply_run_id


# --- AC-3: 권한(뷰어·에이전트 가능, 조직 밖 404) ---


async def test_apply_run_detail_is_readable_by_viewer_and_agent_but_not_other_org(
    migration: Migration, runner: RunnerFixture
) -> None:
    _, apply_run_id = await _approved_patch(migration, runner)
    harness = migration.harness
    now = harness.clock.now()
    viewer = await create_member(
        harness.session_factory, harness.organization, OrganizationRole.VIEWER, now
    )
    _, agent_token = await create_agent_token(
        harness.session_factory, harness.organization, harness.admin.user, now
    )
    other_organization = await create_organization(harness.session_factory, "other-org")
    outsider = await create_member(
        harness.session_factory, other_organization, OrganizationRole.ADMIN, now
    )

    async with make_client(session_token=viewer.session_token) as client:
        by_viewer = await client.get(prechecks_path(migration, f"/{apply_run_id}"))
        viewer_logs = await client.get(prechecks_path(migration, f"/{apply_run_id}/logs"))
    async with make_client(agent_token=agent_token) as client:
        by_agent = await client.get(prechecks_path(migration, f"/{apply_run_id}"))
        agent_logs = await client.get(prechecks_path(migration, f"/{apply_run_id}/logs"))
    async with make_client(session_token=outsider.session_token) as client:
        foreign = await client.get(org_path(other_organization.id, f"/prechecks/{apply_run_id}"))
        foreign_logs = await client.get(
            org_path(other_organization.id, f"/prechecks/{apply_run_id}/logs")
        )
        # 남의 조직 경로로 부르면 검사기가 먼저 막는다(조직 멤버가 아님).
        cross = await client.get(prechecks_path(migration, f"/{apply_run_id}"))

    for response in (by_viewer, viewer_logs, by_agent, agent_logs):
        assert response.status_code == 200, response.text
    assert by_viewer.json()["data"]["kind"] == "APPLY_PATCH"
    assert by_agent.json()["data"]["apply"]["branch"] == f"applied/{apply_run_id}"
    assert _error(foreign) == (404, "PRECHECK_NOT_FOUND")
    assert _error(foreign_logs) == (404, "PRECHECK_NOT_FOUND")
    assert _error(cross) == (404, "ORGANIZATION_NOT_FOUND")


# --- AC-4: 쓰기 경로는 그대로 사전 점검만 ---


async def test_apply_run_id_is_not_accepted_by_precheck_write_routes(
    migration: Migration, runner: RunnerFixture
) -> None:
    _, apply_run_id = await _approved_patch(migration, runner)

    cancel = await migration.author_client.post(
        prechecks_path(migration, f"/{apply_run_id}/cancel")
    )
    approve = await migration.approver_client.post(
        prechecks_path(migration, f"/{apply_run_id}/patches/p1/approve"),
        json={"patchHash": run_patch("p1")["patchHash"]},
    )
    reject = await migration.approver_client.post(
        prechecks_path(migration, f"/{apply_run_id}/patches/p1/reject"),
        json={"patchHash": run_patch("p1")["patchHash"]},
    )

    for response in (cancel, approve, reject):
        assert _error(response) == (404, "PRECHECK_NOT_FOUND")
    assert (await run_row(migration, apply_run_id)).status == AgentRunStatus.QUEUED


# --- AC-5: 사전 점검 id 회귀 · OpenAPI ---


async def test_precheck_detail_keeps_shape_and_marks_kind(
    migration: Migration, runner: RunnerFixture
) -> None:
    detail = await finish_precheck(migration, runner)

    assert (detail["kind"], detail["apply"]) == ("PRECHECK", None)
    assert detail["status"] == "SUCCEEDED"
    assert detail["configDiff"][0]["key"] == "region"
    logs = await _get(migration, detail["id"], "/logs")
    assert [item["seq"] for item in logs["items"]] == [1, 2, 3]


def test_openapi_precheck_detail_documents_kind_and_apply() -> None:
    schemas = app.openapi()["components"]["schemas"]
    detail = schemas["PrecheckDetailResponse"]
    assert {"kind", "apply"} <= set(detail["required"])
    assert detail["properties"]["kind"] == {"$ref": "#/components/schemas/AgentRunKind"}
    assert {"PRECHECK", "APPLY_PATCH"} <= set(schemas["AgentRunKind"]["enum"])
    assert detail["properties"]["apply"]["anyOf"] == [
        {"$ref": "#/components/schemas/PrecheckApplyRunResponse"},
        {"type": "null"},
    ]
    assert set(schemas["PrecheckApplyRunResponse"]["properties"]) == {
        "parentRunId",
        "patchId",
        "patchHash",
        "repo",
        "branch",
        "appliedCommitSha",
    }
