"""AI 러너 경로(SPEC api-runner-prechecks AC-6·7·8·9·11·12).

migration 픽스처 위에서 러너 토큰 행을 직접 넣고 부른다. 시각은 FakeClock 이다.
"""

import asyncio
import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
from httpx import Response
from sqlalchemy import func, select, text

from app.auth.tokens import hash_token
from app.enums import ActorType, AgentRunKind, AgentRunStatus, AuditResult, OrganizationRole
from app.models import AgentRun, AgentRunEvent, AgentRunner, AuditLog
from tests.integration.auth_support import create_agent_token, create_member, make_client, org_path
from tests.integration.migration_support import Migration
from tests.integration.runner_support import (
    CLAIM_PATH,
    ClaimedRunFixture,
    RunnerFixture,
    add_runner,
    claim,
    claim_run,
    create_precheck,
    event,
    prechecks_path,
    run_audit_rows,
    run_result,
    run_row,
)

NONE_PROCESSING: dict[str, Any] = {
    "location": "gcp/asia-northeast3",
    "sandbox": True,
    "modelProvider": "NONE",
    "model": None,
    "modelRegion": None,
    "dataClasses": [],
    "externalTransfer": False,
    "externalTransferBytes": None,
}
ANTHROPIC_PROCESSING: dict[str, Any] = {
    "location": "gcp/asia-northeast3",
    "sandbox": True,
    "modelProvider": "ANTHROPIC",
    "model": "claude-opus-5-5",
    "modelRegion": None,
    "dataClasses": ["CONFIG", "FINDINGS"],
    "externalTransfer": True,
    "externalTransferBytes": 18432,
}
_UNSORTED_CLASSES: dict[str, Any] = {"dataClasses": ["FINDINGS", "CONFIG"]}


@pytest.fixture
async def runner(migration: Migration) -> RunnerFixture:
    return await add_runner(migration.harness.session_factory)


def _error(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["code"]


async def _add_queued_run(migration: Migration) -> uuid.UUID:
    """다른 서비스의 대기 실행(같은 서비스의 열린 실행은 하나뿐이다)."""
    service = await migration.harness.create_service(f"svc-{uuid.uuid4().hex[:8]}")
    now = migration.harness.clock.now()
    run = AgentRun(
        organization_id=migration.harness.organization.id,
        service_id=uuid.UUID(service["id"]),
        kind=AgentRunKind.PRECHECK,
        target_environment_id=migration.gcp.id,
        input_snapshot={"service": {"name": service["name"]}},
        input_hash="sha256:" + "0" * 64,
        requested_by_user_id=migration.author.user.id,
        deadline_at=now + timedelta(minutes=15),
        created_at=now,
        updated_at=now,
    )
    async with migration.harness.session_factory() as session, session.begin():
        session.add(run)
    return run.id


# --- 가져가기(AC-6) ---


async def test_claim_without_queued_run_returns_null(
    migration: Migration, runner: RunnerFixture
) -> None:
    response = await claim(runner)

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"run": None, "runToken": None}


async def test_claim_returns_run_and_token_once_storing_hash_only(
    migration: Migration, runner: RunnerFixture
) -> None:
    created = await create_precheck(migration)
    now = migration.harness.clock.now()

    response = await claim(runner)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    run = data["run"]
    assert set(run) == {
        "id",
        "kind",
        "orgId",
        "serviceId",
        "sourceEnvironmentId",
        "targetEnvironmentId",
        "inputSnapshot",
        "inputHash",
        "scenarioRef",
        "deadlineAt",
        "leaseUntil",
    }
    assert run["id"] == created["id"]
    assert run["kind"] == "PRECHECK"
    assert run["orgId"] == str(migration.harness.organization.id)
    assert run["inputHash"] == created["inputHash"]
    assert run["inputSnapshot"]["service"]["name"] == "shop-api"
    assert run["scenarioRef"] is None
    token = data["runToken"]
    assert token.startswith("echo_run_")
    row = await run_row(migration, run["id"])
    assert row.status == AgentRunStatus.CLAIMED
    assert row.claimed_by_runner_id == runner.runner.id
    assert row.run_token_hash == hash_token(token)
    assert row.claimed_at == now
    assert row.lease_until == now + timedelta(seconds=60)
    assert row.started_at is None
    assert token not in str(row.input_snapshot)
    rows = await run_audit_rows(migration, run["id"])
    assert token not in str([(row.details, row.path, row.route) for row in rows])


