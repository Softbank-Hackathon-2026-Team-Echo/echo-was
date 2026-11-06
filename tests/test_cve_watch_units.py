"""CVE 감시 순수 계산(SPEC sec-6-cve-watch): 버전 매칭·의존성 뽑기·심각도·중복 열쇠."""

import uuid
from typing import Any

import httpx
import pytest

from app.clients.osv import (
    AdvisoryMatch,
    DetailBacklog,
    OsvAdvisoryFeed,
    canonical_advisory_id,
    cvss3_base_score,
    match_from_osv,
    severity_of,
)
from app.enums import SecurityFindingMode
from app.services.cve_watch_packages import (
    DependencyPackage,
    package_from_purl,
    packages_from_cyclonedx,
    packages_from_trivy_report,
)
from app.services.cve_watch_service import SimulatedAdvisory, _Target, dedupe_key
from app.services.cve_watch_versions import compare_versions, fixed_version_for, is_affected

# --- 버전 비교 ---


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("1.10.0", "1.9.9", 1),
        ("19.0.0", "19.0.1", -1),
        ("2.0", "2.0.0", 0),
        ("v1.2.3", "1.2.3", 0),
        ("1.0rc1", "1.0", -1),
        ("1.0.0-beta.2", "1.0.0", -1),
        ("1.0.post1", "1.0", 1),
        ("3.0.11-1~deb12u2", "3.0.11-1~deb12u3", -1),
        ("1:2.36-9", "2.36-9", 0),
        ("0.162.0", "0.99.1", 1),
    ],
)
def test_compare_versions(left: str, right: str, expected: int) -> None:
    assert compare_versions(left, right) == expected


def test_compare_versions_rejects_values_without_numbers() -> None:
    with pytest.raises(ValueError):
        compare_versions("latest", "1.0")


REACT2SHELL_RANGES: list[dict[str, Any]] = [
    {"type": "SEMVER", "events": [{"introduced": "19.0.0"}, {"fixed": "19.0.1"}]},
    {"type": "SEMVER", "events": [{"introduced": "19.1.0"}, {"fixed": "19.1.2"}]},
    {"type": "SEMVER", "events": [{"introduced": "19.2.0"}, {"fixed": "19.2.1"}]},
]


@pytest.mark.parametrize(
    ("version", "affected", "fixed"),
    [
        ("19.0.0", True, "19.0.1"),
        ("19.0.1", False, None),
        ("19.1.1", True, "19.1.2"),
        ("19.2.0", True, "19.2.1"),
        ("18.3.1", False, None),
    ],
)
def test_range_matching_picks_fixed_version_of_the_range_holding_installed(
    version: str, affected: bool, fixed: str | None
) -> None:
    assert any(is_affected(version, r["events"]) for r in REACT2SHELL_RANGES) is affected
    assert fixed_version_for(version, REACT2SHELL_RANGES) == fixed


def test_range_without_fix_and_last_affected() -> None:
    assert is_affected("5.0", [{"introduced": "0"}])
    assert fixed_version_for("5.0", [{"events": [{"introduced": "0"}]}]) is None
    events = [{"introduced": "1.0"}, {"last_affected": "1.4"}]
    assert is_affected("1.4", events)
    assert not is_affected("1.5", events)
    assert not is_affected("not-a-version", events)


# --- 의존성 뽑기 ---


def test_trivy_report_with_list_all_pkgs_uses_source_packages_for_os() -> None:
    report = {
        "SchemaVersion": 2,
        "Metadata": {"OS": {"Family": "debian", "Name": "12.7"}},
        "Results": [
            {
                "Target": "img (debian 12.7)",
                "Class": "os-pkgs",
                "Type": "debian",
                "Packages": [
                    {
                        "Name": "libssl3",
                        "Version": "3.0.11",
                        "Release": "1~deb12u2",
                        "SrcName": "openssl",
                        "SrcVersion": "3.0.11",
                        "SrcRelease": "1~deb12u2",
                        "Identifier": {"PURL": "pkg:deb/debian/libssl3@3.0.11-1~deb12u2"},
                    }
                ],
            },
            {
                "Target": "Python",
                "Class": "lang-pkgs",
                "Type": "python-pkg",
                "Packages": [
                    {"Name": "starlette", "Version": "0.47.1"},
                    {"Name": "starlette", "Version": "0.47.1"},
                ],
            },
            {"Target": "Unknown", "Class": "lang-pkgs", "Type": "mystery", "Packages": [{}]},
        ],
    }

    packages = packages_from_trivy_report(report)

    assert [(p.ecosystem, p.name, p.version) for p in packages] == [
        ("Debian:12", "openssl", "3.0.11-1~deb12u2"),
        ("PyPI", "starlette", "0.47.1"),
    ]


