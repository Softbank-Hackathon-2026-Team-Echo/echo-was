"""기록 함수 단위 시험: 칸 길이 자르기(작업을 실패시키지 않는다)·행위자 값·요청 값."""

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.actions import WRITE_ROUTE_ACTIONS, AuditAction, AuditComponent, AuditTargetType
from app.audit.context import RequestContext, bind_request_scope, current_request_context
from app.audit.recorder import AuditActor, AuditRecorder, AuditTarget
from app.core.clock import Clock
from app.enums import ActorType, AuditResult
from app.models import AuditLog
from app.models.audit_log import (
    AUDIT_ACTION_MAX_LENGTH,
    AUDIT_COMPONENT_MAX_LENGTH,
    AUDIT_REASON_MAX_LENGTH,
    AUDIT_ROUTE_MAX_LENGTH,
    AUDIT_TARGET_LABEL_MAX_LENGTH,
    REQUEST_ID_MAX_LENGTH,
)

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


class _Clock(Clock):
    def now(self) -> datetime:
        return NOW


class _Session:
    """add 만 받는 세션(기록 함수는 add 만 한다)."""

    def __init__(self) -> None:
        self.added: list[AuditLog] = []

    def add(self, row: AuditLog) -> None:
        self.added.append(row)


def _recorder() -> tuple[AuditRecorder, _Session]:
    session = _Session()
    return AuditRecorder(cast(AsyncSession, cast(Any, session)), _Clock()), session


def test_record_truncates_every_string_to_column_length() -> None:
    recorder, session = _recorder()

    row = recorder.record(
        organization_id=uuid.uuid4(),
        actor=AuditActor.system("c" * 500),
        action="A" * 500,
        target=AuditTarget(AuditTargetType.SERVICE, uuid.uuid4(), "라" * 500),
        reason="R" * 500,
        ctx=RequestContext(route="P" * 500, path="/p" * 500, request_id="i" * 500),
    )

    assert session.added == [row]
    assert row.component == "c" * AUDIT_COMPONENT_MAX_LENGTH
    assert row.action == "A" * AUDIT_ACTION_MAX_LENGTH
    assert row.target_label == "라" * AUDIT_TARGET_LABEL_MAX_LENGTH
    assert row.reason == "R" * AUDIT_REASON_MAX_LENGTH
    assert row.route == "P" * AUDIT_ROUTE_MAX_LENGTH
    assert row.request_id == "i" * REQUEST_ID_MAX_LENGTH
    # path 칸은 Text 라 자르지 않는다.
    assert row.path == "/p" * 500


def test_record_keeps_short_values_and_uses_clock() -> None:
    recorder, _ = _recorder()
    user_id = uuid.uuid4()
    target_id = uuid.uuid4()

    row = recorder.record(
        organization_id=None,
        actor=AuditActor.user(user_id),
        action=AuditAction.SERVICE_CREATED,
        target=AuditTarget(AuditTargetType.SERVICE, target_id, "shop-api"),
        details={"changedFields": ["description"]},
    )

    assert (row.actor_type, row.actor_user_id, row.component) == (ActorType.USER, user_id, None)
    assert (row.target_type, row.target_id, row.target_label) == ("service", target_id, "shop-api")
    assert row.result == AuditResult.SUCCEEDED
    assert row.details == {"changedFields": ["description"]}
    assert row.created_at == NOW
    # 요청 밖이면 요청 칸은 비어 있다(일꾼·시험의 서비스 직접 호출).
    assert (row.route, row.path, row.request_id, row.reason) == (None, None, None, None)


def test_record_reads_current_request_when_ctx_is_not_given() -> None:
    recorder, _ = _recorder()
    route = type("Route", (), {"path": "/api/v1/orgs/{org_id}/services"})()
    scope = {
        "method": "POST",
        "path": "/api/v1/orgs/1/services",
        "route": route,
        "state": {"request_id": "req-1"},
    }

    with bind_request_scope(scope):
        context = current_request_context()
        row = recorder.record(
            organization_id=None,
            actor=AuditActor.system(AuditComponent.API),
            action=AuditAction.DEPLOY_FINISHED,
        )

    assert context == RequestContext(
        route="POST /api/v1/orgs/{org_id}/services",
        path="/api/v1/orgs/1/services",
        request_id="req-1",
    )
    assert (row.route, row.path, row.request_id) == (context.route, context.path, "req-1")
    assert current_request_context() is None


def test_request_context_before_routing_has_no_route() -> None:
    with bind_request_scope({"method": "GET", "path": "/nope", "state": {}}):
        context = current_request_context()

    assert context == RequestContext(route=None, path="/nope", request_id=None)


def test_write_route_actions_are_unique_user_actions() -> None:
    names = list(WRITE_ROUTE_ACTIONS.values())

    # API 서버 7 의 24개 + 기한부 예외(보안·거버넌스 3) 3개 + AI 사전 점검 2개 + 재난 2개
    assert len(names) == 31
    assert len(set(names)) == 31
    # 초대 적용·끝 상태·에이전트 읽기는 쓰기 경로의 이름이 아니다.
    assert not set(names) & {
        AuditAction.MEMBER_JOINED,
        AuditAction.DEPLOY_FINISHED,
        AuditAction.ROLLBACK_FINISHED,
        AuditAction.MIGRATION_FINISHED,
        AuditAction.SERVICE_DELETED,
        AuditAction.RESOURCE_READ,
        AuditAction.AGENT_RUN_CLAIMED,
        AuditAction.AGENT_RUN_FINISHED,
        AuditAction.AGENT_RUN_TIMED_OUT,
    }
