"""시연 스냅샷·복귀의 순수 규칙.

단계 상태 요약·restored·callAgain·수동 필요, JSON 왕복, 유지 비교.
"""

import uuid
from datetime import UTC, datetime

import pytest

from app.services.demo_snapshot_service import (
    UNCHANGED,
    DemoAction,
    DemoItemResult,
    DemoSnapshotState,
    DemoStep,
    DemoStepResult,
    DemoStepStatus,
    SnapshotConnection,
    SnapshotEnvironment,
    SnapshotSecurityException,
    SnapshotService,
    changed_anything,
    compare_kept_settings,
    db_cutover_may_have_run,
    has_db_cutover_step,
    is_restored,
    manual_required_steps,
    needs_call_again,
    state_from_json,
    state_to_json,
    summarize_status,
)

S = DemoStepStatus
ENV_A = uuid.UUID(int=1)
ENV_B = uuid.UUID(int=2)


def _step(step: DemoStep, *statuses: DemoStepStatus) -> DemoStepResult:
    return DemoStepResult(step, [DemoItemResult(DemoAction.NONE, status) for status in statuses])


def _state(**changes: object) -> DemoSnapshotState:
    values: dict[str, object] = {
        "services": [
            SnapshotService(
                service_id=uuid.UUID(int=10),
                name="shop-api",
                serving_environment_id=ENV_A,
                connections=[
                    SnapshotConnection(
                        service_environment_id=uuid.UUID(int=20),
                        environment_id=ENV_A,
                        environment_name="aws-tokyo",
                        environment_kind="aws",
                        traffic_weight=100,
                        current_deployment_id=uuid.UUID(int=30),
                        deploy_request_id=uuid.UUID(int=40),
                        commit_sha="a" * 40,
                        image_digest="sha256:" + "b" * 64,
                    ),
                    SnapshotConnection(
                        service_environment_id=uuid.UUID(int=21),
                        environment_id=ENV_B,
                        environment_name="gcp-seoul",
                        environment_kind="gcp",
                        traffic_weight=0,
                        current_deployment_id=None,
                        deploy_request_id=None,
                        commit_sha=None,
                        image_digest=None,
                    ),
                ],
            )
        ],
        "environments": [
            SnapshotEnvironment(ENV_A, "aws-tokyo", "aws", "CONNECTED"),
            SnapshotEnvironment(ENV_B, "gcp-seoul", "gcp", "CONNECTED"),
        ],
        "security_exceptions": [
            SnapshotSecurityException(
                exception_id=uuid.UUID(int=50),
                control_id="C-TLS-1",
                service_id=uuid.UUID(int=10),
                environment_id=None,
                approved=True,
                revoked=False,
                expires_at=datetime(2026, 11, 9, 3, 0, tzinfo=UTC),
            )
        ],
        "allow_self_approval": True,
    }
    values.update(changes)
    return DemoSnapshotState(**values)  # type: ignore[arg-type]


def test_step_status_is_most_urgent_item_and_empty_uses_default() -> None:
    assert summarize_status([]) is S.OK
    assert summarize_status([], S.KEPT) is S.KEPT
    assert summarize_status([S.OK, S.DONE]) is S.DONE
    assert summarize_status([S.DONE, S.STARTED]) is S.STARTED
    assert summarize_status([S.STARTED, S.WAITING]) is S.WAITING
    assert summarize_status([S.WAITING, S.MANUAL_REQUIRED]) is S.MANUAL_REQUIRED
    assert summarize_status([S.MANUAL_REQUIRED, S.FAILED, S.OK]) is S.FAILED
    assert summarize_status([S.OK, S.WOULD_DO]) is S.WOULD_DO


def test_restored_only_when_restoring_steps_are_ok_or_done() -> None:
    settled = [
        _step(DemoStep.MIGRATION_PLANS, S.DONE),
        _step(DemoStep.DISASTERS),
        _step(DemoStep.TRAFFIC, S.OK),
        # 수동 필요·유지 단계는 restored 를 막지 않는다.
        _step(DemoStep.DEMO_APP_DATA, S.MANUAL_REQUIRED),
        _step(DemoStep.KEPT_SETTINGS, S.KEPT),
    ]
    assert is_restored(settled) is True
    assert needs_call_again(settled) is False
    assert manual_required_steps(settled) == [DemoStep.DEMO_APP_DATA]

    for status in (S.STARTED, S.WAITING, S.FAILED, S.MANUAL_REQUIRED, S.WOULD_DO):
        steps = [*settled, _step(DemoStep.DEPLOYMENTS, S.OK, status)]
        assert is_restored(steps) is False, status


def test_call_again_when_any_step_started_or_waiting() -> None:
    assert needs_call_again([_step(DemoStep.TRAFFIC, S.STARTED)]) is True
    assert needs_call_again([_step(DemoStep.DEPLOYMENTS, S.WAITING)]) is True
    assert needs_call_again([_step(DemoStep.TRAFFIC, S.MANUAL_REQUIRED)]) is False
    assert needs_call_again([_step(DemoStep.TRAFFIC, S.FAILED)]) is False