def test_trivy_report_without_packages_falls_back_to_vulnerable_ones() -> None:
    report = {
        "Results": [
            {
                "Class": "lang-pkgs",
                "Type": "node-pkg",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-2025-55182",
                        "PkgName": "react-server-dom-webpack",
                        "InstalledVersion": "19.0.0",
                    }
                ],
            }
        ]
    }

    assert packages_from_trivy_report(report) == [
        DependencyPackage("npm", "react-server-dom-webpack", "19.0.0")
    ]
    assert packages_from_trivy_report({"Results": "broken"}) == []


def test_cyclonedx_components_and_purl_parsing() -> None:
    sbom = {
        "bomFormat": "CycloneDX",
        "components": [
            {"purl": "pkg:npm/%40scope/pkg@1.2.3"},
            {"purl": "pkg:pypi/Requests@2.31.0"},
            {
                "purl": "pkg:maven/org.apache.logging.log4j/log4j-core@2.14.1",
                "components": [{"purl": "pkg:golang/github.com/x/y@v0.1.0"}],
            },
            {"purl": "pkg:deb/debian/openssl@3.0.11-1~deb12u2?arch=amd64&distro=debian-12.7"},
            {"purl": "pkg:generic/thing@1.0"},
            {"name": "no-purl"},
        ],
    }

    found = {(p.ecosystem, p.name, p.version) for p in packages_from_cyclonedx(sbom)}

    assert found == {
        ("npm", "@scope/pkg", "1.2.3"),
        ("PyPI", "Requests", "2.31.0"),
        ("Maven", "org.apache.logging.log4j:log4j-core", "2.14.1"),
        ("Go", "github.com/x/y", "v0.1.0"),
        ("Debian:12", "openssl", "3.0.11-1~deb12u2"),
    }
    assert package_from_purl("pkg:npm/no-version") is None


def test_package_key_normalizes_pypi_names_and_drops_release() -> None:
    assert DependencyPackage("PyPI", "Zope.Interface", "1").key == ("pypi", "zope-interface")
    assert DependencyPackage("Debian:12", "openssl", "1").key == ("debian", "openssl")


# --- 심각도·OSV 응답 ---


def test_cvss3_base_score_matches_first_calculator() -> None:
    # React2Shell(CVE-2025-55182) 벡터 = 10.0, NVD 예시 7.5, 낮은 값 2.7.
    assert cvss3_base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H") == 10.0
    assert cvss3_base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N") == 7.5
    assert cvss3_base_score("CVSS:3.1/AV:N/AC:L/PR:H/UI:N/S:U/C:L/I:N/A:N") == 2.7
    assert cvss3_base_score("CVSS:4.0/AV:N") is None
    assert cvss3_base_score("CVSS:3.1/AV:N") is None


