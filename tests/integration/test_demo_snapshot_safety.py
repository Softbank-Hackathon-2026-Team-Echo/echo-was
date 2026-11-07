"""시연 복귀의 안전 회귀 시험(PR #142 교차 검수 F1~F4).

- F1: 권한 없는 요청의 거부(DENIED) 감사 줄이 최신 스냅샷을 가리지 않는다.
- F2: 복귀는 DB 쓰기 전환(컷오버) Job 을 만들지 않는다. 기존 컷오버 복귀 계획은 취소한다.
- F3: DB 쓰기 담당 확인이 트래픽보다 먼저다. 컷오버가 돌았으면 트래픽을 되돌리지 않는다.
- F4: 같은 조직 복귀는 한 번에 하나(409 DEMO_RESTORE_IN_PROGRESS), 경쟁 결과는 다시 부를 신호다.

가짜 DB 실행기의 시작 기록 수로 「DB Job 을 만들지 않음」을 단언한다(운영 DB·Job 에 닿지 않는다).
"""

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.audit.actions import AuditAction
from app.core.config import DbCutoverExecutorMode
from app.core.exceptions import MigrationPlanOpenError
from app.enums import AuditResult, OrganizationRole
from app.executors.fake import FakeDbCutoverExecutor
from app.models import AuditLog
from app.services.migration_plan_service import MigrationPlanService
from tests.integration.auth_support import (
    create_agent_token,
    create_member,
    make_client,
)
from tests.integration.disaster_support import allow_self_approval
from tests.integration.migration_support import Migration
from tests.integration.test_demo_snapshot import (
    _items,
    _path,
    _plans,
    _restore,
    _snapshot,
    _steps,
)


def _fake_db_cutover(migration: Migration) -> FakeDbCutoverExecutor:
    harness = migration.harness
    harness.use_settings(db_cutover_executor_mode=DbCutoverExecutorMode.FAKE)
    fake = FakeDbCutoverExecutor(harness.clock, 0)
    harness.replace_executors(db_cutover=fake)
    return fake


async def _cutover_to_gcp(migration: Migration) -> dict[str, Any]:
    """스냅샷 뒤 GCP 로 DB 쓰기 전환을 포함한 이전을 끝낸다(가짜 Job 1건)."""
    outward = await migration.create_plan(includeCutover=True)
    await migration.approve(outward["id"])
    await migration.harness.run_until_idle()
    assert await migration.weights() == (0, 100)
    return outward


# --- F1 ---


async def test_denied_snapshot_lines_do_not_hide_latest_snapshot(migration: Migration) -> None:
    harness = migration.harness
    snapshot = await _snapshot(harness)

    # 같은 조직 DEVELOPER(만든 사람 클라이언트)와 읽기 전용 에이전트의 거부.
    denied = await migration.author_client.post(_path(harness, "/snapshot"))
    assert denied.status_code == 403
    _, raw = await create_agent_token(
        harness.session_factory, harness.organization, harness.admin.user, harness.clock.now()
    )
    async with make_client(agent_token=raw) as agent:
        statuses = [
            (await agent.request(method, _path(harness, suffix))).status_code
            for method, suffix in [
                ("POST", "/snapshot"),
                ("GET", "/snapshot"),
                ("POST", "/restore"),
            ]
        ]
    assert statuses == [403, 403, 403]
    async with harness.session_factory() as session:
        rows = list(
            await session.scalars(
                select(AuditLog)
                .where(AuditLog.action == AuditAction.DEMO_SNAPSHOT)
                .order_by(AuditLog.seq)
            )
        )
    # 성공 줄 뒤에 details 가 빈 거부 줄이 쌓였다(이 줄이 예전엔 최신으로 뽑혀 500 이었다).
    assert [(row.result, bool(row.details)) for row in rows] == [
        (AuditResult.SUCCEEDED, True),
        (AuditResult.DENIED, False),
        (AuditResult.DENIED, False),
    ]

    got = await harness.client.get(_path(harness, "/snapshot"))
    assert got.status_code == 200, got.text
    assert got.json()["data"]["id"] == snapshot["id"]
    restored = await _restore(harness)
    assert restored["snapshotId"] == snapshot["id"]
    assert restored["restored"] is True


async def test_only_denied_snapshot_lines_is_404(migration: Migration) -> None:
    harness = migration.harness
    denied = await migration.author_client.post(_path(harness, "/snapshot"))
    assert denied.status_code == 403

    got = await harness.client.get(_path(harness, "/snapshot"))
    restored = await harness.client.post(_path(harness, "/restore"))

    assert (got.status_code, got.json()["code"]) == (404, "DEMO_SNAPSHOT_NOT_FOUND")
    assert (restored.status_code, restored.json()["code"]) == (404, "DEMO_SNAPSHOT_NOT_FOUND")


# --- F2 · F3 ---


