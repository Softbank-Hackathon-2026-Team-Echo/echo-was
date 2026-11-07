"""승격한 DB 쓰기 전환 뒤 요청 기록 재생(API 서버 21, `POST /migration-plans/{id}/replay`).

DB 쓰기 전환 단계가 승격(결과 행 mode = PROMOTE)으로 통과한 계획만 재생한다. 결과는 단계 결과
cutover.replay 에 덮어쓰고, 실행 로그 한 줄과 감사 줄을 남긴다. 기록을 읽지 못하면 아무것도 적지
않는다.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import Response
from sqlalchemy import select

from app.audit.actions import AuditAction
from app.core.config import DbCutoverExecutorMode
from app.enums import CutoverKind
from app.executors.base import CutoverRun, JournalReplayed, JournalReplayUnavailable
from app.executors.fake import FakeDbCutoverExecutor, FakeJournalReplayer
from app.models import AuditLog, Service, ServiceEnvironment
from tests.integration.migration_support import Migration

PAUSED_AT = datetime(2026, 11, 6, 8, 20, tzinfo=UTC)
RPO_MS = 1500
MARGIN_SECONDS = 30
DONE = JournalReplayed(sent=3, created=2, existed=1, failed=0, skipped=1, duration_ms=530)


def _run_row(mode: str) -> CutoverRun:
    return CutoverRun(
        kind=CutoverKind.CUTOVER,
        paused_at=PAUSED_AT,
        resumed_at=PAUSED_AT + timedelta(seconds=26),
        write_pause_ms=26000,
        drain_ms=None,
        catchup_ms=None,
        switch_ms=None,
        rows_total=181,
        missing_rows=0,
        duplicate_rows=0,
        mode=mode,
        rpo_estimate_ms=RPO_MS,
    )


async def _cutover_plan(migration: Migration, mode: str = "PROMOTE") -> dict[str, Any]:
    """DB 쓰기 전환까지 끝난 계획. 결과 행 mode 로 승격·정상 컷오버를 가른다."""
    migration.harness.use_settings(db_cutover_executor_mode=DbCutoverExecutorMode.FAKE)
    migration.harness.replace_executors(
        db_cutover=FakeDbCutoverExecutor(migration.harness.clock, 0, run=_run_row(mode))
    )
    plan = await migration.create_plan(includeCutover=True)
    await migration.approve(plan["id"])
    await migration.harness.run_until_idle()
    final = await migration.get_plan(plan["id"])
    assert final["status"] == "SUCCEEDED"
    return final


def _use_replayer(migration: Migration, result: Any = DONE) -> FakeJournalReplayer:
    fake = FakeJournalReplayer(result)
    migration.harness.replace_executors(journal_replayer=fake)
    return fake


async def _replay(migration: Migration, plan_id: str) -> Response:
    return await migration.act(plan_id, "replay")


async def _audit(migration: Migration, plan_id: str) -> list[AuditLog]:
    async with migration.harness.session_factory() as session:
        rows = await session.scalars(
            select(AuditLog)
            .where(
                AuditLog.target_id == uuid.UUID(plan_id),
                AuditLog.action == AuditAction.MIGRATION_JOURNAL_REPLAYED,
            )
            .order_by(AuditLog.seq)
        )
        return list(rows.all())


async def _addresses(migration: Migration) -> tuple[str, str]:
    """(서비스 대표 주소 host, 도착 gcp 연결의 환경별 주소 host)."""
    async with migration.harness.session_factory() as session:
        service = await session.get_one(Service, uuid.UUID(migration.service_id))
        target = await session.scalar(
            select(ServiceEnvironment).where(
                ServiceEnvironment.service_id == service.id,
                ServiceEnvironment.environment_id == migration.gcp.id,
                ServiceEnvironment.is_deleted.is_(False),
            )
        )
        assert target is not None
        return service.public_host, target.public_host


# --- 재생 ---


async def test_replay_after_promote_sends_service_records_to_target_and_records_result(
    migration: Migration,
) -> None:
    plan = await _cutover_plan(migration)
    fake = _use_replayer(migration)
    assert plan["steps"][5]["cutover"]["replayAvailable"] is True
    assert plan["steps"][5]["cutover"]["replay"] is None

    response = await _replay(migration, plan["id"])

    assert response.status_code == 200, response.text
    replay = response.json()["data"]["steps"][5]["cutover"]["replay"]
    assert {key: replay[key] for key in ("status", "sent", "created", "existed", "failed")} == {
        "status": "DONE",
        "sent": 3,
        "created": 2,
        "existed": 1,
        "failed": 0,
    }
    assert (replay["skipped"], replay["durationMs"], replay["attempts"]) == (1, 530, 1)
    since = PAUSED_AT - timedelta(milliseconds=RPO_MS, seconds=MARGIN_SECONDS)
    assert datetime.fromisoformat(replay["since"]) == since
    service_host, target_host = await _addresses(migration)
    assert [(item.host, item.since, item.target_url) for item in fake.requests] == [
        (service_host, since, f"https://{target_host}")
    ]
    # 상태는 그대로이고 상세 GET 에도 같은 값이 남는다.
    detail = await migration.get_plan(plan["id"])
    assert detail["status"] == "SUCCEEDED"
    assert detail["steps"][5]["cutover"]["replay"] == replay
    rows = await migration.log_rows(plan["id"])
    assert (rows[-1].code, rows[-1].step, rows[-1].level) == ("JOURNAL_REPLAYED", 5, "INFO")
    assert rows[-1].data is not None and rows[-1].data["created"] == 2
    audit = await _audit(migration, plan["id"])
    assert [(row.result, row.details["created"], row.details["attempts"]) for row in audit] == [
        ("SUCCEEDED", 2, 1)
    ]


async def test_replay_again_counts_attempts_and_logs_partial(migration: Migration) -> None:
    plan = await _cutover_plan(migration)
    fake = _use_replayer(migration)
    assert (await _replay(migration, plan["id"])).status_code == 200
    fake.result = JournalReplayed(sent=3, created=0, existed=2, failed=1, skipped=0, duration_ms=9)

    response = await _replay(migration, plan["id"])

    replay = response.json()["data"]["steps"][5]["cutover"]["replay"]
    assert (replay["status"], replay["failed"], replay["attempts"]) == ("PARTIAL", 1, 2)
    codes = await migration.log_codes(plan["id"])
    assert codes[-2:] == ["JOURNAL_REPLAYED", "JOURNAL_REPLAY_PARTIAL"]
    assert len(await _audit(migration, plan["id"])) == 2


# --- 거절 ---


async def test_replay_disabled_is_409(migration: Migration) -> None:
    plan = await _cutover_plan(migration)

    response = await _replay(migration, plan["id"])

    assert (response.status_code, response.json()["code"]) == (409, "JOURNAL_REPLAY_DISABLED")


async def test_replay_not_available_for_normal_cutover(migration: Migration) -> None:
    plan = await _cutover_plan(migration, mode="NORMAL")
    fake = _use_replayer(migration)

    response = await _replay(migration, plan["id"])

    assert plan["steps"][5]["cutover"]["replayAvailable"] is False
    assert (response.status_code, response.json()["code"]) == (409, "REPLAY_NOT_AVAILABLE")
    assert response.json()["details"] == [{"field": "planId", "reason": "NOT_PROMOTED"}]
    assert fake.requests == []


async def test_replay_not_available_without_db_cutover(migration: Migration) -> None:
    plan = await migration.create_plan()
    await migration.approve(plan["id"])
    await migration.harness.run_until_idle()
    _use_replayer(migration)

    response = await _replay(migration, plan["id"])

    assert (response.status_code, response.json()["details"]) == (
        409,
        [{"field": "planId", "reason": "NO_DB_CUTOVER"}],
    )


async def test_replay_not_available_after_a_later_plan(migration: Migration) -> None:
    plan = await _cutover_plan(migration)
    fake = _use_replayer(migration)
    await migration.create_plan(targetEnvironmentId=str(migration.aws.id))

    response = await _replay(migration, plan["id"])

    assert (response.status_code, response.json()["details"]) == (
        409,
        [{"field": "planId", "reason": "SUPERSEDED"}],
    )
    assert fake.requests == []


async def test_replay_journal_unavailable_is_502_and_records_nothing(migration: Migration) -> None:
    plan = await _cutover_plan(migration)
    _use_replayer(migration, JournalReplayUnavailable("JOURNAL_UNREACHABLE"))
    before = await migration.log_codes(plan["id"])

    response = await _replay(migration, plan["id"])

    assert (response.status_code, response.json()["code"]) == (502, "JOURNAL_UNAVAILABLE")
    detail = await migration.get_plan(plan["id"])
    assert detail["steps"][5]["cutover"]["replay"] is None
    assert await migration.log_codes(plan["id"]) == before
    assert await _audit(migration, plan["id"]) == []


async def test_replay_unknown_plan_is_404(migration: Migration) -> None:
    _use_replayer(migration)

    response = await _replay(migration, str(uuid.uuid4()))

    assert response.status_code == 404
