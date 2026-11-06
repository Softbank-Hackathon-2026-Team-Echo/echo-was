"""이전 지문 비교: 배포 없는 연결(현재 배포 없음 + 비율 0)은 지문 connections 에서 빠진다."""

from typing import Any

from app.services.migration_plan_support import (
    FINGERPRINT_CONNECTIONS,
    FINGERPRINT_OPEN_DEPLOY_REQUESTS,
    FINGERPRINT_SERVICE_STATUS,
    fingerprint_changes,
)

SOURCE = ["c-1", "d-1", 100]
STANDBY = ["c-2", "d-2", 0]


def _fingerprint(*connections: list[Any]) -> dict[str, Any]:
    return {
        FINGERPRINT_SERVICE_STATUS: "ACTIVE",
        FINGERPRINT_CONNECTIONS: list(connections),
        FINGERPRINT_OPEN_DEPLOY_REQUESTS: [],
    }


def test_saved_before_rule_with_undeployed_connection_is_not_stale() -> None:
    # 규칙 이전에 저장한 지문(배포 없는 연결 포함)도 같은 규칙으로 걸러 비교한다.
    saved = _fingerprint(SOURCE, STANDBY, ["c-3", None, 0])
    assert fingerprint_changes(saved, _fingerprint(SOURCE, STANDBY)) == []


def test_undeployed_connection_getting_deployment_is_still_a_change() -> None:
    saved = _fingerprint(SOURCE, STANDBY)
    assert fingerprint_changes(saved, _fingerprint(SOURCE, STANDBY, ["c-3", "d-3", 0])) == [
        FINGERPRINT_CONNECTIONS
    ]


def test_weight_or_deployment_change_or_removal_is_still_a_change() -> None:
    saved = _fingerprint(SOURCE, STANDBY)
    for current in (
        _fingerprint(SOURCE, ["c-2", "d-2", 10]),
        _fingerprint(SOURCE, ["c-2", "d-9", 0]),
        _fingerprint(SOURCE),
        # 배포를 잃은 연결도 항목이 사라지므로 달라진다.
        _fingerprint(SOURCE, ["c-2", None, 0]),
        # 비율이 있는데 배포가 없는 이상한 모양은 거르지 않는다.
        _fingerprint(SOURCE, STANDBY, ["c-3", None, 10]),
    ):
        assert fingerprint_changes(saved, current) == [FINGERPRINT_CONNECTIONS], current
