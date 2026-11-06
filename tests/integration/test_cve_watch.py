"""CVE 감시 통합 시험(SPEC sec-6-cve-watch AC-1~AC-9).

가짜 OSV 는 httpx.MockTransport 로 실제 `OsvAdvisoryFeed` 에 끼운다(querybatch → vulns/{id}).
"""

import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text

from app.audit.actions import AuditAction
from app.clients.osv import OsvAdvisoryFeed
from app.dependencies import get_advisory_feed
from app.enums import (
    ActorType,
    DependencySnapshotSource,
    OrganizationRole,
    SecurityControlId,
    SecurityFindingMode,
    SecurityFindingSource,
    SecurityResult,
)
from app.main import app
from app.models import AuditLog, Finding, ServiceDependencySnapshot, ServiceEnvironment
from app.services.cve_watch_loop import CveWatchLoop
from app.services.cve_watch_packages import DependencyPackage
from app.services.cve_watch_service import SIMULATED_ACTIVE_LIMIT, CveWatchService
from app.services.security_finding_service import SecurityFindingService
from app.services.security_normalizers import normalize_trivy_image
from app.services.security_rules import SecurityScope
from tests.integration.auth_support import create_member, make_client, org_path
from tests.integration.migration_support import Migration
from tests.integration.security_gate_support import prepare_passing_gate
from tests.integration.test_security_scan_collect import (
    ScanningBuildExecutor,
    _deploy_and_finish,
    _scan,
    _trivy,
)

STARLETTE_GHSA = "GHSA-test-star-0001"
REQUESTS_GHSA = "GHSA-test-reqs-0001"
URLLIB3_GHSA = "GHSA-test-url3-0001"
PACKAGES = [
    DependencyPackage("PyPI", "starlette", "0.47.1"),
    DependencyPackage("PyPI", "urllib3", "2.7.0"),
    DependencyPackage("PyPI", "requests", "2.31.0"),
    DependencyPackage("Debian:12", "openssl", "3.0.11-1~deb12u2"),
]
OSV_VULNS: dict[str, dict[str, Any]] = {
    STARLETTE_GHSA: {
        "id": STARLETTE_GHSA,
        "aliases": ["CVE-2099-0001"],
        "summary": "starlette multipart DoS",
        "database_specific": {"severity": "HIGH"},
        "affected": [
            {
                "package": {"name": "starlette", "ecosystem": "PyPI"},
                "ranges": [
                    {"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "0.49.1"}]}
                ],
            }
        ],
    },
    URLLIB3_GHSA: {
        "id": URLLIB3_GHSA,
        "database_specific": {"severity": "MODERATE"},
        "affected": [],
    },
    REQUESTS_GHSA: {
        "id": REQUESTS_GHSA,
        "aliases": ["CVE-2026-11111"],
        "database_specific": {"severity": "CRITICAL"},
        "affected": [],
    },
}
OSV_IDS = {"starlette": [STARLETTE_GHSA], "urllib3": [URLLIB3_GHSA], "requests": [REQUESTS_GHSA]}


def _osv_handler(
    calls: list[str], *, fail: bool = False
) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if fail:
            return httpx.Response(503, json={"message": "unavailable"})
        if request.url.path == "/v1/querybatch":
            queries = json.loads(request.content)["queries"]
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"vulns": [{"id": vid} for vid in OSV_IDS.get(q["package"]["name"], [])]}
                        for q in queries
                    ]
                },
            )
        vid = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=OSV_VULNS[vid])

    return handle


def _use_fake_osv(fail: bool = False) -> list[str]:
    calls: list[str] = []
    feed = OsvAdvisoryFeed(
        "https://osv.test", 5.0, 50, transport=httpx.MockTransport(_osv_handler(calls, fail=fail))
    )
    app.dependency_overrides[get_advisory_feed] = lambda: feed
    return calls


async def _digest(migration: Migration) -> str:
    async with migration.harness.session_factory() as session:
        rows = await session.scalars(
            select(ServiceEnvironment).where(
                ServiceEnvironment.service_id == uuid.UUID(migration.service_id)
            )
        )
        connections = list(rows)
        from app.models import Deployment

        digests = {
            (await session.get(Deployment, c.current_deployment_id)).image_digest  # type: ignore[union-attr]
            for c in connections
        }
    assert len(digests) == 1
    return digests.pop()


