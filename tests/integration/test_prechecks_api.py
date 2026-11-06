"""AI 사전 점검 API, 사람 쪽(SPEC api-runner-prechecks AC-4·5·10, AC-8 logs).

migration 픽스처: shop-api 가 aws-seoul(서비스 중 100)·gcp-seoul(예비)에 있고 개발자 A·B 가 있다.
시각은 FakeClock 이다.
"""

import uuid
from datetime import timedelta
from typing import Any

import pytest
from httpx import Response
from sqlalchemy import select, update

from app.auth.tokens import RUN_TOKEN_PREFIX
from app.enums import (
    ActorType,
    AgentRunKind,
    AgentRunStatus,
    AuditResult,
    OrganizationRole,
    SecurityControlId,
    SecurityFindingMode,
    SecurityFindingSource,
    ServiceStatus,
)
from app.models import AgentRun, AuditLog, Service, ServiceEnvironment
from app.repositories.agent_run_repository import AgentRunRepository
from app.services.run_result import compute_patch_hash
from app.services.security_finding_service import SecurityFindingService
from app.services.security_rules import SecurityScope
from tests.integration.auth_support import (
    create_agent_token,
    create_member,
    create_organization,
    make_client,
    org_path,
)
from tests.integration.migration_support import Migration
from tests.integration.runner_support import (
    RunnerFixture,
    add_runner,
    claim_run,
    create_precheck,
    event,
    finish_precheck,
    prechecks_path,
    request_precheck,
    run_audit_rows,
    run_result,
    run_row,
)
from tests.integration.security_gate_support import _finding, target_digest

UNKNOWN_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
async def runner(migration: Migration) -> RunnerFixture:
    return await add_runner(migration.harness.session_factory)


