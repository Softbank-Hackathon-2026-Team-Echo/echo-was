"""C-AI-1: 실제 결과 우선 선택과 조직·서비스·출발·도착·24시간 범위."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.enums import (
    AgentRunKind,
    AgentRunStatus,
    MigrationLogCode,
    MigrationPlanMode,
    MigrationPlanStatus,
    SecurityControlId,
)
from app.models import AgentRun, DeployRequest, MigrationPlan
from tests.integration.auth_support import create_organization
from tests.integration.migration_support import Migration
from tests.integration.runner_support import add_runner, finish_precheck
from tests.integration.security_gate_support import (
    add_exception,
    add_precheck,
    prepare_passing_gate,
    record_passing_evidence,
)


async def _gate_evidence_ids(migration: Migration, plan_id: str) -> list[list[str]]:
    return [
        row.data["findingIds"]
        for row in await migration.log_rows(plan_id)
        if row.code == MigrationLogCode.SECURITY_GATE_PASSED and row.data is not None
    ]


@pytest.mark.parametrize("mode", ["PLANNED", "EMERGENCY"])
async def test_later_simulation_does_not_hide_real_precheck_at_approval_and_start(
    migration: Migration, mode: str
) -> None:
    await prepare_passing_gate(migration)
    runner = await add_runner(migration.harness.session_factory)
    real = await finish_precheck(migration, runner)
    migration.harness.clock.advance(60)
    simulated = await finish_precheck(migration, runner, simulated=True)
    plan = await migration.create_plan()
    # EMERGENCY는 사용자 생성 API 입력이 아니다. 저장된 긴급 계획에서도 같은 게이트를 확인한다.
    if mode == "EMERGENCY":
        async with migration.harness.session_factory() as session, session.begin():
            row = await session.get_one(MigrationPlan, uuid.UUID(plan["id"]))
            row.mode = MigrationPlanMode.EMERGENCY

    await migration.approve(plan["id"])
    await migration.harness.run_until_idle()

    assert (await migration.plan_row(plan["id"])).status == MigrationPlanStatus.SUCCEEDED
    evidence = await _gate_evidence_ids(migration, plan["id"])
    assert len(evidence) == 2
    assert all(real["id"] in ids and simulated["id"] not in ids for ids in evidence)


@pytest.mark.parametrize("simulated_ago", [timedelta(minutes=1), timedelta(hours=3)])
async def test_latest_real_precheck_is_selected(
    migration: Migration, simulated_ago: timedelta
) -> None:
    await prepare_passing_gate(migration)
    latest = await add_precheck(migration, finished_ago=timedelta(minutes=10))
    simulated = await add_precheck(migration, finished_ago=simulated_ago, simulated=True)
    plan = await migration.create_plan()

    await migration.approve(plan["id"])

    evidence = (await _gate_evidence_ids(migration, plan["id"]))[0]
    assert str(latest) in evidence
    assert str(simulated) not in evidence


@pytest.mark.parametrize(
    "mismatch",
    [
        "organization",
        "service",
        "source",
        "missing_source",
        "target",
        "kind",
        "failed",
        "old",
        "boundary",
    ],
)
async def test_precheck_outside_scope_or_validity_does_not_satisfy_gate(
    migration: Migration, mismatch: str
) -> None:
    # 행의 메타데이터까지 조직 격리를 검증한다. 정상 API가 만들지 않는 불일치도 제외해야 한다.
    await prepare_passing_gate(migration)
    harness = migration.harness
    other_org = await create_organization(harness.session_factory, "precheck-other")
    other_service = await harness.create_service(name="precheck-other")
    async with harness.session_factory() as session, session.begin():
        run = await session.scalar(
            select(AgentRun).where(AgentRun.service_id == uuid.UUID(migration.service_id))
        )
        assert run is not None
        if mismatch == "organization":
            run.organization_id = other_org.id
        elif mismatch == "service":
            run.service_id = uuid.UUID(other_service["id"])
        elif mismatch == "source":
            run.source_environment_id = migration.gcp.id
        elif mismatch == "missing_source":
            run.source_environment_id = None
        elif mismatch == "target":
            run.target_environment_id = migration.aws.id
        elif mismatch == "kind":
            run.deploy_request_id = await session.scalar(
                select(DeployRequest.id)
                .where(DeployRequest.service_id == uuid.UUID(migration.service_id))
                .limit(1)
            )
            run.kind = AgentRunKind.CHANGE_PROPOSAL
        elif mismatch == "failed":
            run.status = AgentRunStatus.FAILED
            run.failure_code = "RUNNER_ERROR"
        else:
            run.finished_at = harness.clock.now() - timedelta(
                hours=24, seconds=1 if mismatch == "old" else 0
            )
    plan = await migration.create_plan()

    response = await migration.act(plan["id"], "approve")

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "SECURITY_GATE_BLOCKED"
    assert response.json()["details"] == [{"field": "C-AI-1", "reason": "PRECHECK_MISSING"}]
    assert await migration.plan_job(plan["id"]) is None


async def test_expired_real_does_not_override_fresh_simulated_diagnostic(
    migration: Migration,
) -> None:
    await prepare_passing_gate(migration)
    migration.harness.clock.advance(timedelta(hours=24).total_seconds())
    # 다른 통제의 증거는 새로 맞추고, 실제 PRECHECK만 24시간 밖으로 보낸다.
    await record_passing_evidence(migration)
    await add_exception(migration, SecurityControlId.DB_1)
    await add_exception(migration, SecurityControlId.DST_1)
    await add_precheck(migration, finished_ago=timedelta(minutes=1), simulated=True)
    plan = await migration.create_plan()

    response = await migration.act(plan["id"], "approve")

    assert response.status_code == 409, response.text
    assert response.json()["details"] == [
        {"field": "C-AI-1", "reason": "PRECHECK_MISSING"},
        {"field": "C-AI-1", "reason": "EVIDENCE_SIMULATED"},
    ]
    assert await migration.plan_job(plan["id"]) is None