async def test_claim_gives_oldest_run_first_and_never_twice(
    migration: Migration, runner: RunnerFixture
) -> None:
    created = await create_precheck(migration)
    migration.harness.clock.advance(1)
    later = await _add_queued_run(migration)

    first = await claim_run(runner)
    second = await claim_run(runner)
    third = await claim(runner)

    assert first.run_id == created["id"]
    assert second.run_id == str(later)
    assert first.run_token != second.run_token
    assert third.json()["data"]["run"] is None


async def test_claim_two_runners_concurrently_only_one_gets_the_run(
    migration: Migration, runner: RunnerFixture
) -> None:
    other = await add_runner(migration.harness.session_factory, "second-runner")
    await create_precheck(migration)

    responses = await asyncio.gather(claim(runner), claim(other))

    assert [response.status_code for response in responses] == [200, 200]
    runs = [response.json()["data"]["run"] for response in responses]
    assert sum(run is not None for run in runs) == 1


async def test_claim_concurrently_gives_different_runs(
    migration: Migration, runner: RunnerFixture
) -> None:
    other = await add_runner(migration.harness.session_factory, "second-runner")
    await create_precheck(migration)
    await _add_queued_run(migration)

    responses = await asyncio.gather(claim(runner), claim(other))

    ids = {response.json()["data"]["run"]["id"] for response in responses}
    assert len(ids) == 2


@pytest.mark.parametrize("capabilities", [[], ["APPLY_PATCH"], ["SOMETHING_ELSE"]])
async def test_claim_without_precheck_capability_returns_null(
    migration: Migration, runner: RunnerFixture, capabilities: list[str]
) -> None:
    created = await create_precheck(migration)

    response = await claim(runner, capabilities=capabilities)

    assert response.json()["data"] == {"run": None, "runToken": None}
    assert (await run_row(migration, created["id"])).status == AgentRunStatus.QUEUED


async def test_claim_never_gives_other_kinds(migration: Migration, runner: RunnerFixture) -> None:
    run_id = await _add_queued_run(migration)
    async with migration.harness.session_factory() as session, session.begin():
        row = await session.get(AgentRun, run_id)
        assert row is not None
        row.kind = AgentRunKind.CHANGE_PROPOSAL

    response = await claim(runner, capabilities=["PRECHECK", "CHANGE_PROPOSAL", "APPLY_PATCH"])

    assert response.json()["data"]["run"] is None


async def test_claim_expires_queued_run_past_deadline(
    migration: Migration, runner: RunnerFixture
) -> None:
    created = await create_precheck(migration)
    migration.harness.clock.advance(timedelta(minutes=15).total_seconds())

    response = await claim(runner)

    assert response.json()["data"]["run"] is None
    row = await run_row(migration, created["id"])
    assert (row.status, row.failure_code) == (AgentRunStatus.TIMED_OUT, "DEADLINE_EXCEEDED")
    assert row.finished_at == migration.harness.clock.now()


async def test_claim_expires_overdue_lease_of_other_runs(
    migration: Migration, runner: RunnerFixture
) -> None:
    await create_precheck(migration)
    claimed = await claim_run(runner)
    migration.harness.clock.advance(61)

    await claim(runner)

    row = await run_row(migration, claimed.run_id)
    assert (row.status, row.failure_code) == (AgentRunStatus.TIMED_OUT, "LEASE_EXPIRED")


async def test_claim_does_not_expire_queued_run_for_lease(
    migration: Migration, runner: RunnerFixture
) -> None:
    await create_precheck(migration)
    migration.harness.clock.advance(120)

    claimed = await claim_run(runner)

    assert (await run_row(migration, claimed.run_id)).status == AgentRunStatus.CLAIMED


async def test_claim_revoked_runner_is_unauthorized(migration: Migration) -> None:
    revoked = await add_runner(
        migration.harness.session_factory, "gone", revoked_at=migration.harness.clock.now()
    )

    assert _error(await claim(revoked)) == (401, "RUNNER_TOKEN_INVALID")


