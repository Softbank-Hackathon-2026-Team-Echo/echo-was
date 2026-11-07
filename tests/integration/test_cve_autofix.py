"""실제 PostgreSQL 저장·조직 격리·취소 회귀. DB 미준비면 기존 conftest가 skip한다."""

import uuid

from app.models import Finding
from app.repositories.cve_autofix_repository import CveAutofixRepository
from app.services.cve_watch_packages import DependencyPackage
from tests.integration.migration_support import Migration
from tests.integration.test_cve_watch import _path, _seed


async def _finding(migration: Migration) -> uuid.UUID:
    await _seed(migration, [DependencyPackage("npm", "minimist", "1.2.5")])
    response = await migration.author_client.post(
        _path(migration, "/cve-watch:inject"),
        json={
            "advisory": {
                "id": "GHSA-xvch-5gv4-984h",
                "aliases": ["CVE-2021-44906"],
                "ecosystem": "npm",
                "package": "minimist",
                "introduced": "1.0.0",
                "fixed": "1.2.6",
            },
            "serviceId": migration.service_id,
        },
    )
    assert response.status_code == 200, response.text
    return uuid.UUID(response.json()["data"]["matches"][0]["findingId"])


async def test_autofix_repository_isolates_tenant_and_persists_receipt(
    migration: Migration,
) -> None:
    fid = await _finding(migration)
    factory = migration.harness.session_factory
    org = migration.harness.organization.id
    async with factory() as session, session.begin():
        repository = CveAutofixRepository(session)
        assert await repository.find_finding(uuid.uuid4(), fid) is None
        finding = await repository.find_finding(org, fid)
        assert finding is not None
        finding.remediation = {
            **finding.remediation,
            "autofix": {"status": "FAILED", "failure_code": "TEST"},
        }
    async with factory() as session:
        finding = await session.get(Finding, fid)
        assert finding.remediation["autofix"]["status"] == "FAILED"
        assert finding.mode == "SIMULATED" and finding.result == "FAIL"


async def test_autofix_cancel_http_persists_even_with_flags_off(migration: Migration) -> None:
    fid = await _finding(migration)
    factory = migration.harness.session_factory
    receipt = {
        "run_id": str(uuid.uuid4()),
        "source_finding_id": str(fid),
        "status": "RUNNING",
        "cve_id": "CVE-2021-44906",
        "input_hash": "sha256:" + "a" * 64,
    }
    async with factory() as session, session.begin():
        finding = await session.get(Finding, fid)
        finding.remediation = {**finding.remediation, "autofix": receipt}
    path = _path(migration, f"/findings/{fid}/diagnosis")
    response = await migration.author_client.post(path, json={"operation": "CANCEL"})
    assert response.status_code == 201, response.text
    assert response.json()["data"]["status"] == "CANCELLED"
    async with factory() as session:
        finding = await session.get(Finding, fid)
        assert finding.remediation["autofix"]["status"] == "CANCELLED"


async def test_autofix_http_rejects_arbitrary_repo_and_commands_in_body(
    migration: Migration,
) -> None:
    fid = await _finding(migration)
    response = await migration.author_client.post(
        _path(migration, f"/findings/{fid}/diagnosis"),
        json={"repository": "arbitrary/repo", "command": "echo should-not-run"},
    )
    assert response.status_code == 422


async def test_autofix_repository_serializes_duplicate_reservations(migration: Migration) -> None:
    import asyncio

    fid = await _finding(migration)
    factory = migration.harness.session_factory
    org = migration.harness.organization.id
    entered = asyncio.Event()

    async def competing_read():
        async with factory() as session, session.begin():
            entered.set()
            row = await CveAutofixRepository(session).find_finding(org, fid)
            return row.remediation["autofix"]

    task = None
    try:
        async with factory() as session, session.begin():
            row = await CveAutofixRepository(session).find_finding(org, fid)
            task = asyncio.create_task(competing_read())
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.sleep(0.05)
            assert not task.done()
            row.remediation = {**row.remediation, "autofix": {"run_id": "reserved-once"}}
        assert await asyncio.wait_for(task, 2) == {"run_id": "reserved-once"}
    finally:
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_autofix_cancel_rejects_viewer_and_other_tenant(migration: Migration) -> None:
    from app.enums import OrganizationRole
    from tests.integration.auth_support import create_member, make_client

    fid = await _finding(migration)
    harness = migration.harness
    viewer = await create_member(
        harness.session_factory,
        harness.organization,
        OrganizationRole.VIEWER,
        harness.clock.now(),
        "cve-viewer",
    )
    path = _path(migration, f"/findings/{fid}/diagnosis")
    async with make_client(session_token=viewer.session_token) as client:
        response = await client.post(path, json={"operation": "CANCEL"})
    assert response.status_code == 403
    assert response.json()["code"] == "ROLE_FORBIDDEN"
    response = await migration.author_client.post(
        path.replace(str(harness.organization.id), str(uuid.uuid4())), json={"operation": "CANCEL"}
    )
    assert response.status_code == 404
