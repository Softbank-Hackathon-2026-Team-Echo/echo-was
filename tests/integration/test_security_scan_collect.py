"""빌드 성공 직후 보안 검사 원문 → findings 기록 통합 시험(보안·거버넌스 2-3 SPEC AC-3~AC-7)."""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from app.enums import (
    DeployRequestStatus,
    SecurityControlId,
    SecurityFindingReason,
    SecurityFindingSource,
    SecurityResult,
)
from app.executors.base import (
    BuildPollResult,
    BuildSecurityScan,
    BuildSpec,
    BuildSucceeded,
    SourceAccess,
)
from app.executors.fake import FAKE_REGISTRY, FakeBuildExecutor
from app.models import Build, Finding
from tests.integration.deploy_harness import DeployHarness

SOURCE_REF = "github-actions:team-echo/echo-was/actions/runs/9001"

type ScanFactory = Callable[[str, str], BuildSecurityScan | None]


class ScanningBuildExecutor(FakeBuildExecutor):
    """가짜 빌드 + 보안 검사 원문(실제 빌드 실행기처럼 BuildSecurityScanReader 를 갖춘다)."""

    def __init__(self, *args: Any, scan: ScanFactory, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._scan = scan
        self._build_ids: dict[str, str] = {}
        self._digests: dict[str, str] = {}
        self.scan_reads: list[str] = []

    async def start(self, build: BuildSpec, source: SourceAccess, idempotency_key: str) -> str:
        external_ref = await super().start(build, source, idempotency_key)
        self._build_ids[external_ref] = idempotency_key
        return external_ref

    async def poll(self, external_ref: str) -> BuildPollResult:
        result = await super().poll(external_ref)
        if isinstance(result, BuildSucceeded):
            self._digests[external_ref] = result.image_digest
        return result

    async def read_security_scan(self, external_ref: str) -> BuildSecurityScan | None:
        self.scan_reads.append(external_ref)
        return self._scan(self._build_ids[external_ref], self._digests[external_ref])


def _trivy(digest: str, vulnerabilities: list[dict[str, Any]]) -> dict[str, Any]:
    name = f"{FAKE_REGISTRY}/shop-api@{digest}"
    return {
        "SchemaVersion": 2,
        "ArtifactName": name,
        "Metadata": {"RepoDigests": [name]},
        "Results": [
            {"Target": "Python", "Vulnerabilities": vulnerabilities, "Secrets": []},
        ],
    }


FIXABLE_HIGH = {
    "VulnerabilityID": "CVE-2026-97687",
    "PkgName": "urllib3",
    "InstalledVersion": "2.7.0",
    "FixedVersion": "2.8.0",
    "Severity": "HIGH",
}


def _scan(
    *,
    build_id: str | None = None,
    digest: str | None = None,
    trivy: Callable[[str], dict[str, Any] | None] = lambda digest: _trivy(digest, [FIXABLE_HIGH]),
    gitleaks: object | None = (),
    scanned_at: datetime | None = None,
) -> ScanFactory:
    def make(actual_build_id: str, actual_digest: str) -> BuildSecurityScan:
        used_digest = digest or actual_digest
        return BuildSecurityScan(
            build_id=build_id or actual_build_id,
            image_digest=used_digest,
            source_ref=SOURCE_REF,
            scanned_at=scanned_at,
            trivy_version="0.75.0",
            gitleaks_version="8.30.1",
            trivy_report=trivy(used_digest),
            gitleaks_report=list(gitleaks) if isinstance(gitleaks, tuple) else gitleaks,
        )

    return make


async def _deploy_and_finish(
    harness: DeployHarness, executor: FakeBuildExecutor, environment_name: str = "aws-seoul"
) -> tuple[dict[str, Any], dict[str, Any]]:
    harness.replace_executors(build=executor)
    service = await harness.create_service("shop-api")
    environment = await harness.add_environment(environment_name)
    request = await harness.deploy(service["id"], environment.id)
    await harness.run_until(request["id"], {DeployRequestStatus.SUCCEEDED})
    return service, request


async def _findings(harness: DeployHarness) -> list[Finding]:
    async with harness.session_factory() as session:
        rows = await session.scalars(
            select(Finding)
            .where(Finding.organization_id == harness.organization.id)
            .order_by(Finding.control_id, Finding.source)
        )
        return list(rows)


async def _build(harness: DeployHarness) -> Build:
    async with harness.session_factory() as session:
        return (await session.scalars(select(Build))).one()


# --- AC-3 빌드 성공 직후 같은 digest 의 증거 3건 ---


async def test_build_success_records_image_findings_for_built_digest(
    harness: DeployHarness,
) -> None:
    scanned_at = harness.clock.now() - timedelta(minutes=2)
    executor = ScanningBuildExecutor(harness.clock, 0, scan=_scan(scanned_at=scanned_at))

    service, _ = await _deploy_and_finish(harness, executor)

    build = await _build(harness)
    findings = await _findings(harness)
    assert [(f.control_id, f.source, f.result) for f in findings] == [
        (SecurityControlId.IMG_1, SecurityFindingSource.TRIVY_IMAGE, SecurityResult.FAIL),
        (SecurityControlId.IMG_2, SecurityFindingSource.GITLEAKS, SecurityResult.PASS),
        (SecurityControlId.IMG_2, SecurityFindingSource.TRIVY_SECRET, SecurityResult.PASS),
    ]
    assert findings[0].reason == SecurityFindingReason.FIXABLE_HIGH_VULNERABILITY
    assert {f.image_digest for f in findings} == {build.image_digest}
    assert {str(f.service_id) for f in findings} == {service["id"]}
    assert {f.source_ref for f in findings} == {SOURCE_REF}
    assert {f.collected_at for f in findings} == {scanned_at}
    assert executor.scan_reads == [build.external_ref]


async def test_scan_time_ahead_of_our_clock_uses_now(harness: DeployHarness) -> None:
    ahead = harness.clock.now() + timedelta(hours=1)
    executor = ScanningBuildExecutor(harness.clock, 0, scan=_scan(scanned_at=ahead))

    await _deploy_and_finish(harness, executor)

    assert all(f.collected_at < ahead for f in await _findings(harness))


# --- AC-4 재사용 빌드는 다시 모으지 않는다 ---


async def test_reused_build_is_not_collected_again(harness: DeployHarness) -> None:
    executor = ScanningBuildExecutor(harness.clock, 0, scan=_scan())
    service, _ = await _deploy_and_finish(harness, executor)
    second = await harness.add_environment("aws-tokyo")

    request = await harness.deploy(service["id"], second.id)
    await harness.run_until(request["id"], {DeployRequestStatus.SUCCEEDED})

    async with harness.session_factory() as session:
        build_count = await session.scalar(select(func.count()).select_from(Build))
    assert build_count == 1
    assert len(executor.scan_reads) == 1
    assert len(await _findings(harness)) == 3


# --- AC-5 다른 빌드·다른 digest 의 원문은 버린다 ---


async def test_scan_for_other_build_or_digest_is_ignored_and_deploy_continues(
    harness: DeployHarness,
) -> None:
    executor = ScanningBuildExecutor(
        harness.clock, 0, scan=_scan(build_id="00000000-0000-0000-0000-000000000000")
    )

    _, request = await _deploy_and_finish(harness, executor)

    assert await harness.request_status(request["id"]) == DeployRequestStatus.SUCCEEDED
    assert await _findings(harness) == []


async def test_scan_for_other_digest_is_ignored(harness: DeployHarness) -> None:
    executor = ScanningBuildExecutor(harness.clock, 0, scan=_scan(digest="sha256:" + "0" * 64))

    await _deploy_and_finish(harness, executor)

    assert await _findings(harness) == []


# --- AC-6 검사가 실패해 보고서가 없으면 UNKNOWN 증거 ---


async def test_missing_reports_are_recorded_as_unknown(harness: DeployHarness) -> None:
    executor = ScanningBuildExecutor(
        harness.clock, 0, scan=_scan(trivy=lambda digest: None, gitleaks=None)
    )

    await _deploy_and_finish(harness, executor)

    findings = await _findings(harness)
    assert [f.result for f in findings] == [SecurityResult.UNKNOWN] * 3
    assert {f.reason for f in findings} == {SecurityFindingReason.REPORT_INVALID}


# --- AC-7 원문이 없거나 가짜 실행기면 아무것도 남기지 않고 배포는 그대로 ---


async def test_no_scan_records_nothing_and_deploy_succeeds(harness: DeployHarness) -> None:
    executor = ScanningBuildExecutor(harness.clock, 0, scan=lambda build_id, digest: None)

    _, request = await _deploy_and_finish(harness, executor)

    assert await harness.request_status(request["id"]) == DeployRequestStatus.SUCCEEDED
    assert len(executor.scan_reads) == 1
    assert await _findings(harness) == []


async def test_fake_build_executor_records_nothing(harness: DeployHarness) -> None:
    _, request = await _deploy_and_finish(harness, FakeBuildExecutor(harness.clock, 0))

    assert await harness.request_status(request["id"]) == DeployRequestStatus.SUCCEEDED
    assert await _findings(harness) == []
