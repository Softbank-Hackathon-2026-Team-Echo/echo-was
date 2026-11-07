"""실제 적용 모드의 보안 증거·외부 호출 기록 대역. 보안 게이트를 생략하지 않는다."""

from dataclasses import replace
from typing import Any

from app.executors.fake import FakeDeployExecutor
from tests.integration.deploy_harness import DeployHarness
from tests.integration.test_security_scan_collect import ScanningBuildExecutor, _scan, _trivy


class RealDeployRecorder(FakeDeployExecutor):
    @property
    def simulated(self) -> bool:
        return False


class RealScanningBuild(ScanningBuildExecutor):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.metadata: dict[str, dict[str, str]] = {}
        self.scan_changes: dict[str, Any] = {}

    async def start(self, build: Any, source: Any, idempotency_key: str) -> str:
        external_ref = await super().start(build, source, idempotency_key)
        self.metadata[external_ref] = {
            "repository": build.repo_url.removeprefix("https://github.com/"),
            "commit_sha": build.commit_sha,
        }
        return external_ref

    async def read_security_scan(self, external_ref: str) -> Any:
        scan = await super().read_security_scan(external_ref)
        return (
            replace(scan, **{**self.metadata[external_ref], **self.scan_changes}) if scan else None
        )

    @property
    def simulated(self) -> bool:
        return False


def install_scanned_executors(harness: DeployHarness, scenario: str = "pass") -> None:
    scanned_at = harness.clock.now()

    def scan(build_id: str, digest: str) -> Any:
        if scenario == "absent":
            return None
        factory = _scan(
            digest="sha256:" + "b" * 64 if scenario == "wrong-digest" else None,
            trivy=(lambda d: _trivy(d, []))
            if scenario != "vulnerability"
            else (
                lambda d: _trivy(
                    d,
                    [
                        {
                            "VulnerabilityID": "CVE-2026-97687",
                            "PkgName": "urllib3",
                            "InstalledVersion": "2.7.0",
                            "FixedVersion": "2.8.0",
                            "Severity": "HIGH",
                        }
                    ],
                )
            ),
            gitleaks=None if scenario == "missing-source" else (),
            scanned_at=scanned_at,
        )
        result = factory(build_id, digest)
        if scenario == "missing-image":
            result = replace(result, trivy_report=None)
        if scenario == "secret":
            result = replace(
                result,
                gitleaks_report=[
                    {"RuleID": "generic-api-key", "File": "config.py", "StartLine": 1}
                ],
            )
        return result

    harness.replace_executors(
        build=RealScanningBuild(harness.clock, 0, scan=scan),
        deploy=RealDeployRecorder(harness.clock, 0),
    )