async def _seed(migration: Migration, packages: list[DependencyPackage] = PACKAGES) -> str:
    digest = await _digest(migration)
    harness = migration.harness
    async with harness.session_factory() as session:
        await CveWatchService(session, harness.clock).record_snapshot(
            organization_id=harness.organization.id,
            service_id=uuid.UUID(migration.service_id),
            image_digest=digest,
            source=DependencySnapshotSource.BUILD_SCAN,
            packages=packages,
            source_ref="test",
            collected_at=harness.clock.now(),
        )
    return digest


def _path(migration: Migration, suffix: str) -> str:
    return org_path(migration.harness.organization.id, f"/security{suffix}")


async def _cve_findings(migration: Migration) -> list[Finding]:
    async with migration.harness.session_factory() as session:
        rows = await session.scalars(
            select(Finding)
            .where(Finding.source == SecurityFindingSource.CVE_WATCH)
            .order_by(Finding.environment_id)
        )
        return list(rows)


async def _audit(migration: Migration, action: AuditAction) -> list[AuditLog]:
    async with migration.harness.session_factory() as session:
        rows = await session.scalars(select(AuditLog).where(AuditLog.action == action))
        return list(rows)


# --- AC-1 가짜 권고 주입: 관련 없음 / 관련 있음 + 중복 제거 ---


async def test_inject_default_node2shell_advisory_does_not_touch_python_service(
    migration: Migration,
) -> None:
    await _seed(migration)

    response = await migration.author_client.post(_path(migration, "/cve-watch:inject"))

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["simulated"] is True
    assert (data["targets"], data["targetsWithoutDependencies"], data["packages"]) == (2, 0, 4)
    assert (data["created"], data["matches"]) == (0, [])
    assert await _cve_findings(migration) == []
    rows = await _audit(migration, AuditAction.CVE_WATCH_INJECTED)
    assert len(rows) == 1
    assert rows[0].details["advisoryId"] == "SIM-NODE2SHELL-0001"
    assert rows[0].details["package"] == "npm/react-server-dom-webpack"


async def test_inject_matching_advisory_creates_simulated_findings_once_per_environment(
    migration: Migration,
) -> None:
    digest = await _seed(migration)
    body = {
        "advisory": {
            "id": "SIM-STARLETTE-0001",
            "summary": "시연용 가짜 권고",
            "severity": "HIGH",
            "ecosystem": "PyPI",
            "package": "Starlette",
            "introduced": "0.40.0",
            "fixed": "0.49.1",
        },
        "serviceId": migration.service_id,
    }

    first = await migration.author_client.post(_path(migration, "/cve-watch:inject"), json=body)
    second = await migration.author_client.post(_path(migration, "/cve-watch:inject"), json=body)

    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert (data["created"], data["duplicates"]) == (2, 0)
    match = data["matches"][0]
    assert (match["package"], match["installedVersion"], match["fixedVersion"]) == (
        "starlette",
        "0.47.1",
        "0.49.1",
    )
    assert match["result"] == "FAIL" and match["imageDigest"] == digest
    again = second.json()["data"]
    assert (again["created"], again["duplicates"]) == (0, 2)
    assert {m["findingId"] for m in again["matches"]} == {m["findingId"] for m in data["matches"]}

    findings = await _cve_findings(migration)
    assert len(findings) == 2
    assert {f.environment_id for f in findings} == {migration.aws.id, migration.gcp.id}
    for finding in findings:
        assert finding.control_id == SecurityControlId.IMG_1
        assert finding.mode == SecurityFindingMode.SIMULATED
        assert finding.result == SecurityResult.FAIL
        assert finding.source_ref == "cve-watch:simulated:SIM-STARLETTE-0001"
        assert finding.image_digest == digest
        assert finding.remediation is not None
        assert finding.remediation["kind"] == "DEPENDENCY_BUMP"
        assert (finding.remediation["fromVersion"], finding.remediation["toVersion"]) == (
            "0.47.1",
            "0.49.1",
        )

    listed = await migration.harness.client.get(
        _path(migration, "/findings"), params={"controlId": "C-IMG-1"}
    )
    remediation = listed.json()["data"]["items"][0]["remediation"]
    assert remediation["kind"] == "DEPENDENCY_BUMP"
    assert remediation["package"] == "starlette"
    assert remediation["toVersion"] == "0.49.1"
    # 가짜 증거는 매트릭스 판정에 쓰이지 않는다.
    matrix = (await migration.harness.client.get(_path(migration, "/matrix"))).json()["data"]
    image = next(
        c for c in matrix["rows"][0]["cells"]["policy"]["controls"] if c["controlId"] == "C-IMG-1"
    )
    assert image["result"] != "FAIL"
    # 판정 조회가 SIMULATED 를 아예 읽지 않는다 — 가짜 증거는 이유로도 나오지 않는다.
    assert "EVIDENCE_SIMULATED" not in image["reasons"]
    assert "EVIDENCE_MISSING" in image["reasons"]