def _error(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["code"]


async def _get_detail(migration: Migration, run_id: str) -> dict[str, Any]:
    response = await migration.author_client.get(prechecks_path(migration, f"/{run_id}"))
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


# --- 레지던시 기록(AI 3 AC-3) ---


async def test_precheck_detail_shows_processing_when_reported(
    migration: Migration, runner: RunnerFixture
) -> None:
    processing = {
        "location": "gcp/asia-northeast3",
        "sandbox": True,
        "modelProvider": "ANTHROPIC",
        "model": "claude-opus-5-5",
        "modelRegion": None,
        "dataClasses": ["CONFIG", "FINDINGS"],
        "externalTransfer": True,
        "externalTransferBytes": 18432,
    }

    data = await finish_precheck(migration, runner, processing=processing)

    assert data["processing"] == processing


async def test_precheck_detail_shows_null_processing_when_not_reported(
    migration: Migration, runner: RunnerFixture
) -> None:
    created = await create_precheck(migration)
    assert created["processing"] is None

    claimed = await claim_run(runner)
    response = await claimed.post("result", run_result(migration))
    assert response.status_code == 200, response.text

    assert (await _get_detail(migration, created["id"]))["processing"] is None


# --- 만들기 ---


async def test_create_precheck_returns_queued_detail_with_empty_result(
    migration: Migration,
) -> None:
    now = migration.harness.clock.now()

    response = await request_precheck(migration)

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["status"] == "QUEUED"
    assert data["serviceId"] == migration.service_id
    assert data["sourceEnvironmentId"] == str(migration.aws.id)
    assert data["targetEnvironmentId"] == str(migration.gcp.id)
    assert data["requestedByUserId"] == str(migration.author.user.id)
    assert data["simulated"] is False
    assert data["aiReviewed"] is False
    assert data["failureCode"] is None
    assert data["inputHash"].startswith("sha256:")
    assert data["sandboxId"] is None
    assert data["model"] is None
    assert data["leaseUntil"] is None
    assert data["steps"] == []
    for key in ("configDiff", "featureGaps", "findings", "patches", "blockedAttempts"):
        assert data[key] == []
    row = await run_row(migration, data["id"])
    assert row.kind == AgentRunKind.PRECHECK
    assert row.created_at == now
    assert row.deadline_at == now + timedelta(minutes=15)
    assert row.run_token_hash is None
    assert row.scenario_ref is None
    assert RUN_TOKEN_PREFIX not in response.text


async def test_create_precheck_accepts_environment_without_connection(
    migration: Migration, runner: RunnerFixture
) -> None:
    other = await migration.harness.add_environment("gcp-tokyo")

    response = await request_precheck(migration, targetEnvironmentId=str(other.id))

    assert response.status_code == 201, response.text
    snapshot = (await run_row(migration, response.json()["data"]["id"])).input_snapshot
    assert snapshot["target"]["environmentId"] == str(other.id)
    assert snapshot["target"]["imageDigest"] is None
    assert snapshot["target"]["variableKeys"] == []


async def test_create_precheck_rejects_unknown_service(migration: Migration) -> None:
    response = await request_precheck(migration, serviceId=UNKNOWN_ID)

    assert _error(response) == (404, "SERVICE_NOT_FOUND")


async def test_create_precheck_rejects_unknown_environment(migration: Migration) -> None:
    response = await request_precheck(migration, targetEnvironmentId=UNKNOWN_ID)

    assert _error(response) == (404, "ENVIRONMENT_NOT_FOUND")


async def test_create_precheck_rejects_other_organization_environment(
    migration: Migration,
) -> None:
    other_organization = await create_organization(migration.harness.session_factory, "other-org")
    foreign = await migration.harness.add_environment("gcp-tokyo", organization=other_organization)

    response = await request_precheck(migration, targetEnvironmentId=str(foreign.id))

    assert _error(response) == (404, "ENVIRONMENT_NOT_FOUND")


async def test_create_precheck_rejects_other_organization_service(migration: Migration) -> None:
    other_organization = await create_organization(migration.harness.session_factory, "other-org")
    now = migration.harness.clock.now()
    outsider = await create_member(
        migration.harness.session_factory, other_organization, OrganizationRole.DEVELOPER, now
    )
    environment = await migration.harness.add_environment(
        "gcp-tokyo", organization=other_organization
    )
    async with make_client(session_token=outsider.session_token) as client:
        response = await client.post(
            org_path(other_organization.id, "/prechecks"),
            json={"serviceId": migration.service_id, "targetEnvironmentId": str(environment.id)},
        )

    assert _error(response) == (404, "SERVICE_NOT_FOUND")


async def test_create_precheck_rejects_deleting_service(migration: Migration) -> None:
    async with migration.harness.session_factory() as session, session.begin():
        await session.execute(
            update(Service)
            .where(Service.id == uuid.UUID(migration.service_id))
            .values(status=ServiceStatus.DELETING)
        )

    response = await request_precheck(migration)

    assert _error(response) == (409, "SERVICE_DELETING")


@pytest.mark.parametrize(
    "weights",
    [(50, 50), (0, 0)],
    ids=["split", "nothing-serving"],
)
async def test_create_precheck_rejects_when_source_is_not_single(
    migration: Migration, weights: tuple[int, int]
) -> None:
    async with migration.harness.session_factory() as session, session.begin():
        for environment_id, weight in zip(
            (migration.aws.id, migration.gcp.id), weights, strict=True
        ):
            await session.execute(
                update(ServiceEnvironment)
                .where(ServiceEnvironment.environment_id == environment_id)
                .values(traffic_weight=weight)
            )

    response = await request_precheck(migration)

    assert _error(response) == (409, "SOURCE_NOT_SINGLE")


async def test_create_precheck_rejects_same_environment(migration: Migration) -> None:
    response = await request_precheck(migration, targetEnvironmentId=str(migration.aws.id))

    assert _error(response) == (422, "VALIDATION_ERROR")
    assert response.json()["details"] == [
        {"field": "targetEnvironmentId", "reason": "SAME_ENVIRONMENT"}
    ]


async def test_create_precheck_rejects_open_run_with_run_id(migration: Migration) -> None:
    first = await create_precheck(migration)

    response = await request_precheck(migration)

    assert _error(response) == (409, "PRECHECK_IN_PROGRESS")
    assert response.json()["details"] == [
        {"field": "runId", "reason": "PRECHECK_IN_PROGRESS", "value": first["id"]}
    ]


async def test_create_precheck_rejects_race_through_partial_unique_index(
    migration: Migration, monkeypatch: pytest.MonkeyPatch
) -> None:
    """서비스 행 잠금을 지나 INSERT 까지 왔어도 부분 unique 위반은 같은 409 다."""
    first = await create_precheck(migration)
    original = AgentRunRepository.find_open_for_service
    calls = {"count": 0}

    async def miss_first_read(self: AgentRunRepository, service_id: uuid.UUID) -> AgentRun | None:
        calls["count"] += 1
        return None if calls["count"] == 1 else await original(self, service_id)

    monkeypatch.setattr(AgentRunRepository, "find_open_for_service", miss_first_read)

    response = await request_precheck(migration)

    assert _error(response) == (409, "PRECHECK_IN_PROGRESS")
    assert response.json()["details"][0]["value"] == first["id"]


async def test_create_precheck_expires_overdue_run_of_the_service_first(
    migration: Migration, runner: RunnerFixture
) -> None:
    await create_precheck(migration)
    claimed = await claim_run(runner)
    migration.harness.clock.advance(61)

    response = await request_precheck(migration)

    assert response.status_code == 201, response.text
    row = await run_row(migration, claimed.run_id)
    assert (row.status, row.failure_code) == (AgentRunStatus.TIMED_OUT, "LEASE_EXPIRED")


async def test_create_precheck_requires_uuid_fields(migration: Migration) -> None:
    response = await migration.author_client.post(
        prechecks_path(migration), json={"serviceId": "nope", "targetEnvironmentId": "x"}
    )

    assert _error(response) == (422, "VALIDATION_ERROR")


# --- 입력 사본 ---


async def test_input_snapshot_has_no_secrets_and_stable_hash(migration: Migration) -> None:
    harness = migration.harness
    secret_value = "s3cr3t-value-of-the-variable"
    response = await harness.client.put(
        f"{harness.services}/{migration.service_id}/environments/{migration.gcp.id}"
        "/variables/API_KEY",
        json={"value": secret_value},
    )
    assert response.status_code in (200, 201), response.text

    first = await create_precheck(migration)
    snapshot = (await run_row(migration, first["id"])).input_snapshot
    canceled = await migration.author_client.post(
        prechecks_path(migration, f"/{first['id']}/cancel")
    )
    assert canceled.status_code == 200, canceled.text
    harness.clock.advance(5)
    second = await create_precheck(migration)
    again = (await run_row(migration, second["id"])).input_snapshot

    assert snapshot == again
    assert first["inputHash"] == second["inputHash"]
    assert first["id"] != second["id"]
    assert set(snapshot) == {"schemaVersion", "service", "source", "target", "findings"}
    assert snapshot["schemaVersion"] == 1
    assert snapshot["service"]["name"] == "shop-api"
    assert set(snapshot["service"]) == {"id", "name", "containerPort", "healthCheckPath"}
    for side, environment in (("source", migration.aws), ("target", migration.gcp)):
        assert snapshot[side]["environmentId"] == str(environment.id)
        assert snapshot[side]["kind"] == environment.kind
        assert set(snapshot[side]) == {
            "environmentId",
            "kind",
            "config",
            "imageDigest",
            "variableKeys",
        }
        assert snapshot[side]["config"]["clusterName"]
    assert snapshot["target"]["variableKeys"] == ["API_KEY"]
    assert snapshot["source"]["imageDigest"] == snapshot["target"]["imageDigest"]
    dumped = str(snapshot)
    assert secret_value not in dumped
    assert "secretRef" not in dumped
    assert "secret_ref" not in dumped
    assert "ECHO_SECRET" not in dumped
    assert "secretName" not in dumped


async def test_input_hash_changes_when_state_changes(migration: Migration) -> None:
    first = await create_precheck(migration)
    await migration.author_client.post(prechecks_path(migration, f"/{first['id']}/cancel"))
    async with migration.harness.session_factory() as session, session.begin():
        session.add(migration.gcp)
        migration.gcp.config = {**migration.gcp.config, "region": "asia-northeast1"}

    second = await create_precheck(migration)

    assert first["inputHash"] != second["inputHash"]


async def test_input_snapshot_masks_token_shaped_config_values(migration: Migration) -> None:
    token = "ghp_" + "Ab1" * 10
    async with migration.harness.session_factory() as session, session.begin():
        session.add(migration.gcp)
        migration.gcp.config = {**migration.gcp.config, "cluster_name": token}

    data = await create_precheck(migration)

    snapshot = (await run_row(migration, data["id"])).input_snapshot
    assert token not in str(snapshot)
    assert snapshot["target"]["config"]["clusterName"] == "***"


async def test_input_snapshot_carries_recent_real_findings_of_the_service_only(
    migration: Migration,
) -> None:
    digest = await target_digest(migration)
    now = migration.harness.clock.now()
    organization_id = migration.harness.organization.id
    relevant = _finding(
        SecurityControlId.IMG_1,
        SecurityFindingSource.TRIVY_IMAGE,
        SecurityScope(organization_id, image_digest=digest),
        now,
    )
    unrelated = _finding(
        SecurityControlId.SEC_1,
        SecurityFindingSource.GITLEAKS,
        SecurityScope(organization_id),
        now,
    )
    async with migration.harness.session_factory() as session:
        await SecurityFindingService(session).record_all([relevant, unrelated])

    data = await create_precheck(migration)

    findings = (await run_row(migration, data["id"])).input_snapshot["findings"]
    assert [(item["controlId"], item["source"]) for item in findings] == [
        (SecurityControlId.IMG_1.value, SecurityFindingSource.TRIVY_IMAGE.value)
    ]
    assert set(findings[0]) == {"controlId", "result", "source", "environmentId", "collectedAt"}
    assert SecurityFindingMode.SIMULATED.value not in str(findings)


# --- 조회 ---


async def test_get_precheck_returns_result_fields_and_computed_steps(
    migration: Migration, runner: RunnerFixture
) -> None:
    patch_hash = compute_patch_hash("--- a/Dockerfile\n")
    created = await create_precheck(migration)
    claimed = await claim_run(runner)
    await claimed.post(
        "events",
        {
            "events": [
                event(1, "SANDBOX_CREATE", at="2026-11-05T00:00:00Z"),
                event(2, "ANALYZE", at="2026-11-05T00:00:10Z"),
                event(3, "ANALYZE", at="2026-11-05T00:00:20Z", level="ERROR"),
                event(4, "ANALYZE", at="2026-11-05T00:00:30Z", level="WARN"),
            ]
        },
    )
    await claimed.post(
        "result",
        run_result(
            migration,
            patches=[
                {
                    "id": "p1",
                    "patchHash": patch_hash,
                    "files": ["Dockerfile"],
                    "diff": "--- a/Dockerfile\n",
                    "reason": "pin base image",
                    "touchesProtectedFiles": False,
                    "checks": [
                        {"name": "TESTS", "result": "PASS", "summary": "19 passed"},
                        {"name": "TRIVY", "result": "FAIL", "toolVersion": "trivy 0.75.0"},
                    ],
                }
            ],
            blockedAttempts=[
                {
                    "action": "POST /api/v1/orgs/x/services",
                    "target": "service shop-api",
                    "at": "2026-11-05T00:00:15Z",
                    "reason": "AGENT_READ_ONLY",
                }
            ],
        ),
    )

    detail = await _get_detail(migration, created["id"])

    assert detail["steps"] == [
        {
            "name": "SANDBOX_CREATE",
            "firstAt": "2026-11-05T00:00:00Z",
            "lastAt": "2026-11-05T00:00:00Z",
            "level": "INFO",
        },
        {
            "name": "ANALYZE",
            "firstAt": "2026-11-05T00:00:10Z",
            "lastAt": "2026-11-05T00:00:30Z",
            "level": "ERROR",
        },
    ]
    patch = detail["patches"][0]
    assert patch["status"] == "BLOCKED"
    assert patch["blockReason"] == "CHECK_NOT_PASSED"
    assert patch["verification"] == {"passed": 1, "total": 2}
    assert patch["patchHash"] == patch_hash
    assert detail["blockedAttempts"][0]["auditLogId"] is None
    assert detail["blockedAttempts"][0]["confirmed"] is False
    assert detail["findings"][0]["confirmation"] is None
    assert detail["finishedAt"] is not None


async def test_get_precheck_is_not_found_for_other_org_unknown_id_and_other_kinds(
    migration: Migration,
) -> None:
    created = await create_precheck(migration)
    other_organization = await create_organization(migration.harness.session_factory, "other-org")
    outsider = await create_member(
        migration.harness.session_factory,
        other_organization,
        OrganizationRole.ADMIN,
        migration.harness.clock.now(),
    )
    async with migration.harness.session_factory() as session, session.begin():
        row = await session.get(AgentRun, created["id"])
        assert row is not None
        row.kind = AgentRunKind.CHANGE_PROPOSAL

    async with make_client(session_token=outsider.session_token) as client:
        other_org = await client.get(org_path(other_organization.id, f"/prechecks/{created['id']}"))
    wrong_kind = await migration.author_client.get(prechecks_path(migration, f"/{created['id']}"))
    unknown = await migration.author_client.get(prechecks_path(migration, f"/{UNKNOWN_ID}"))
    logs = await migration.author_client.get(prechecks_path(migration, f"/{UNKNOWN_ID}/logs"))

    for response in (other_org, wrong_kind, unknown, logs):
        assert _error(response) == (404, "PRECHECK_NOT_FOUND")


async def test_get_precheck_does_not_write_expired_state(
    migration: Migration, runner: RunnerFixture
) -> None:
    created = await create_precheck(migration)
    await claim_run(runner)
    migration.harness.clock.advance(61)

    detail = await _get_detail(migration, created["id"])

    assert detail["status"] == "CLAIMED"
    assert (await run_row(migration, created["id"])).status == AgentRunStatus.CLAIMED


async def test_agent_token_can_read_precheck_and_logs_but_not_write(migration: Migration) -> None:
    created = await create_precheck(migration)
    harness = migration.harness
    _, agent_token = await create_agent_token(
        harness.session_factory, harness.organization, harness.admin.user, harness.clock.now()
    )

    async with make_client(agent_token=agent_token) as agent:
        detail = await agent.get(prechecks_path(migration, f"/{created['id']}"))
        logs = await agent.get(prechecks_path(migration, f"/{created['id']}/logs"))
        cancel = await agent.post(prechecks_path(migration, f"/{created['id']}/cancel"))

    assert detail.status_code == 200
    assert logs.status_code == 200
    assert _error(cancel) == (403, "AGENT_READ_ONLY")


# --- 이벤트 페이지 ---


async def test_logs_page_after_seq(migration: Migration, runner: RunnerFixture) -> None:
    created = await create_precheck(migration)
    claimed = await claim_run(runner)
    await claimed.post(
        "events",
        {
            "events": [
                event(seq, "ANALYZE", data={"index": seq} if seq == 2 else None)
                for seq in (1, 2, 3, 4, 5)
            ]
        },
    )
    path = prechecks_path(migration, f"/{created['id']}/logs")

    first = (await migration.author_client.get(path, params={"size": 2})).json()["data"]
    second = (
        await migration.author_client.get(path, params={"after": first["nextAfter"], "size": 2})
    ).json()["data"]
    third = (
        await migration.author_client.get(path, params={"after": second["nextAfter"], "size": 2})
    ).json()["data"]
    empty = (await migration.author_client.get(path, params={"after": third["nextAfter"]})).json()[
        "data"
    ]
    too_big = await migration.author_client.get(path, params={"size": 501})
    negative = await migration.author_client.get(path, params={"after": -1})

    assert [item["seq"] for item in first["items"]] == [1, 2]
    assert first["nextAfter"] == 2
    assert first["items"][1] == {
        "seq": 2,
        "at": "2026-11-05T00:00:00Z",
        "step": "ANALYZE",
        "level": "INFO",
        "message": "analyze done",
        "data": {"index": 2},
    }
    assert [item["seq"] for item in second["items"]] == [3, 4]
    assert [item["seq"] for item in third["items"]] == [5]
    assert empty == {"items": [], "nextAfter": 5}
    assert too_big.status_code == 422
    assert negative.status_code == 422


# --- 취소 ---


async def test_cancel_precheck_queued_claimed_and_running(
    migration: Migration, runner: RunnerFixture
) -> None:
    for prepare in ("queued", "claimed", "running"):
        created = await create_precheck(migration)
        if prepare != "queued":
            claimed = await claim_run(runner)
            if prepare == "running":
                await claimed.post("heartbeat")

        response = await migration.author_client.post(
            prechecks_path(migration, f"/{created['id']}/cancel")
        )

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["status"] == "CANCELLED"
        assert data["finishedAt"] is not None
        assert data["failureCode"] is None


async def test_cancel_precheck_finished_run_is_conflict(
    migration: Migration, runner: RunnerFixture
) -> None:
    detail = await finish_precheck(migration, runner)

    response = await migration.author_client.post(
        prechecks_path(migration, f"/{detail['id']}/cancel")
    )

    assert _error(response) == (409, "RUN_NOT_ACTIVE")


async def test_cancel_precheck_twice_is_conflict_with_one_audit_line(migration: Migration) -> None:
    created = await create_precheck(migration)
    path = prechecks_path(migration, f"/{created['id']}/cancel")
    await migration.author_client.post(path)

    response = await migration.author_client.post(path)

    assert _error(response) == (409, "RUN_NOT_ACTIVE")
    rows = await run_audit_rows(migration, created["id"])
    assert [row.action for row in rows] == ["PRECHECK_REQUESTED", "PRECHECK_CANCELLED"]


async def test_cancel_precheck_invalidates_run_token(
    migration: Migration, runner: RunnerFixture
) -> None:
    created = await create_precheck(migration)
    claimed = await claim_run(runner)
    await migration.author_client.post(prechecks_path(migration, f"/{created['id']}/cancel"))

    heartbeat = await claimed.post("heartbeat")
    events = await claimed.post("events", {"events": [event(1)]})
    result = await claimed.post("result", run_result(migration))

    assert _error(heartbeat) == (409, "RUN_NOT_ACTIVE")
    assert _error(events) == (409, "RUN_NOT_ACTIVE")
    assert _error(result) == (409, "RESULT_ALREADY_SUBMITTED")


async def test_cancel_precheck_overdue_run_times_out_and_is_conflict(
    migration: Migration, runner: RunnerFixture
) -> None:
    created = await create_precheck(migration)
    await claim_run(runner)
    migration.harness.clock.advance(61)

    response = await migration.author_client.post(
        prechecks_path(migration, f"/{created['id']}/cancel")
    )

    assert _error(response) == (409, "RUN_NOT_ACTIVE")
    row = await run_row(migration, created["id"])
    assert (row.status, row.failure_code) == (AgentRunStatus.TIMED_OUT, "LEASE_EXPIRED")
    rows = await run_audit_rows(migration, created["id"])
    assert rows[-1].action == "AGENT_RUN_TIMED_OUT"


async def test_precheck_user_actions_write_user_audit_lines_with_details(
    migration: Migration,
) -> None:
    created = await create_precheck(migration)
    await migration.author_client.post(prechecks_path(migration, f"/{created['id']}/cancel"))

    async with migration.harness.session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(AuditLog)
                    .where(AuditLog.target_id == created["id"])
                    .order_by(AuditLog.seq)
                )
            ).all()
        )

    assert [(row.action, row.actor_type, row.result) for row in rows] == [
        ("PRECHECK_REQUESTED", ActorType.USER, AuditResult.SUCCEEDED),
        ("PRECHECK_CANCELLED", ActorType.USER, AuditResult.SUCCEEDED),
    ]
    expected = {
        "serviceId": migration.service_id,
        "targetEnvironmentId": str(migration.gcp.id),
    }
    assert all(row.details == expected for row in rows)
    assert {row.target_type for row in rows} == {"agent_run"}
    assert {row.target_label for row in rows} == {"shop-api"}
