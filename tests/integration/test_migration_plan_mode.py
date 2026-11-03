"""이전 계획 종류 mode(API 서버 8-3).

서버가 계획을 만들 때 같은 서비스의 가장 늦게 끝난 SUCCEEDED 계획을 보고 판정해 저장한다.
그 계획의 출발·도착을 거꾸로 한 계획이고 그 계획이 FAILBACK 이 아니면 FAILBACK, 아니면 PLANNED.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.enums import MigrationPlanMode, MigrationPlanStatus
from app.main import app
from app.models import Environment
from tests.integration.migration_support import Migration
from tests.integration.rollback_support import deploy_and_finish

# 끝 상태로 바꿀 때 error_code 가 있어야 하는 상태(CHECK error_code_consistency).
_ERROR_CODE_BY_STATUS = {
    MigrationPlanStatus.ROLLED_BACK: "THRESHOLD_EXCEEDED",
    MigrationPlanStatus.FAILED: "PREFLIGHT_FAILED",
}


async def _finish(
    migration: Migration, plan_id: str, status: MigrationPlanStatus, finished_at: datetime
) -> None:
    """만든 계획을 끝 상태로 바로 적는다(실행 없이 이력만 만든다)."""
    async with migration.harness.session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE migration_plans SET status = :status, finished_at = :finished_at, "
                "error_code = :error_code, updated_at = :finished_at WHERE id = :id"
            ),
            {
                "status": str(status),
                "finished_at": finished_at,
                "error_code": _ERROR_CODE_BY_STATUS.get(status),
                "id": plan_id,
            },
        )


async def _set_weights(migration: Migration, weight_by_environment: dict[uuid.UUID, int]) -> None:
    """시험에서 연결 비율을 바로 바꾼다(출발을 바꾸려고)."""
    async with migration.harness.session_factory() as session, session.begin():
        for environment_id, weight in weight_by_environment.items():
            await session.execute(
                text(
                    "UPDATE service_environments SET traffic_weight = :weight "
                    "WHERE service_id = :service AND environment_id = :environment "
                    "AND is_deleted = false"
                ),
                {"weight": weight, "service": migration.service_id, "environment": environment_id},
            )


async def _run_to_success(migration: Migration, plan: dict[str, Any]) -> None:
    await migration.approve(plan["id"])
    await migration.harness.run_until_idle()
    assert (await migration.get_plan(plan["id"]))["status"] == "SUCCEEDED"


async def _add_onprem(migration: Migration) -> Environment:
    harness = migration.harness
    onprem = await harness.add_environment("onprem-k3s")
    await deploy_and_finish(harness, migration.service_id, onprem.id, branch="main")
    return onprem


# --- AC-8-3-1: 첫 계획 ---


async def test_create_first_plan_without_success_history_is_planned(migration: Migration) -> None:
    plan = await migration.create_plan()
    row = await migration.plan_row(plan["id"])
    created_log = (await migration.log_rows(plan["id"]))[0]

    assert plan["mode"] == "PLANNED"
    assert row.mode == MigrationPlanMode.PLANNED
    assert (created_log.code, (created_log.data or {}).get("mode")) == ("PLAN_CREATED", "PLANNED")


# --- AC-8-3-2·3: 복귀와 복귀의 복귀 ---


async def test_create_reverse_of_last_success_is_failback_and_failback_of_failback_is_planned(
    migration: Migration,
) -> None:
    first = await migration.create_plan()
    await _run_to_success(migration, first)
    back = await migration.create_plan(targetEnvironmentId=str(migration.aws.id))
    back_log = (await migration.log_rows(back["id"]))[0]
    await _run_to_success(migration, back)
    again = await migration.create_plan()

    assert first["mode"] == "PLANNED"
    # AC-8-3-2: aws→gcp 성공 뒤 gcp→aws 는 복귀.
    assert (back["sourceEnvironmentId"], back["targetEnvironmentId"]) == (
        str(migration.gcp.id),
        str(migration.aws.id),
    )
    assert back["mode"] == "FAILBACK"
    assert (back_log.data or {}).get("mode") == "FAILBACK"
    # AC-8-3-3: 복귀가 성공한 뒤 다시 aws→gcp 는 새 이전.
    assert again["mode"] == "PLANNED"
    # 단건·목록 응답도 저장값을 그대로 싣는다.
    assert (await migration.get_plan(back["id"]))["mode"] == "FAILBACK"
    listed = await migration.harness.client.get(migration.plans_path)
    mode_by_id = {item["id"]: item["mode"] for item in listed.json()["data"]["items"]}
    assert mode_by_id == {first["id"]: "PLANNED", back["id"]: "FAILBACK", again["id"]: "PLANNED"}


# --- AC-8-3-4: 성공만 본다 ---


@pytest.mark.parametrize(
    "status",
    [
        MigrationPlanStatus.ROLLED_BACK,
        MigrationPlanStatus.FAILED,
        MigrationPlanStatus.CANCELLED,
        MigrationPlanStatus.REJECTED,
        MigrationPlanStatus.EXPIRED,
    ],
)
async def test_create_reverse_of_unsuccessful_plan_is_planned(
    migration: Migration, status: MigrationPlanStatus
) -> None:
    clock = migration.harness.clock
    forward = await migration.create_plan()
    await _finish(migration, forward["id"], status, clock.now())
    await _set_weights(migration, {migration.aws.id: 0, migration.gcp.id: 100})

    reverse = await migration.create_plan(targetEnvironmentId=str(migration.aws.id))

    assert reverse["mode"] == "PLANNED"


# --- AC-8-3-5: 가장 늦게 끝난 성공만 본다 ---


async def test_create_compares_only_latest_finished_success(migration: Migration) -> None:
    clock = migration.harness.clock
    onprem = await _add_onprem(migration)
    to_gcp = await migration.create_plan()
    await _finish(migration, to_gcp["id"], MigrationPlanStatus.SUCCEEDED, clock.now())
    to_onprem = await migration.create_plan(targetEnvironmentId=str(onprem.id))
    # 늦게 만든 계획이 먼저 끝났어도 finished_at 이 늦은 쪽이 기준이다.
    await _finish(
        migration,
        to_onprem["id"],
        MigrationPlanStatus.SUCCEEDED,
        clock.now() + timedelta(seconds=10),
    )
    await _set_weights(migration, {migration.aws.id: 0, migration.gcp.id: 100, onprem.id: 0})
    from_gcp = await migration.create_plan(targetEnvironmentId=str(migration.aws.id))
    await migration.act(from_gcp["id"], "cancel", client=migration.author_client)
    await _set_weights(migration, {migration.aws.id: 0, migration.gcp.id: 0, onprem.id: 100})
    from_onprem = await migration.create_plan(targetEnvironmentId=str(migration.aws.id))

    assert from_gcp["mode"] == "PLANNED"
    assert from_onprem["mode"] == "FAILBACK"


async def test_create_breaks_finished_at_tie_by_created_at(migration: Migration) -> None:
    clock = migration.harness.clock
    onprem = await _add_onprem(migration)
    finished_at = clock.now() + timedelta(seconds=30)
    to_gcp = await migration.create_plan()
    await _finish(migration, to_gcp["id"], MigrationPlanStatus.SUCCEEDED, finished_at)
    clock.advance(1)
    to_onprem = await migration.create_plan(targetEnvironmentId=str(onprem.id))
    await _finish(migration, to_onprem["id"], MigrationPlanStatus.SUCCEEDED, finished_at)
    await _set_weights(migration, {migration.aws.id: 0, onprem.id: 100})

    from_onprem = await migration.create_plan(targetEnvironmentId=str(migration.aws.id))

    # 같은 시각에 끝났으면 늦게 만든 aws→onprem 이 기준이다.
    assert from_onprem["mode"] == "FAILBACK"


# --- AC-8-3-6: 응답·OpenAPI·요청 ---


async def test_create_rejects_mode_in_request_body(migration: Migration) -> None:
    response = await migration.request_plan(mode="FAILBACK")

    # 요청 모델은 모르는 칸을 거부한다(클라이언트가 종류를 정할 수 없다).
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["details"][0]["field"] == "mode"
    listed = await migration.harness.client.get(migration.plans_path)
    assert listed.json()["data"]["total"] == 0


def test_openapi_plan_responses_expose_mode_enum() -> None:
    schemas = app.openapi()["components"]["schemas"]

    assert schemas["MigrationPlanMode"]["enum"] == ["PLANNED", "EMERGENCY", "FAILBACK"]
    for name in ("MigrationPlanSummaryResponse", "MigrationPlanDetailResponse"):
        mode = schemas[name]["properties"]["mode"]
        assert mode == {"$ref": "#/components/schemas/MigrationPlanMode"}
        assert "mode" in schemas[name]["required"]
    assert "mode" not in schemas["CreateMigrationPlanRequest"]["properties"]


# --- AC-8-3-7: DB 기본값과 CHECK ---


async def test_mode_column_defaults_to_planned_and_check_rejects_other_values(
    migration: Migration,
) -> None:
    plan = await migration.create_plan()
    async with migration.harness.session_factory() as session:
        default = await session.scalar(
            text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name = 'migration_plans' AND column_name = 'mode'"
            )
        )
    # EMERGENCY 는 API 로는 만들지 않지만(API 서버 9 부터) DB 값으로는 받는다.
    async with migration.harness.session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE migration_plans SET mode = 'EMERGENCY' WHERE id = :id"),
            {"id": plan["id"]},
        )
    with pytest.raises(IntegrityError, match="ck_migration_plans_mode_valid"):
        async with migration.harness.session_factory() as session, session.begin():
            await session.execute(
                text("UPDATE migration_plans SET mode = 'ROLLBACK' WHERE id = :id"),
                {"id": plan["id"]},
            )

    assert default == "'PLANNED'::character varying"
    assert (await migration.plan_row(plan["id"])).mode == MigrationPlanMode.EMERGENCY