# --- AC-2 실제 조회(가짜 OSV 응답) → REAL 증거, 낮은 심각도·이미 알려진 것 제외 ---


async def test_check_with_osv_records_new_high_findings_and_reflects_in_matrix(
    migration: Migration,
) -> None:
    harness = migration.harness
    digest = await _seed(migration)
    harness.use_settings(cve_watch_enabled=True)
    calls = _use_fake_osv()
    # 빌드 검사(Trivy)가 이미 보고한 requests 의 CVE 는 새 취약점이 아니다.
    known = normalize_trivy_image(
        _trivy_with_known(digest),
        scope=SecurityScope(harness.organization.id, image_digest=digest),
        collected_at=harness.clock.now(),
    )
    async with harness.session_factory() as session:
        await SecurityFindingService(session).record(known)

    response = await migration.author_client.post(_path(migration, "/cve-watch:check"))

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["simulated"] is False
    assert (data["created"], data["skippedLowSeverity"], data["skippedKnown"]) == (2, 2, 2)
    assert {m["advisoryId"] for m in data["matches"]} == {STARLETTE_GHSA}
    assert calls[0] == "POST /v1/querybatch"
    findings = await _cve_findings(migration)
    assert {(f.mode, f.result, f.source_ref) for f in findings} == {
        (
            SecurityFindingMode.REAL,
            SecurityResult.FAIL,
            f"https://osv.dev/vulnerability/{STARLETTE_GHSA}",
        )
    }
    rows = await _audit(migration, AuditAction.CVE_WATCH_CHECKED)
    assert [(r.actor_type, r.details["created"]) for r in rows] == [(ActorType.USER, 2)]

    again = (await migration.author_client.post(_path(migration, "/cve-watch:check"))).json()
    assert (again["data"]["created"], again["data"]["duplicates"]) == (0, 2)
    assert len(await _cve_findings(migration)) == 2

    matrix = (await harness.client.get(_path(migration, "/matrix"))).json()["data"]
    for row in matrix["rows"]:
        image = next(c for c in row["cells"]["policy"]["controls"] if c["controlId"] == "C-IMG-1")
        assert image["result"] == "FAIL"
        assert {
            str(f.id) for f in findings if f.environment_id == uuid.UUID(row["environmentId"])
        } <= set(image["findingIds"])


def _trivy_with_known(digest: str) -> dict[str, Any]:
    name = f"registry.echo.test/shop-api@{digest}"
    return {
        "SchemaVersion": 2,
        "ArtifactName": name,
        "Metadata": {"RepoDigests": [name]},
        "Results": [
            {
                "Target": "Python",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-2026-11111",
                        "PkgName": "requests",
                        "InstalledVersion": "2.31.0",
                        "Severity": "CRITICAL",
                    }
                ],
            }
        ],
    }


async def test_check_feed_failure_is_502_and_records_nothing(migration: Migration) -> None:
    await _seed(migration)
    migration.harness.use_settings(cve_watch_enabled=True)
    _use_fake_osv(fail=True)

    response = await migration.author_client.post(_path(migration, "/cve-watch:check"))

    assert response.status_code == 502, response.text
    assert response.json()["code"] == "CVE_FEED_UNAVAILABLE"
    assert await _cve_findings(migration) == []


async def test_check_without_dependency_snapshot_counts_targets_and_skips_feed(
    migration: Migration,
) -> None:
    migration.harness.use_settings(cve_watch_enabled=True)
    calls = _use_fake_osv()

    response = await migration.author_client.post(_path(migration, "/cve-watch:check"))

    data = response.json()["data"]
    assert (data["targets"], data["targetsWithoutDependencies"], data["packages"]) == (2, 2, 0)
    assert calls == []