def test_changed_anything_counts_done_and_started_items_only() -> None:
    assert changed_anything([_step(DemoStep.MIGRATION_PLANS, S.DONE)]) is True
    assert changed_anything([_step(DemoStep.TRAFFIC, S.STARTED)]) is True
    untouched = [
        _step(DemoStep.MIGRATION_PLANS, S.OK, S.WAITING, S.WOULD_DO, S.FAILED),
        _step(DemoStep.DEMO_APP_DATA, S.MANUAL_REQUIRED),
        _step(DemoStep.KEPT_SETTINGS, S.KEPT),
    ]
    assert changed_anything(untouched) is False


def test_state_json_round_trip_keeps_every_field() -> None:
    state = _state()

    data = state_to_json(state)

    assert data["schemaVersion"] == 1
    assert data["organization"] == {"allowSelfApproval": True}
    assert state_from_json(data) == state


def test_kept_settings_unchanged_is_single_kept_line() -> None:
    items = compare_kept_settings(_state(), _state())

    assert [(item.status, item.code) for item in items] == [(S.KEPT, UNCHANGED)]


def test_kept_settings_lists_every_changed_item_without_restoring() -> None:
    before = _state()
    added = SnapshotEnvironment(uuid.UUID(int=3), "onprem-osaka", "onprem", "CONNECTED")
    after = _state(
        environments=[
            SnapshotEnvironment(ENV_A, "aws-tokyo", "aws", "FAILED"),
            added,
        ],
        security_exceptions=[
            SnapshotSecurityException(
                exception_id=uuid.UUID(int=50),
                control_id="C-TLS-1",
                service_id=uuid.UUID(int=10),
                environment_id=None,
                approved=True,
                revoked=True,
                expires_at=datetime(2026, 11, 9, 3, 0, tzinfo=UTC),
            )
        ],
        allow_self_approval=False,
    )

    items = compare_kept_settings(before, after)

    assert {(item.target_type, item.label, item.code) for item in items} == {
        ("environment", "aws-tokyo", "ENVIRONMENT_CONNECTION_STATUS_CHANGED"),
        ("environment", "gcp-seoul", "ENVIRONMENT_REMOVED"),
        ("environment", "onprem-osaka", "ENVIRONMENT_ADDED"),
        ("security_exception", "C-TLS-1", "SECURITY_EXCEPTION_CHANGED"),
        ("organization", None, "ALLOW_SELF_APPROVAL_CHANGED"),
    }
    assert all(item.status is S.KEPT for item in items)


def test_db_cutover_step_detection() -> None:
    assert has_db_cutover_step([{"kind": "SHIFT"}, {"kind": "DB_CUTOVER"}]) is True
    assert has_db_cutover_step([{"kind": "PREFLIGHT"}, {"kind": "SHIFT"}]) is False
    assert has_db_cutover_step([]) is False


_CUTOVER_STEPS = [{"kind": "PREFLIGHT"}, {"kind": "DB_CUTOVER"}, {"kind": "SHIFT"}]


def _results(cutover: str) -> list[dict[str, str]]:
    return [{"status": "PASSED"}, {"status": cutover}, {"status": "PENDING"}]


@pytest.mark.parametrize(
    ("status", "cutover_result", "may_have_run"),
    [
        # 승인 전에는 Job 이 없다.
        ("PENDING_APPROVAL", "PENDING", False),
        # 열린 계획은 곧 돌거나 도는 중이라 모른다.
        ("APPROVED", "PENDING", True),
        ("RUNNING", "RUNNING", True),
        ("ROLLING_BACK", "FAILED", True),
        # 끝난 계획: 그 단계가 돌지 않았을 때만 쓰기 담당이 그대로다.
        ("CANCELLED", "SKIPPED", False),
        ("EXPIRED", "SKIPPED", False),
        ("ROLLED_BACK", "PENDING", False),
        ("SUCCEEDED", "PASSED", True),
        ("ROLLED_BACK", "FAILED", True),
        ("FAILED", "RUNNING", True),
    ],
)
def test_db_cutover_may_have_run(status: str, cutover_result: str, may_have_run: bool) -> None:
    assert db_cutover_may_have_run(status, _CUTOVER_STEPS, _results(cutover_result)) is (
        may_have_run
    )


def test_db_cutover_may_have_run_edges() -> None:
    # DB 쓰기 전환 단계가 없으면 늘 False.
    assert (
        db_cutover_may_have_run("SUCCEEDED", [{"kind": "SHIFT"}], [{"status": "PASSED"}]) is False
    )
    # 결과 칸이 모자라면(읽을 수 없음) 모른다.
    assert db_cutover_may_have_run("FAILED", _CUTOVER_STEPS, [{"status": "PASSED"}]) is True
