"""DB 쓰기 전환 단계 검증 반영(PR #106 독립 검증 P1 4건·P2 1건) 통합 시험.

원칙: 컷오버 결과(쓰기 담당)를 모르면 가중치를 되돌리지 않는다 — 트래픽과 DB 쓰기 담당이 갈리는 것이
가장 나쁘다.
- P1-1 생성 응답 유실: 같은 이름 Job 조회 → 있으면 기다림, 없고 결과 행도 없을 때만 원복,
  모르면 UNKNOWN
- P1-2 종료 1: 근거 우선순위 결과 행 > 지금 쓰기 담당 > 로그 NO_CHANGE. 도착이 쓰기 담당이면
  로그가 무엇이든 UNKNOWN, 출발이면 원복, 확인 못 했을 때만 NO_CHANGE 로 원복
- P1-3 원본 DB 단절 긴급 이전(EMERGENCY + DB 쓰기 전환): 데이터 확인을 건너뛰고(경고)
  승격 Job 에 닿는다
- P1-4 상태 조회 지속 예외: 흡수해 연속 횟수·시계 상한으로 끝낸다(RUNNING 에 남지 않음)
- P2 승격 동의: 상세·승인 응답 promoteWarning, 승인 본문 acknowledgePromote 없으면 422
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select, update

from app.audit.actions import AuditAction
from app.core.config import DbCutoverExecutorMode
from app.enums import MigrationPlanMode
from app.executors.base import (
    DataSyncLag,
    DataSyncResult,
    DataSyncUnavailable,
    DbCutoverJobAbsent,
    DbCutoverPollResult,
    DbCutoverRequest,
    DbCutoverStartFailed,
    DbCutoverStartResult,
    DeployTarget,
)
from app.executors.fake import FakeDataSyncChecker, FakeDbCutoverExecutor
from app.models import AuditLog, MigrationPlan
from app.services.migration_plan_processor import MAX_CUTOVER_POLL_ERRORS
from app.services.migration_plan_support import PROMOTE_WARNING
from tests.integration.deploy_harness import DeployHarness
from tests.integration.disaster_support import (
    allow_self_approval,
    declare_disaster,
    tokyo_serving_seoul_standby,
)
from tests.integration.migration_support import Migration

UNREACHABLE = "JOB_CREATE_UNREACHABLE"


def _enable(migration: Migration, **settings: Any) -> None:
    migration.harness.use_settings(db_cutover_executor_mode=DbCutoverExecutorMode.FAKE, **settings)


def _install(migration: Migration, fake: FakeDbCutoverExecutor) -> FakeDbCutoverExecutor:
    migration.harness.replace_executors(db_cutover=fake)
    return fake


async def _run(migration: Migration, **body: Any) -> dict[str, Any]:
    plan = await migration.create_plan(**body)
    await migration.approve(plan["id"])
    await migration.harness.run_until_idle()
    return await migration.get_plan(plan["id"])


def _outcome(plan: dict[str, Any]) -> tuple[str, str | None, str | None]:
    return plan["status"], plan["errorCode"], plan["rollbackStatus"]


# --- P1-1: 생성 응답 유실 ---


class _AcceptedButLost(FakeDbCutoverExecutor):
    """창구가 Job 을 만들었지만(가짜 안에 기록) 응답을 잃었다."""

    async def start(self, request: DbCutoverRequest) -> DbCutoverStartResult:
        await super().start(request)
        return DbCutoverStartFailed(UNREACHABLE, retryable=True)


async def test_lost_create_response_reattaches_to_job_by_name(migration: Migration) -> None:
    _enable(migration, migration_db_cutover_timeout_seconds=10)
    fake = _install(migration, _AcceptedButLost(migration.harness.clock, 0))

    final = await _run(migration, includeCutover=True)
    cutover = final["steps"][5]["cutover"]

    assert final["status"] == "SUCCEEDED"
    assert fake.lookups == 1
    assert (cutover["startUncertain"], cutover["startConfirmed"]) == (True, True)
    assert cutover["jobName"] is not None and cutover["startFailure"] is None
    assert await migration.weights() == (0, 100)


async def test_lost_create_response_without_job_or_row_rolls_back(migration: Migration) -> None:
    _enable(migration, migration_db_cutover_timeout_seconds=10)
    fake = _install(
        migration,
        FakeDbCutoverExecutor(
            migration.harness.clock,
            0,
            start_failure=DbCutoverStartFailed(UNREACHABLE, retryable=True),
            job_lookup=DbCutoverJobAbsent(),
        ),
    )

    final = await _run(migration, includeCutover=True)

    assert _outcome(final) == ("ROLLED_BACK", "DB_CUTOVER_FAILED", "SUCCEEDED")
    assert fake.lookups == 1
    assert final["steps"][5]["cutover"]["startUncertain"] is True
    assert await migration.weights() == (100, 0)


async def test_lost_create_response_unconfirmed_keeps_weights(migration: Migration) -> None:
    _enable(migration, migration_db_cutover_timeout_seconds=10)
    fake = _install(
        migration,
        FakeDbCutoverExecutor(
            migration.harness.clock,
            0,
            start_failure=DbCutoverStartFailed(UNREACHABLE, retryable=True),
        ),
    )

    final = await _run(migration, includeCutover=True)
    codes = await migration.log_codes(final["id"])

    assert _outcome(final) == ("FAILED", "DB_CUTOVER_UNKNOWN", "SKIPPED")
    assert final["steps"][5]["cutover"]["reason"] == "START_UNCONFIRMED"
    assert fake.lookups == 1
    assert await migration.weights() == (0, 100)
    assert "DB_CUTOVER_MANUAL_REQUIRED" in codes and "ROLLBACK_STARTED" not in codes


async def test_unsent_create_request_rolls_back_without_lookup(migration: Migration) -> None:
    _enable(migration, migration_db_cutover_timeout_seconds=10)
    fake = _install(
        migration,
        FakeDbCutoverExecutor(
            migration.harness.clock,
            0,
            start_failure=DbCutoverStartFailed(UNREACHABLE, retryable=True, not_sent=True),
        ),
    )

    final = await _run(migration, includeCutover=True)

    assert _outcome(final) == ("ROLLED_BACK", "DB_CUTOVER_FAILED", "SUCCEEDED")
    assert fake.lookups == 0 and len(fake.started) > 1
    assert await migration.weights() == (100, 0)


# --- P1-2: 종료 1 은 쓰기 담당을 확인한 뒤에만 원복 ---


class _SyncThenWriter:
    """데이터 확인(3단계)은 통과시키고, 그 뒤 쓰기 담당 확인에는 later 를 돌려준다."""

    def __init__(self, later: DataSyncResult) -> None:
        self.later = later
        self.calls = 0

    @property
    def simulated(self) -> bool:
        return True

    async def replication_lag(self, source: DeployTarget, target: DeployTarget) -> DataSyncResult:
        self.calls += 1
        return DataSyncLag(seconds=0.1) if self.calls == 1 else self.later


@pytest.mark.parametrize(
    ("later", "reason"),
    [
        (DataSyncLag(seconds=0.0, reason="TARGET_IS_WRITER"), "TARGET_IS_WRITER"),
        (DataSyncUnavailable(), "WRITER_UNCONFIRMED"),
    ],
)
async def test_exit_one_without_source_writer_evidence_keeps_weights(
    migration: Migration, later: DataSyncResult, reason: str
) -> None:
    _enable(migration)
    _install(migration, FakeDbCutoverExecutor(migration.harness.clock, 0, exit_code=1))
    checker = _SyncThenWriter(later)
    migration.harness.replace_executors(data_sync=checker)

    final = await _run(migration, includeCutover=True)
    cutover = final["steps"][5]["cutover"]

    assert _outcome(final) == ("FAILED", "DB_CUTOVER_UNKNOWN", "SKIPPED")
    assert (cutover["exitCode"], cutover["reason"]) == (1, reason)
    assert checker.calls == 2
    assert await migration.weights() == (0, 100)


async def test_exit_one_with_no_change_marker_rolls_back(migration: Migration) -> None:
    _enable(migration)
    _install(
        migration,
        FakeDbCutoverExecutor(migration.harness.clock, 0, exit_code=1, reason="NO_CHANGE"),
    )
    migration.harness.replace_executors(data_sync=_SyncThenWriter(DataSyncUnavailable()))

    final = await _run(migration, includeCutover=True)

    assert _outcome(final) == ("ROLLED_BACK", "DB_CUTOVER_FAILED", "SUCCEEDED")
    assert final["steps"][5]["cutover"]["writer"] == "UNKNOWN"
    assert await migration.weights() == (100, 0)


async def test_target_writer_overrides_stale_no_change_marker(migration: Migration) -> None:
    """근거 우선순위(검증 2회차 P1): 쓰기 담당 확인 > 로그 표식.

    로그가 NO_CHANGE 여도 지금 도착이 쓰기 담당이면 원복하지 않고 사람 확인으로 끝낸다.
    """
    _enable(migration)
    _install(
        migration,
        FakeDbCutoverExecutor(migration.harness.clock, 0, exit_code=1, reason="NO_CHANGE"),
    )
    checker = _SyncThenWriter(DataSyncLag(seconds=0.0, reason="TARGET_IS_WRITER"))
    migration.harness.replace_executors(data_sync=checker)

    final = await _run(migration, includeCutover=True)
    cutover = final["steps"][5]["cutover"]

    assert _outcome(final) == ("FAILED", "DB_CUTOVER_UNKNOWN", "SKIPPED")
    assert (cutover["writer"], cutover["reason"]) == ("TARGET", "TARGET_IS_WRITER")
    assert checker.calls == 2
    assert await migration.weights() == (0, 100)
    assert not [k for k in migration.traffic_keys() if ":rollback:" in k]


async def test_exit_one_with_source_writer_rolls_back(migration: Migration) -> None:
    _enable(migration)
    _install(migration, FakeDbCutoverExecutor(migration.harness.clock, 0, exit_code=1))

    final = await _run(migration, includeCutover=True)

    assert _outcome(final) == ("ROLLED_BACK", "DB_CUTOVER_FAILED", "SUCCEEDED")
    assert final["steps"][5]["cutover"]["writer"] == "SOURCE"


# --- P1-3: 원본 DB 단절 긴급 이전 ---


async def _make_emergency(migration: Migration, plan_id: str, *, stamp: bool) -> None:
    async with migration.harness.session_factory() as session, session.begin():
        plan = await session.get_one(MigrationPlan, uuid.UUID(plan_id))
        values: dict[str, Any] = {"mode": MigrationPlanMode.EMERGENCY}
        if stamp:
            values["steps"] = [
                {**step, "allowPromote": True} if step["kind"] == "DB_CUTOVER" else step
                for step in plan.steps
            ]
        await session.execute(
            update(MigrationPlan).where(MigrationPlan.id == plan.id).values(**values)
        )


async def test_emergency_with_cutover_skips_unreachable_source_and_promotes(
    migration: Migration,
) -> None:
    _enable(migration)
    fake = _install(migration, FakeDbCutoverExecutor(migration.harness.clock, 0))
    migration.harness.replace_executors(data_sync=FakeDataSyncChecker(unavailable=True))
    plan = await migration.create_plan(includeCutover=True)
    await _make_emergency(migration, plan["id"], stamp=True)

    response = await migration.approver_client.post(
        migration.plan_path(plan["id"], "/approve"), json={"acknowledgePromote": True}
    )
    assert response.status_code == 202, response.text
    await migration.harness.run_until_idle()
    final = await migration.get_plan(plan["id"])
    rows = await migration.log_rows(plan["id"])
    skipped = [row for row in rows if row.code == "DATA_SYNC_SKIPPED"]

    assert final["status"] == "SUCCEEDED"
    assert [(item.allow_promote) for item in fake.started] == [True]
    assert len(skipped) == 1 and skipped[0].data == {"reason": "EMERGENCY_SOURCE_UNREACHABLE"}
    assert skipped[0].level == "WARN"


async def test_emergency_without_cutover_still_fails_on_unreachable_source(
    migration: Migration,
) -> None:
    _enable(migration)
    fake = _install(migration, FakeDbCutoverExecutor(migration.harness.clock, 0))
    migration.harness.replace_executors(data_sync=FakeDataSyncChecker(unavailable=True))
    plan = await migration.create_plan()
    await _make_emergency(migration, plan["id"], stamp=False)

    await migration.approve(plan["id"])
    await migration.harness.run_until_idle()
    final = await migration.get_plan(plan["id"])

    assert (final["status"], final["errorCode"]) == ("ROLLED_BACK", "METRICS_UNAVAILABLE")
    assert fake.started == []


# --- P1-4: 상태 조회 지속 예외 ---


class _PollRaises(FakeDbCutoverExecutor):
    def __init__(self, *args: Any, failures: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.failures = failures

    failures: int

    async def poll(self, job_name: str) -> DbCutoverPollResult:
        if self.failures:
            self.failures -= 1
            raise FileNotFoundError("주입한 SA 토큰 읽기 실패")
        return await super().poll(job_name)


async def test_persistent_poll_errors_end_unknown_without_rollback(migration: Migration) -> None:
    _enable(migration, migration_db_cutover_timeout_seconds=600)
    # 결과 행이 없다(행이 있으면 전환이 끝난 것이라 행으로 판정한다).
    fake = _install(migration, _PollRaises(migration.harness.clock, 0, failures=1000, no_run=True))

    final = await _run(migration, includeCutover=True)
    cutover = final["steps"][5]["cutover"]

    assert _outcome(final) == ("FAILED", "DB_CUTOVER_UNKNOWN", "SKIPPED")
    assert (cutover["reason"], cutover["pollErrors"]) == ("POLL_ERRORS", MAX_CUTOVER_POLL_ERRORS)
    assert fake.failures == 1000 - MAX_CUTOVER_POLL_ERRORS
    assert await migration.weights() == (0, 100)
    job = await migration.plan_job(final["id"])
    assert job is not None and job.lease_losses == 0


async def test_transient_poll_errors_recover(migration: Migration) -> None:
    _enable(migration)
    _install(migration, _PollRaises(migration.harness.clock, 0, failures=2))

    final = await _run(migration, includeCutover=True)

    assert final["status"] == "SUCCEEDED"
    assert final["steps"][5]["cutover"]["pollErrors"] == 0


# --- P2: 승격 동의 ---


@pytest.fixture
async def tokyo(harness: DeployHarness) -> AsyncIterator[Migration]:
    setup = await tokyo_serving_seoul_standby(harness)
    try:
        yield setup
    finally:
        await setup.author_client.aclose()
        await setup.approver_client.aclose()


def _enable_on_disaster(tokyo: Migration) -> FakeDbCutoverExecutor:
    _enable(tokyo, migration_db_cutover_on_disaster=True)
    return _install(tokyo, FakeDbCutoverExecutor(tokyo.harness.clock, 0))


async def test_emergency_cutover_plan_needs_promote_acknowledgement(tokyo: Migration) -> None:
    fake = _enable_on_disaster(tokyo)
    disaster = await declare_disaster(tokyo)
    [result] = disaster["plans"]
    plan_id = result["planId"]
    detail = await tokyo.get_plan(plan_id)
    assert (detail["mode"], detail["status"]) == ("EMERGENCY", "PENDING_APPROVAL")
    assert detail["steps"][5]["kind"] == "DB_CUTOVER"
    assert detail["promoteWarning"] == PROMOTE_WARNING

    missing = await tokyo.act(plan_id, "approve")
    refused = await tokyo.approver_client.post(
        tokyo.plan_path(plan_id, "/approve"), json={"acknowledgePromote": False}
    )

    assert (missing.status_code, missing.json()["code"]) == (422, "PROMOTE_ACK_REQUIRED")
    assert refused.status_code == 422
    assert (await tokyo.get_plan(plan_id))["status"] == "PENDING_APPROVAL"
    assert await tokyo.plan_job(plan_id) is None

    approved = await tokyo.approver_client.post(
        tokyo.plan_path(plan_id, "/approve"), json={"acknowledgePromote": True}
    )
    assert approved.status_code == 202, approved.text
    assert approved.json()["data"]["promoteWarning"] == PROMOTE_WARNING
    async with tokyo.harness.session_factory() as session:
        audit = await session.scalar(
            select(AuditLog).where(
                AuditLog.action == AuditAction.MIGRATION_PLAN_APPROVED,
                AuditLog.target_id == uuid.UUID(plan_id),
            )
        )
    assert audit is not None and (audit.details or {}).get("acknowledgePromote") is True

    await tokyo.harness.run_until_idle()
    assert (await tokyo.get_plan(plan_id))["status"] == "SUCCEEDED"
    assert [item.allow_promote for item in fake.started] == [True]


async def test_disaster_auto_approval_carries_operator_consent(tokyo: Migration) -> None:
    fake = _enable_on_disaster(tokyo)
    await allow_self_approval(tokyo.harness)

    disaster = await declare_disaster(tokyo)
    [result] = disaster["plans"]

    assert result["outcome"] == "APPROVED"
    await tokyo.harness.run_until_idle()
    assert (await tokyo.get_plan(result["planId"]))["status"] == "SUCCEEDED"
    assert [item.allow_promote for item in fake.started] == [True]


async def test_normal_plans_need_no_acknowledgement(migration: Migration) -> None:
    _enable(migration)
    _install(migration, FakeDbCutoverExecutor(migration.harness.clock, 0))
    plan = await migration.create_plan(includeCutover=True)

    assert plan["promoteWarning"] is None
    approved = await migration.approve(plan["id"])
    assert approved["promoteWarning"] is None


async def test_emergency_mode_without_consent_stamp_never_allows_promote(
    migration: Migration,
) -> None:
    """승격 허용은 만들 때 단 표시(승인 동의를 받은 계획)가 있어야만이다."""
    _enable(migration)
    fake = _install(migration, FakeDbCutoverExecutor(migration.harness.clock, 0))
    plan = await migration.create_plan(includeCutover=True)
    await _make_emergency(migration, plan["id"], stamp=False)

    await migration.approve(plan["id"])
    await migration.harness.run_until_idle()

    assert [item.allow_promote for item in fake.started] == [False]