async def test_claim_runner_id_mismatch_is_rejected(
    migration: Migration, runner: RunnerFixture
) -> None:
    await create_precheck(migration)

    response = await claim(runner, runner_id="someone-else")

    assert _error(response) == (422, "VALIDATION_ERROR")
    assert response.json()["details"] == [{"field": "runnerId", "reason": "RUNNER_ID_MISMATCH"}]


async def test_claim_unknown_body_field_is_rejected(
    migration: Migration, runner: RunnerFixture
) -> None:
    async with runner.client() as client:
        response = await client.post(
            CLAIM_PATH,
            json={"runnerId": runner.runner.name, "capabilities": [], "extra": 1},
        )

    assert _error(response) == (422, "VALIDATION_ERROR")


async def test_claim_updates_last_seen_at_at_most_once_a_minute(
    migration: Migration, runner: RunnerFixture
) -> None:
    clock = migration.harness.clock
    start = clock.now()

    async def last_seen() -> Any:
        async with migration.harness.session_factory() as session:
            row = await session.get(AgentRunner, runner.runner.id)
            assert row is not None
            return row.last_seen_at

    await claim(runner)
    assert await last_seen() == start
    clock.advance(30)
    await claim(runner)
    assert await last_seen() == start
    clock.advance(30)
    await claim(runner)
    assert await last_seen() == clock.now()


@pytest.mark.parametrize(
    "authorization",
    [
        "Bearer echo_rnr_unknown-token",
        "Bearer echo_run_wrong-prefix-for-claim",
        "Bearer something-else",
        "Basic abc",
    ],
)
async def test_claim_bad_token_is_unauthorized(migration: Migration, authorization: str) -> None:
    async with make_client(origin=None) as client:
        response = await client.post(
            CLAIM_PATH,
            json={"runnerId": "x", "capabilities": []},
            headers={"Authorization": authorization},
        )

    assert _error(response) == (401, "RUNNER_TOKEN_INVALID")


# --- 자격 증명 경계(AC-7·AC-11) ---


async def test_runner_routes_reject_cookie_and_agent_token_without_denial_line(
    migration: Migration, runner: RunnerFixture
) -> None:
    harness = migration.harness
    _, agent_token = await create_agent_token(
        harness.session_factory, harness.organization, harness.admin.user, harness.clock.now()
    )
    await create_precheck(migration)
    claimed = await claim_run(runner)
    body = {"runnerId": runner.runner.name, "capabilities": ["PRECHECK"]}

    async with make_client(agent_token=agent_token, origin=None) as agent:
        agent_claim = await agent.post(CLAIM_PATH, json=body)
        agent_heartbeat = await agent.post(claimed.path("heartbeat"))
    human_claim = await migration.author_client.post(CLAIM_PATH, json=body)
    human_result = await migration.author_client.post(
        claimed.path("result"), json=run_result(migration)
    )

    for response in (agent_claim, agent_heartbeat, human_claim, human_result):
        assert _error(response) == (403, "RUNNER_FORBIDDEN")
    async with harness.session_factory() as session:
        denied = await session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.result == AuditResult.DENIED)
        )
    assert denied == 0


async def test_runner_token_cannot_call_org_routes(
    migration: Migration, runner: RunnerFixture
) -> None:
    created = await create_precheck(migration)
    claimed = await claim_run(runner)
    path = prechecks_path(migration, f"/{created['id']}")
    organization_path = org_path(migration.harness.organization.id, "/services")

    async with runner.client() as runner_client:
        by_runner = await runner_client.get(path)
        by_runner_list = await runner_client.get(organization_path)
    async with claimed.client() as run_client:
        by_run = await run_client.get(path)

    for response in (by_runner, by_runner_list, by_run):
        assert _error(response) == (401, "UNAUTHENTICATED")


