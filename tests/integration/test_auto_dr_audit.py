"""사전동의·철회의 거부 감사와 tenant 경계를 실제 API/격리 DB로 검증한다."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.audit.actions import WRITE_ROUTE_ACTIONS, AuditAction
from app.enums import ActorType, AuditResult, OrganizationRole
from app.models import AuditLog, Job, MigrationPlan
from tests.integration.auth_support import (
    create_agent_token,
    create_member,
    create_organization,
    make_client,
    org_path,
)
from tests.integration.deploy_harness import DeployHarness
from tests.integration.migration_support import Migration


@pytest.mark.parametrize("revoke", [False, True], ids=["consent", "revoke"])
@pytest.mark.parametrize("actor", ["viewer", "developer", "agent", "outsider", "anonymous"])
async def test_consent_routes_denial_audit_actor_tenant(
    harness: DeployHarness, actor: str, revoke: bool
) -> None:
    suffix = "/disasters/auto-dr/consents"
    template = f"/api/v1/orgs/{{org_id}}{suffix}"
    if revoke:
        suffix += f"/{uuid.uuid4()}/revoke"
        template += "/{consent_id}/revoke"
    path = org_path(harness.organization.id, suffix)
    user_id = None
    token_id = None
    credentials: dict[str, str] = {}
    if actor == "agent":
        token, raw = await create_agent_token(
            harness.session_factory, harness.organization, harness.admin.user, harness.clock.now()
        )
        credentials["agent_token"] = raw
        token_id = token.id
    elif actor != "anonymous":
        organization = harness.organization
        role = OrganizationRole.VIEWER if actor == "viewer" else OrganizationRole.DEVELOPER
        if actor == "outsider":
            organization = await create_organization(harness.session_factory, "foreign")
            role = OrganizationRole.ADMIN
        member = await create_member(
            harness.session_factory, organization, role, harness.clock.now(), actor
        )
        credentials["session_token"] = member.session_token
        user_id = member.user.id
    async with make_client(**credentials) as client:
        response = await client.post(path, json={})
    async with harness.session_factory() as session:
        rows = list(await session.scalars(select(AuditLog)))
    if actor in {"anonymous", "outsider"}:
        assert response.status_code == (401 if actor == "anonymous" else 404)
        assert rows == []
        return
    assert response.status_code == 403, response.text
    (row,) = rows
    assert row.action == WRITE_ROUTE_ACTIONS[("POST", template)]
    assert row.result == AuditResult.DENIED
    assert row.reason == ("AGENT_READ_ONLY" if actor == "agent" else "ROLE_FORBIDDEN")
    assert row.organization_id == harness.organization.id
    assert row.actor_type == (ActorType.AGENT if actor == "agent" else ActorType.USER)
    assert (row.actor_user_id, row.actor_agent_token_id, row.component) == (user_id, token_id, None)
    assert (row.route, row.path) == (f"POST {template}", path)
    assert row.request_id == response.headers["x-request-id"]
    assert row.details == {}


async def test_consent_foreign_resources_and_conflicts_have_no_success_audit(
    migration: Migration,
) -> None:
    harness = migration.harness
    base = org_path(harness.organization.id, "/disasters/auto-dr/consents")
    body = {
        "serviceId": migration.service_id,
        "sourceEnvironmentId": str(migration.aws.id),
        "targetEnvironmentId": str(migration.gcp.id),
        "expiresAt": (harness.clock.now() + timedelta(hours=1)).isoformat(),
        "acknowledgeAutomaticExecution": True,
    }
    created = await harness.client.post(base, json=body)
    assert created.status_code == 201, created.text
    consent_id = created.json()["data"]["consentId"]
    other = await create_organization(harness.session_factory, "foreign")
    other_admin = await create_member(
        harness.session_factory, other, OrganizationRole.ADMIN, harness.clock.now(), "foreign-admin"
    )
    async with make_client(session_token=other_admin.session_token) as client:
        foreign_base = org_path(other.id, "/disasters/auto-dr/consents")
        foreign_create = await client.post(foreign_base, json=body)
        foreign_revoke = await client.post(f"{foreign_base}/{consent_id}/revoke")
    assert foreign_create.status_code == 404, foreign_create.text
    assert (foreign_revoke.status_code, foreign_revoke.json()["code"]) == (409, "AUTO_DR_REFUSED")
    assert foreign_revoke.json()["details"] == [
        {"field": "automatic", "reason": "CONSENT_NOT_FOUND"}
    ]
    invalid_expiry = await harness.client.post(
        base, json={**body, "expiresAt": harness.clock.now().isoformat()}
    )
    assert (invalid_expiry.status_code, invalid_expiry.json()["code"]) == (409, "AUTO_DR_REFUSED")
    async with harness.session_factory() as session:
        rows = list(
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.action.in_(
                        [
                            AuditAction.DISASTER_AUTO_DR_CONSENTED,
                            AuditAction.DISASTER_AUTO_DR_REVOKED,
                        ]
                    )
                )
            )
        )
        jobs = list(await session.scalars(select(Job).where(Job.kind == "MIGRATION_PLAN")))
        plans = list(await session.scalars(select(MigrationPlan)))
    assert [str(row.id) for row in rows] == [consent_id]
    assert rows[0].organization_id == harness.organization.id
    assert rows[0].actor_user_id == harness.admin.user.id
    assert rows[0].result == AuditResult.SUCCEEDED
    assert jobs == plans == []
