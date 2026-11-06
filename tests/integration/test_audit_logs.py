"""감사 로그 저장·조회 통합 시험(SPEC api-7 AC-1~AC-9, AC-12).

칸·트리거·마이그레이션(AC-10·AC-11)은 test_migrations.py, 자르기 단위 시험은 test_audit_recorder.py,
데모 시드(AC-6)는 test_demo_seed.py 에 있다.
"""

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from httpx import Response
from sqlalchemy import select, text

from app.audit.actions import RUNNER_ROUTE_ACTIONS, WRITE_ROUTE_ACTIONS, AuditAction
from app.audit.recorder import AuditRecorder
from app.clients.app_secret_store import FakeAppSecretStore
from app.clients.github_user import GithubUser
from app.enums import (
    ActorType,
    AuditResult,
    DeployRequestStatus,
    JobKind,
    JobStatus,
    OrganizationRole,
)
from app.executors.base import HealthHttpError
from app.executors.fake import FakeBuildExecutor, FakeTeardownExecutor
from app.main import app
from app.models import AuditLog, Environment, Job, Service, User
from app.services.service_variable_service import app_secret_name
from tests.integration.auth_support import (
    create_agent_token,
    create_member,
    create_organization,
    create_session,
    create_user,
    make_client,
    org_path,
)
from tests.integration.deploy_harness import DeployHarness, ScriptedHealthChecker, connection_of
from tests.integration.factories import ENVIRONMENT_CONFIGS, secret_env_name, secret_ref_for
from tests.integration.migration_support import Migration
from tests.integration.rollback_support import deploy_and_finish, request_rollback, rollback
from tests.integration.runner_support import add_runner, finish_precheck, run_patch
from tests.integration.test_auth_login import _login
from tests.integration.test_authorization import iter_api_routes
from tests.integration.test_environment_api import _start_gated_check

LOGOUT = ("POST", "/api/v1/auth/logout")
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
ROUTE_BY_ACTION = {
    action: f"{method} {template}" for (method, template), action in WRITE_ROUTE_ACTIONS.items()
}
BOB = GithubUser(id=7001, login="bob-ops", name="Bob", avatar_url=None)
DAY_PLUS_ONE_SECOND = 24 * 60 * 60 + 1
ITEM_KEYS = {
    "id",
    "at",
    "actor",
    "action",
    "route",
    "path",
    "target",
    "result",
    "reason",
    "details",
    "requestId",
}
ACTOR_KEYS = {"type", "userId", "githubLogin", "agentTokenId", "agentTokenName", "component"}


async def _rows(harness: DeployHarness, *conditions: Any) -> list[AuditLog]:
    async with harness.session_factory() as session:
        return list(
            (await session.scalars(select(AuditLog).where(*conditions).order_by(AuditLog.seq)))
            .unique()
            .all()
        )


async def _user_actions(harness: DeployHarness, *conditions: Any) -> list[str]:
    rows = await _rows(harness, AuditLog.actor_type == ActorType.USER, *conditions)
    return [row.action for row in rows]


async def _target_rows(harness: DeployHarness, target_id: str | uuid.UUID) -> list[AuditLog]:
    return await _rows(harness, AuditLog.target_id == uuid.UUID(str(target_id)))


async def _finish_rows(harness: DeployHarness, target_id: str | uuid.UUID) -> list[AuditLog]:
    rows = await _target_rows(harness, target_id)
    return [row for row in rows if row.actor_type == ActorType.SYSTEM]


def _data(response: Response) -> Any:
    assert response.is_success, response.text
    return response.json()["data"]


def _assert_finished(
    row: AuditLog,
    action: AuditAction,
    component: str,
    result: AuditResult,
    **details: Any,
) -> None:
    assert (row.action, row.component, row.result) == (action, component, result)
    assert (row.actor_user_id, row.actor_agent_token_id) == (None, None)
    for key, value in details.items():
        assert row.details[key] == value, key


async def _set_service_deleting(harness: DeployHarness, service_id: str) -> None:
    async with harness.session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE services SET status = 'DELETING' WHERE id = :id"), {"id": service_id}
        )


async def _close_job(harness: DeployHarness, subject_id: str, status: str) -> None:
    async with harness.session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE jobs SET status = :status WHERE subject_id = :id"),
            {"status": status, "id": subject_id},
        )


# --- AC-1: 이름표 완전성 ---


def test_every_write_route_has_an_action_name() -> None:
    write_routes = {
        (method, route.path)
        for route in iter_api_routes()
        for method in route.methods
        if method in WRITE_METHODS
    }

    # 쓰기 경로 = 사람 표 ∪ 러너 표 ∪ 로그아웃. 두 표는 겹치지 않는다.
    assert write_routes - {LOGOUT} == set(WRITE_ROUTE_ACTIONS) | set(RUNNER_ROUTE_ACTIONS)
    assert not set(WRITE_ROUTE_ACTIONS) & set(RUNNER_ROUTE_ACTIONS)
    assert LOGOUT in write_routes
    assert len(WRITE_ROUTE_ACTIONS) == 37
    assert len(RUNNER_ROUTE_ACTIONS) == 4


# --- AC-2: 쓰기 37개 경로(API 서버 7 의 24개 + 기한부 예외 3개 + 사전 점검 4개 + 재난 2개
# + 변수 복사 1개 + 배포 실패 진단 1개 + 실제 재난 피드 2개) ---


