"""조직 AI 데이터 레지던시 요약 API(AI 3 AC-5·7).

migration 픽스처 위에 실행 행을 직접 넣고 부른다. 시각은 FakeClock 이다.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from httpx import Response

from app.enums import AgentRunKind, AgentRunStatus, OrganizationRole
from app.main import app
from app.models import AgentRun, Organization
from app.services import ai_residency_service
from tests.integration.auth_support import (
    create_agent_token,
    create_member,
    create_organization,
    make_client,
    org_path,
)
from tests.integration.migration_support import Migration
from tests.integration.runner_support import add_runner, finish_precheck

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
SECRETS_PROCESSING: dict[str, Any] = ANTHROPIC_PROCESSING | {
    "modelRegion": "ap-northeast-1",
    "dataClasses": ["SECRETS"],
}
DEFAULT_POLICY: dict[str, Any] = {
    "source": "DEFAULT",
    "allowedModelRegions": ["asia-northeast3", "ap-northeast-1", "ap-northeast-2"],
    "forbidExternalForClasses": ["SECRETS"],
    "allowedRegionlessProviders": [{"provider": "ANTHROPIC", "condition": "NO_FORBIDDEN_CLASSES"}],
}


def _path(migration: Migration) -> str:
    return org_path(migration.harness.organization.id, "/ai/residency")


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


async def _add_run(
    migration: Migration,
    *,
    finished_at: datetime | None,
    processing: dict[str, Any] | None = None,
    status: AgentRunStatus = AgentRunStatus.SUCCEEDED,
    kind: AgentRunKind = AgentRunKind.PRECHECK,
    organization: Organization | None = None,
    parent_run_id: uuid.UUID | None = None,
    model_id: str | None = None,
    simulated: bool = False,
) -> AgentRun:
    created_at = (finished_at or migration.harness.clock.now()) - timedelta(minutes=1)
    run = AgentRun(
        organization_id=(organization or migration.harness.organization).id,
        service_id=uuid.UUID(migration.service_id),
        kind=kind,
        status=status,
        target_environment_id=migration.gcp.id,
        parent_run_id=parent_run_id,
        input_snapshot={"service": {"name": "shop-api"}},
        input_hash="sha256:" + "0" * 64,
        requested_by_user_id=migration.author.user.id,
        deadline_at=created_at + timedelta(minutes=15),
        finished_at=finished_at,
        model_id=model_id,
        simulated=simulated,
        processing=processing,
        created_at=created_at,
        updated_at=created_at,
    )
    async with migration.harness.session_factory() as session, session.begin():
        session.add(run)
    return run


async def _get_summary(migration: Migration, **params: Any) -> dict[str, Any]:
    response = await migration.author_client.get(_path(migration), params=params)
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


def _error(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["code"]


# --- 요약 숫자(AC-5) ---


async def test_residency_summary_matches_spec_example(migration: Migration) -> None:
    now = migration.harness.clock.now()
    anthropic = await _add_run(
        migration, finished_at=now - timedelta(hours=1), processing=ANTHROPIC_PROCESSING
    )
    no_model = await _add_run(
        migration, finished_at=now - timedelta(hours=2), processing=NONE_PROCESSING
    )
    unrecorded_new = await _add_run(migration, finished_at=now - timedelta(hours=3))
    unrecorded_old = await _add_run(migration, finished_at=now - timedelta(hours=4))

    data = await _get_summary(migration)

    assert data["from"] == _iso(now - timedelta(days=7))
    assert data["to"] == _iso(now)
    assert data["policy"] == DEFAULT_POLICY
    assert data["runs"] == {
        "total": 4,
        "recorded": 2,
        "unrecorded": 2,
        "sandboxed": 2,
        "externalTransfer": 1,
        "truncated": False,
    }
    assert data["byProvider"] == [{"key": "ANTHROPIC", "runs": 1}, {"key": "NONE", "runs": 1}]
    assert data["byModel"] == [{"key": "claude-opus-5-5", "runs": 1}]
    assert data["byLocation"] == [{"key": "gcp/asia-northeast3", "runs": 2}]
    assert data["byModelRegion"] == [{"key": None, "runs": 1}]
    assert data["byDataClass"] == [{"key": "CONFIG", "runs": 1}, {"key": "FINDINGS", "runs": 1}]
    assert data["externalTransferBytes"] == 18432
    assert data["sensitiveExternalTransfer"] == {
        "classes": ["SECRETS"],
        "runs": 0,
        "verdict": "ZERO_WITH_UNRECORDED",
    }
    assert data["violations"] == []
    assert [item["runId"] for item in data["recentRuns"]] == [
        str(run.id) for run in (anthropic, no_model, unrecorded_new, unrecorded_old)
    ]
    assert data["recentRuns"][0] == {
        "runId": str(anthropic.id),
        "serviceId": migration.service_id,
        "kind": "PRECHECK",
        "status": "SUCCEEDED",
        "finishedAt": _iso(now - timedelta(hours=1)),
        "simulated": False,
        "aiReviewed": False,
        "processing": ANTHROPIC_PROCESSING,
        "violations": [],
        "regionlessAllowed": True,
    }
    assert data["recentRuns"][1]["processing"] == NONE_PROCESSING
    assert data["recentRuns"][1]["violations"] == []
    assert data["recentRuns"][1]["regionlessAllowed"] is False
    assert data["recentRuns"][2]["processing"] is None
    assert data["recentRuns"][2]["violations"] == []
    assert data["recentRuns"][2]["regionlessAllowed"] is False


async def test_residency_summary_regionless_anthropic_with_secrets_keeps_both_violations(
    migration: Migration,
) -> None:
    now = migration.harness.clock.now()
    run = await _add_run(
        migration,
        finished_at=now - timedelta(hours=1),
        processing=ANTHROPIC_PROCESSING | {"dataClasses": ["CONFIG", "SECRETS"]},
    )

    data = await _get_summary(migration)

    assert data["policy"] == DEFAULT_POLICY
    assert data["violations"] == [
        {
            "runId": str(run.id),
            "serviceId": migration.service_id,
            "finishedAt": _iso(now - timedelta(hours=1)),
            "reasons": ["MODEL_REGION_NOT_ALLOWED", "FORBIDDEN_CLASS_SENT_EXTERNALLY"],
        }
    ]
    assert data["recentRuns"][0]["regionlessAllowed"] is False
    assert data["sensitiveExternalTransfer"]["verdict"] == "DETECTED"


async def test_residency_summary_regionless_other_provider_is_still_a_violation(
    migration: Migration,
) -> None:
    now = migration.harness.clock.now()
    run = await _add_run(
        migration,
        finished_at=now - timedelta(hours=1),
        processing=ANTHROPIC_PROCESSING | {"modelProvider": "OPENAI", "model": "gpt-5"},
    )

    data = await _get_summary(migration)

    assert [item["runId"] for item in data["violations"]] == [str(run.id)]
    assert data["violations"][0]["reasons"] == ["MODEL_REGION_NOT_ALLOWED"]
    assert data["recentRuns"][0]["regionlessAllowed"] is False


def test_openapi_documents_regionless_policy_and_marker() -> None:
    schemas = app.openapi()["components"]["schemas"]

    assert schemas["AiResidencyRegionlessCondition"]["enum"] == ["NO_FORBIDDEN_CLASSES"]
    policy = schemas["AiResidencyPolicyResponse"]
    assert "allowedRegionlessProviders" in policy["required"]
    assert policy["properties"]["allowedRegionlessProviders"]["items"] == {
        "$ref": "#/components/schemas/AiResidencyRegionlessProviderResponse"
    }
    item = schemas["AiResidencyRegionlessProviderResponse"]
    assert item["properties"]["provider"] == {"$ref": "#/components/schemas/AgentRunModelProvider"}
    assert item["properties"]["condition"] == {
        "$ref": "#/components/schemas/AiResidencyRegionlessCondition"
    }
    assert sorted(item["required"]) == ["condition", "provider"]
    recent = schemas["AiResidencyRecentRunResponse"]
    assert recent["properties"]["regionlessAllowed"]["type"] == "boolean"
    assert "regionlessAllowed" in recent["required"]


async def test_residency_summary_with_no_runs_is_zero(migration: Migration) -> None:
    data = await _get_summary(migration)

    assert data["runs"] == {
        "total": 0,
        "recorded": 0,
        "unrecorded": 0,
        "sandboxed": 0,
        "externalTransfer": 0,
        "truncated": False,
    }
    assert data["sensitiveExternalTransfer"]["verdict"] == "ZERO"
    assert data["externalTransferBytes"] == 0
    assert data["violations"] == []
    assert data["recentRuns"] == []


async def test_residency_summary_detects_secrets_sent_externally(migration: Migration) -> None:
    now = migration.harness.clock.now()
    run = await _add_run(
        migration, finished_at=now - timedelta(hours=1), processing=SECRETS_PROCESSING
    )
    await _add_run(migration, finished_at=now - timedelta(hours=2))

    data = await _get_summary(migration)

    assert data["sensitiveExternalTransfer"] == {
        "classes": ["SECRETS"],
        "runs": 1,
        "verdict": "DETECTED",
    }
    assert [item["runId"] for item in data["violations"]] == [str(run.id)]
    assert data["violations"][0]["reasons"] == ["FORBIDDEN_CLASS_SENT_EXTERNALLY"]


async def test_residency_summary_only_recorded_runs_is_zero(migration: Migration) -> None:
    now = migration.harness.clock.now()
    await _add_run(migration, finished_at=now - timedelta(hours=1), processing=NONE_PROCESSING)

    data = await _get_summary(migration)

    assert data["sensitiveExternalTransfer"]["verdict"] == "ZERO"


# --- 무엇을 세는가(AC-5, AD-6) ---


async def test_residency_summary_excludes_out_of_range_open_and_other_organization_runs(
    migration: Migration,
) -> None:
    now = migration.harness.clock.now()
    other = await create_organization(migration.harness.session_factory, "other-org")
    inside = await _add_run(
        migration, finished_at=now - timedelta(days=7), processing=NONE_PROCESSING
    )
    await _add_run(migration, finished_at=now - timedelta(days=7, seconds=1))
    await _add_run(migration, finished_at=now, processing=SECRETS_PROCESSING)
    await _add_run(migration, finished_at=None, status=AgentRunStatus.QUEUED)
    await _add_run(
        migration,
        finished_at=now - timedelta(hours=1),
        organization=other,
        processing=SECRETS_PROCESSING,
    )

    data = await _get_summary(migration)

    assert data["runs"]["total"] == 1
    assert [item["runId"] for item in data["recentRuns"]] == [str(inside.id)]
    assert data["sensitiveExternalTransfer"]["verdict"] == "ZERO"


async def test_residency_summary_days_widens_the_window(migration: Migration) -> None:
    now = migration.harness.clock.now()
    await _add_run(migration, finished_at=now - timedelta(days=30), processing=NONE_PROCESSING)

    assert (await _get_summary(migration))["runs"]["total"] == 0
    wide = await _get_summary(migration, days=30)
    assert wide["runs"]["total"] == 1
    assert wide["from"] == _iso(now - timedelta(days=30))
    assert (await _get_summary(migration, days=1))["runs"]["total"] == 0
    assert (await _get_summary(migration, days=90))["runs"]["total"] == 1


async def test_residency_summary_counts_every_finished_status_and_kind_as_unrecorded(
    migration: Migration,
) -> None:
    now = migration.harness.clock.now()
    finished_at = now - timedelta(hours=1)
    parent = await _add_run(migration, finished_at=finished_at, status=AgentRunStatus.FAILED)
    await _add_run(migration, finished_at=finished_at, status=AgentRunStatus.TIMED_OUT)
    await _add_run(migration, finished_at=finished_at, status=AgentRunStatus.CANCELLED)
    await _add_run(
        migration,
        finished_at=finished_at,
        kind=AgentRunKind.APPLY_PATCH,
        parent_run_id=parent.id,
    )

    data = await _get_summary(migration)

    assert data["runs"]["total"] == 4
    assert data["runs"]["unrecorded"] == 4
    assert data["sensitiveExternalTransfer"]["verdict"] == "ZERO_WITH_UNRECORDED"
    assert {item["status"] for item in data["recentRuns"]} == {
        "FAILED",
        "TIMED_OUT",
        "CANCELLED",
        "SUCCEEDED",
    }
    assert {item["kind"] for item in data["recentRuns"]} == {"PRECHECK", "APPLY_PATCH"}


async def test_residency_summary_ai_reviewed_follows_model_id_not_processing(
    migration: Migration,
) -> None:
    now = migration.harness.clock.now()
    await _add_run(
        migration,
        finished_at=now - timedelta(hours=1),
        model_id="claude-opus-5-5",
        simulated=True,
    )

    recent = (await _get_summary(migration))["recentRuns"][0]

    assert recent["aiReviewed"] is True
    assert recent["simulated"] is True
    assert recent["processing"] is None


async def test_residency_summary_reports_truncated_when_over_read_limit(
    migration: Migration, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ai_residency_service, "RESIDENCY_RUN_LIMIT", 2)
    now = migration.harness.clock.now()
    newest = await _add_run(
        migration, finished_at=now - timedelta(hours=1), processing=NONE_PROCESSING
    )
    second = await _add_run(
        migration, finished_at=now - timedelta(hours=2), processing=NONE_PROCESSING
    )
    await _add_run(migration, finished_at=now - timedelta(hours=3), processing=SECRETS_PROCESSING)

    data = await _get_summary(migration)

    assert data["runs"]["truncated"] is True
    assert data["runs"]["total"] == 2
    assert [item["runId"] for item in data["recentRuns"]] == [str(newest.id), str(second.id)]
    assert data["sensitiveExternalTransfer"]["verdict"] == "ZERO_WITH_UNRECORDED"


async def test_residency_summary_counts_run_finished_through_runner_api(
    migration: Migration,
) -> None:
    runner = await add_runner(migration.harness.session_factory)
    detail = await finish_precheck(migration, runner, processing=ANTHROPIC_PROCESSING)
    migration.harness.clock.advance(1)

    data = await _get_summary(migration)

    assert data["runs"]["recorded"] == 1
    assert data["recentRuns"][0]["runId"] == detail["id"]
    assert data["recentRuns"][0]["processing"] == ANTHROPIC_PROCESSING


@pytest.mark.parametrize("days", ["0", "91", "-1", "abc", "1.5"])
async def test_residency_summary_rejects_days_out_of_range(migration: Migration, days: str) -> None:
    response = await migration.author_client.get(_path(migration), params={"days": days})

    assert _error(response) == (422, "VALIDATION_ERROR")


# --- 권한(AC-7) ---


async def test_residency_summary_allows_viewer_and_agent_of_the_organization(
    migration: Migration,
) -> None:
    harness = migration.harness
    now = harness.clock.now()
    viewer = await create_member(
        harness.session_factory, harness.organization, OrganizationRole.VIEWER, now
    )
    _, agent_token = await create_agent_token(
        harness.session_factory, harness.organization, migration.author.user, now
    )

    async with make_client(session_token=viewer.session_token) as client:
        as_viewer = await client.get(_path(migration))
    async with make_client(agent_token=agent_token, origin=None) as client:
        as_agent = await client.get(_path(migration))

    assert as_viewer.status_code == 200, as_viewer.text
    assert as_agent.status_code == 200, as_agent.text


async def test_residency_summary_rejects_anonymous_and_hides_from_other_organization(
    migration: Migration,
) -> None:
    harness = migration.harness
    now = harness.clock.now()
    other = await create_organization(harness.session_factory, "other-org")
    outsider = await create_member(harness.session_factory, other, OrganizationRole.ADMIN, now)
    _, other_agent_token = await create_agent_token(
        harness.session_factory, other, outsider.user, now
    )

    async with make_client() as client:
        anonymous = await client.get(_path(migration))
    async with make_client(session_token=outsider.session_token) as client:
        other_member = await client.get(_path(migration))
    async with make_client(agent_token=other_agent_token, origin=None) as client:
        other_agent = await client.get(_path(migration))

    assert anonymous.status_code == 401
    # 다른 조직은 기존 검사기대로 조직을 숨긴다(모든 조직 경로 공통).
    assert _error(other_member) == (404, "ORGANIZATION_NOT_FOUND")
    assert _error(other_agent) == (404, "ORGANIZATION_NOT_FOUND")