async def test_run_token_for_other_run_id_is_unauthorized(
    migration: Migration, runner: RunnerFixture
) -> None:
    await create_precheck(migration)
    claimed = await claim_run(runner)
    other_run = await _add_queued_run(migration)

    async with claimed.client() as client:
        unknown = await client.post(claimed.path("heartbeat", str(uuid.uuid4())))
        other = await client.post(claimed.path("heartbeat", str(other_run)))
        malformed = await client.post(claimed.path("heartbeat", "not-a-uuid"))
    async with claimed.client(token=runner.token) as client:
        runner_token = await client.post(claimed.path("heartbeat"))
    async with claimed.client(token="echo_run_unknown") as client:
        unknown_token = await client.post(claimed.path("heartbeat"))

    for response in (unknown, other, malformed, runner_token, unknown_token):
        assert _error(response) == (401, "RUNNER_TOKEN_INVALID")


# --- 하트비트·임대(AC-7) ---


async def test_heartbeat_moves_claimed_to_running_and_extends_lease(
    migration: Migration, runner: RunnerFixture
) -> None:
    await create_precheck(migration)
    claimed = await claim_run(runner)
    clock = migration.harness.clock
    clock.advance(30)
    started = clock.now()

    first = await claimed.post("heartbeat")
    clock.advance(30)
    second = await claimed.post("heartbeat")

    assert first.status_code == 200, first.text
    assert first.json()["data"]["status"] == "RUNNING"
    assert second.json()["data"]["status"] == "RUNNING"
    row = await run_row(migration, claimed.run_id)
    assert row.started_at == started
    assert row.lease_until == clock.now() + timedelta(seconds=60)


async def test_run_token_lease_boundary_is_inclusive(
    migration: Migration, runner: RunnerFixture
) -> None:
    await create_precheck(migration)
    claimed = await claim_run(runner)
    migration.harness.clock.advance(60)

    response = await claimed.post("heartbeat")

    assert response.status_code == 200, response.text


async def test_heartbeat_lease_never_passes_deadline(
    migration: Migration, runner: RunnerFixture
) -> None:
    await create_precheck(migration)
    claimed = await claim_run(runner)
    clock = migration.harness.clock
    # 임대를 이어 가며 마감 10초 전까지 간다.
    for _ in range(14):
        clock.advance(60)
        assert (await claimed.post("heartbeat")).status_code == 200
    clock.advance(50)

    response = await claimed.post("heartbeat")

    assert response.status_code == 200, response.text
    row = await run_row(migration, claimed.run_id)
    assert row.lease_until == row.deadline_at


async def test_heartbeat_past_deadline_times_out(
    migration: Migration, runner: RunnerFixture
) -> None:
    await create_precheck(migration)
    claimed = await claim_run(runner)
    clock = migration.harness.clock
    for _ in range(14):
        clock.advance(60)
        await claimed.post("heartbeat")
    clock.advance(60)

    response = await claimed.post("heartbeat")

    assert _error(response) == (409, "RUN_NOT_ACTIVE")
    row = await run_row(migration, claimed.run_id)
    assert (row.status, row.failure_code) == (AgentRunStatus.TIMED_OUT, "DEADLINE_EXCEEDED")


async def test_heartbeat_after_lease_expiry_times_out_and_commits(
    migration: Migration, runner: RunnerFixture
) -> None:
    await create_precheck(migration)
    claimed = await claim_run(runner)
    migration.harness.clock.advance(61)

    response = await claimed.post("heartbeat")

    assert _error(response) == (409, "RUN_NOT_ACTIVE")
    row = await run_row(migration, claimed.run_id)
    assert (row.status, row.failure_code) == (AgentRunStatus.TIMED_OUT, "LEASE_EXPIRED")
    assert row.finished_at == migration.harness.clock.now()
    assert row.run_token_hash is not None


async def test_run_token_after_result_heartbeat_events_conflict(
    migration: Migration, runner: RunnerFixture
) -> None:
    await create_precheck(migration)
    claimed = await claim_run(runner)
    assert (await claimed.post("result", run_result(migration))).status_code == 200

    heartbeat = await claimed.post("heartbeat")
    events = await claimed.post("events", {"events": [event(1)]})
    second = await claimed.post("result", run_result(migration))

    assert _error(heartbeat) == (409, "RUN_NOT_ACTIVE")
    assert _error(events) == (409, "RUN_NOT_ACTIVE")
    assert _error(second) == (409, "RESULT_ALREADY_SUBMITTED")