@dataclass
class _Ledger:
    """요청 하나마다 새 USER 줄을 확인한다."""

    harness: DeployHarness
    after_seq: int = 0
    seen: set[str] = field(default_factory=set)

    async def _new_user_rows(self) -> list[AuditLog]:
        rows = await _rows(self.harness, AuditLog.seq > self.after_seq)
        if rows:
            self.after_seq = rows[-1].seq
        return [row for row in rows if row.actor_type == ActorType.USER]

    async def skip(self) -> None:
        await self._new_user_rows()

    async def expect_none(self, response: Response) -> None:
        assert response.is_success, response.text
        assert await self._new_user_rows() == []

    async def expect_one(
        self,
        response: Response,
        action: AuditAction,
        user: User,
        organization_id: uuid.UUID,
        target_type: str,
        target_id: str | uuid.UUID,
        label: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        assert response.is_success, response.text
        rows = await self._new_user_rows()
        assert [row.action for row in rows] == [action]
        row = rows[0]
        assert row.result == AuditResult.SUCCEEDED
        assert row.reason is None
        assert (row.actor_user_id, row.actor_agent_token_id, row.component) == (user.id, None, None)
        assert row.organization_id == organization_id
        assert (row.target_type, str(row.target_id), row.target_label) == (
            target_type,
            str(target_id),
            label,
        )
        assert row.route == ROUTE_BY_ACTION[action]
        assert row.path == response.request.url.path
        assert row.request_id == response.headers["x-request-id"]
        assert row.details == (details or {})
        self.seen.add(action)


async def test_every_write_route_records_one_user_line_and_idempotent_retries_none(
    migration: Migration, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = migration.harness
    admin = harness.admin
    team_id = harness.organization.id
    client = harness.client
    ledger = _Ledger(harness)
    await ledger.skip()

    # 조직
    created = await client.post("/api/v1/orgs", json={"slug": "acme-co", "name": "Acme"})
    acme_id = uuid.UUID(_data(created)["id"])
    await ledger.expect_one(
        created,
        AuditAction.ORGANIZATION_CREATED,
        admin.user,
        acme_id,
        "organization",
        acme_id,
        "Acme",
    )
    settings_path = org_path(team_id, "/settings")
    changed = await client.patch(settings_path, json={"allowSelfApproval": True})
    await ledger.expect_one(
        changed,
        AuditAction.ORGANIZATION_SETTINGS_CHANGED,
        admin.user,
        team_id,
        "organization",
        team_id,
        harness.organization.name,
        {"allowSelfApproval": {"from": False, "to": True}},
    )
    await ledger.expect_none(await client.patch(settings_path, json={"allowSelfApproval": True}))

    # 멤버
    vic = await create_member(
        harness.session_factory,
        harness.organization,
        OrganizationRole.VIEWER,
        harness.clock.now(),
        "vic",
    )
    member_path = org_path(team_id, f"/members/{vic.user.id}")
    role_changed = await client.patch(member_path, json={"role": "DEVELOPER"})
    await ledger.expect_one(
        role_changed,
        AuditAction.MEMBER_ROLE_CHANGED,
        admin.user,
        team_id,
        "user",
        vic.user.id,
        "vic",
        {"role": {"from": "VIEWER", "to": "DEVELOPER"}},
    )
    await ledger.expect_none(await client.patch(member_path, json={"role": "DEVELOPER"}))
    await ledger.expect_one(
        await client.delete(member_path),
        AuditAction.MEMBER_REMOVED,
        admin.user,
        team_id,
        "user",
        vic.user.id,
        "vic",
        {"role": "DEVELOPER"},
    )

    # 초대
    harness.github_users.directory["bob-ops"] = BOB
    invited = await client.post(
        org_path(team_id, "/invitations"), json={"githubLogin": "bob-ops", "role": "VIEWER"}
    )
    invitation_id = _data(invited)["id"]
    await ledger.expect_one(
        invited,
        AuditAction.INVITATION_CREATED,
        admin.user,
        team_id,
        "invitation",
        invitation_id,
        "bob-ops",
        {"githubLogin": "bob-ops", "role": "VIEWER"},
    )
    await ledger.expect_one(
        await client.delete(org_path(team_id, f"/invitations/{invitation_id}")),
        AuditAction.INVITATION_REVOKED,
        admin.user,
        team_id,
        "invitation",
        invitation_id,
        "bob-ops",
    )

    # 에이전트 토큰
    issued = await client.post(org_path(team_id, "/agent-tokens"), json={"name": "ai-runner"})
    token = _data(issued)
    await ledger.expect_one(
        issued,
        AuditAction.AGENT_TOKEN_ISSUED,
        admin.user,
        team_id,
        "agent_token",
        token["id"],
        "ai-runner",
        {"name": "ai-runner"},
    )
    token_path = org_path(team_id, f"/agent-tokens/{token['id']}")
    await ledger.expect_one(
        await client.delete(token_path),
        AuditAction.AGENT_TOKEN_REVOKED,
        admin.user,
        team_id,
        "agent_token",
        token["id"],
        "ai-runner",
    )
    await ledger.expect_none(await client.delete(token_path))

    # 환경(등록 뒤 접속 확인은 줄을 더 쓰지 않는다)
    monkeypatch.setenv(secret_env_name("aws-tokyo"), "fake-kubeconfig")
    environment_body = {
        "name": "aws-tokyo",
        "kind": "aws",
        "config": {"region": "ap-northeast-1", "clusterName": "eks"},
        "secretRef": secret_ref_for("aws-tokyo"),
    }
    registered = await client.post(harness.environments, json=environment_body)
    environment_id = _data(registered)["id"]
    await ledger.expect_one(
        registered,
        AuditAction.ENVIRONMENT_CREATED,
        admin.user,
        team_id,
        "environment",
        environment_id,
        "aws-tokyo",
        {"kind": "aws"},
    )
    environment_path = f"{harness.environments}/{environment_id}"
    await ledger.expect_one(
        await client.patch(environment_path, json={"name": "aws-osaka"}),
        AuditAction.ENVIRONMENT_UPDATED,
        admin.user,
        team_id,
        "environment",
        environment_id,
        "aws-osaka",
        {"changedFields": ["name"]},
    )
    await ledger.expect_none(await client.patch(environment_path, json={"name": "aws-osaka"}))
    await ledger.expect_one(
        await client.post(f"{environment_path}/connection-check"),
        AuditAction.ENVIRONMENT_CONNECTION_CHECKED,
        admin.user,
        team_id,
        "environment",
        environment_id,
        "aws-osaka",
        {"connectionStatus": "CONNECTED"},
    )
    await ledger.expect_one(
        await client.delete(environment_path),
        AuditAction.ENVIRONMENT_DELETED,
        admin.user,
        team_id,
        "environment",
        environment_id,
        "aws-osaka",
    )

    # 서비스·배포·되돌리기
    service_created = await client.post(
        harness.services,
        json={"name": "pay-api", "repoUrl": "https://github.com/team-echo/pay-api"},
    )
    service_id = _data(service_created)["id"]
    await ledger.expect_one(
        service_created,
        AuditAction.SERVICE_CREATED,
        admin.user,
        team_id,
        "service",
        service_id,
        "pay-api",
    )
    service_path = f"{harness.services}/{service_id}"
    await ledger.expect_one(
        await client.patch(service_path, json={"description": "결제"}),
        AuditAction.SERVICE_UPDATED,
        admin.user,
        team_id,
        "service",
        service_id,
        "pay-api",
        {"changedFields": ["description"]},
    )
    await ledger.expect_none(await client.patch(service_path, json={"description": "결제"}))
    deploy = await harness.request_deploy(service_id, migration.aws.id, branch="main")
    first = _data(deploy)
    await ledger.expect_one(
        deploy,
        AuditAction.DEPLOY_REQUESTED,
        admin.user,
        team_id,
        "deploy_request",
        first["id"],
        f"pay-api · aws-seoul · {first['commitSha'][:7]}",
        {
            "environmentId": str(migration.aws.id),
            "ref": "main",
            "commitSha": first["commitSha"],
        },
    )
    await harness.run_until_idle()
    await deploy_and_finish(harness, service_id, migration.aws.id, branch="release")
    await ledger.skip()
    rollback_response = await request_rollback(harness, first["id"])
    rollback_data = _data(rollback_response)
    await ledger.expect_one(
        rollback_response,
        AuditAction.ROLLBACK_REQUESTED,
        admin.user,
        team_id,
        "deploy_request",
        rollback_data["id"],
        f"pay-api · aws-seoul · {first['commitSha'][:7]}",
        {"rollbackOfId": first["id"], "environmentId": str(migration.aws.id)},
    )
    await harness.run_until_idle()
    await ledger.skip()

    # 앱 환경 변수: 키만 적고 값·비밀 참조·버전은 적지 않는다.
    variable_path = f"{service_path}/environments/{migration.aws.id}/variables/DATABASE_URL"
    variable_label = "pay-api · aws-seoul · DATABASE_URL"
    variable_details = {"environmentId": str(migration.aws.id), "key": "DATABASE_URL"}
    variable_value = f"postgres://user:sv-{uuid.uuid4()}@db/app"
    variable_saved = await client.put(variable_path, json={"value": variable_value})
    await ledger.expect_one(
        variable_saved,
        AuditAction.SERVICE_VARIABLE_SET,
        admin.user,
        team_id,
        "service",
        service_id,
        variable_label,
        variable_details,
    )
    # 같은 값을 다시 넣어도 새 버전이 생긴 변경이다(값을 다시 읽지 않아 같은지 모른다).
    variable_resaved = await client.put(variable_path, json={"value": variable_value})
    assert _data(variable_resaved)["version"] == _data(variable_saved)["version"] + 1
    await ledger.expect_one(
        variable_resaved,
        AuditAction.SERVICE_VARIABLE_SET,
        admin.user,
        team_id,
        "service",
        service_id,
        variable_label,
        variable_details,
    )
    variable_secret_ref = _data(variable_resaved)["secretRef"]
    # 복사(이슈 #99): 대상·키마다 한 줄, 키와 원본 환경만 적는다(값 없음).
    variable_copied = await client.post(
        f"{service_path}/variables:copy",
        json={
            "sourceEnvironmentId": str(migration.aws.id),
            "targetEnvironmentIds": [str(migration.gcp.id)],
            "keys": ["DATABASE_URL"],
        },
    )
    assert _data(variable_copied)["failed"] == []
    await ledger.expect_one(
        variable_copied,
        AuditAction.SERVICE_VARIABLE_COPIED,
        admin.user,
        team_id,
        "service",
        service_id,
        "pay-api · gcp-seoul · DATABASE_URL",
        {
            "environmentId": str(migration.gcp.id),
            "key": "DATABASE_URL",
            "sourceEnvironmentId": str(migration.aws.id),
        },
    )
    await ledger.expect_one(
        await client.delete(variable_path),
        AuditAction.SERVICE_VARIABLE_DELETED,
        admin.user,
        team_id,
        "service",
        service_id,
        variable_label,
        variable_details,
    )
    missing_variable = await client.delete(variable_path)
    assert (missing_variable.status_code, missing_variable.json()["code"]) == (
        404,
        "ENV_VAR_NOT_FOUND",
    )
    assert await _user_actions(harness, AuditLog.seq > ledger.after_seq) == []

    await ledger.expect_one(
        await client.delete(service_path),
        AuditAction.SERVICE_DELETE_REQUESTED,
        admin.user,
        team_id,
        "service",
        service_id,
        "pay-api",
    )
    await ledger.expect_none(await client.delete(service_path))
    await harness.run_until_idle()
    await ledger.skip()

    # 이전 계획
    label = "shop-api · aws-seoul → gcp-seoul"
    plan_created = await migration.request_plan()
    plan = _data(plan_created)
    await ledger.expect_one(
        plan_created,
        AuditAction.MIGRATION_PLAN_CREATED,
        migration.author.user,
        team_id,
        "migration_plan",
        plan["id"],
        label,
        {
            "sourceEnvironmentId": str(migration.aws.id),
            "targetEnvironmentId": str(migration.gcp.id),
        },
    )
    await ledger.expect_one(
        await migration.act(plan["id"], "approve"),
        AuditAction.MIGRATION_PLAN_APPROVED,
        migration.approver.user,
        team_id,
        "migration_plan",
        plan["id"],
        label,
        {"selfApproved": False},
    )
    await ledger.expect_one(
        await migration.act(plan["id"], "abort"),
        AuditAction.MIGRATION_ABORT_REQUESTED,
        migration.approver.user,
        team_id,
        "migration_plan",
        plan["id"],
        label,
    )
    await ledger.expect_none(await migration.act(plan["id"], "abort"))
    await harness.run_until_idle()
    await ledger.skip()
    to_reject = await migration.create_plan()
    await ledger.skip()
    await ledger.expect_one(
        await migration.act(to_reject["id"], "reject"),
        AuditAction.MIGRATION_PLAN_REJECTED,
        migration.approver.user,
        team_id,
        "migration_plan",
        to_reject["id"],
        label,
    )
    to_cancel = await migration.create_plan()
    await ledger.skip()
    await ledger.expect_one(
        await migration.act(to_cancel["id"], "cancel", client=migration.author_client),
        AuditAction.MIGRATION_PLAN_CANCELLED,
        migration.author.user,
        team_id,
        "migration_plan",
        to_cancel["id"],
        label,
    )

    # 기한부 예외(보안·거버넌스 3): 요청은 만든 사람, 승인은 다른 개발자, 철회는 만든 사람
    exceptions_path = org_path(team_id, "/security/exceptions")
    expires_at = harness.clock.now() + timedelta(days=1)
    requested = await migration.author_client.post(
        exceptions_path,
        json={
            "controlId": "C-DB-1",
            "serviceId": migration.service_id,
            "environmentId": str(migration.gcp.id),
            "reason": "D04~D07 결정 전, 시연은 쓰기 정지 구간에서 수동 확인",
            "expiresAt": expires_at.isoformat(),
        },
    )
    exception_id = _data(requested)["id"]
    await ledger.expect_one(
        requested,
        AuditAction.SECURITY_EXCEPTION_REQUESTED,
        migration.author.user,
        team_id,
        "security_exception",
        exception_id,
        "C-DB-1",
        {"expiresAt": expires_at.isoformat()},
    )
    await ledger.expect_one(
        await migration.approver_client.post(f"{exceptions_path}/{exception_id}/approve"),
        AuditAction.SECURITY_EXCEPTION_APPROVED,
        migration.approver.user,
        team_id,
        "security_exception",
        exception_id,
        "C-DB-1",
    )
    await ledger.expect_one(
        await migration.author_client.post(f"{exceptions_path}/{exception_id}/revoke"),
        AuditAction.SECURITY_EXCEPTION_REVOKED,
        migration.author.user,
        team_id,
        "security_exception",
        exception_id,
        "C-DB-1",
    )

    # AI 사전 점검(러너 레인 A): 만들기·취소는 만든 개발자
    prechecks_path = org_path(team_id, "/prechecks")
    precheck_details = {
        "serviceId": migration.service_id,
        "targetEnvironmentId": str(migration.gcp.id),
    }
    precheck = await migration.author_client.post(
        prechecks_path,
        json={"serviceId": migration.service_id, "targetEnvironmentId": str(migration.gcp.id)},
    )
    precheck_id = _data(precheck)["id"]
    await ledger.expect_one(
        precheck,
        AuditAction.PRECHECK_REQUESTED,
        migration.author.user,
        team_id,
        "agent_run",
        precheck_id,
        migration.service["name"],
        precheck_details,
    )
    await ledger.expect_one(
        await migration.author_client.post(f"{prechecks_path}/{precheck_id}/cancel"),
        AuditAction.PRECHECK_CANCELLED,
        migration.author.user,
        team_id,
        "agent_run",
        precheck_id,
        migration.service["name"],
        precheck_details,
    )

    # 패치 승인·거절(API 서버 10): 러너가 낸 패치 둘을 만든 사람이 아닌 개발자가 결정한다.
    runner = await add_runner(harness.session_factory)
    patched_id = (
        await finish_precheck(migration, runner, patches=[run_patch("p1"), run_patch("p2")])
    )["id"]
    await ledger.skip()
    await ledger.expect_one(
        await migration.approver_client.post(
            f"{prechecks_path}/{patched_id}/patches/p1/approve",
            json={"patchHash": run_patch("p1")["patchHash"]},
        ),
        AuditAction.AGENT_PATCH_APPROVED,
        migration.approver.user,
        team_id,
        "agent_run",
        patched_id,
        migration.service["name"],
        {"patchId": "p1", "patchHash": run_patch("p1")["patchHash"], "applyRunId": None},
    )
    await ledger.expect_one(
        await migration.approver_client.post(
            f"{prechecks_path}/{patched_id}/patches/p2/reject",
            json={"patchHash": run_patch("p2")["patchHash"]},
        ),
        AuditAction.AGENT_PATCH_REJECTED,
        migration.approver.user,
        team_id,
        "agent_run",
        patched_id,
        migration.service["name"],
        {"patchId": "p2", "patchHash": run_patch("p2")["patchHash"]},
    )
    await ledger.expect_none(
        await migration.approver_client.post(
            f"{prechecks_path}/{patched_id}/patches/p1/approve",
            json={"patchHash": run_patch("p1")["patchHash"]},
        )
    )

    # 배포 실패 진단(SPEC api-98): 실패한 배포에서 개발자가 만든다. 같은 요청을 다시 보내면 200 이고
    # 줄을 쓰지 않는다. 러너 스위치가 꺼져 있어(기본) 가짜 진단이다.
    failed = await deploy_and_finish(
        harness, migration.service_id, migration.aws.id, simulate={"failAt": "VERIFYING"}
    )
    assert failed["status"] == "FAILED"
    await ledger.skip()
    diagnosis_path = org_path(team_id, f"/deploy-requests/{failed['id']}/diagnosis")
    diagnosis = await migration.author_client.post(diagnosis_path)
    assert diagnosis.status_code == 201, diagnosis.text
    await ledger.expect_one(
        diagnosis,
        AuditAction.DIAGNOSIS_REQUESTED,
        migration.author.user,
        team_id,
        "agent_run",
        _data(diagnosis)["id"],
        migration.service["name"],
        {
            "serviceId": migration.service_id,
            "deployRequestId": failed["id"],
            "environmentId": str(migration.aws.id),
            "simulated": True,
        },
    )
    again = await migration.author_client.post(diagnosis_path)
    assert again.status_code == 200, again.text
    await ledger.expect_none(again)

    # 재난(API 서버 9): 태평양 한가운데의 기준 미달 지진이라 계획이 생기지 않고 재난 줄 하나다.
    disasters_path = org_path(team_id, "/disasters")
    disaster = await migration.author_client.post(
        disasters_path,
        json={"type": "EARTHQUAKE", "epicenter": {"lat": 0, "lng": -150}, "magnitude": 6.0},
    )
    disaster_id = _data(disaster)["id"]
    await ledger.expect_one(
        disaster,
        AuditAction.DISASTER_DECLARED,
        migration.author.user,
        team_id,
        "disaster",
        disaster_id,
        "EARTHQUAKE M6.0",
        {
            "type": "EARTHQUAKE",
            "magnitude": 6.0,
            "radiusKm": 103,
            "meetsThreshold": False,
            "affectedEnvironmentIds": [],
        },
    )
    # 실제 재난 피드(API 서버 18): 대피 시작(기준 미달이라 계획 없음)과 시연 주입(기준 미달)
    await ledger.expect_one(
        await migration.author_client.post(f"{disasters_path}/{disaster_id}/evacuate"),
        AuditAction.DISASTER_EVACUATION_REQUESTED,
        migration.author.user,
        team_id,
        "disaster",
        disaster_id,
        "EARTHQUAKE M6.0",
        {"source": "SIMULATED", "plans": []},
    )
    injected = await migration.author_client.post(
        f"{disasters_path}/feed:inject",
        json={"magnitude": 5.0, "epicenter": {"lat": 0, "lng": 0}},
    )
    await ledger.expect_one(
        injected,
        AuditAction.DISASTER_FEED_INJECTED,
        migration.author.user,
        team_id,
        "organization",
        team_id,
        harness.organization.name,
        {
            "feedEventId": _data(injected)["feedEventId"],
            "magnitude": 5.0,
            "epicenter": {"lat": 0.0, "lng": 0.0},
            "matched": False,
            "reason": "BELOW_THRESHOLD",
        },
    )
    await ledger.expect_one(
        await migration.author_client.post(f"{disasters_path}/{disaster_id}/end"),
        AuditAction.DISASTER_ENDED,
        migration.author.user,
        team_id,
        "disaster",
        disaster_id,
        "EARTHQUAKE M6.0",
    )

    # 37개를 모두 지났다. 어느 줄에도 토큰 원문·환경 변수 값·비밀 참조가 없다.
    assert ledger.seen == set(WRITE_ROUTE_ACTIONS.values())
    everything = await _rows(harness)
    dumped = json.dumps(
        [
            [row.action, row.route, row.path, row.reason, row.target_label, row.details]
            for row in everything
        ],
        ensure_ascii=False,
    )
    assert token["token"] not in dumped
    assert "echo_agt_" not in dumped
    assert variable_value not in dumped
    assert variable_secret_ref not in dumped
    assert "versions/" not in dumped


async def _variable_setup(harness: DeployHarness) -> tuple[str, Environment]:
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    await deploy_and_finish(harness, service["id"], aws.id, branch="main")
    return service["id"], aws


async def test_variable_write_that_fails_has_no_user_line(harness: DeployHarness) -> None:
    service_id, aws = await _variable_setup(harness)
    before = await _user_actions(harness)
    base = f"{harness.services}/{service_id}/environments"
    secret = f"sv-{uuid.uuid4()}"

    invalid_key = await harness.client.put(
        f"{base}/{aws.id}/variables/bad-key", json={"value": "x"}
    )
    no_connection = await harness.client.put(
        f"{base}/{uuid.uuid4()}/variables/DATABASE_URL", json={"value": secret}
    )
    assert isinstance(harness.app_secret_store, FakeAppSecretStore)
    harness.app_secret_store.unavailable_names.add(
        app_secret_name(
            harness.settings.app_secret_name_prefix,
            harness.organization.slug,
            uuid.UUID(service_id),
            aws.id,
            "DATABASE_URL",
        )
    )
    store_down = await harness.client.put(
        f"{base}/{aws.id}/variables/DATABASE_URL", json={"value": secret}
    )

    assert invalid_key.status_code == 422
    assert no_connection.status_code == 404
    assert (store_down.status_code, store_down.json()["code"]) == (
        502,
        "SECRET_STORE_UNAVAILABLE",
    )
    assert await _user_actions(harness) == before


async def test_agent_variable_put_is_denied_without_value_in_the_line(
    harness: DeployHarness,
) -> None:
    service_id, aws = await _variable_setup(harness)
    _, raw = await create_agent_token(
        harness.session_factory, harness.organization, harness.admin.user, harness.clock.now()
    )
    secret = f"sv-{uuid.uuid4()}"

    async with make_client(agent_token=raw) as agent:
        response = await agent.put(
            f"{harness.services}/{service_id}/environments/{aws.id}/variables/DATABASE_URL",
            json={"value": secret},
        )

    assert (response.status_code, response.json()["code"]) == (403, "AGENT_READ_ONLY")
    rows = await _rows(harness, AuditLog.result == AuditResult.DENIED)
    assert [row.action for row in rows] == [AuditAction.SERVICE_VARIABLE_SET]
    assert rows[0].route == ROUTE_BY_ACTION[AuditAction.SERVICE_VARIABLE_SET]
    assert rows[0].details == {}
    assert secret not in json.dumps([rows[0].path, rows[0].target_label, rows[0].details])


async def test_connection_check_discarded_result_is_still_recorded(
    harness: DeployHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = await harness.add_environment("aws-seoul")
    monkeypatch.setenv(secret_env_name("aws-seoul"), "fake-kubeconfig")
    async with make_client(
        session_token=harness.admin.session_token, base_path=org_path(harness.organization.id)
    ) as client:
        adapter, first_check = await _start_gated_check(client, str(environment.id))
        patched = await client.patch(
            f"/environments/{environment.id}",
            json={"config": {"region": "us-east-1", "clusterName": "eks"}},
        )
        adapter.release.set()
        first = await first_check

    assert patched.status_code == 200 and first.status_code == 200
    rows = await _rows(harness, AuditLog.target_id == environment.id)
    # 수정의 뒤따르는 다시 확인은 줄을 쓰지 않는다.
    assert [row.action for row in rows] == [
        AuditAction.ENVIRONMENT_UPDATED,
        AuditAction.ENVIRONMENT_CONNECTION_CHECKED,
    ]
    assert rows[1].details == {"connectionStatus": "CONNECTED", "discarded": True}


async def test_service_name_retry_keeps_only_committed_attempt(harness: DeployHarness) -> None:
    harness.host_suffixes.queue("aaaaaaaa")
    await harness.create_service("shop-api")
    other = await create_organization(harness.session_factory, "acme-co")
    other_admin = await create_member(
        harness.session_factory, other, OrganizationRole.ADMIN, harness.clock.now(), "acme-admin"
    )
    harness.host_suffixes.queue("aaaaaaaa", "bbbbbbbb")
    async with make_client(session_token=other_admin.session_token) as client:
        response = await client.post(
            org_path(other.id, "/services"),
            json={"name": "shop-api", "repoUrl": "https://github.com/acme/shop-api"},
        )

    assert response.status_code == 201
    assert await _user_actions(harness, AuditLog.organization_id == other.id) == [
        AuditAction.SERVICE_CREATED
    ]


# --- AC-3: 같은 트랜잭션·성공 분기 ---


async def test_write_ending_in_conflict_has_no_user_line(migration: Migration) -> None:
    harness = migration.harness
    plan = await migration.create_plan()
    await migration.approve(plan["id"])
    before = await _user_actions(harness)

    blocked = await harness.request_deploy(migration.service_id, migration.aws.id, branch="next")
    last_admin = await harness.client.delete(
        org_path(harness.organization.id, f"/members/{harness.admin.user.id}")
    )

    assert (blocked.status_code, blocked.json()["code"]) == (409, "MIGRATION_IN_PROGRESS")
    assert (last_admin.status_code, last_admin.json()["code"]) == (409, "LAST_ADMIN")
    assert await _user_actions(harness) == before


async def test_recorder_failure_rolls_back_the_work(
    harness: DeployHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(self: AuditRecorder, **values: Any) -> None:
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(AuditRecorder, "record", fail)

    response = await harness.client.post(
        harness.services,
        json={"name": "shop-api", "repoUrl": "https://github.com/team-echo/shop-api"},
    )

    async with harness.session_factory() as session:
        services = (await session.scalars(select(Service))).all()
    assert response.status_code == 500
    assert services == []


# --- AC-4: 두 줄(접수 + 끝) ---


async def test_migration_success_has_created_approved_and_finished_lines(
    migration: Migration,
) -> None:
    harness = migration.harness
    plan = await migration.create_plan()
    harness.clock.advance(5)
    await migration.approve(plan["id"])
    harness.clock.advance(5)
    await harness.run_until_idle()

    response = await harness.client.get(
        org_path(harness.organization.id, "/audit-logs"), params={"targetId": plan["id"]}
    )
    items = _data(response)["items"]

    # 최신순 응답을 거꾸로 읽으면 시간 순이다.
    assert [item["action"] for item in reversed(items)] == [
        "MIGRATION_PLAN_CREATED",
        "MIGRATION_PLAN_APPROVED",
        "MIGRATION_FINISHED",
    ]
    finished = items[0]
    assert finished["actor"]["type"] == "SYSTEM"
    assert finished["actor"]["component"] == "migration-worker"
    assert finished["result"] == "SUCCEEDED"
    assert finished["details"] == {"status": "SUCCEEDED", "simulated": True}
    assert finished["route"] is None and finished["requestId"] is None
    assert items[2]["at"] < items[1]["at"] < finished["at"]


async def test_migration_simulated_failure_finishes_failed_rolled_back(
    migration: Migration,
) -> None:
    plan = await migration.create_plan(simulate={"failAtStep": 2, "reason": "ERROR_RATE"})
    await migration.approve(plan["id"])
    await migration.harness.run_until_idle()

    rows = await _finish_rows(migration.harness, plan["id"])

    assert len(rows) == 1
    _assert_finished(
        rows[0],
        AuditAction.MIGRATION_FINISHED,
        "migration-worker",
        AuditResult.FAILED,
        status="ROLLED_BACK",
        errorCode="THRESHOLD_EXCEEDED",
    )


async def test_deploy_success_and_failure_have_one_finish_line(harness: DeployHarness) -> None:
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")

    succeeded = await deploy_and_finish(harness, service["id"], aws.id, branch="main")
    failed = await deploy_and_finish(
        harness, service["id"], aws.id, branch="next", simulate={"failAt": "DEPLOYING"}
    )

    ok_rows = await _target_rows(harness, succeeded["id"])
    failed_rows = await _target_rows(harness, failed["id"])
    assert [row.action for row in ok_rows] == [
        AuditAction.DEPLOY_REQUESTED,
        AuditAction.DEPLOY_FINISHED,
    ]
    _assert_finished(
        ok_rows[1],
        AuditAction.DEPLOY_FINISHED,
        "deploy-worker",
        AuditResult.SUCCEEDED,
        status="SUCCEEDED",
        trigger="MANUAL",
        simulated=True,
    )
    assert ok_rows[1].target_label == f"shop-api · aws-seoul · {succeeded['commitSha'][:7]}"
    assert [row.action for row in failed_rows] == [
        AuditAction.DEPLOY_REQUESTED,
        AuditAction.DEPLOY_FINISHED,
    ]
    _assert_finished(
        failed_rows[1],
        AuditAction.DEPLOY_FINISHED,
        "deploy-worker",
        AuditResult.FAILED,
        status="FAILED",
        errorCode="DEPLOY_FAILED",
    )


async def test_rollback_has_requested_and_finished_lines(harness: DeployHarness) -> None:
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    v1 = await deploy_and_finish(harness, service["id"], aws.id, branch="main")
    await deploy_and_finish(harness, service["id"], aws.id, branch="release")

    accepted = await rollback(harness, v1["id"])
    await harness.run_until_idle()

    rows = await _target_rows(harness, accepted["id"])
    assert [row.action for row in rows] == [
        AuditAction.ROLLBACK_REQUESTED,
        AuditAction.ROLLBACK_FINISHED,
    ]
    _assert_finished(
        rows[1],
        AuditAction.ROLLBACK_FINISHED,
        "deploy-worker",
        AuditResult.SUCCEEDED,
        status="SUCCEEDED",
        trigger="ROLLBACK",
    )


async def test_service_delete_has_requested_and_deleted_lines(harness: DeployHarness) -> None:
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    await deploy_and_finish(harness, service["id"], aws.id, branch="main")

    response = await harness.client.delete(f"{harness.services}/{service['id']}")
    await harness.run_until_idle()

    assert response.status_code == 202
    rows = await _target_rows(harness, service["id"])
    assert [row.action for row in rows] == [
        AuditAction.SERVICE_CREATED,
        AuditAction.SERVICE_DELETE_REQUESTED,
        AuditAction.SERVICE_DELETED,
    ]
    _assert_finished(rows[2], AuditAction.SERVICE_DELETED, "teardown-worker", AuditResult.SUCCEEDED)
    assert rows[2].organization_id == harness.organization.id
    assert rows[2].target_label == "shop-api"


async def _delete_lines(harness: DeployHarness, service_id: str) -> list[dict[str, Any]]:
    rows = await _rows(
        harness,
        AuditLog.target_id == uuid.UUID(service_id),
        AuditLog.action == AuditAction.SERVICE_DELETE_REQUESTED,
    )
    return [row.details for row in rows]


async def test_repeat_delete_records_retry_only_when_teardown_really_restarts(
    harness: DeployHarness,
) -> None:
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    await deploy_and_finish(harness, service["id"], aws.id, branch="main")
    detail = await harness.get_service(service["id"])
    link = uuid.UUID(connection_of(detail, "aws-seoul")["serviceEnvironmentId"])
    teardown = FakeTeardownExecutor(harness.clock, 0, fail_service_environment_ids={link})
    harness.replace_executors(teardown=teardown)
    path = f"{harness.services}/{service['id']}"

    await harness.client.delete(path)
    # 내리는 중: 바뀐 것이 없어 줄이 없다.
    await harness.client.delete(path)
    assert await _delete_lines(harness, service["id"]) == [{}]

    # 내리기 실패는 끝 상태가 아니라 끝 줄이 없다.
    await harness.run_until_idle()
    stuck = await harness.get_service(service["id"])
    assert stuck["deleteError"] == "TEARDOWN_FAILED"
    assert await _finish_rows(harness, service["id"]) == []
    assert await _delete_lines(harness, service["id"]) == [{}]

    # 다시 삭제: 실패한 연결을 다시 내리기 시작한다.
    teardown.recover_service_environment(link)
    retried = await harness.client.delete(path)
    assert retried.status_code == 202
    lines = await _rows(
        harness,
        AuditLog.target_id == uuid.UUID(service["id"]),
        AuditLog.action == AuditAction.SERVICE_DELETE_REQUESTED,
    )
    assert [row.details for row in lines] == [{}, {"retry": True}]
    assert (lines[1].actor_type, lines[1].actor_user_id) == (ActorType.USER, harness.admin.user.id)
    assert lines[1].result == AuditResult.SUCCEEDED
    assert lines[1].request_id == retried.headers["x-request-id"]

    # 다시 내리는 중의 또 다른 요청은 바뀐 것이 없다.
    await harness.client.delete(path)
    assert await _delete_lines(harness, service["id"]) == [{}, {"retry": True}]

    await harness.run_until_idle()
    finished = await _finish_rows(harness, service["id"])
    assert [row.action for row in finished] == [AuditAction.SERVICE_DELETED]


@pytest.mark.parametrize("delete_error", [None, "WORKER_LOST"])
async def test_repeat_delete_with_closed_job_records_retry(
    harness: DeployHarness, delete_error: str | None
) -> None:
    """할 일이 닫혀 새로 만들거나(일꾼 상실 포함) 실패 표시를 지우면 다시 시작한 것이다."""
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    await deploy_and_finish(harness, service["id"], aws.id, branch="main")
    path = f"{harness.services}/{service['id']}"
    await harness.client.delete(path)
    await _close_job(harness, service["id"], JobStatus.DONE)
    async with harness.session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE services SET delete_error = :error WHERE id = :id"),
            {"error": delete_error, "id": service["id"]},
        )

    response = await harness.client.delete(path)

    assert response.status_code == 202
    assert await _delete_lines(harness, service["id"]) == [{}, {"retry": True}]


async def test_standby_sync_finish_line_carries_trigger(migration: Migration) -> None:
    harness = migration.harness
    service_id = migration.service_id
    before = {row.id for row in await _rows(harness)}

    await deploy_and_finish(harness, service_id, migration.aws.id, branch="release")

    rows = [row for row in await _rows(harness) if row.id not in before]
    synced = [row for row in rows if row.details.get("trigger") == "STANDBY_SYNC"]
    assert len(synced) == 1
    _assert_finished(
        synced[0],
        AuditAction.DEPLOY_FINISHED,
        "deploy-worker",
        AuditResult.SUCCEEDED,
        status="SUCCEEDED",
    )
    assert (synced[0].target_label or "").startswith("shop-api · gcp-seoul · ")
    # 따라가기는 사람이 누른 것이 아니라 접수 줄이 없다.
    assert await _user_actions(harness, AuditLog.target_id == synced[0].target_id) == []


# --- AC-5: 끝 상태 망라 ---


async def _hold_build(harness: DeployHarness, service_id: str, environment: Environment) -> Any:
    busy = await harness.deploy(service_id, environment.id, branch="next")
    harness.replace_executors(
        build=FakeBuildExecutor(harness.clock, 0, stall_commits={busy["commitSha"]})
    )
    await harness.run_until(busy["id"], {DeployRequestStatus.BUILDING})
    return busy


async def test_superseded_request_finishes_with_api_component(harness: DeployHarness) -> None:
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    await _hold_build(harness, service["id"], aws)
    queued = await harness.deploy(service["id"], aws.id, branch="later")

    newest_response = await harness.request_deploy(service["id"], aws.id, branch="latest")

    rows = await _finish_rows(harness, queued["id"])
    assert len(rows) == 1
    _assert_finished(
        rows[0],
        AuditAction.DEPLOY_FINISHED,
        "api",
        AuditResult.FAILED,
        status="CANCELLED",
        cancelReason="SUPERSEDED",
    )
    # 사람 요청 안에서 끝낸 줄이라 그 요청의 값을 싣는다.
    assert rows[0].request_id == newest_response.headers["x-request-id"]
    assert rows[0].route == ROUTE_BY_ACTION[AuditAction.DEPLOY_REQUESTED]


async def test_standby_sync_superseding_queued_request_finishes_with_worker_component(
    migration: Migration,
) -> None:
    harness = migration.harness
    await _hold_build(harness, migration.service_id, migration.gcp)
    queued = await harness.deploy(migration.service_id, migration.gcp.id, branch="later")

    source = await harness.deploy(migration.service_id, migration.aws.id, branch="release")
    await harness.run_until(source["id"], {DeployRequestStatus.SUCCEEDED})

    rows = await _finish_rows(harness, queued["id"])
    assert len(rows) == 1
    _assert_finished(
        rows[0],
        AuditAction.DEPLOY_FINISHED,
        "deploy-worker",
        AuditResult.FAILED,
        status="CANCELLED",
        cancelReason="SUPERSEDED",
    )
    assert rows[0].request_id is None


async def test_service_delete_cancels_queued_request_with_api_component(
    harness: DeployHarness,
) -> None:
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    queued = await harness.deploy(service["id"], aws.id, branch="main")

    await harness.client.delete(f"{harness.services}/{service['id']}")

    rows = await _finish_rows(harness, queued["id"])
    assert len(rows) == 1
    _assert_finished(
        rows[0],
        AuditAction.DEPLOY_FINISHED,
        "api",
        AuditResult.FAILED,
        status="CANCELLED",
        cancelReason="SERVICE_DELETED",
    )


async def test_teardown_worker_cancels_queued_request_with_its_component(
    harness: DeployHarness,
) -> None:
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    queued = await harness.deploy(service["id"], aws.id, branch="main")
    await _set_service_deleting(harness, service["id"])
    await _close_job(harness, queued["id"], JobStatus.CANCELLED)
    now = harness.clock.now()
    async with harness.session_factory() as session, session.begin():
        session.add(
            Job(
                kind=JobKind.SERVICE_TEARDOWN,
                subject_id=uuid.UUID(service["id"]),
                status=JobStatus.QUEUED,
                run_after=now,
                created_at=now,
                updated_at=now,
            )
        )

    await harness.run_until_idle()

    rows = await _finish_rows(harness, queued["id"])
    assert len(rows) == 1
    _assert_finished(
        rows[0],
        AuditAction.DEPLOY_FINISHED,
        "teardown-worker",
        AuditResult.FAILED,
        status="CANCELLED",
        cancelReason="SERVICE_DELETED",
    )
    service_rows = await _finish_rows(harness, service["id"])
    assert [row.action for row in service_rows] == [AuditAction.SERVICE_DELETED]


async def test_deploy_worker_cancels_queued_request_of_deleting_service(
    harness: DeployHarness,
) -> None:
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    queued = await harness.deploy(service["id"], aws.id, branch="main")
    await _set_service_deleting(harness, service["id"])

    await harness.run_until_idle()

    rows = await _finish_rows(harness, queued["id"])
    assert len(rows) == 1
    _assert_finished(
        rows[0],
        AuditAction.DEPLOY_FINISHED,
        "deploy-worker",
        AuditResult.FAILED,
        status="CANCELLED",
        cancelReason="SERVICE_DELETED",
    )


async def test_rollback_cancelled_base_changed_finishes_once(harness: DeployHarness) -> None:
    service = await harness.create_service("shop-api")
    aws = await harness.add_environment("aws-seoul")
    v1 = await deploy_and_finish(harness, service["id"], aws.id, branch="main")
    await deploy_and_finish(harness, service["id"], aws.id, branch="release")
    await _hold_build(harness, service["id"], aws)
    accepted = await rollback(harness, v1["id"])

    harness.replace_executors(build=FakeBuildExecutor(harness.clock, 0))
    await harness.run_until_idle()

    rows = await _finish_rows(harness, accepted["id"])
    assert len(rows) == 1
    _assert_finished(
        rows[0],
        AuditAction.ROLLBACK_FINISHED,
        "deploy-worker",
        AuditResult.FAILED,
        status="CANCELLED",
        cancelReason="BASE_CHANGED",
    )


@pytest.mark.parametrize("action", ["approve", "reject", "cancel"])
async def test_expired_plan_action_has_api_finish_line_and_no_user_line(
    migration: Migration, action: str
) -> None:
    harness = migration.harness
    plan = await migration.create_plan()
    harness.clock.advance(DAY_PLUS_ONE_SECOND)
    client = migration.author_client if action == "cancel" else migration.approver_client

    response = await migration.act(plan["id"], action, client=client)

    assert (response.status_code, response.json()["code"]) == (409, "PLAN_EXPIRED")
    rows = await _target_rows(harness, plan["id"])
    assert [row.action for row in rows] == [
        AuditAction.MIGRATION_PLAN_CREATED,
        AuditAction.MIGRATION_FINISHED,
    ]
    _assert_finished(
        rows[1], AuditAction.MIGRATION_FINISHED, "api", AuditResult.FAILED, status="EXPIRED"
    )
    assert rows[1].request_id == response.headers["x-request-id"]


async def test_stale_plan_at_approve_has_api_finish_line(migration: Migration) -> None:
    plan = await migration.create_plan()
    await migration.set_weights(90, 10)

    response = await migration.act(plan["id"], "approve")

    assert (response.status_code, response.json()["code"]) == (409, "PLAN_STALE")
    rows = await _target_rows(migration.harness, plan["id"])
    assert [row.action for row in rows] == [
        AuditAction.MIGRATION_PLAN_CREATED,
        AuditAction.MIGRATION_FINISHED,
    ]
    _assert_finished(
        rows[1], AuditAction.MIGRATION_FINISHED, "api", AuditResult.FAILED, status="STALE"
    )


async def test_new_plan_expiring_old_plan_has_api_finish_line(migration: Migration) -> None:
    old = await migration.create_plan()
    migration.harness.clock.advance(DAY_PLUS_ONE_SECOND)

    new = await migration.create_plan()

    rows = await _finish_rows(migration.harness, old["id"])
    assert len(rows) == 1
    _assert_finished(
        rows[0], AuditAction.MIGRATION_FINISHED, "api", AuditResult.FAILED, status="EXPIRED"
    )
    assert await _finish_rows(migration.harness, new["id"]) == []


async def test_abort_with_missing_job_has_user_and_api_lines(migration: Migration) -> None:
    plan = await migration.create_plan()
    await migration.approve(plan["id"])
    await _close_job(migration.harness, plan["id"], JobStatus.DONE)

    response = await migration.act(plan["id"], "abort")

    assert response.status_code == 202
    rows = await _target_rows(migration.harness, plan["id"])
    assert [row.action for row in rows][-2:] == [
        AuditAction.MIGRATION_ABORT_REQUESTED,
        AuditAction.MIGRATION_FINISHED,
    ]
    _assert_finished(
        rows[-1], AuditAction.MIGRATION_FINISHED, "api", AuditResult.FAILED, status="CANCELLED"
    )


async def test_worker_finishes_stale_cancelled_and_failed_plans_once(
    migration: Migration,
) -> None:
    harness = migration.harness
    stale = await migration.create_plan()
    await migration.approve(stale["id"])
    await migration.set_weights(90, 10)
    await harness.run_until_idle()
    await migration.set_weights(100, 0)

    cancelled = await migration.create_plan()
    await migration.approve(cancelled["id"])
    await migration.act(cancelled["id"], "abort")
    await harness.run_until_idle()

    harness.replace_executors(health=ScriptedHealthChecker(default=HealthHttpError(503)))
    preflight = await migration.create_plan()
    await migration.approve(preflight["id"])
    await harness.run_until_idle()

    expected = {
        stale["id"]: {"status": "STALE"},
        cancelled["id"]: {"status": "CANCELLED"},
        preflight["id"]: {"status": "FAILED", "errorCode": "PREFLIGHT_FAILED"},
    }
    for plan_id, details in expected.items():
        rows = await _finish_rows(harness, plan_id)
        assert len(rows) == 1, plan_id
        _assert_finished(
            rows[0],
            AuditAction.MIGRATION_FINISHED,
            "migration-worker",
            AuditResult.FAILED,
            **details,
        )


async def test_worker_finishes_plan_when_rollback_never_applies(migration: Migration) -> None:
    harness = migration.harness
    harness.executors.traffic.fail_when(lambda key: ":rollback:" in key)
    plan = await migration.create_plan(simulate={"failAtStep": 2, "reason": "ERROR_RATE"})
    await migration.approve(plan["id"])

    await harness.run_until_idle()

    rows = await _finish_rows(harness, plan["id"])
    assert len(rows) == 1
    _assert_finished(
        rows[0],
        AuditAction.MIGRATION_FINISHED,
        "migration-worker",
        AuditResult.FAILED,
        status="FAILED",
        errorCode="THRESHOLD_EXCEEDED",
    )


async def test_worker_finishes_plan_failed_before_start(migration: Migration) -> None:
    plan = await migration.create_plan()
    await migration.approve(plan["id"])
    await _set_service_deleting(migration.harness, migration.service_id)

    await migration.harness.run_until_idle()

    rows = await _finish_rows(migration.harness, plan["id"])
    assert len(rows) == 1
    _assert_finished(
        rows[0],
        AuditAction.MIGRATION_FINISHED,
        "migration-worker",
        AuditResult.FAILED,
        status="FAILED",
        errorCode="INTERNAL_ERROR",
    )


# --- AC-6: 초대 적용 ---


async def _invite_bob(harness: DeployHarness) -> dict[str, Any]:
    harness.github_users.directory["bob-ops"] = BOB
    response = await harness.client.post(
        org_path(harness.organization.id, "/invitations"),
        json={"githubLogin": "bob-ops", "role": "DEVELOPER"},
    )
    data: dict[str, Any] = _data(response)
    return data


async def test_invitation_applied_on_me_records_one_member_joined(harness: DeployHarness) -> None:
    bob = await create_user(harness.session_factory, "bob-ops", BOB.id)
    token = await create_session(harness.session_factory, bob, harness.clock.now())
    invitation = await _invite_bob(harness)

    async with make_client(session_token=token) as client:
        first = await client.get("/api/v1/me")
        second = await client.get("/api/v1/me")

    assert (first.status_code, second.status_code) == (200, 200)
    rows = await _rows(harness, AuditLog.action == AuditAction.MEMBER_JOINED)
    assert len(rows) == 1
    row = rows[0]
    assert (row.actor_type, row.actor_user_id) == (ActorType.USER, bob.id)
    assert row.organization_id == harness.organization.id
    assert (row.target_type, row.target_id, row.target_label) == ("user", bob.id, "bob-ops")
    assert row.details == {"role": "DEVELOPER", "invitationId": invitation["id"]}
    assert row.route == "GET /api/v1/me"
    assert row.request_id == first.headers["x-request-id"]


async def test_invitation_applied_on_login_callback_records_one_member_joined(
    harness: DeployHarness,
) -> None:
    await _invite_bob(harness)
    harness.github_users.add_user("code-bob", BOB)

    async with make_client() as client:
        response = await _login(client, code="code-bob")

    assert response.status_code == 302
    rows = await _rows(harness, AuditLog.action == AuditAction.MEMBER_JOINED)
    assert len(rows) == 1
    assert rows[0].organization_id == harness.organization.id
    assert rows[0].route == "GET /api/v1/auth/github/callback"
    # 첫 로그인의 개인 조직 자동 생성은 기록하지 않는다.
    assert await _rows(harness, AuditLog.action == AuditAction.ORGANIZATION_CREATED) == []


async def test_concurrent_me_records_one_member_joined(harness: DeployHarness) -> None:
    bob = await create_user(harness.session_factory, "bob-ops", BOB.id)
    token = await create_session(harness.session_factory, bob, harness.clock.now())
    await _invite_bob(harness)

    async with make_client(session_token=token) as client:
        responses = await asyncio.gather(client.get("/api/v1/me"), client.get("/api/v1/me"))

    assert [response.status_code for response in responses] == [200, 200]
    assert len(await _rows(harness, AuditLog.action == AuditAction.MEMBER_JOINED)) == 1


# --- AC-7: 거부 ---


async def test_agent_deploy_denied_uses_action_name_and_route(harness: DeployHarness) -> None:
    token_row, raw = await create_agent_token(
        harness.session_factory, harness.organization, harness.admin.user, harness.clock.now()
    )
    service_id = uuid.uuid4()
    path = f"{harness.services}/{service_id}/deploy-requests"

    async with make_client(agent_token=raw) as agent:
        response = await agent.post(path, json={"environmentId": str(uuid.uuid4())})

    assert (response.status_code, response.json()["code"]) == (403, "AGENT_READ_ONLY")
    rows = await _rows(harness)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == AuditAction.DEPLOY_REQUESTED
    assert row.route == "POST /api/v1/orgs/{org_id}/services/{service_id}/deploy-requests"
    assert (row.result, row.reason) == (AuditResult.DENIED, "AGENT_READ_ONLY")
    assert (row.actor_type, row.actor_agent_token_id) == (ActorType.AGENT, token_row.id)
    assert (row.target_type, row.target_id, row.target_label) == (None, None, None)
    assert row.path == path


async def test_denied_read_keeps_method_and_route_as_action(harness: DeployHarness) -> None:
    _, raw = await create_agent_token(
        harness.session_factory, harness.organization, harness.admin.user, harness.clock.now()
    )

    async with make_client(agent_token=raw) as agent:
        response = await agent.get(org_path(harness.organization.id, "/members"))

    assert response.status_code == 403
    rows = await _rows(harness)
    assert [row.action for row in rows] == ["GET /api/v1/orgs/{org_id}/members"]
    assert rows[0].route == "GET /api/v1/orgs/{org_id}/members"


# --- AC-8: 에이전트 읽기 ---


async def test_agent_successful_reads_are_recorded(harness: DeployHarness) -> None:
    service = await harness.create_service("shop-api")
    token_row, raw = await create_agent_token(
        harness.session_factory, harness.organization, harness.admin.user, harness.clock.now()
    )
    path = f"{harness.services}/{service['id']}"

    async with make_client(agent_token=raw) as agent:
        detail = await agent.get(path)
        kinds = await agent.get("/api/v1/environment-kinds")
        missing = await agent.get(f"{harness.services}/{uuid.uuid4()}")
        foreign = await agent.get(org_path(uuid.uuid4(), "/services"))

    assert (detail.status_code, kinds.status_code) == (200, 200)
    assert (missing.status_code, foreign.status_code) == (404, 404)
    rows = await _rows(harness, AuditLog.action == AuditAction.RESOURCE_READ)
    assert [row.route for row in rows] == [
        "GET /api/v1/orgs/{org_id}/services/{service_id}",
        "GET /api/v1/environment-kinds",
    ]
    for row in rows:
        assert (row.actor_type, row.actor_agent_token_id) == (ActorType.AGENT, token_row.id)
        assert (row.organization_id, row.result) == (
            harness.organization.id,
            AuditResult.SUCCEEDED,
        )
        assert row.target_type is None
    assert rows[0].path == path
    assert rows[0].request_id == detail.headers["x-request-id"]


async def test_human_reads_are_not_recorded(harness: DeployHarness) -> None:
    response = await harness.client.get(harness.services)

    assert response.status_code == 200
    assert await _rows(harness) == []


async def test_agent_read_survives_recording_failure(
    harness: DeployHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, raw = await create_agent_token(
        harness.session_factory, harness.organization, harness.admin.user, harness.clock.now()
    )

    def fail(self: AuditRecorder, **values: Any) -> None:
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(AuditRecorder, "record", fail)
    async with make_client(agent_token=raw) as agent:
        response = await agent.get(harness.services)
    monkeypatch.undo()

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert await _rows(harness) == []


# --- AC-9: 조회 ---


def _audit_path(harness: DeployHarness) -> str:
    return org_path(harness.organization.id, "/audit-logs")


async def _search(harness: DeployHarness, **params: Any) -> list[dict[str, Any]]:
    response = await harness.client.get(_audit_path(harness), params=params)
    items: list[dict[str, Any]] = _data(response)["items"]
    return items


async def test_audit_logs_access_by_role(harness: DeployHarness) -> None:
    viewer = await create_member(
        harness.session_factory,
        harness.organization,
        OrganizationRole.VIEWER,
        harness.clock.now(),
        "vic",
    )
    other = await create_organization(harness.session_factory, "acme-co")
    outsider = await create_member(
        harness.session_factory, other, OrganizationRole.ADMIN, harness.clock.now(), "acme-admin"
    )
    _, raw = await create_agent_token(
        harness.session_factory, harness.organization, harness.admin.user, harness.clock.now()
    )

    async with make_client(session_token=viewer.session_token) as client:
        by_viewer = await client.get(_audit_path(harness))
    async with make_client(agent_token=raw) as client:
        by_agent = await client.get(_audit_path(harness))
        read_lines = await client.get(_audit_path(harness), params={"action": "RESOURCE_READ"})
    async with make_client(session_token=outsider.session_token) as client:
        by_outsider = await client.get(_audit_path(harness))
    async with make_client() as client:
        anonymous = await client.get(_audit_path(harness))

    assert (by_viewer.status_code, by_agent.status_code) == (200, 200)
    assert (by_outsider.status_code, by_outsider.json()["code"]) == (404, "ORGANIZATION_NOT_FOUND")
    assert anonymous.status_code == 401
    # 조회 API 자체를 에이전트가 읽어도 같은 규칙으로 남는다.
    assert _data(read_lines)["total"] == 1


async def test_audit_logs_response_shape_and_newest_first_with_same_timestamp(
    harness: DeployHarness,
) -> None:
    first = await harness.create_service("shop-api")
    second = await harness.create_service("pay-api")

    response = await harness.client.get(_audit_path(harness))
    page = _data(response)
    items = page["items"]

    assert (page["total"], page["page"], page["size"]) == (2, 0, 20)
    # 시계가 멈춰 있어 created_at 이 같아도 나중 줄이 위다(uuid 는 정렬 보조로 못 쓴다).
    assert items[0]["at"] == items[1]["at"]
    assert [item["target"]["id"] for item in items] == [second["id"], first["id"]]
    latest = items[0]
    assert set(latest) == ITEM_KEYS
    assert set(latest["actor"]) == ACTOR_KEYS
    assert latest["actor"] == {
        "type": "USER",
        "userId": str(harness.admin.user.id),
        "githubLogin": harness.admin.user.github_login,
        "agentTokenId": None,
        "agentTokenName": None,
        "component": None,
    }
    assert latest["target"] == {"type": "service", "id": second["id"], "label": "pay-api"}
    assert latest["action"] == "SERVICE_CREATED"
    assert latest["result"] == "SUCCEEDED"
    assert (latest["reason"], latest["details"]) == (None, {})
    assert latest["route"] == "POST /api/v1/orgs/{org_id}/services"


async def test_action_just_made_is_on_top(harness: DeployHarness) -> None:
    await harness.create_service("shop-api")

    created = await harness.client.post(
        harness.services,
        json={"name": "pay-api", "repoUrl": "https://github.com/team-echo/pay-api"},
    )

    top = (await _search(harness, size=1))[0]
    assert top["requestId"] == created.headers["x-request-id"]
    assert top["target"]["label"] == "pay-api"


async def test_audit_logs_filters(harness: DeployHarness) -> None:
    service = await harness.create_service("shop-api")
    harness.clock.advance(10)
    middle_time = harness.clock.now()
    aws = await harness.add_environment("aws-seoul")
    deployed = await deploy_and_finish(harness, service["id"], aws.id, branch="main")
    harness.clock.advance(10)
    token_row, raw = await create_agent_token(
        harness.session_factory, harness.organization, harness.admin.user, harness.clock.now()
    )
    async with make_client(agent_token=raw) as agent:
        await agent.get(harness.services)
        await agent.delete(f"{harness.services}/{service['id']}")

    async def actions(**params: Any) -> list[str]:
        return [item["action"] for item in await _search(harness, **params)]

    everything = await actions()
    assert everything == [
        "SERVICE_DELETE_REQUESTED",
        "RESOURCE_READ",
        "DEPLOY_FINISHED",
        "DEPLOY_REQUESTED",
        "SERVICE_CREATED",
    ]
    assert await actions(actorType="USER") == ["DEPLOY_REQUESTED", "SERVICE_CREATED"]
    assert await actions(actorType="AGENT") == ["SERVICE_DELETE_REQUESTED", "RESOURCE_READ"]
    assert await actions(actorType="SYSTEM") == ["DEPLOY_FINISHED"]
    assert await actions(result="DENIED") == ["SERVICE_DELETE_REQUESTED"]
    assert await actions(action="DEPLOY_FINISHED") == ["DEPLOY_FINISHED"]
    assert await actions(targetType="service") == ["SERVICE_CREATED"]
    assert await actions(targetId=deployed["id"]) == ["DEPLOY_FINISHED", "DEPLOY_REQUESTED"]
    assert await actions(includeReads="false") == [
        "SERVICE_DELETE_REQUESTED",
        "DEPLOY_FINISHED",
        "DEPLOY_REQUESTED",
        "SERVICE_CREATED",
    ]
    assert await actions(to=middle_time.isoformat()) == ["SERVICE_CREATED"]
    assert await actions(
        **{"from": middle_time.isoformat(), "to": (middle_time + timedelta(seconds=1)).isoformat()}
    ) == ["DEPLOY_FINISHED", "DEPLOY_REQUESTED"]
    # q: 행동 이름·대상 이름·사람 아이디·토큰 이름, 대소문자 무시. %·_ 는 글자 그대로 찾는다.
    assert await actions(q="deploy_fin") == ["DEPLOY_FINISHED"]
    assert await actions(q="SHOP-API · AWS") == ["DEPLOY_FINISHED", "DEPLOY_REQUESTED"]
    assert await actions(q=harness.admin.user.github_login.upper()) == [
        "DEPLOY_REQUESTED",
        "SERVICE_CREATED",
    ]
    assert await actions(q=token_row.name.upper()) == ["SERVICE_DELETE_REQUESTED", "RESOURCE_READ"]
    assert await actions(q="%") == []
    # 페이지
    paged = _data(await harness.client.get(_audit_path(harness), params={"size": 2, "page": 1}))
    assert (paged["total"], paged["page"], paged["size"]) == (5, 1, 2)
    assert [item["action"] for item in paged["items"]] == ["DEPLOY_FINISHED", "DEPLOY_REQUESTED"]


@pytest.mark.parametrize(
    "params",
    [
        {"size": 101},
        {"size": 0},
        {"page": -1},
        {"actorType": "ROBOT"},
        {"result": "MAYBE"},
        {"targetId": "not-a-uuid"},
        {"from": "not-a-time"},
        {"from": "2026-11-04T00:00:00"},
        {"q": ""},
        {"q": "x" * 101},
        {"includeReads": "perhaps"},
        {"from": "2026-11-04T00:00:00Z", "to": "2026-11-04T00:00:00Z"},
        {"from": "2026-11-05T00:00:00Z", "to": "2026-11-04T00:00:00Z"},
    ],
)
async def test_audit_logs_invalid_query_is_422(
    harness: DeployHarness, params: dict[str, Any]
) -> None:
    response = await harness.client.get(_audit_path(harness), params=params)

    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_ERROR")


async def test_audit_logs_reads_old_denial_rows_and_hides_null_organization(
    harness: DeployHarness,
) -> None:
    async with harness.session_factory() as session, session.begin():
        for organization_id in (harness.organization.id, None):
            await session.execute(
                text(
                    "INSERT INTO audit_logs "
                    "(organization_id, actor_type, actor_user_id, action, path, reason) "
                    "VALUES (:org, 'USER', :user, 'POST /api/v1/orgs/{org_id}/services', "
                    "'/api/v1/orgs/x/services', 'ROLE_FORBIDDEN')"
                ),
                {"org": organization_id, "user": harness.admin.user.id},
            )

    items = await _search(harness)

    assert len(items) == 1
    item = items[0]
    assert item["action"] == "POST /api/v1/orgs/{org_id}/services"
    assert (item["result"], item["reason"]) == ("DENIED", "ROLE_FORBIDDEN")
    assert (item["route"], item["target"], item["details"]) == (None, None, {})


async def test_audit_logs_show_revoked_token_name_and_hide_other_organizations(
    harness: DeployHarness,
) -> None:
    token_row, raw = await create_agent_token(
        harness.session_factory, harness.organization, harness.admin.user, harness.clock.now()
    )
    async with make_client(agent_token=raw) as agent:
        await agent.get(harness.services)
    await harness.client.delete(org_path(harness.organization.id, f"/agent-tokens/{token_row.id}"))
    other = await create_organization(harness.session_factory, "acme-co")
    other_admin = await create_member(
        harness.session_factory, other, OrganizationRole.ADMIN, harness.clock.now(), "acme-admin"
    )
    async with make_client(session_token=other_admin.session_token) as client:
        await client.post(
            org_path(other.id, "/services"),
            json={"name": "acme-api", "repoUrl": "https://github.com/acme/acme-api"},
        )

    items = await _search(harness, includeReads="true", actorType="AGENT")

    assert [item["actor"]["agentTokenName"] for item in items] == [token_row.name]
    assert all(item["target"] is None for item in items)
    assert "acme-api" not in json.dumps(await _search(harness))


def test_openapi_documents_audit_logs_route() -> None:
    operation = app.openapi()["paths"]["/api/v1/orgs/{org_id}/audit-logs"]["get"]

    names = {parameter["name"] for parameter in operation["parameters"]}
    assert {
        "actorType",
        "result",
        "action",
        "targetType",
        "targetId",
        "from",
        "to",
        "q",
        "includeReads",
        "page",
        "size",
    } <= names


# --- AC-12: 가장 긴 라벨 ---


async def _environment_with_own_domain(
    harness: DeployHarness, name: str, label: str
) -> Environment:
    kind, config = ENVIRONMENT_CONFIGS["aws-seoul"]
    environment = Environment(
        organization_id=harness.organization.id,
        name=name,
        kind=kind,
        config={**config, "ingress_domain": f"{label}.{harness.settings.public_base_domain}"},
        secret_ref=secret_ref_for("aws-seoul"),
    )
    environment.record_connection_check(harness.clock.now(), None)
    async with harness.session_factory() as session, session.begin():
        session.add(environment)
    return environment


async def test_longest_migration_label_is_stored_whole(harness: DeployHarness) -> None:
    service_name, source_name, target_name = "s" * 63, "a" * 63, "b" * 63
    service = await harness.create_service(service_name)
    # 이름이 길면 환경별 주소가 잘려 겹치므로 환경마다 하위 도메인을 준다.
    source = await _environment_with_own_domain(harness, source_name, "aws-a")
    target = await _environment_with_own_domain(harness, target_name, "aws-b")
    await deploy_and_finish(harness, service["id"], source.id, branch="main")
    await deploy_and_finish(harness, service["id"], target.id, branch="main")
    response = await harness.client.post(
        f"{harness.services}/{service['id']}/migration-plans",
        json={"targetEnvironmentId": str(target.id)},
    )

    assert response.status_code == 201, response.text
    rows = await _target_rows(harness, _data(response)["id"])
    label = rows[0].target_label
    assert label == f"{service_name} · {source_name} → {target_name}"
    assert len(label) == 195
