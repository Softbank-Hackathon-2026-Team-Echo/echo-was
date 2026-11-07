"""재난 이벤트 → 피해 환경 서비스 긴급 이전 계획 자동 생성(API 서버 9, SPEC 7절 AC)."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import func, select

from app.audit.actions import AuditAction
from app.enums import (
    ActorType,
    AuditResult,
    ConnectionErrorCode,
    JobKind,
    MigrationPlanStatus,
    OrganizationRole,
)
from app.models import AuditLog, Disaster, Environment, Job, MigrationPlan
from app.services.migration_plan_service import MigrationPlanService
from tests.integration.auth_support import (
    create_agent_token,
    create_member,
    create_organization,
    make_client,
    org_path,
)
from tests.integration.deploy_harness import DeployHarness
from tests.integration.disaster_support import (
    MID_PACIFIC,
    NEAR_SEOUL,
    add_located_environment,
    add_standby_service,
    allow_self_approval,
    aws_config,
    declare_disaster,
    disaster_body,
    disasters_path,
    gcp_config,
    onprem_config,
    request_disaster,
    tokyo_serving_seoul_standby,
)
from tests.integration.migration_support import Migration
from tests.integration.rollback_support import deploy_and_finish
from tests.integration.security_gate_support import enable_gate, prepare_passing_gate

OTHER_COMMIT = "f" * 40


@pytest.fixture
async def tokyo(harness: DeployHarness) -> AsyncIterator[Migration]:
    setup = await tokyo_serving_seoul_standby(harness)
    try:
        yield setup
    finally:
        await setup.author_client.aclose()
        await setup.approver_client.aclose()


def _only_plan(disaster: dict[str, Any]) -> dict[str, Any]:
    (plan,) = disaster["plans"]
    assert isinstance(plan, dict)
    return plan


def _plan_for(disaster: dict[str, Any], service_id: str) -> dict[str, Any]:
    (plan,) = [item for item in disaster["plans"] if item["serviceId"] == service_id]
    assert isinstance(plan, dict)
    return plan


async def _disaster_count(harness: DeployHarness) -> int:
    async with harness.session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(Disaster)) or 0)


async def _plan_count(harness: DeployHarness) -> int:
    async with harness.session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(MigrationPlan)) or 0)


async def _plan_job_count(harness: DeployHarness, plan_id: str) -> int:
    async with harness.session_factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.kind == JobKind.MIGRATION_PLAN, Job.subject_id == uuid.UUID(plan_id))
            )
            or 0
        )


async def _audit_rows(harness: DeployHarness, action: AuditAction) -> list[AuditLog]:
    async with harness.session_factory() as session:
        return list(
            (
                await session.scalars(
                    select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.seq)
                )
            )
            .unique()
            .all()
        )


# --- AC-1: 미리보기·만들기의 반경과 피해/안전/모름 ---


async def test_preview_near_tokyo_m72_returns_radius_and_classification_without_saving(
    tokyo: Migration,
) -> None:
    harness = tokyo.harness
    onprem = await add_located_environment(
        harness, "onprem-k3s", "onprem", onprem_config("team-laptop")
    )

    response = await harness.client.get(
        disasters_path(harness, "/preview"),
        params={"type": "EARTHQUAKE", "lat": 35.2, "lng": 139.4, "magnitude": 7.2},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "radiusKm": 302,
        "meetsThreshold": True,
        "affected": [str(tokyo.aws.id)],
        "safe": [str(tokyo.gcp.id)],
        "unknown": [str(onprem.id)],
    }
    assert await _disaster_count(harness) == 0
    assert await _plan_count(harness) == 0


async def test_create_returns_same_classification_as_preview_and_disaster_shape(
    tokyo: Migration,
) -> None:
    onprem = await add_located_environment(
        tokyo.harness, "onprem-k3s", "onprem", onprem_config("team-laptop")
    )

    response = await request_disaster(tokyo)

    assert response.status_code == 201, response.text
    disaster = response.json()["data"]
    plan = disaster.pop("plans")[0]
    disaster_id = disaster.pop("id")
    started_at = disaster.pop("startedAt")
    assert uuid.UUID(disaster_id) and started_at
    assert disaster == {
        "type": "EARTHQUAKE",
        "source": "SIMULATED",
        "simulated": True,
        "magnitude": 7.2,
        "epicenter": {"lat": 35.2, "lng": 139.4},
        "radiusKm": 302,
        "meetsThreshold": True,
        "status": "ACTIVE",
        "affectedEnvironmentIds": [str(tokyo.aws.id)],
        "safeEnvironmentIds": [str(tokyo.gcp.id)],
        "unknownEnvironmentIds": [str(onprem.id)],
        "createdBy": {"id": str(tokyo.author.user.id), "login": "dev-a"},
        "endedBy": None,
        "endedAt": None,
        "feedEvent": None,
    }
    assert plan["serviceId"] == tokyo.service_id


@pytest.mark.parametrize(
    "params",
    [
        {"lat": 91},
        {"lat": -91},
        {"lng": 181},
        {"lng": -181},
        {"magnitude": 10.5},
        {"magnitude": -0.1},
        {"magnitude": 7.25},
        {"type": "FLOOD"},
    ],
)
async def test_preview_rejects_out_of_range_query_with_422(
    harness: DeployHarness, params: dict[str, Any]
) -> None:
    base = {"type": "EARTHQUAKE", "lat": 35.2, "lng": 139.4, "magnitude": 7.2}

    response = await harness.client.get(
        disasters_path(harness, "/preview"), params={**base, **params}
    )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "body",
    [
        {"type": "FLOOD", "epicenter": {"lat": 0, "lng": 0}, "magnitude": 7},
        {"type": "EARTHQUAKE", "epicenter": {"lat": 91, "lng": 0}, "magnitude": 7},
        {"type": "EARTHQUAKE", "epicenter": {"lat": 0, "lng": 0}, "magnitude": 11},
        {"type": "EARTHQUAKE", "epicenter": {"lat": 0, "lng": 0}, "magnitude": 7.25},
        {"type": "EARTHQUAKE", "epicenter": {"lat": 0, "lng": 0}},
        {"type": "EARTHQUAKE", "epicenter": {"lat": 0, "lng": 0}, "magnitude": 7, "mode": "X"},
    ],
)
async def test_create_rejects_invalid_body_with_422_and_saves_nothing(
    harness: DeployHarness, body: dict[str, Any]
) -> None:
    response = await harness.client.post(disasters_path(harness), json=body)

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert await _disaster_count(harness) == 0


# --- AC-2: 긴급 계획 ---


async def test_create_makes_emergency_plan_to_standby_with_disaster_log_before_plan_created(
    tokyo: Migration,
) -> None:
    disaster = await declare_disaster(tokyo)

    result = _only_plan(disaster)
    assert result["outcome"] == "PENDING_APPROVAL"
    assert result["targetEnvironmentId"] == str(tokyo.gcp.id)
    plan = await tokyo.get_plan(result["planId"])
    assert plan["mode"] == "EMERGENCY"
    assert plan["disasterId"] == disaster["id"]
    assert plan["sourceEnvironmentId"] == str(tokyo.aws.id)
    assert plan["targetEnvironmentId"] == str(tokyo.gcp.id)
    assert plan["createdBy"]["login"] == "dev-a"
    assert plan["simulated"] is True
    lines = await tokyo.log_rows(result["planId"])
    assert [line.code for line in lines] == ["DISASTER_TRIGGERED", "PLAN_CREATED"]
    assert lines[0].data == {
        "disasterId": disaster["id"],
        "magnitude": 7.2,
        "distanceKm": pytest.approx(59.7, abs=1),
    }
    # 목록 응답에도 재난 id 가 있다.
    summary = await tokyo.harness.client.get(tokyo.plans_path)
    assert summary.json()["data"]["items"][0]["disasterId"] == disaster["id"]


async def test_create_picks_farthest_safe_standby_from_epicenter(tokyo: Migration) -> None:
    harness = tokyo.harness
    osaka = await add_located_environment(harness, "aws-osaka", "aws", aws_config("ap-northeast-3"))
    await deploy_and_finish(harness, tokyo.service_id, osaka.id, branch="main")

    disaster = await declare_disaster(tokyo)

    assert str(osaka.id) in disaster["safeEnvironmentIds"]
    assert _only_plan(disaster)["targetEnvironmentId"] == str(tokyo.gcp.id)


async def test_plan_without_disaster_has_null_disaster_id_and_unchanged_logs(
    tokyo: Migration,
) -> None:
    plan = await tokyo.create_plan()

    assert plan["disasterId"] is None
    assert plan["mode"] == "PLANNED"
    assert plan["failback"] == {"allowed": True, "reason": None, "disasterId": None}
    assert await tokyo.log_codes(plan["id"]) == ["PLAN_CREATED"]


# --- AC-3: 자기 승인 허용 → 같은 요청에서 실행 시작 ---


async def test_self_approval_allowed_starts_plan_and_worker_runs_it_to_success(
    tokyo: Migration,
) -> None:
    await allow_self_approval(tokyo.harness)

    disaster = await declare_disaster(tokyo)

    result = _only_plan(disaster)
    assert result["outcome"] == "APPROVED"
    assert result["reason"] is None
    assert result["planStatus"] == "APPROVED"
    plan = await tokyo.get_plan(result["planId"])
    assert plan["status"] == "APPROVED"
    assert plan["selfApproved"] is True
    assert plan["approvedBy"]["login"] == "dev-a"
    assert await _plan_job_count(tokyo.harness, result["planId"]) == 1
    assert (await tokyo.log_codes(result["planId"]))[:3] == [
        "DISASTER_TRIGGERED",
        "PLAN_CREATED",
        "SELF_APPROVED",
    ]

    await tokyo.harness.run_until_idle()

    finished = await tokyo.get_plan(result["planId"])
    assert finished["status"] == "SUCCEEDED"
    assert finished["mode"] == "EMERGENCY"
    assert await tokyo.weights() == (0, 100)


# --- AC-4: 자기 승인 꺼짐 → 승인 대기, 다른 개발자가 시작 ---


async def test_self_approval_disabled_waits_and_other_developer_starts_execution(
    tokyo: Migration,
) -> None:
    disaster = await declare_disaster(tokyo)

    result = _only_plan(disaster)
    assert result["outcome"] == "PENDING_APPROVAL"
    assert result["reason"] == "SELF_APPROVAL_DISABLED"
    assert result["planStatus"] == "PENDING_APPROVAL"
    assert await _plan_job_count(tokyo.harness, result["planId"]) == 0
    # 승인을 시도하지 않으므로 거부 줄도 없다.
    async with tokyo.harness.session_factory() as session:
        denied = await session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.result == AuditResult.DENIED)
        )
    assert denied == 0

    approved = await tokyo.approve(result["planId"])

    assert approved["status"] == "APPROVED"
    assert approved["selfApproved"] is False
    await tokyo.harness.run_until_idle()
    assert (await tokyo.get_plan(result["planId"]))["status"] == "SUCCEEDED"


async def test_emergency_plan_can_be_cancelled_and_aborted_with_existing_actions(
    tokyo: Migration,
) -> None:
    pending = _only_plan(await declare_disaster(tokyo))
    cancelled = await tokyo.act(pending["planId"], "cancel", tokyo.author_client)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["data"]["status"] == "CANCELLED"

    await allow_self_approval(tokyo.harness)
    started = _only_plan(await declare_disaster(tokyo))
    aborted = await tokyo.act(started["planId"], "abort", tokyo.author_client)
    assert aborted.status_code == 202, aborted.text

    await tokyo.harness.run_until_idle()

    assert (await tokyo.get_plan(started["planId"]))["status"] == "CANCELLED"
    assert await tokyo.weights() == (100, 0)


# --- AC-5: 기준 미달·태풍 ---


async def test_earthquake_below_threshold_saves_disaster_without_plans(tokyo: Migration) -> None:
    disaster = await declare_disaster(tokyo, magnitude=6.9)

    assert disaster["meetsThreshold"] is False
    assert disaster["radiusKm"] == 231
    assert disaster["status"] == "ACTIVE"
    assert disaster["affectedEnvironmentIds"] == [str(tokyo.aws.id)]
    assert disaster["plans"] == []
    assert await _disaster_count(tokyo.harness) == 1
    assert await _plan_count(tokyo.harness) == 0


async def test_earthquake_on_threshold_makes_plan(tokyo: Migration) -> None:
    disaster = await declare_disaster(tokyo, magnitude=7.0)

    assert disaster["meetsThreshold"] is True
    assert _only_plan(disaster)["planId"] is not None


async def test_typhoon_is_target_regardless_of_magnitude(tokyo: Migration) -> None:
    disaster = await declare_disaster(tokyo, magnitude=6.0, type_="TYPHOON")

    assert disaster["meetsThreshold"] is True
    assert disaster["radiusKm"] == 103
    plan = await tokyo.get_plan(_only_plan(disaster)["planId"])
    assert plan["mode"] == "EMERGENCY"


async def test_no_affected_environment_means_no_plans(tokyo: Migration) -> None:
    disaster = await declare_disaster(tokyo, epicenter=MID_PACIFIC)

    assert disaster["meetsThreshold"] is True
    assert disaster["affectedEnvironmentIds"] == []
    assert disaster["plans"] == []


async def test_standby_in_affected_environment_is_not_a_target_service(tokyo: Migration) -> None:
    """서비스 중 환경이 안전하면 예비가 피해를 입어도 이 재난의 대상이 아니다."""
    disaster = await declare_disaster(tokyo, epicenter=NEAR_SEOUL)

    assert disaster["affectedEnvironmentIds"] == [str(tokyo.gcp.id)]
    assert disaster["plans"] == []


# --- AC-6: 멱등·건너뜀·독립 ---


async def test_repeated_button_skips_service_with_open_plan(tokyo: Migration) -> None:
    first = _only_plan(await declare_disaster(tokyo))

    second = _only_plan(await declare_disaster(tokyo))

    assert second["outcome"] == "SKIPPED"
    assert second["reason"] == "MIGRATION_PLAN_OPEN"
    assert second["planId"] == first["planId"]
    assert await _plan_count(tokyo.harness) == 1


async def test_running_plan_is_skipped_as_open_not_as_no_target(tokyo: Migration) -> None:
    """비율을 옮기는 중에도 NO_SAFE_TARGET 이 아니라 열린 계획으로 답한다."""
    await allow_self_approval(tokyo.harness)
    first = _only_plan(await declare_disaster(tokyo))
    await tokyo.set_weights(50, 50)

    second = _only_plan(await declare_disaster(tokyo))

    assert (second["outcome"], second["reason"]) == ("SKIPPED", "MIGRATION_PLAN_OPEN")
    assert second["planId"] == first["planId"]


async def test_overdue_pending_plan_is_expired_and_replaced(tokyo: Migration) -> None:
    first = _only_plan(await declare_disaster(tokyo))
    tokyo.harness.clock.advance(tokyo.harness.settings.migration_plan_approval_ttl_seconds + 1)

    second = _only_plan(await declare_disaster(tokyo))

    assert second["planId"] not in (None, first["planId"])
    assert (await tokyo.get_plan(first["planId"]))["status"] == "EXPIRED"


async def test_service_without_standby_is_skipped_no_safe_target(harness: DeployHarness) -> None:
    tokyo_env = await add_located_environment(
        harness, "aws-tokyo", "aws", aws_config("ap-northeast-1")
    )
    service = await harness.create_service("solo-api")
    await deploy_and_finish(harness, service["id"], tokyo_env.id, branch="main")

    response = await harness.client.post(disasters_path(harness), json=disaster_body())

    assert response.status_code == 201, response.text
    (plan,) = response.json()["data"]["plans"]
    assert plan == {
        "serviceId": service["id"],
        "planId": None,
        "outcome": "SKIPPED",
        "reason": "NO_SAFE_TARGET",
        "targetEnvironmentId": None,
        "planStatus": None,
    }


async def test_failed_or_unmapped_or_unknown_standby_is_not_a_target(tokyo: Migration) -> None:
    harness = tokyo.harness
    failed = await add_located_environment(
        harness, "gcp-osaka", "gcp", gcp_config("asia-northeast2")
    )
    nameless = await add_located_environment(
        harness, "onprem-busan", "onprem", onprem_config("busan")
    )
    unknown = await add_located_environment(
        harness, "onprem-lab", "onprem", onprem_config("team-laptop")
    )
    service = await harness.create_service("edge-api")
    await deploy_and_finish(harness, service["id"], tokyo.aws.id, branch="main")
    for environment in (failed, nameless, unknown):
        await deploy_and_finish(harness, service["id"], environment.id, branch="main")
    async with harness.session_factory() as session, session.begin():
        row = await session.get_one(Environment, failed.id)
        row.record_connection_check(harness.clock.now(), ConnectionErrorCode.AUTH_FAILED)

    disaster = await declare_disaster(tokyo)

    edge = _plan_for(disaster, service["id"])
    assert (edge["outcome"], edge["reason"]) == ("SKIPPED", "NO_SAFE_TARGET")
    # shop-api 는 gcp-seoul 로 잘 만들어진다.
    assert _plan_for(disaster, tokyo.service_id)["targetEnvironmentId"] == str(tokyo.gcp.id)


async def test_environment_in_entrypoint_map_can_be_target(harness: DeployHarness) -> None:
    tokyo_env = await add_located_environment(
        harness, "aws-tokyo", "aws", aws_config("ap-northeast-1")
    )
    busan = await add_located_environment(harness, "onprem-busan", "onprem", onprem_config("busan"))
    service = await harness.create_service("edge-api")
    await deploy_and_finish(harness, service["id"], tokyo_env.id, branch="main")
    await deploy_and_finish(harness, service["id"], busan.id, branch="main")
    harness.use_settings(entrypoint_environment_map=[f"{busan.id}=gcp"])

    response = await harness.client.post(disasters_path(harness), json=disaster_body())

    assert response.status_code == 201, response.text
    (plan,) = response.json()["data"]["plans"]
    assert plan["targetEnvironmentId"] == str(busan.id)


async def test_environment_in_other_active_disaster_is_not_a_target(tokyo: Migration) -> None:
    seoul_disaster = await declare_disaster(tokyo, epicenter=NEAR_SEOUL)
    assert seoul_disaster["plans"] == []

    disaster = await declare_disaster(tokyo)

    result = _only_plan(disaster)
    assert (result["outcome"], result["reason"]) == ("SKIPPED", "NO_SAFE_TARGET")


async def test_environment_in_below_threshold_disaster_can_be_target(tokyo: Migration) -> None:
    below = await declare_disaster(tokyo, magnitude=6.9, epicenter=NEAR_SEOUL)
    assert below["affectedEnvironmentIds"] == [str(tokyo.gcp.id)]

    disaster = await declare_disaster(tokyo)

    result = _only_plan(disaster)
    assert result["outcome"] == "PENDING_APPROVAL"
    assert result["targetEnvironmentId"] == str(tokyo.gcp.id)


async def test_rejected_service_does_not_block_later_service_after_rollback(
    tokyo: Migration,
) -> None:
    """앞 서비스의 create_plan 이 트랜잭션 안에서 거절돼도(롤백) 뒤 서비스 계획은 만들어진다."""
    harness = tokyo.harness
    await harness.create_service("alpha-api")
    await harness.create_service("beta-api")
    services = (await harness.client.get(harness.services)).json()["data"]["items"]
    # id 순으로 처리하므로 id 가 작은 쪽을 다른 이미지로 만든다.
    first, second = sorted(
        item["id"] for item in services if item["name"] in ("alpha-api", "beta-api")
    )
    await deploy_and_finish(harness, first, tokyo.aws.id, branch="main")
    await deploy_and_finish(harness, first, tokyo.gcp.id, commitSha=OTHER_COMMIT)
    await deploy_and_finish(harness, second, tokyo.aws.id, branch="main")
    await deploy_and_finish(harness, second, tokyo.gcp.id, branch="main")

    disaster = await declare_disaster(tokyo)

    rejected = _plan_for(disaster, first)
    assert (rejected["outcome"], rejected["reason"]) == ("SKIPPED", "TARGET_NOT_READY")
    assert rejected["planId"] is None
    created = _plan_for(disaster, second)
    assert created["outcome"] == "PENDING_APPROVAL"
    assert _plan_for(disaster, tokyo.service_id)["outcome"] == "PENDING_APPROVAL"
    assert [item["serviceId"] for item in disaster["plans"]] == sorted(
        item["serviceId"] for item in disaster["plans"]
    )


async def test_unexpected_failure_for_one_service_keeps_disaster_and_other_services(
    tokyo: Migration, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = await add_standby_service(tokyo, "orders-api")
    original = MigrationPlanService.create_plan

    async def fail_for_shop_api(
        self: MigrationPlanService, organization_id: Any, service_id: Any, *args: Any, **kw: Any
    ) -> Any:
        if str(service_id) == tokyo.service_id:
            raise RuntimeError("boom")
        return await original(self, organization_id, service_id, *args, **kw)

    monkeypatch.setattr(MigrationPlanService, "create_plan", fail_for_shop_api)

    disaster = await declare_disaster(tokyo)

    failed = _plan_for(disaster, tokyo.service_id)
    assert (failed["outcome"], failed["reason"]) == ("SKIPPED", "INTERNAL_ERROR")
    assert _plan_for(disaster, other)["outcome"] == "PENDING_APPROVAL"
    assert await _disaster_count(tokyo.harness) == 1


async def test_get_lists_only_plans_of_disaster_with_summarized_outcome(tokyo: Migration) -> None:
    harness = tokyo.harness
    await add_standby_service(tokyo, "orders-api", standby_commit=OTHER_COMMIT)
    disaster = await declare_disaster(tokyo)
    assert len(disaster["plans"]) == 2

    detail = await harness.client.get(disasters_path(harness, f"/{disaster['id']}"))

    assert detail.status_code == 200, detail.text
    (plan,) = detail.json()["data"]["plans"]
    assert plan["serviceId"] == tokyo.service_id
    assert plan["outcome"] == "PENDING_APPROVAL"
    assert plan["reason"] is None
    assert plan["planStatus"] == "PENDING_APPROVAL"

    rejected = await tokyo.act(plan["planId"], "reject")
    assert rejected.status_code == 200, rejected.text
    after_reject = await harness.client.get(disasters_path(harness, f"/{disaster['id']}"))
    (closed,) = after_reject.json()["data"]["plans"]
    assert (closed["outcome"], closed["reason"]) == ("SKIPPED", "REJECTED")
    assert closed["planStatus"] == "REJECTED"


async def test_get_after_approval_reports_approved_with_real_status(tokyo: Migration) -> None:
    await allow_self_approval(tokyo.harness)
    disaster = await declare_disaster(tokyo)
    await tokyo.harness.run_until_idle()

    detail = await tokyo.harness.client.get(disasters_path(tokyo.harness, f"/{disaster['id']}"))

    (plan,) = detail.json()["data"]["plans"]
    assert plan["outcome"] == "APPROVED"
    assert plan["planStatus"] == "SUCCEEDED"


async def test_list_filters_by_status_newest_first_and_pages(tokyo: Migration) -> None:
    harness = tokyo.harness
    first = await declare_disaster(tokyo, epicenter=MID_PACIFIC)
    harness.clock.advance(1)
    second = await declare_disaster(tokyo, epicenter=MID_PACIFIC)
    ended = await tokyo.author_client.post(disasters_path(harness, f"/{first['id']}/end"))
    assert ended.status_code == 200, ended.text

    everything = (await harness.client.get(disasters_path(harness))).json()["data"]
    active = (
        await harness.client.get(disasters_path(harness), params={"status": "ACTIVE"})
    ).json()["data"]
    finished = (
        await harness.client.get(disasters_path(harness), params={"status": "ENDED"})
    ).json()["data"]
    second_page = (
        await harness.client.get(disasters_path(harness), params={"page": 1, "size": 1})
    ).json()["data"]

    assert [item["id"] for item in everything["items"]] == [second["id"], first["id"]]
    assert everything["total"] == 2
    assert [item["id"] for item in active["items"]] == [second["id"]]
    assert [item["id"] for item in finished["items"]] == [first["id"]]
    assert finished["items"][0]["endedBy"]["login"] == "dev-a"
    assert [item["id"] for item in second_page["items"]] == [first["id"]]
    invalid = await harness.client.get(disasters_path(harness), params={"status": "NOPE"})
    assert invalid.status_code == 422


# --- AC-7: 복귀 차단 ---


async def test_failback_blocked_while_disaster_active_and_allowed_after_end(
    tokyo: Migration,
) -> None:
    harness = tokyo.harness
    await allow_self_approval(harness)
    disaster = await declare_disaster(tokyo)
    plan_id = _only_plan(disaster)["planId"]
    await harness.run_until_idle()
    assert await tokyo.weights() == (0, 100)

    blocked_plan = await tokyo.get_plan(plan_id)
    assert blocked_plan["failback"] == {
        "allowed": False,
        "reason": "DISASTER_ACTIVE",
        "disasterId": disaster["id"],
    }
    blocked = await tokyo.request_plan(targetEnvironmentId=str(tokyo.aws.id))
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["code"] == "DISASTER_ACTIVE"
    assert blocked.json()["details"] == [
        {"field": "disasterId", "reason": "DISASTER_ACTIVE", "value": disaster["id"]}
    ]

    ended = await tokyo.author_client.post(disasters_path(harness, f"/{disaster['id']}/end"))

    assert ended.status_code == 200, ended.text
    assert ended.json()["data"]["status"] == "ENDED"
    assert ended.json()["data"]["endedBy"]["login"] == "dev-a"
    assert ended.json()["data"]["endedAt"] is not None
    assert (await tokyo.get_plan(plan_id))["failback"] == {
        "allowed": True,
        "reason": None,
        "disasterId": None,
    }
    failback = await tokyo.create_plan(targetEnvironmentId=str(tokyo.aws.id))
    assert failback["mode"] == "FAILBACK"
    assert failback["disasterId"] is None


async def test_failback_stays_blocked_while_any_active_disaster_affects_target(
    tokyo: Migration,
) -> None:
    harness = tokyo.harness
    await allow_self_approval(harness)
    first = await declare_disaster(tokyo)
    await harness.run_until_idle()
    second = await declare_disaster(tokyo, magnitude=8.0)
    assert await tokyo.weights() == (0, 100)

    closed = await tokyo.author_client.post(disasters_path(harness, f"/{second['id']}/end"))
    assert closed.status_code == 200, closed.text

    blocked = await tokyo.request_plan(targetEnvironmentId=str(tokyo.aws.id))
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["details"][0]["value"] == first["id"]


async def test_active_disaster_check_runs_after_same_environment_check(tokyo: Migration) -> None:
    pending = _only_plan(await declare_disaster(tokyo))
    cancelled = await tokyo.act(pending["planId"], "cancel", tokyo.author_client)
    assert cancelled.status_code == 200, cancelled.text

    same = await tokyo.request_plan(targetEnvironmentId=str(tokyo.aws.id))

    assert same.status_code == 422, same.text
    assert same.json()["details"][0]["reason"] == "SAME_ENVIRONMENT"


async def test_below_threshold_active_disaster_does_not_block_plan_or_failback(
    tokyo: Migration,
) -> None:
    """기준 미달 재난은 이전을 일으키지 않으므로 그 지역으로 가는 것도 막지 않는다."""
    near_target = await declare_disaster(tokyo, magnitude=6.9, epicenter=NEAR_SEOUL)
    near_source = await declare_disaster(tokyo, magnitude=6.9)
    assert (near_target["status"], near_target["meetsThreshold"]) == ("ACTIVE", False)
    assert near_target["affectedEnvironmentIds"] == [str(tokyo.gcp.id)]
    assert (near_source["status"], near_source["meetsThreshold"]) == ("ACTIVE", False)
    assert near_source["affectedEnvironmentIds"] == [str(tokyo.aws.id)]

    created = await tokyo.request_plan()

    assert created.status_code == 201, created.text
    plan = created.json()["data"]
    assert plan["failback"] == {"allowed": True, "reason": None, "disasterId": None}
    assert (await tokyo.get_plan(plan["id"]))["failback"]["allowed"] is True


async def test_end_twice_returns_409_not_active_and_unknown_returns_404(tokyo: Migration) -> None:
    harness = tokyo.harness
    disaster = await declare_disaster(tokyo, epicenter=MID_PACIFIC)
    path = disasters_path(harness, f"/{disaster['id']}/end")
    assert (await tokyo.author_client.post(path)).status_code == 200

    again = await tokyo.author_client.post(path)
    missing = await tokyo.author_client.post(disasters_path(harness, f"/{uuid.uuid4()}/end"))

    assert again.status_code == 409, again.text
    assert again.json()["code"] == "DISASTER_NOT_ACTIVE"
    assert missing.status_code == 404, missing.text
    assert missing.json()["code"] == "DISASTER_NOT_FOUND"
    assert len(await _audit_rows(harness, AuditAction.DISASTER_ENDED)) == 1


# --- AC-8: 권한 ---


async def test_viewer_and_agent_cannot_declare_or_end_but_can_read(tokyo: Migration) -> None:
    harness = tokyo.harness
    now = harness.clock.now()
    viewer = await create_member(
        harness.session_factory, harness.organization, OrganizationRole.VIEWER, now, "viewer-a"
    )
    _, agent_token = await create_agent_token(
        harness.session_factory, harness.organization, harness.admin.user, now
    )
    disaster = await declare_disaster(tokyo, epicenter=MID_PACIFIC)
    body = disaster_body(epicenter=MID_PACIFIC)
    end_path = disasters_path(harness, f"/{disaster['id']}/end")
    preview_params = {"type": "EARTHQUAKE", "lat": 0, "lng": -150, "magnitude": 7}

    async with (
        make_client(session_token=viewer.session_token) as viewer_client,
        make_client(agent_token=agent_token) as agent_client,
    ):
        for client, code in ((viewer_client, "ROLE_FORBIDDEN"), (agent_client, "AGENT_READ_ONLY")):
            created = await client.post(disasters_path(harness), json=body)
            ended = await client.post(end_path)
            assert (created.status_code, ended.status_code) == (403, 403)
            assert (created.json()["code"], ended.json()["code"]) == (code, code)
            assert (await client.get(disasters_path(harness))).status_code == 200
            assert (
                await client.get(disasters_path(harness, f"/{disaster['id']}"))
            ).status_code == 200
            assert (
                await client.get(disasters_path(harness, "/preview"), params=preview_params)
            ).status_code == 200

    assert await _disaster_count(harness) == 1


async def test_disaster_of_other_organization_is_404_and_lists_are_separate(
    tokyo: Migration,
) -> None:
    harness = tokyo.harness
    other = await create_organization(harness.session_factory, "other-org")
    outsider = await create_member(
        harness.session_factory, other, OrganizationRole.ADMIN, harness.clock.now(), "outsider"
    )
    disaster = await declare_disaster(tokyo)

    async with make_client(session_token=outsider.session_token) as client:
        read = await client.get(org_path(other.id, f"/disasters/{disaster['id']}"))
        end = await client.post(org_path(other.id, f"/disasters/{disaster['id']}/end"))
        listed = await client.get(org_path(other.id, "/disasters"))
        hidden_org = await client.get(disasters_path(harness))

    assert (read.status_code, read.json()["code"]) == (404, "DISASTER_NOT_FOUND")
    assert (end.status_code, end.json()["code"]) == (404, "DISASTER_NOT_FOUND")
    assert listed.json()["data"]["items"] == []
    assert hidden_org.status_code == 404


async def test_declare_in_other_organization_does_not_touch_this_organization(
    tokyo: Migration,
) -> None:
    harness = tokyo.harness
    other = await create_organization(harness.session_factory, "other-org")
    outsider = await create_member(
        harness.session_factory, other, OrganizationRole.ADMIN, harness.clock.now(), "outsider"
    )

    async with make_client(session_token=outsider.session_token) as client:
        response = await client.post(org_path(other.id, "/disasters"), json=disaster_body())

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["affectedEnvironmentIds"] == []
    assert data["plans"] == []
    assert await _plan_count(harness) == 0


# --- AC-9: 감사 ---


async def test_audit_records_declared_and_ended_user_lines_and_disaster_details_on_plans(
    tokyo: Migration,
) -> None:
    harness = tokyo.harness
    await allow_self_approval(harness)

    disaster = await declare_disaster(tokyo)
    await harness.run_until_idle()
    ended = await tokyo.author_client.post(disasters_path(harness, f"/{disaster['id']}/end"))
    assert ended.status_code == 200, ended.text

    (declared,) = await _audit_rows(harness, AuditAction.DISASTER_DECLARED)
    assert declared.actor_type == ActorType.USER
    assert declared.actor_user_id == tokyo.author.user.id
    assert declared.target_type == "disaster"
    assert str(declared.target_id) == disaster["id"]
    assert declared.target_label == "EARTHQUAKE M7.2"
    assert declared.details == {
        "type": "EARTHQUAKE",
        "magnitude": 7.2,
        "radiusKm": 302,
        "meetsThreshold": True,
        "affectedEnvironmentIds": [str(tokyo.aws.id)],
    }
    (end_line,) = await _audit_rows(harness, AuditAction.DISASTER_ENDED)
    assert (end_line.actor_type, end_line.actor_user_id) == (ActorType.USER, tokyo.author.user.id)
    assert end_line.details == {}
    expected = {"disasterId": disaster["id"], "trigger": "DISASTER"}
    (created,) = await _audit_rows(harness, AuditAction.MIGRATION_PLAN_CREATED)
    assert created.details == {
        "sourceEnvironmentId": str(tokyo.aws.id),
        "targetEnvironmentId": str(tokyo.gcp.id),
        **expected,
    }
    (approved,) = await _audit_rows(harness, AuditAction.MIGRATION_PLAN_APPROVED)
    assert approved.details == {"selfApproved": True, **expected}
    (finished,) = await _audit_rows(harness, AuditAction.MIGRATION_FINISHED)
    assert finished.result == AuditResult.SUCCEEDED


async def test_other_developer_approval_line_also_has_disaster_details(tokyo: Migration) -> None:
    disaster = await declare_disaster(tokyo)

    await tokyo.approve(_only_plan(disaster)["planId"])

    (approved,) = await _audit_rows(tokyo.harness, AuditAction.MIGRATION_PLAN_APPROVED)
    assert approved.details == {
        "selfApproved": False,
        "disasterId": disaster["id"],
        "trigger": "DISASTER",
    }


async def test_plain_plan_audit_details_have_no_disaster_fields(tokyo: Migration) -> None:
    plan = await tokyo.create_plan()
    await tokyo.approve(plan["id"])

    (created,) = await _audit_rows(tokyo.harness, AuditAction.MIGRATION_PLAN_CREATED)
    (approved,) = await _audit_rows(tokyo.harness, AuditAction.MIGRATION_PLAN_APPROVED)
    assert "disasterId" not in created.details
    assert approved.details == {"selfApproved": False}


async def test_skipped_services_are_kept_in_second_declared_audit_line(tokyo: Migration) -> None:
    first = _only_plan(await declare_disaster(tokyo))

    second = await declare_disaster(tokyo)

    rows = await _audit_rows(tokyo.harness, AuditAction.DISASTER_DECLARED)
    assert [str(row.target_id) for row in rows[1:]] == [second["id"], second["id"]]
    assert rows[2].details == {
        "skippedPlans": [
            {
                "serviceId": tokyo.service_id,
                "planId": first["planId"],
                "reason": "MIGRATION_PLAN_OPEN",
            }
        ]
    }


# --- AC-10: 보안 게이트 같은 판정 ---


async def test_gate_blocks_emergency_plan_with_same_controls_as_planned_plan(
    tokyo: Migration,
) -> None:
    harness = tokyo.harness
    await allow_self_approval(harness)
    enable_gate(tokyo)
    planned = await tokyo.create_plan()
    # 다른 서비스의 긴급 계획과 비교한다. PLANNED 는 자기 승인도 같은 스위치를 따른다.
    refused = await tokyo.act(planned["id"], "approve", tokyo.author_client)
    assert refused.status_code == 409, refused.text
    planned_controls = {item["field"] for item in refused.json()["details"]}
    other = await add_standby_service(tokyo, "orders-api")

    disaster = await declare_disaster(tokyo)

    # 계획이 열려 있는 shop-api 는 건너뛴다.
    assert _plan_for(disaster, tokyo.service_id)["reason"] == "MIGRATION_PLAN_OPEN"
    result = _plan_for(disaster, other)
    assert result["outcome"] == "PENDING_APPROVAL"
    assert result["reason"] == "SECURITY_GATE_BLOCKED"
    assert result["planStatus"] == MigrationPlanStatus.PENDING_APPROVAL
    assert await _plan_job_count(harness, result["planId"]) == 0
    lines = await tokyo.log_rows(result["planId"])
    assert [line.code for line in lines] == [
        "DISASTER_TRIGGERED",
        "PLAN_CREATED",
        "SECURITY_GATE_BLOCKED",
    ]
    data = lines[-1].data
    assert data is not None
    assert {item["controlId"] for item in data["blocking"]} == planned_controls


async def test_gate_passes_emergency_plan_when_evidence_is_complete(tokyo: Migration) -> None:
    await prepare_passing_gate(tokyo)
    await allow_self_approval(tokyo.harness)

    disaster = await declare_disaster(tokyo)

    result = _only_plan(disaster)
    assert result["outcome"] == "APPROVED"
    codes = await tokyo.log_codes(result["planId"])
    assert codes[:4] == [
        "DISASTER_TRIGGERED",
        "PLAN_CREATED",
        "SECURITY_GATE_PASSED",
        "SELF_APPROVED",
    ]
    await tokyo.harness.run_until_idle()
    assert (await tokyo.get_plan(result["planId"]))["status"] == "SUCCEEDED"