# --- AC-3 스위치 끔 · 권한 ---


async def test_switches_off_reject_check_and_inject_and_loop_does_nothing(
    migration: Migration,
) -> None:
    harness = migration.harness
    await _seed(migration)
    calls = _use_fake_osv()
    assert harness.settings.cve_watch_enabled is False

    check = await migration.author_client.post(_path(migration, "/cve-watch:check"))
    loop = CveWatchLoop(
        harness.session_factory,
        app.dependency_overrides[get_advisory_feed](),
        harness.clock,
        harness.settings,
    )
    created = await loop.run_once()
    harness.use_settings(cve_watch_simulate_enabled=False)
    inject = await migration.author_client.post(_path(migration, "/cve-watch:inject"))

    assert check.status_code == 409 and check.json()["code"] == "CVE_WATCH_DISABLED"
    assert created == 0 and calls == []
    assert inject.status_code == 409
    assert inject.json()["code"] == "CVE_WATCH_SIMULATE_DISABLED"
    assert await _cve_findings(migration) == []


async def test_periodic_loop_records_findings_with_system_audit_line(
    migration: Migration,
) -> None:
    harness = migration.harness
    await _seed(migration)
    harness.use_settings(cve_watch_enabled=True)
    _use_fake_osv()
    loop = CveWatchLoop(
        harness.session_factory,
        app.dependency_overrides[get_advisory_feed](),
        harness.clock,
        harness.settings,
    )

    # starlette(HIGH, 고친 버전 있음 → FAIL) + requests(CRITICAL, 고친 버전 없음 → WARN) × 환경 2.
    assert await loop.run_once() == 4
    assert await loop.run_once() == 0

    rows = await _audit(migration, AuditAction.CVE_WATCH_FINDINGS_RECORDED)
    assert [(r.actor_type, r.component, r.details["created"]) for r in rows] == [
        (ActorType.SYSTEM, "cve-watch", 4)
    ]
    results = {(f.items[0]["target"], f.result) for f in await _cve_findings(migration)}
    assert results == {("starlette", SecurityResult.FAIL), ("requests", SecurityResult.WARN)}


async def test_viewer_cannot_check_or_inject(migration: Migration) -> None:
    harness = migration.harness
    harness.use_settings(cve_watch_enabled=True)
    viewer = await create_member(
        harness.session_factory,
        harness.organization,
        OrganizationRole.VIEWER,
        harness.clock.now(),
        "viewer-1",
    )
    async with make_client(session_token=viewer.session_token) as client:
        for suffix in ("/cve-watch:check", "/cve-watch:inject"):
            response = await client.post(_path(migration, suffix))
            assert response.status_code == 403, response.text


# --- AC-4 진단 연결(인터페이스만, #98 병합 전 501) ---


async def test_diagnosis_interface_checks_finding_then_answers_not_implemented(
    migration: Migration,
) -> None:
    harness = migration.harness
    digest = await _seed(migration)
    body: dict[str, Any] = {
        "advisory": {"ecosystem": "PyPI", "package": "starlette", "introduced": "0", "fixed": None}
    }
    injected = await migration.author_client.post(_path(migration, "/cve-watch:inject"), json=body)
    no_fix_id = injected.json()["data"]["matches"][0]["findingId"]
    body["advisory"]["fixed"] = "0.49.1"
    body["advisory"]["id"] = "SIM-FIX-0001"
    fixable = await migration.author_client.post(_path(migration, "/cve-watch:inject"), json=body)
    fixable_id = fixable.json()["data"]["matches"][0]["findingId"]
    trivy = normalize_trivy_image(
        _trivy_with_known(digest),
        scope=SecurityScope(harness.organization.id, image_digest=digest),
        collected_at=harness.clock.now(),
    )
    async with harness.session_factory() as session:
        trivy_id = await SecurityFindingService(session).record(trivy)

    async def diagnose(finding_id: object) -> httpx.Response:
        return await migration.author_client.post(
            _path(migration, f"/findings/{finding_id}/diagnosis")
        )

    missing = await diagnose(uuid.uuid4())
    without_remediation = await diagnose(trivy_id)
    without_fix = await diagnose(no_fix_id)
    pending = await diagnose(fixable_id)

    assert missing.status_code == 404 and missing.json()["code"] == "FINDING_NOT_FOUND"
    for response in (without_remediation, without_fix):
        assert response.status_code == 409
        assert response.json()["code"] == "REMEDIATION_NOT_AVAILABLE"
    assert pending.status_code == 501, pending.text
    assert pending.json()["code"] == "NOT_IMPLEMENTED"
    assert await _audit(migration, AuditAction.CVE_DIAGNOSIS_REQUESTED) == []