async def test_run_token_result_after_lease_expiry_is_conflict_and_saves_timed_out(
    migration: Migration, runner: RunnerFixture
) -> None:
    await create_precheck(migration)
    claimed = await claim_run(runner)
    migration.harness.clock.advance(61)

    response = await claimed.post("result", run_result(migration))

    assert _error(response) == (409, "RUN_NOT_ACTIVE")
    row = await run_row(migration, claimed.run_id)
    assert (row.status, row.failure_code) == (AgentRunStatus.TIMED_OUT, "LEASE_EXPIRED")
    assert row.result is None
    assert _error(await claimed.post("result", run_result(migration))) == (
        409,
        "RESULT_ALREADY_SUBMITTED",
    )


async def test_run_token_events_after_lease_expiry_saves_timed_out_and_no_events(
    migration: Migration, runner: RunnerFixture
) -> None:
    await create_precheck(migration)
    claimed = await claim_run(runner)
    migration.harness.clock.advance(61)

    response = await claimed.post("events", {"events": [event(1)]})

    assert _error(response) == (409, "RUN_NOT_ACTIVE")
    row = await run_row(migration, claimed.run_id)
    assert row.status == AgentRunStatus.TIMED_OUT
    async with migration.harness.session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(AgentRunEvent))
    assert count == 0


# --- 이벤트(AC-8) ---


async def _claim(migration: Migration, runner: RunnerFixture) -> ClaimedRunFixture:
    await create_precheck(migration)
    return await claim_run(runner)


async def test_events_first_event_makes_running_and_extends_lease(
    migration: Migration, runner: RunnerFixture
) -> None:
    claimed = await _claim(migration, runner)
    clock = migration.harness.clock
    clock.advance(10)

    response = await claimed.post("events", {"events": [event(1)]})

    assert response.json()["data"] == {"acceptedUpTo": 1}
    row = await run_row(migration, claimed.run_id)
    assert row.status == AgentRunStatus.RUNNING
    assert row.started_at == clock.now()
    assert row.lease_until == clock.now() + timedelta(seconds=60)


async def test_events_same_seq_is_ignored_and_accepted_up_to_is_max(
    migration: Migration, runner: RunnerFixture
) -> None:
    claimed = await _claim(migration, runner)
    await claimed.post("events", {"events": [event(1, message="first"), event(2)]})

    resent = await claimed.post(
        "events", {"events": [event(1, message="changed"), event(2), event(2)]}
    )
    gap = await claimed.post("events", {"events": [event(10)]})
    older = await claimed.post("events", {"events": [event(5)]})

    assert resent.json()["data"] == {"acceptedUpTo": 2}
    assert gap.json()["data"] == {"acceptedUpTo": 10}
    assert older.json()["data"] == {"acceptedUpTo": 10}
    async with migration.harness.session_factory() as session:
        rows = list(
            (await session.scalars(select(AgentRunEvent).order_by(AgentRunEvent.seq))).all()
        )
    assert [row.seq for row in rows] == [1, 2, 5, 10]
    assert rows[0].message == "first"


@pytest.mark.parametrize(
    "body",
    [
        {"events": []},
        {"events": [event(index + 1) for index in range(101)]},
        {"events": [event(1, message="x" * 201)]},
        {"events": [event(1, step="NOPE")]},
        {"events": [event(0)]},
        {"events": [event(1, message="a\x00b")]},
        {"unknown": []},
    ],
    ids=["empty", "101-events", "message-201", "unknown-step", "seq-0", "nul", "unknown-field"],
)
async def test_events_invalid_payload_is_rejected(
    migration: Migration, runner: RunnerFixture, body: dict[str, Any]
) -> None:
    claimed = await _claim(migration, runner)

    response = await claimed.post("events", body)

    assert _error(response) == (422, "RESULT_INVALID")
    assert response.json()["details"]
    row = await run_row(migration, claimed.run_id)
    assert row.status == AgentRunStatus.CLAIMED


async def test_events_message_of_200_is_accepted(
    migration: Migration, runner: RunnerFixture
) -> None:
    claimed = await _claim(migration, runner)

    response = await claimed.post("events", {"events": [event(1, message="x" * 200)]})

    assert response.status_code == 200, response.text


