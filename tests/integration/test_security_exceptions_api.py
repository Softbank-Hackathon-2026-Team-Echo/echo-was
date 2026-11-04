"""기한부 예외 API: 요청·승인·철회·조회(SPEC sec-3-2).

migration 픽스처: shop-api 가 aws-seoul(서비스 중)·gcp-seoul(예비)에 있고, 개발자 A(dev-a)가
요청하고 개발자 B(dev-b)가 승인한다. 시각은 FakeClock 이다.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

from httpx import AsyncClient, Response
from sqlalchemy import select

from app.enums import OrganizationRole, SecurityControlId
from app.models import AuditLog, SecurityException
from tests.integration.auth_support import create_member, create_organization, make_client, org_path
from tests.integration.migration_support import Migration
from tests.integration.security_gate_support import (
    add_precheck,
    enable_gate,
    record_passing_evidence,
    target_digest,
)

_C = SecurityControlId
REASON = "D04~D07 결정 전, 시연은 쓰기 정지 구간에서 수동 확인"


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _path(migration: Migration, suffix: str = "") -> str:
    return org_path(migration.harness.organization.id, f"/security/exceptions{suffix}")


def _error(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["code"]


def _details(response: Response) -> set[tuple[str, str]]:
    return {(item["field"], item["reason"]) for item in response.json()["details"]}


def _body(migration: Migration, control: SecurityControlId = _C.DB_1, **changes: Any) -> dict:
    body: dict[str, Any] = {
        "controlId": control.value,
        "serviceId": migration.service_id,
        "environmentId": str(migration.gcp.id),
        "reason": REASON,
        "expiresAt": _iso(migration.harness.clock.now() + timedelta(days=1)),
    }
    body.update(changes)
    return {key: value for key, value in body.items() if value is not None}


async def _create(
    migration: Migration, client: AsyncClient | None = None, **body: Any
) -> dict[str, Any]:
    response = await (client or migration.author_client).post(_path(migration), json=body)
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def _act(
    migration: Migration, exception_id: str, action: str, client: AsyncClient | None = None
) -> Response:
    return await (client or migration.approver_client).post(
        _path(migration, f"/{exception_id}/{action}")
    )


async def test_create_exception_up_to_exactly_seven_days_uses_app_clock(
    migration: Migration,
) -> None:
    now = migration.harness.clock.now()

    created = await _create(migration, **_body(migration, expiresAt=_iso(now + timedelta(days=7))))
    async with migration.harness.session_factory() as session:
        row = await session.get(SecurityException, uuid.UUID(created["id"]))

    assert created["status"] == "PENDING"
    assert created["controlId"] == "C-DB-1"
    assert (created["serviceId"], created["environmentId"]) == (
        migration.service_id,
        str(migration.gcp.id),
    )
    assert created["requestedBy"]["login"] == "dev-a"
    assert created["approvedBy"] is None and created["revokedBy"] is None
    assert _parse(created["createdAt"]) == now
    assert _parse(created["expiresAt"]) == now + timedelta(days=7)
    # 만든 시각을 앱 시계로 넣어 DB CHECK(7일 이하)도 같은 기준으로 통과한다.
    assert row is not None and row.created_at == now


async def test_create_exception_rejects_expiry_past_seven_days_or_not_in_future(
    migration: Migration,
) -> None:
    now = migration.harness.clock.now()
    client = migration.author_client

    too_late = await client.post(
        _path(migration),
        json=_body(migration, expiresAt=_iso(now + timedelta(days=7, seconds=1))),
    )
    now_expiry = await client.post(_path(migration), json=_body(migration, expiresAt=_iso(now)))
    naive = await client.post(
        _path(migration), json=_body(migration, expiresAt="2026-11-05T03:00:00")
    )
    blank_reason = await client.post(_path(migration), json=_body(migration, reason="   "))

    assert _error(too_late) == (422, "VALIDATION_ERROR")
    assert _details(too_late) == {("expiresAt", "MAX_7_DAYS")}
    assert _details(now_expiry) == {("expiresAt", "NOT_AFTER_NOW")}
    assert _error(naive) == (422, "VALIDATION_ERROR")
    assert _error(blank_reason) == (422, "VALIDATION_ERROR")


async def test_create_exception_requires_exactly_the_control_scope(migration: Migration) -> None:
    client = migration.author_client
    digest = await target_digest(migration)

    missing = await client.post(_path(migration), json=_body(migration, environmentId=None))
    extra = await client.post(_path(migration), json=_body(migration, imageDigest=digest))
    image_without_service = await _create(
        migration,
        **_body(migration, _C.IMG_1, serviceId=None, environmentId=None, imageDigest=digest),
    )
    organization_wide = await _create(
        migration, **_body(migration, _C.SEC_1, serviceId=None, environmentId=None)
    )
    bad_digest = await client.post(
        _path(migration),
        json=_body(migration, _C.IMG_1, serviceId=None, environmentId=None, imageDigest="x"),
    )

    assert _details(missing) == {("environmentId", "REQUIRED_FOR_CONTROL")}
    assert _details(extra) == {("imageDigest", "NOT_ALLOWED_FOR_CONTROL")}
    assert image_without_service["imageDigest"] == digest
    assert organization_wide["serviceId"] is None
    assert _error(bad_digest) == (422, "VALIDATION_ERROR")


async def test_create_exception_for_unknown_service_or_environment_is_not_found(
    migration: Migration,
) -> None:
    client = migration.author_client

    service = await client.post(
        _path(migration), json=_body(migration, serviceId=str(uuid.uuid4()))
    )
    environment = await client.post(
        _path(migration), json=_body(migration, environmentId=str(uuid.uuid4()))
    )

    assert _error(service) == (404, "SERVICE_NOT_FOUND")
    assert _error(environment) == (404, "ENVIRONMENT_NOT_FOUND")


async def test_approve_by_other_developer_and_self_approval_follows_switch(
    migration: Migration,
) -> None:
    harness = migration.harness
    first = await _create(migration, **_body(migration))
    second = await _create(migration, **_body(migration, _C.DST_1))

    self_approve = await _act(migration, first["id"], "approve", client=migration.author_client)
    approved = await _act(migration, first["id"], "approve")
    again = await _act(migration, first["id"], "approve")
    switch = await harness.client.patch(
        org_path(harness.organization.id, "/settings"), json={"allowSelfApproval": True}
    )
    self_approved = await _act(migration, second["id"], "approve", client=migration.author_client)
    async with harness.session_factory() as session:
        reasons = list(await session.scalars(select(AuditLog.reason)))

    assert _error(self_approve) == (403, "SELF_APPROVAL_FORBIDDEN")
    assert approved.status_code == 200, approved.text
    data = approved.json()["data"]
    assert data["status"] == "ACTIVE"
    assert data["approvedBy"]["login"] == "dev-b"
    assert _parse(data["approvedAt"]) == harness.clock.now()
    assert _error(again) == (409, "SECURITY_EXCEPTION_NOT_PENDING")
    assert switch.status_code == 200
    assert self_approved.status_code == 200, self_approved.text
    assert self_approved.json()["data"]["approvedBy"]["login"] == "dev-a"
    assert reasons == ["SELF_APPROVAL_FORBIDDEN"]


async def test_expired_or_revoked_exception_cannot_be_approved_or_revoked_again(
    migration: Migration,
) -> None:
    clock = migration.harness.clock
    short = await _create(
        migration, **_body(migration, expiresAt=_iso(clock.now() + timedelta(minutes=10)))
    )
    pending = await _create(migration, **_body(migration, _C.DST_1))
    active = await _create(migration, **_body(migration, _C.AI_1))
    await _act(migration, active["id"], "approve")

    revoked_pending = await _act(migration, pending["id"], "revoke", client=migration.author_client)
    revoked_active = await _act(migration, active["id"], "revoke", client=migration.author_client)
    revoke_twice = await _act(migration, active["id"], "revoke")
    approve_revoked = await _act(migration, pending["id"], "approve")
    clock.advance(10 * 60)
    approve_expired = await _act(migration, short["id"], "approve")
    revoke_expired = await _act(migration, short["id"], "revoke")
    expired = await migration.approver_client.get(_path(migration, f"/{short['id']}"))

    assert revoked_pending.json()["data"]["status"] == "REVOKED"
    assert revoked_pending.json()["data"]["approvedBy"] is None
    revoked = revoked_active.json()["data"]
    assert (revoked["status"], revoked["revokedBy"]["login"]) == ("REVOKED", "dev-a")
    assert revoked["approvedBy"]["login"] == "dev-b"
    assert _error(revoke_twice) == (409, "SECURITY_EXCEPTION_CLOSED")
    assert _error(approve_revoked) == (409, "SECURITY_EXCEPTION_NOT_PENDING")
    assert _error(approve_expired) == (409, "SECURITY_EXCEPTION_NOT_PENDING")
    assert _error(revoke_expired) == (409, "SECURITY_EXCEPTION_CLOSED")
    assert expired.json()["data"]["status"] == "EXPIRED"


async def test_search_exceptions_filters_and_stays_in_organization(
    migration: Migration,
) -> None:
    harness = migration.harness
    # 목록은 만든 시각 역순이다. 같은 시각이 되지 않게 시계를 1초씩 민다.
    db = await _create(migration, **_body(migration))
    harness.clock.advance(1)
    dst = await _create(migration, **_body(migration, _C.DST_1))
    await _act(migration, dst["id"], "approve")
    harness.clock.advance(1)
    organization_wide = await _create(
        migration, **_body(migration, _C.SEC_1, serviceId=None, environmentId=None)
    )
    other = await create_organization(harness.session_factory, "other-org")
    outsider = await create_member(
        harness.session_factory, other, OrganizationRole.DEVELOPER, harness.clock.now(), "dev-x"
    )
    async with make_client(session_token=outsider.session_token) as outsider_client:
        outsider_get = await outsider_client.get(
            org_path(other.id, f"/security/exceptions/{db['id']}")
        )
        outsider_list = await outsider_client.get(org_path(other.id, "/security/exceptions"))

    async def ids(**params: Any) -> list[str]:
        response = await migration.approver_client.get(_path(migration), params=params)
        assert response.status_code == 200, response.text
        return [item["id"] for item in response.json()["data"]["items"]]

    assert await ids() == [organization_wide["id"], dst["id"], db["id"]]
    assert await ids(status="ACTIVE") == [dst["id"]]
    assert await ids(status=["PENDING", "ACTIVE"], controlId="C-DB-1") == [db["id"]]
    assert await ids(serviceId=migration.service_id) == [dst["id"], db["id"]]
    assert await ids(environmentId=str(migration.aws.id)) == []
    assert _error(outsider_get) == (404, "SECURITY_EXCEPTION_NOT_FOUND")
    assert outsider_list.json()["data"]["total"] == 0


async def test_api_exceptions_let_the_security_gate_pass_db_and_destination(
    migration: Migration,
) -> None:
    # 게이트를 켜고 다른 필수 통제 증거를 넣은 뒤,
    # C-DB-1·C-DST-1 은 API 로 만든 예외로만 통과시킨다.
    enable_gate(migration)
    await record_passing_evidence(migration)
    await add_precheck(migration)
    plan = await migration.create_plan()
    blocked = await migration.act(plan["id"], "approve")

    for control in (_C.DB_1, _C.DST_1):
        created = await _create(migration, **_body(migration, control))
        assert (await _act(migration, created["id"], "approve")).status_code == 200
    approved = await migration.act(plan["id"], "approve")

    assert _error(blocked) == (409, "SECURITY_GATE_BLOCKED")
    assert approved.status_code == 202, approved.text