# --- AC-5 빌드 보안 검사 → 의존성 목록 ---


async def test_build_scan_records_dependency_snapshot_for_built_digest(harness: Any) -> None:
    def trivy(digest: str) -> dict[str, Any]:
        report = _trivy(digest, [])
        report["Results"][0].update(
            {
                "Class": "lang-pkgs",
                "Type": "python-pkg",
                "Packages": [{"Name": "fastapi", "Version": "0.162.0"}],
            }
        )
        return report

    executor = ScanningBuildExecutor(harness.clock, 0, scan=_scan(trivy=trivy))

    service, _ = await _deploy_and_finish(harness, executor)

    async with harness.session_factory() as session:
        snapshots = list(await session.scalars(select(ServiceDependencySnapshot)))
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert str(snapshot.service_id) == service["id"]
    assert snapshot.source == DependencySnapshotSource.BUILD_SCAN
    assert snapshot.packages == [{"ecosystem": "PyPI", "name": "fastapi", "version": "0.162.0"}]
    assert snapshot.package_count == 1


@pytest.fixture(autouse=True)
def _clear_feed_override() -> Any:
    yield
    app.dependency_overrides.pop(get_advisory_feed, None)


# --- 검증 반영: 가짜 증거·대량 증거가 실제 판정을 바꾸지 않는다 ---


async def _image_cells(migration: Migration) -> list[dict[str, Any]]:
    response = await migration.harness.client.get(_path(migration, "/matrix"))
    assert response.status_code == 200, response.text
    return [
        next(c for c in row["cells"]["policy"]["controls"] if c["controlId"] == "C-IMG-1")
        for row in response.json()["data"]["rows"]
    ]


async def _active_simulated(migration: Migration) -> int:
    now = migration.harness.clock.now()
    return sum(
        1
        for finding in await _cve_findings(migration)
        if finding.mode == SecurityFindingMode.SIMULATED and finding.expires_at > now
    )


async def test_inject_flood_keeps_simulated_cap_and_leaves_real_gate_passing(
    migration: Migration,
) -> None:
    await _seed(migration)
    await prepare_passing_gate(migration)
    plan = await migration.create_plan()
    assert [c["result"] for c in await _image_cells(migration)] == ["PASS", "PASS"]
    body: dict[str, Any] = {
        "advisory": {
            "id": "SIM-FLOOD",
            "ecosystem": "PyPI",
            "package": "starlette",
            "introduced": "0",
            "fixed": "0.49.1",
        }
    }

    retired = 0
    for index in range(15):
        body["advisory"]["id"] = f"SIM-FLOOD-{index:04}"
        response = await migration.author_client.post(
            _path(migration, "/cve-watch:inject"), json=body
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["created"] == 2
        retired += data["retiredSimulated"]

    # 15번 × 환경 2 = 30건을 만들었지만 살아 있는 가짜 증거는 조직당 20건까지다.
    assert await _active_simulated(migration) == SIMULATED_ACTIVE_LIMIT
    assert retired == 30 - SIMULATED_ACTIVE_LIMIT
    injected = await _audit(migration, AuditAction.CVE_WATCH_INJECTED)
    assert sum(int(line.details.get("retiredSimulated", 0)) for line in injected) == retired
    # 판정 불변: 매트릭스 PASS, 실제 이전 승인 통과.
    cells = await _image_cells(migration)
    assert [c["result"] for c in cells] == ["PASS", "PASS"]
    assert all("EVIDENCE_SIMULATED" not in c["reasons"] for c in cells)
    approval = await migration.act(plan["id"], "approve")
    assert approval.status_code == 202, approval.text
    assert approval.json()["data"]["status"] == "APPROVED"


async def test_single_inject_beyond_cap_keeps_total_simulated_within_limit(
    migration: Migration,
) -> None:
    """설치 버전 11개 × 환경 2 = 22건을 낳는 주입 한 번도 보관 총수는 20건 이하다(재검증 P2)."""
    versions = [DependencyPackage("PyPI", "starlette", f"0.40.{n}") for n in range(11)]
    await _seed(migration, versions)
    body: dict[str, Any] = {
        "advisory": {
            "id": "SIM-OLD",
            "ecosystem": "PyPI",
            "package": "starlette",
            "introduced": "0",
            "fixed": "0.49.1",
        }
    }
    # 먼저 옛 가짜 증거 20건을 채운다(같은 수만큼 만들고 넘친 2건은 상한으로 뺀다).
    first = await migration.author_client.post(_path(migration, "/cve-watch:inject"), json=body)
    assert first.status_code == 200, first.text
    first_data = first.json()["data"]
    assert (first_data["created"], first_data["omittedSimulated"]) == (20, 2)
    assert len(first_data["matches"]) == SIMULATED_ACTIVE_LIMIT
    assert await _active_simulated(migration) == SIMULATED_ACTIVE_LIMIT
    old_ids = {m["findingId"] for m in first_data["matches"]}

    body["advisory"]["id"] = "SIM-NEW"
    response = await migration.author_client.post(_path(migration, "/cve-watch:inject"), json=body)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["created"], data["omittedSimulated"]) == (20, 2)
    assert data["retiredSimulated"] == 20
    new_ids = {m["findingId"] for m in data["matches"]}
    assert len(new_ids) == SIMULATED_ACTIVE_LIMIT
    now = migration.harness.clock.now()
    alive = {
        str(f.id)
        for f in await _cve_findings(migration)
        if f.mode == SecurityFindingMode.SIMULATED and f.expires_at > now
    }
    # 이번 주입분 포함 총수 20건 — 이번 응답이 돌려준 증거만 남고 옛 것은 모두 지워졌다.
    assert alive == new_ids
    assert not alive & old_ids
    rows = await _audit(migration, AuditAction.CVE_WATCH_INJECTED)
    assert [line.details.get("omittedSimulated") for line in rows] == [2, 2]


