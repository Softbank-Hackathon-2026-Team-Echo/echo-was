"""고친 버전을 외부 입력이 아니라 고정 OSV 원문으로 검증한다."""

from typing import Any, NoReturn

from app.clients.osv import AdvisoryMatch, match_from_osv
from app.core.exceptions import ConflictError
from app.services.cve_watch_packages import DependencyPackage
from app.services.cve_watch_versions import is_affected


class CveAutofixRejectedError(ConflictError):
    code = "CVE_AUTOFIX_REJECTED"


def reject(reason: str) -> NoReturn:
    raise CveAutofixRejectedError(reason)


def validate_advisory(
    response: dict[str, Any], package: str, version: str, cve_id: str, fixed_version: str
) -> AdvisoryMatch:
    if response.get("next_page_token") or not isinstance(response.get("vulns"), list):
        reject("ADVISORY_INCOMPLETE")
    matches = []
    for vuln in response["vulns"]:
        if not isinstance(vuln, dict) or vuln.get("withdrawn"):
            reject("ADVISORY_INVALID")
        if cve_id not in {vuln.get("id"), *vuln.get("aliases", [])}:
            continue
        match = match_from_osv(DependencyPackage("npm", package, version), vuln)
        if not match.fixed_version or match.fixed_version != fixed_version:
            reject("FIX_VERSION_MISMATCH")
        ranges = [
            r
            for affected in vuln.get("affected", [])
            if affected.get("package", {}).get("name") == package
            and affected.get("package", {}).get("ecosystem") == "npm"
            for r in affected.get("ranges", [])
            if r.get("type") == "SEMVER"
        ]
        if not any(is_affected(version, r["events"], "semver") for r in ranges):
            reject("VERSION_NOT_AFFECTED")
        if any(is_affected(fixed_version, r["events"], "semver") for r in ranges):
            reject("FIX_STILL_AFFECTED")
        matches.append(match)
    if len(matches) != 1:
        reject("ADVISORY_NOT_UNIQUE")
    return matches[0]


def validate_rescan(response: dict[str, Any]) -> None:
    if set(response) - {"vulns", "next_page_token"} or response.get("next_page_token"):
        reject("RESCAN_INCOMPLETE")
    vulns = response.get("vulns", [])
    if not isinstance(vulns, list) or vulns:
        reject("RESCAN_VULNERABILITIES_REMAIN")


def validate_context(
    finding: Any,
    organization_id: str,
    binding: dict[str, Any],
    packages: list[dict[str, Any]],
    repository: str,
) -> None:
    # 첫 구현은 독립 fixture 전용이다. 모의 증거를 REAL로 바꾸거나 운영 게이트를 풀지 않는다.
    if (
        finding.mode != "SIMULATED"
        or binding.get("fixture") is not True
        or binding.get("image_digest_kind") != "FIXTURE_SOURCE_TREE"
    ):
        reject("FIXTURE_ONLY")
    for key, actual in (
        ("organization_id", finding.organization_id),
        ("service_id", finding.service_id),
        ("environment_id", finding.environment_id),
        ("source_finding_id", finding.id),
        ("image_digest", finding.image_digest),
        ("repository", repository),
    ):
        if str(actual) != binding.get(key):
            reject("SCOPE_MISMATCH")
    if str(finding.organization_id) != organization_id or finding.source != "CVE_WATCH":
        reject("SCOPE_MISMATCH")
    remediation = finding.remediation or {}
    if remediation.get("kind") != "DEPENDENCY_BUMP" or binding.get("ecosystem") != "npm":
        reject("ECOSYSTEM_UNSUPPORTED")
    for key, field in (
        ("ecosystem", "ecosystem"),
        ("package", "package"),
        ("from_version", "fromVersion"),
        ("fixed_version", "toVersion"),
    ):
        if remediation.get(field) != binding.get(key):
            reject("PACKAGE_MISMATCH")
    if binding.get("cve_id") not in {
        remediation.get("advisoryId"),
        *remediation.get("aliases", []),
    }:
        reject("CVE_MISMATCH")
    if not any(
        p.get("ecosystem") == "npm"
        and p.get("name") == binding["package"]
        and p.get("version") == binding["from_version"]
        for p in packages
    ):
        reject("PACKAGE_NOT_IN_SNAPSHOT")