async def test_events_malformed_json_is_rejected(
    migration: Migration, runner: RunnerFixture
) -> None:
    claimed = await _claim(migration, runner)

    response = await claimed.post_raw("events", b"{not json")

    assert _error(response) == (422, "RESULT_INVALID")


async def test_events_are_masked_before_storage(
    migration: Migration, runner: RunnerFixture
) -> None:
    token = "echo_agt_" + "A1b" * 10
    claimed = await _claim(migration, runner)

    response = await claimed.post(
        "events",
        {"events": [event(1, message=f"used {token}", data={"note": token})]},
    )

    assert response.status_code == 200, response.text
    async with migration.harness.session_factory() as session:
        row = await session.scalar(select(AgentRunEvent))
    assert row is not None
    assert row.message == "used ***"
    assert row.data == {"note": "***"}


# --- 결과(AC-9 저장) ---


async def test_result_from_claimed_state_without_heartbeat_is_accepted(
    migration: Migration, runner: RunnerFixture
) -> None:
    claimed = await _claim(migration, runner)
    clock = migration.harness.clock
    clock.advance(5)

    response = await claimed.post("result", run_result(migration))

    assert response.json()["data"] == {"status": "SUCCEEDED"}
    row = await run_row(migration, claimed.run_id)
    assert row.status == AgentRunStatus.SUCCEEDED
    assert row.finished_at == clock.now()
    assert row.started_at is None
    assert row.failure_code is None
    assert row.simulated is False
    assert row.model_id is None
    assert row.prompt_version is None
    assert row.result is not None
    assert not {"status", "failureCode", "simulated"} & set(row.result)


async def test_result_failed_keeps_failure_code_and_model_columns(
    migration: Migration, runner: RunnerFixture
) -> None:
    claimed = await _claim(migration, runner)
    model = {"id": "claude-opus-5-5", "effort": "medium", "promptVersion": "v1", "refusal": True}

    response = await claimed.post(
        "result",
        run_result(migration, status="FAILED", failureCode="DEADLINE", simulated=True, model=model),
    )

    assert response.json()["data"] == {"status": "FAILED"}
    row = await run_row(migration, claimed.run_id)
    assert (row.status, row.failure_code) == (AgentRunStatus.FAILED, "DEADLINE")
    assert (row.model_id, row.prompt_version, row.simulated) == ("claude-opus-5-5", "v1", True)


async def test_result_is_masked_before_storage(migration: Migration, runner: RunnerFixture) -> None:
    token = "echo_run_" + "A1b" * 10
    github = "ghp_" + "Z9y" * 10
    claimed = await _claim(migration, runner)
    finding = run_result(migration)["findings"][0] | {
        "reason": f"found {token}",
        "suggestedFix": f"rotate {github}",
    }

    response = await claimed.post("result", run_result(migration, findings=[finding]))

    assert response.status_code == 200, response.text
    row = await run_row(migration, claimed.run_id)
    assert row.result is not None
    dumped = json.dumps(row.result)
    assert token not in dumped
    assert github not in dumped
    assert row.result["findings"][0]["reason"] == "found ***"
    detail = await migration.author_client.get(prechecks_path(migration, f"/{claimed.run_id}"))
    assert token not in detail.text
    assert github not in detail.text


async def test_result_invalid_payload_does_not_finish_run(
    migration: Migration, runner: RunnerFixture
) -> None:
    claimed = await _claim(migration, runner)
    body = run_result(migration)
    del body["simulated"]

    missing = await claimed.post("result", body)
    ai_pass = await claimed.post(
        "result",
        run_result(
            migration,
            findings=[run_result(migration)["findings"][0] | {"origin": "AI", "result": "PASS"}],
        ),
    )
    too_big = await claimed.post_raw("result", b" " * (1024 * 1024 + 1))
    status_mismatch = await claimed.post("result", run_result(migration, failureCode="X"))

    for response in (missing, ai_pass, too_big, status_mismatch):
        assert _error(response) == (422, "RESULT_INVALID")
    assert (await run_row(migration, claimed.run_id)).status == AgentRunStatus.CLAIMED


# --- 감사(AC-12) ---


