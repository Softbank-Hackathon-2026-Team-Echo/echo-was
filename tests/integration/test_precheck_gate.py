"""AI 사전 점검 실행이 끝까지 돌아 게이트의 C-AI-1 증거가 되는가(SPEC api-runner-prechecks AC-1~3).

게이트는 enable_gate(가짜 DNS·TLS)로 켠다. C-AI-1 만 보이도록 다른 필수 통제는 기존 게이트 시험과
같은 방식으로 맞춘다: record_passing_evidence(C-IMG-1·2·C-SEC-1·C-IAM-2·C-AUD-1 PASS 증거) +
add_exception(C-DB-1·C-DST-1 기한부 예외). C-IMG-3·C-TLS-1 은 게이트가 직접 확인해 통과한다.
add_precheck(행 직접 넣기)는 쓰지 않는다. C-AI-1 증거는 API 흐름이 만든 실행 행뿐이다.
"""

from datetime import timedelta

import pytest

from app.enums import (
    MigrationLogCode,
    MigrationPlanStatus,
    SecurityControlId,
    SecurityReasonCode,
)
from tests.integration.migration_support import Migration
from tests.integration.runner_support import (
    RunnerFixture,
    add_runner,
    claim_run,
    event,
    finish_precheck,
    prechecks_path,
    request_precheck,
    run_result,
)
from tests.integration.security_gate_support import (
    add_exception,
    enable_gate,
    record_passing_evidence,
)

_C = SecurityControlId
_R = SecurityReasonCode


@pytest.fixture
async def runner(migration: Migration) -> RunnerFixture:
    return await add_runner(migration.harness.session_factory)


async def _open_gate_except_ai_1(migration: Migration) -> None:
    enable_gate(migration)
    await record_passing_evidence(migration)
    await add_exception(migration, _C.DB_1)
    await add_exception(migration, _C.DST_1)


def _blocking(details: list[dict[str, str]]) -> dict[str, set[str]]:
    by_control: dict[str, set[str]] = {}
    for item in details:
        by_control.setdefault(item["field"], set()).add(item["reason"])
    return by_control


async def test_precheck_round_trip_satisfies_ai_1_gate(
    migration: Migration, runner: RunnerFixture
) -> None:
    await _open_gate_except_ai_1(migration)

    created = await request_precheck(migration)
    assert created.status_code == 201, created.text
    run_id = created.json()["data"]["id"]
    assert created.json()["data"]["status"] == "QUEUED"
    claimed = await claim_run(runner)
    assert claimed.run_id == run_id
    events = await claimed.post(
        "events",
        {"events": [event(1, "SANDBOX_CREATE"), event(2, "INPUT_LOAD"), event(3, "CONFIG_DIFF")]},
    )
    assert events.json()["data"] == {"acceptedUpTo": 3}
    submitted = await claimed.post("result", run_result(migration))
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["data"] == {"status": "SUCCEEDED"}

    detail = (await migration.author_client.get(prechecks_path(migration, f"/{run_id}"))).json()[
        "data"
    ]
    assert detail["status"] == "SUCCEEDED"
    assert detail["simulated"] is False
    assert detail["aiReviewed"] is False
    assert detail["model"] is None
    assert [item["key"] for item in detail["configDiff"]] == ["region"]
    assert [item["result"] for item in detail["findings"]] == ["WARN"]
    assert detail["sandboxId"] == "echo-ws-1"
    assert [step["name"] for step in detail["steps"]] == [
        "CONFIG_DIFF",
        "INPUT_LOAD",
        "SANDBOX_CREATE",
    ]

    plan = await migration.create_plan()
    approved = await migration.approve(plan["id"])

    assert approved["status"] == MigrationPlanStatus.APPROVED
    lines = await migration.log_rows(plan["id"])
    passed = next(line for line in lines if line.code == MigrationLogCode.SECURITY_GATE_PASSED)
    assert passed.data is not None
    assert passed.data["blocking"] == []
    assert run_id in passed.data["findingIds"]


async def test_simulated_precheck_does_not_satisfy_gate(
    migration: Migration, runner: RunnerFixture
) -> None:
    await _open_gate_except_ai_1(migration)
    detail = await finish_precheck(migration, runner, simulated=True)
    assert detail["simulated"] is True

    plan = await migration.create_plan()
    response = await migration.act(plan["id"], "approve")

    assert response.status_code == 409, response.text
    blocking = _blocking(response.json()["details"])
    assert set(blocking) == {_C.AI_1.value}
    assert _R.EVIDENCE_SIMULATED.value in blocking[_C.AI_1.value]


async def test_ai_reviewed_precheck_also_satisfies_gate(
    migration: Migration, runner: RunnerFixture
) -> None:
    await _open_gate_except_ai_1(migration)
    model = {"id": "claude-opus-5-5", "effort": "medium", "promptVersion": "v1", "refusal": False}
    detail = await finish_precheck(migration, runner, model=model)
    assert detail["aiReviewed"] is True
    assert detail["model"] == model

    plan = await migration.create_plan()

    assert (await migration.approve(plan["id"]))["status"] == MigrationPlanStatus.APPROVED


async def test_failed_precheck_does_not_satisfy_gate(
    migration: Migration, runner: RunnerFixture
) -> None:
    await _open_gate_except_ai_1(migration)
    await finish_precheck(migration, runner, status="FAILED", failureCode="RUNNER_ERROR")

    plan = await migration.create_plan()
    response = await migration.act(plan["id"], "approve")

    assert response.status_code == 409, response.text
    assert _blocking(response.json()["details"]) == {_C.AI_1.value: {_R.PRECHECK_MISSING.value}}


async def test_precheck_evidence_expires_after_24h(
    migration: Migration, runner: RunnerFixture
) -> None:
    await _open_gate_except_ai_1(migration)
    await finish_precheck(migration, runner)
    migration.harness.clock.advance(timedelta(hours=24, seconds=1).total_seconds())

    plan = await migration.create_plan()
    response = await migration.act(plan["id"], "approve")

    assert response.status_code == 409, response.text
    blocking = _blocking(response.json()["details"])
    assert blocking[_C.AI_1.value] == {_R.PRECHECK_MISSING.value}


async def test_precheck_for_other_target_is_ignored(
    migration: Migration, runner: RunnerFixture
) -> None:
    await _open_gate_except_ai_1(migration)
    other = await migration.harness.add_environment("gcp-tokyo")
    created = await request_precheck(migration, targetEnvironmentId=str(other.id))
    assert created.status_code == 201, created.text
    claimed = await claim_run(runner)
    submitted = await claimed.post("result", run_result(migration))
    assert submitted.status_code == 200, submitted.text

    plan = await migration.create_plan()
    response = await migration.act(plan["id"], "approve")

    assert response.status_code == 409, response.text
    assert _blocking(response.json()["details"]) == {_C.AI_1.value: {_R.PRECHECK_MISSING.value}}