async def test_existing_cutover_failback_plan_is_cancelled_and_no_db_job_starts(
    migration: Migration,
) -> None:
    harness = migration.harness
    fake = _fake_db_cutover(migration)
    await _snapshot(harness)
    await _cutover_to_gcp(migration)
    backward = await migration.create_plan(
        targetEnvironmentId=str(migration.aws.id), includeCutover=True
    )
    started_before = len(fake.started)

    first = await _restore(harness)
    await harness.run_until_idle()
    second = await _restore(harness)
    await harness.run_until_idle()

    # 기존 컷오버 복귀 계획은 승인하지 않고 취소했다.
    assert (await migration.get_plan(backward["id"]))["status"] == "CANCELLED"
    (cancel,) = _steps(first)["MIGRATION_PLANS"]["items"]
    assert (cancel["action"], cancel["status"], cancel["detail"]) == (
        "CANCEL_PLAN",
        "DONE",
        "includeDbCutover=true",
    )
    # restore 전체가 DB Job 을 하나도 만들지 않았다(가짜 실행기 시작 기록 수 불변).
    assert len(fake.started) == started_before == 1
    # 컷오버가 돌아 쓰기 담당을 모르므로 트래픽도 되돌리지 않았다(새 복귀 계획 0).
    for result in (first, second):
        assert _items(result, "TRAFFIC") == [
            ("HOLD_FOR_DB_WRITER", "MANUAL_REQUIRED", "DB_WRITER_UNCONFIRMED")
        ]
        assert {"DB_WRITER", "TRAFFIC"} <= set(result["manualRequired"])
        assert (result["restored"], result["callAgain"]) == (False, False)
    assert await migration.weights() == (0, 100)
    assert len(await _plans(harness)) == 2


async def test_pending_cutover_failback_is_replaced_by_plan_without_db_cutover(
    migration: Migration,
) -> None:
    """컷오버 없이 GCP 로 옮긴 뒤 누가 컷오버 든 복귀 계획을 열어 둔 경우: 취소하고 DB 전환 없는
    복귀 계획으로 바꿔 끝까지 돌린다. DB Job 0."""
    harness = migration.harness
    fake = _fake_db_cutover(migration)
    await allow_self_approval(harness)
    await _snapshot(harness)
    await migration.set_weights(0, 100)
    backward = await migration.create_plan(
        targetEnvironmentId=str(migration.aws.id), includeCutover=True
    )

    first = await _restore(harness)

    assert _items(first, "MIGRATION_PLANS") == [("CANCEL_PLAN", "DONE", None)]
    assert _items(first, "DB_WRITER") == [("CHECK_DB_WRITER", "OK", "DB_CUTOVER_NOT_STARTED")]
    assert _items(first, "TRAFFIC") == [("APPROVE_FAILBACK_PLAN", "STARTED", None)]
    plans = await _plans(harness)
    (replacement,) = [plan for plan in plans if str(plan.id) != backward["id"]]
    assert not any(step["kind"] == "DB_CUTOVER" for step in replacement.steps)

    await harness.run_until_idle()
    done = await _restore(harness)

    assert await migration.weights() == (100, 0)
    assert (done["restored"], done["callAgain"]) == (True, False)
    assert fake.started == []


async def test_aborting_approved_cutover_plan_starts_no_db_job(migration: Migration) -> None:
    """승인만 되고 아직 돌지 않은 컷오버 계획은 중단(시작 전 취소)되고 DB Job 은 0 이다."""
    harness = migration.harness
    fake = _fake_db_cutover(migration)
    await _snapshot(harness)
    outward = await migration.create_plan(includeCutover=True)
    await migration.approve(outward["id"])

    first = await _restore(harness)
    await harness.run_until_idle()
    done = await _restore(harness)

    assert _items(first, "MIGRATION_PLANS") == [("ABORT_PLAN", "STARTED", None)]
    assert (await migration.get_plan(outward["id"]))["status"] == "CANCELLED"
    assert _items(done, "DB_WRITER") == [("CHECK_DB_WRITER", "OK", "DB_CUTOVER_NOT_STARTED")]
    assert (done["restored"], done["callAgain"]) == (True, False)
    assert await migration.weights() == (100, 0)
    assert fake.started == []