async def test_runner_actions_are_system_ai_runner(
    migration: Migration, runner: RunnerFixture
) -> None:
    claimed = await _claim(migration, runner)
    await claimed.post("heartbeat")
    await claimed.post("events", {"events": [event(1)]})
    await claimed.post("result", run_result(migration))

    rows = await run_audit_rows(migration, claimed.run_id)

    assert [row.action for row in rows] == [
        "PRECHECK_REQUESTED",
        "AGENT_RUN_CLAIMED",
        "AGENT_RUN_FINISHED",
    ]
    claim_line, finish_line = rows[1], rows[2]
    for row in (claim_line, finish_line):
        assert row.actor_type == ActorType.SYSTEM
        assert row.component == "ai-runner"
        assert row.organization_id == migration.harness.organization.id
        assert (row.target_type, row.target_label) == ("agent_run", "shop-api")
    assert claim_line.details == {"runnerName": runner.runner.name}
    assert claim_line.route == f"POST {CLAIM_PATH}"
    assert finish_line.result == AuditResult.SUCCEEDED
    assert finish_line.details == {"status": "SUCCEEDED", "simulated": False, "aiReviewed": False}


async def test_runner_failed_result_audit_line_has_failure_code_and_failed_result(
    migration: Migration, runner: RunnerFixture
) -> None:
    claimed = await _claim(migration, runner)
    model = {"id": "m", "effort": None, "promptVersion": None, "refusal": False}

    await claimed.post(
        "result", run_result(migration, status="FAILED", failureCode="BOOM", model=model)
    )

    finish_line = (await run_audit_rows(migration, claimed.run_id))[-1]
    assert finish_line.result == AuditResult.FAILED
    assert finish_line.details == {
        "status": "FAILED",
        "failureCode": "BOOM",
        "simulated": False,
        "aiReviewed": True,
    }


async def test_runner_result_audit_line_carries_processing_only_when_reported(
    migration: Migration, runner: RunnerFixture
) -> None:
    claimed = await _claim(migration, runner)

    response = await claimed.post(
        "result", run_result(migration, processing=ANTHROPIC_PROCESSING | _UNSORTED_CLASSES)
    )

    assert response.status_code == 200, response.text
    finish_line = (await run_audit_rows(migration, claimed.run_id))[-1]
    assert finish_line.details == {
        "status": "SUCCEEDED",
        "simulated": False,
        "aiReviewed": False,
        "processing": ANTHROPIC_PROCESSING,
    }


# --- 레지던시 기록(AI 3 AC-1·2) ---


async def _processing_is_sql_null(migration: Migration, run_id: str) -> bool | None:
    async with migration.harness.session_factory() as session:
        return await session.scalar(
            text("SELECT processing IS NULL FROM agent_runs WHERE id = :id"), {"id": run_id}
        )


async def test_runner_result_stores_processing_sorted_and_keeps_it_out_of_result(
    migration: Migration, runner: RunnerFixture
) -> None:
    claimed = await _claim(migration, runner)

    response = await claimed.post(
        "result", run_result(migration, processing=ANTHROPIC_PROCESSING | _UNSORTED_CLASSES)
    )

    assert response.status_code == 200, response.text
    row = await run_row(migration, claimed.run_id)
    assert row.status == AgentRunStatus.SUCCEEDED
    assert row.processing == ANTHROPIC_PROCESSING
    assert row.result is not None and "processing" not in row.result


async def test_runner_result_fills_omitted_optional_processing_fields_with_null(
    migration: Migration, runner: RunnerFixture
) -> None:
    claimed = await _claim(migration, runner)
    body = {
        "location": "gcp/asia-northeast3",
        "sandbox": False,
        "modelProvider": "NONE",
        "dataClasses": [],
        "externalTransfer": False,
    }

    response = await claimed.post("result", run_result(migration, processing=body))

    assert response.status_code == 200, response.text
    row = await run_row(migration, claimed.run_id)
    assert row.processing == NONE_PROCESSING | {"sandbox": False}