def test_severity_prefers_github_label_then_cvss_then_unknown() -> None:
    assert severity_of({"database_specific": {"severity": "MODERATE"}}) == "MEDIUM"
    vector = {"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}
    assert severity_of({"severity": [vector]}) == "CRITICAL"
    assert severity_of({"id": "X"}) == "UNKNOWN"


def test_match_from_osv_reads_only_ranges_of_the_same_package() -> None:
    package = DependencyPackage("npm", "react-server-dom-webpack", "19.1.1")
    vuln = {
        "id": "GHSA-fv66-9v8q-g76r",
        "aliases": ["CVE-2025-55182"],
        "summary": "React Server Components are Vulnerable to RCE",
        "database_specific": {"severity": "CRITICAL"},
        "affected": [
            {
                "package": {"name": "react-server-dom-parcel", "ecosystem": "npm"},
                "ranges": [{"events": [{"introduced": "19.1.0"}, {"fixed": "19.1.9"}]}],
            },
            {
                "package": {"name": "react-server-dom-webpack", "ecosystem": "npm"},
                "ranges": REACT2SHELL_RANGES,
            },
        ],
    }

    match = match_from_osv(package, vuln)

    assert match.advisory.severity == "CRITICAL"
    assert match.advisory.aliases == ("CVE-2025-55182",)
    assert match.fixed_version == "19.1.2"


# --- 가짜 권고·중복 열쇠 ---


def test_simulated_advisory_matches_only_affected_installed_versions() -> None:
    advisory = SimulatedAdvisory()
    packages = [
        DependencyPackage("npm", "react-server-dom-webpack", "19.0.0"),
        DependencyPackage("npm", "react-server-dom-webpack", "19.0.1"),
        DependencyPackage("PyPI", "react-server-dom-webpack", "19.0.0"),
    ]

    matches = advisory.match(packages)

    assert [(m.package.version, m.fixed_version) for m in matches] == [("19.0.0", "19.0.1")]


def _match(version: str, advisory_id: str = "SIM-1") -> AdvisoryMatch:
    package = DependencyPackage("npm", "react-server-dom-webpack", version)
    return SimulatedAdvisory(advisory_id=advisory_id).match([package])[0]


def test_dedupe_key_is_stable_and_separates_scope_version_and_mode() -> None:
    org = uuid.uuid4()
    target = _Target(uuid.uuid4(), uuid.uuid4(), "sha256:" + "a" * 64)
    other_env = _Target(target.service_id, uuid.uuid4(), target.image_digest)
    other_image = _Target(target.service_id, target.environment_id, "sha256:" + "b" * 64)
    real, simulated = SecurityFindingMode.REAL, SecurityFindingMode.SIMULATED

    key = dedupe_key(org, target, _match("19.0.0"), real)

    assert key == dedupe_key(org, target, _match("19.0.0"), real)
    assert key.startswith("cve:") and len(key) == 68
    others = {
        dedupe_key(org, other_env, _match("19.0.0"), real),
        dedupe_key(org, other_image, _match("19.0.0"), real),
        dedupe_key(org, target, _match("19.0.0", "SIM-2"), real),
        dedupe_key(org, target, _match("19.0.0"), simulated),
        dedupe_key(uuid.uuid4(), target, _match("19.0.0"), real),
    }
    assert key not in others and len(others) == 5


# --- 검증 반영: semver 사전 배포·빌드 메타, dpkg, 배포판별 고친 버전 ---


@pytest.mark.parametrize(
    ("left", "right", "scheme", "expected"),
    [
        ("1.0.0-1", "1.0.0", "semver", -1),
        ("1.0.0+build.1", "1.0.0", "semver", 0),
        ("1.0.0-alpha.2", "1.0.0-alpha.10", "semver", -1),
        ("1.0.0-alpha", "1.0.0-alpha.1", "semver", -1),
        ("1.0.0-1", "1.0.0-alpha", "semver", -1),
        ("1.0.0-rc.1+build.5", "1.0.0-rc.1", "semver", 0),
        ("3.0.11-1~deb12u2", "3.0.11-1~deb12u3", "debian", -1),
        ("3.0.11-1", "3.0.11", "debian", 1),
        ("1.0~rc1-1", "1.0-1", "debian", -1),
        ("1:2.36-9", "2.36-9", "debian", 1),
        ("2.36-9+deb12u4", "2.36-9", "debian", 1),
    ],
)
def test_compare_versions_by_scheme(left: str, right: str, scheme: str, expected: int) -> None:
    assert compare_versions(left, right, scheme) == expected
    assert compare_versions(right, left, scheme) == -expected


def test_numeric_prerelease_is_inside_range_and_gets_fixed_version() -> None:
    events = [{"introduced": "1.0.0"}, {"fixed": "2.0.0"}]
    assert is_affected("2.0.0-1", events) is True
    assert is_affected("2.0.0+build.1", events) is False
    ranges = [{"type": "SEMVER", "events": events}]
    assert fixed_version_for("2.0.0-1", ranges, "npm") == "2.0.0"


def test_fixed_version_uses_only_same_distro_release() -> None:
    package = DependencyPackage("Debian:12", "openssl", "3.0.11-1~deb12u2")
    vuln = {
        "id": "DEBIAN-CVE-2099-1",
        "database_specific": {"severity": "HIGH"},
        "affected": [
            {
                "package": {"name": "openssl", "ecosystem": "Debian:11"},
                "ranges": [
                    {"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "3.0.12"}]}
                ],
            },
            {
                "package": {"name": "openssl", "ecosystem": "Debian:12"},
                "ranges": [
                    {"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "3.0.13"}]}
                ],
            },
        ],
    }
    assert match_from_osv(package, vuln).fixed_version == "3.0.13"


def test_git_ranges_are_not_compared_as_versions() -> None:
    ranges = [{"type": "GIT", "events": [{"introduced": "0"}, {"fixed": "9abc0123"}]}]
    assert fixed_version_for("1.0.0", ranges, "npm") is None


# --- 검증 반영: purl 유무가 섞여도 같은 패키지, PyPI 이름 정규화로 중복 제거 ---


def test_package_identity_ignores_purl() -> None:
    plain = DependencyPackage("PyPI", "x", "1")
    with_purl = DependencyPackage("PyPI", "x", "1", "pkg:pypi/x@1")
    assert plain == with_purl
    assert sorted({plain, with_purl}) == [plain]


def test_trivy_report_merges_equivalent_pypi_names() -> None:
    raw = {
        "Results": [
            {
                "Type": "python-pkg",
                "Class": "lang-pkgs",
                "Packages": [
                    {"Name": "Zope.Interface", "Version": "1.0"},
                    {"Name": "zope-interface", "Version": "1.0"},
                ],
            }
        ]
    }
    assert len(packages_from_trivy_report(raw)) == 1


# --- 검증 반영: 상세 상한 이월·깨진 응답 건별 건너뛰기·별칭 합치기 ---


def _feed(handler: Any, max_details: int = 200) -> OsvAdvisoryFeed:
    return OsvAdvisoryFeed(
        "https://osv.test",
        1.0,
        max_details,
        transport=httpx.MockTransport(handler),
        backlog=DetailBacklog(),
    )


async def test_detail_cap_carries_over_unseen_advisories_to_next_run() -> None:
    hits: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            ids = [{"id": vid} for vid in ("A", "B", "Z-HIGH")]
            return httpx.Response(200, json={"results": [{"vulns": ids}]})
        vid = request.url.path.rsplit("/", 1)[-1]
        hits.append(vid)
        severity = "HIGH" if vid == "Z-HIGH" else "LOW"
        return httpx.Response(200, json={"id": vid, "database_specific": {"severity": severity}})

    feed = _feed(handle, max_details=2)
    package = DependencyPackage("npm", "x", "1.0.0")
    first = await feed.find([package])
    assert (sorted(hits), feed.last_deferred) == (["A", "B"], 1)
    second = await feed.find([package])
    assert "Z-HIGH" in hits[2:]
    assert "Z-HIGH" in {m.advisory.advisory_id for m in second}
    assert len(first) == 2


@pytest.mark.parametrize(
    ("batch", "detail"),
    [
        ({"results": [{"vulns": 1}]}, {"id": "A"}),
        ({"results": [{"vulns": [{"id": "A"}]}]}, {"id": "A", "affected": 1}),
        ({"results": [{"vulns": [{"id": "A"}]}]}, {"id": "A", "severity": 1}),
        ({"results": [{"vulns": [{"id": "A"}]}]}, {"id": "A", "aliases": "CVE-1"}),
    ],
)
async def test_malformed_nested_fields_skip_that_item_and_count_error(
    batch: dict[str, Any], detail: dict[str, Any]
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=batch)
        return httpx.Response(200, json=detail)

    feed = _feed(handle)
    matches = await feed.find([DependencyPackage("npm", "x", "1.0.0")])
    assert matches == []
    assert feed.last_errors == 1


async def test_aliases_of_same_cve_merge_into_one_match() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            ids = [{"id": "CVE-2099-1"}, {"id": "GHSA-x"}]
            return httpx.Response(200, json={"results": [{"vulns": ids}]})
        vid = request.url.path.rsplit("/", 1)[-1]
        body = {
            "id": vid,
            "aliases": ["CVE-2099-1", "GHSA-x"],
            "database_specific": {"severity": "HIGH"},
        }
        return httpx.Response(200, json=body)

    matches = await _feed(handle).find([DependencyPackage("npm", "x", "1.0.0")])
    assert len(matches) == 1
    advisory = matches[0].advisory
    assert {advisory.advisory_id, *advisory.aliases} == {"CVE-2099-1", "GHSA-x"}
    assert canonical_advisory_id(advisory) == "CVE-2099-1"