async def test_completed_cutover_keeps_traffic_until_people_fail_back(
    migration: Migration,
) -> None:
    harness = migration.harness
    fake = _fake_db_cutover(migration)
    await allow_self_approval(harness)
    await _snapshot(harness)
    outward = await _cutover_to_gcp(migration)

    first = await _restore(harness)
    await harness.run_until_idle()
    again = await _restore(harness)

    for result in (first, again):
        assert _items(result, "TRAFFIC") == [
            ("HOLD_FOR_DB_WRITER", "MANUAL_REQUIRED", "DB_WRITER_UNCONFIRMED")
        ]
        (writer,) = _steps(result)["DB_WRITER"]["items"]
        assert (writer["status"], writer["code"], writer["targetId"]) == (
            "MANUAL_REQUIRED",
            "DB_CUTOVER_SINCE_SNAPSHOT",
            outward["id"],
        )
        assert (result["restored"], result["callAgain"]) == (False, False)
    # 트래픽은 GCP 그대로, 복귀가 만든 계획·DB Job 0.
    assert await migration.weights() == (0, 100)
    assert len(await _plans(harness)) == 1
    assert len(fake.started) == 1

    # 사람이 쓰기 담당을 맡은 뒤 DB 전환 없는 복귀를 직접 돌리면 restore 가 나머지를 이어 간다.
    manual = await migration.create_plan(targetEnvironmentId=str(migration.aws.id))
    await migration.approve(manual["id"])
    await harness.run_until_idle()
    done = await _restore(harness)

    assert await migration.weights() == (100, 0)
    assert _items(done, "TRAFFIC") == [("NONE", "OK", None)]
    assert done["restored"] is True
    # DB 쓰기 담당 안내는 계속 남는다(WAS 는 쓰기 담당을 확인하지 못한다).
    assert "DB_WRITER" in done["manualRequired"]
    assert len(fake.started) == 1


# --- F4 ---


async def test_concurrent_restore_is_409_and_first_continues(
    migration: Migration, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = migration.harness
    await allow_self_approval(harness)
    await _snapshot(harness)
    await migration.set_weights(0, 100)
    second = await create_member(
        harness.session_factory,
        harness.organization,
        OrganizationRole.ADMIN,
        harness.clock.now(),
        "second-admin",
    )
    original = MigrationPlanService.create_plan
    entered = asyncio.Event()
    release = asyncio.Event()

    async def gated(self: MigrationPlanService, *args: Any, **kwargs: Any) -> Any:
        entered.set()
        await asyncio.wait_for(release.wait(), 20)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(MigrationPlanService, "create_plan", gated)
    url = _path(harness, "/restore")
    async with make_client(session_token=second.session_token) as other:
        first = asyncio.create_task(harness.client.post(url))
        await asyncio.wait_for(entered.wait(), 20)

        raced = await other.post(url)
        preview = await other.post(url, json={"dryRun": True})
        release.set()
        finished = await first
        monkeypatch.setattr(MigrationPlanService, "create_plan", original)
        after = await other.post(url)

    assert (raced.status_code, raced.json()["code"]) == (409, "DEMO_RESTORE_IN_PROGRESS")
    # dryRun 은 아무것도 바꾸지 않아 잠그지 않는다.
    assert preview.status_code == 200, preview.text
    assert finished.status_code == 200, finished.text
    data = finished.json()["data"]
    assert _items(data, "TRAFFIC") == [("APPROVE_FAILBACK_PLAN", "STARTED", None)]
    assert data["callAgain"] is True
    # 앞 호출이 끝나면 잠금이 풀린다. 진행 중 복귀를 기다리라는 신호(callAgain)를 준다.
    assert after.status_code == 200, after.text
    after_data = after.json()["data"]
    assert _items(after_data, "MIGRATION_PLANS") == [
        ("WAIT_PLAN", "WAITING", "FAILBACK_IN_PROGRESS")
    ]
    assert after_data["callAgain"] is True
    assert len(await _plans(harness)) == 1

    await harness.run_until_idle()
    done = await _restore(harness)
    assert (done["restored"], done["callAgain"]) == (True, False)


async def test_restore_lock_is_released_after_unexpected_error(
    migration: Migration, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = migration.harness
    await allow_self_approval(harness)
    await _snapshot(harness)
    await migration.set_weights(0, 100)
    original = MigrationPlanService.create_plan

    async def broken(self: MigrationPlanService, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(MigrationPlanService, "create_plan", broken)
    failed = await harness.client.post(_path(harness, "/restore"))
    monkeypatch.setattr(MigrationPlanService, "create_plan", original)
    retried = await _restore(harness)

    assert failed.status_code == 500
    assert _items(retried, "TRAFFIC") == [("APPROVE_FAILBACK_PLAN", "STARTED", None)]


async def test_plan_opened_by_someone_else_is_waiting_call_again(
    migration: Migration, monkeypatch: pytest.MonkeyPatch
) -> None:
    """복귀가 읽은 뒤 다른 사람이 이 서비스에 계획을 열면: 실패가 아니라 다시 부를 대기다."""
    harness = migration.harness
    await allow_self_approval(harness)
    await _snapshot(harness)
    await migration.set_weights(0, 100)

    async def opened(self: MigrationPlanService, *args: Any, **kwargs: Any) -> Any:
        raise MigrationPlanOpenError(str(uuid.uuid4()))

    monkeypatch.setattr(MigrationPlanService, "create_plan", opened)
    result = await _restore(harness)

    assert _items(result, "TRAFFIC") == [("CREATE_FAILBACK_PLAN", "WAITING", "MIGRATION_PLAN_OPEN")]
    assert (result["restored"], result["callAgain"]) == (False, True)