async def test_runner_failed_result_accepts_external_transfer_with_null_model(
    migration: Migration, runner: RunnerFixture
) -> None:
    claimed = await _claim(migration, runner)

    response = await claimed.post(
        "result",
        run_result(
            migration, status="FAILED", failureCode="MODEL_ERROR", processing=ANTHROPIC_PROCESSING
        ),
    )

    assert response.status_code == 200, response.text
    row = await run_row(migration, claimed.run_id)
    assert row.status == AgentRunStatus.FAILED
    assert row.model_id is None
    assert row.processing == ANTHROPIC_PROCESSING


@pytest.mark.parametrize(
    "changes",
    [{}, {"processing": None}],
    ids=["omitted", "null"],
)
async def test_runner_result_without_processing_stores_sql_null(
    migration: Migration, runner: RunnerFixture, changes: dict[str, Any]
) -> None:
    claimed = await _claim(migration, runner)

    response = await claimed.post("result", run_result(migration, **changes))

    assert response.status_code == 200, response.text
    assert await _processing_is_sql_null(migration, claimed.run_id) is True


async def test_runner_result_rejects_each_processing_rule_and_keeps_run_open(
    migration: Migration, runner: RunnerFixture
) -> None:
    claimed = await _claim(migration, runner)
    violations = {
        "unknown field": NONE_PROCESSING | {"extra": 1},
        "NONE with model": NONE_PROCESSING | {"model": "m"},
        "provider without model": ANTHROPIC_PROCESSING | {"model": None},
        "NONE with external transfer": NONE_PROCESSING | {"externalTransfer": True},
        "NONE with data classes": NONE_PROCESSING | {"dataClasses": ["CONFIG"]},
        "duplicate data classes": ANTHROPIC_PROCESSING | {"dataClasses": ["CONFIG", "CONFIG"]},
        "location format": NONE_PROCESSING | {"location": "SECRET-LOCATION"},
        "bytes without transfer": NONE_PROCESSING | {"externalTransferBytes": 1},
        "negative bytes": ANTHROPIC_PROCESSING | {"externalTransferBytes": -1},
    }

    for rule, processing in violations.items():
        response = await claimed.post("result", run_result(migration, processing=processing))

        assert _error(response) == (422, "RESULT_INVALID"), rule
        assert response.json()["details"], rule
        assert "SECRET-LOCATION" not in response.text
    row = await run_row(migration, claimed.run_id)
    assert row.status == AgentRunStatus.CLAIMED
    assert row.result is None
    assert await _processing_is_sql_null(migration, claimed.run_id) is True
    assert [r.action for r in await run_audit_rows(migration, claimed.run_id)][-1] == (
        "AGENT_RUN_CLAIMED"
    )


async def test_runner_timeout_audit_line_is_system_ai_runner_once(
    migration: Migration, runner: RunnerFixture
) -> None:
    claimed = await _claim(migration, runner)
    migration.harness.clock.advance(61)

    await claimed.post("heartbeat")
    await claimed.post("heartbeat")
    await claim(runner)

    lines = [
        row
        for row in await run_audit_rows(migration, claimed.run_id)
        if row.action == "AGENT_RUN_TIMED_OUT"
    ]
    assert len(lines) == 1
    assert lines[0].actor_type == ActorType.SYSTEM
    assert lines[0].component == "ai-runner"
    assert lines[0].result == AuditResult.FAILED
    assert lines[0].details == {"failureCode": "LEASE_EXPIRED"}


async def test_precheck_viewer_cannot_create_or_cancel(
    migration: Migration, runner: RunnerFixture
) -> None:
    harness = migration.harness
    viewer = await create_member(
        harness.session_factory, harness.organization, OrganizationRole.VIEWER, harness.clock.now()
    )
    created = await create_precheck(migration)

    async with make_client(session_token=viewer.session_token) as client:
        read = await client.get(prechecks_path(migration, f"/{created['id']}"))
        cancel = await client.post(prechecks_path(migration, f"/{created['id']}/cancel"))
        create = await client.post(
            prechecks_path(migration),
            json={"serviceId": migration.service_id, "targetEnvironmentId": str(migration.gcp.id)},
        )

    assert read.status_code == 200
    assert _error(cancel) == (403, "ROLE_FORBIDDEN")
    assert _error(create) == (403, "ROLE_FORBIDDEN")