async def test_judgement_read_keeps_latest_evidence_of_every_control_beyond_search_limit(
    migration: Migration,
) -> None:
    """판정 조회 상한(500) 너머로 같은 통제·다른 출처의 새 증거나 가짜 증거가 쌓여도
    통제·출처마다 최신 실제 증거는 남는다."""
    await prepare_passing_gate(migration)
    plan = await migration.create_plan()
    trivy = (await gate_findings_by_source(migration, SecurityFindingSource.TRIVY_IMAGE))[0]
    columns = [column.name for column in Finding.__table__.columns]
    overrides = {
        "id": "gen_random_uuid()",
        "image_digest": "'sha256:' || md5(g::text) || md5(g::text)",
        "dedupe_key": "NULL",
        "collected_at": "collected_at + g * interval '1 millisecond'",
        "expires_at": "expires_at + g * interval '1 millisecond'",
        "created_at": "now()",
        "updated_at": "now()",
    }
    select_list = ", ".join(overrides.get(name, name) for name in columns)
    async with migration.harness.session_factory() as session, session.begin():
        for source, mode in (("CVE_WATCH", "REAL"), ("CVE_WATCH", "SIMULATED")):
            await session.execute(
                text(
                    f"INSERT INTO findings ({', '.join(columns)}) "
                    f"SELECT {select_list} "
                    "FROM findings, generate_series(1, 600) AS g WHERE findings.id = :id"
                ).bindparams(id=trivy.id)
            )
            await session.execute(
                text(
                    "UPDATE findings SET source = :source, mode = :mode "
                    "WHERE source = 'TRIVY_IMAGE' AND id <> :id"
                ).bindparams(source=source, mode=mode, id=trivy.id)
            )
    async with migration.harness.session_factory() as session:
        total = await session.scalar(text("SELECT count(*) FROM findings"))
    assert total is not None and total > 1200

    cells = await _image_cells(migration)
    assert [c["result"] for c in cells] == ["PASS", "PASS"]
    approval = await migration.act(plan["id"], "approve")
    assert approval.status_code == 202, approval.text
    assert approval.json()["data"]["status"] == "APPROVED"


async def gate_findings_by_source(
    migration: Migration, source: SecurityFindingSource
) -> list[Finding]:
    async with migration.harness.session_factory() as session:
        rows = await session.scalars(select(Finding).where(Finding.source == source))
        return list(rows.all())
