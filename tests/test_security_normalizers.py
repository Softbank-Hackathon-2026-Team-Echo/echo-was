"""검사 원문 → 공통 증거 정규화(순수 계산). SPEC sec-2 AC-1~AC-3·AC-5."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from app.clients.tls_probe import TlsProbeResult
from app.enums import (
    SecurityCheckPhase,
    SecurityControlId,
    SecurityFindingMode,
    SecurityFindingReason,
    SecurityFindingSource,
    SecurityResult,
)
from app.services.security_normalizers import (
    MAX_ITEMS,
    FindingItem,
    NormalizedFinding,
    normalize_gitleaks,
    normalize_image_match,
    normalize_secret_delivery,
    normalize_tls_probes,
    normalize_trivy_config,
    normalize_trivy_image,
    normalize_trivy_image_secrets,
)
from app.services.security_rules import (
    POLICY_VERSION,
    SecurityScope,
    SecurityTarget,
    evaluate_security,
)

NOW = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)
ORG = UUID("00000000-0000-0000-0000-0000000000a1")
SERVICE = UUID("00000000-0000-0000-0000-0000000000b1")
ENV = UUID("00000000-0000-0000-0000-0000000000c1")
DIGEST = "sha256:" + "a" * 64
REPO = "ghcr.io/softbank-hackathon-2026-team-echo/echo-demo-app"
SCOPE = SecurityScope(
    organization_id=ORG,
    service_id=SERVICE,
    environment_id=ENV,
    image_digest=DIGEST,
    config_commit="c" * 40,
    was_version="b7c108c",
)
FAKE_SECRET = "not-a-real-secret-value-0001"

R = SecurityResult
Reason = SecurityFindingReason


def trivy_image_report(
    vulnerabilities: list[dict[str, Any]] | None = None,
    *,
    secrets: list[dict[str, Any]] | None = None,
    digest: str = DIGEST,
) -> dict[str, Any]:
    result: dict[str, Any] = {"Target": "Python", "Class": "lang-pkgs", "Type": "python-pkg"}
    if vulnerabilities is not None:
        result["Vulnerabilities"] = vulnerabilities
    if secrets is not None:
        result["Secrets"] = secrets
    return {
        "SchemaVersion": 2,
        "ArtifactName": f"{REPO}@{digest}",
        "ArtifactType": "container_image",
        "Metadata": {"RepoDigests": [f"{REPO}@{digest}"]},
        "Results": [result],
    }


def vuln(cve: str, severity: str, fixed: str | None, pkg: str = "urllib3") -> dict[str, Any]:
    entry = {
        "VulnerabilityID": cve,
        "PkgName": pkg,
        "InstalledVersion": "2.7.0",
        "Severity": severity,
    }
    if fixed is not None:
        entry["FixedVersion"] = fixed
    return entry


def image(report: dict[str, Any], **kwargs: Any) -> NormalizedFinding:
    return normalize_trivy_image(report, scope=SCOPE, collected_at=NOW, **kwargs)


# --- C-IMG-1 이미지 취약점 (AC-1) ---


def test_trivy_image_fixable_high_is_fail_with_items() -> None:
    normalized = image(
        trivy_image_report(
            [
                vuln("CVE-2026-0001", "HIGH", "2.8.0"),
                vuln("CVE-2026-0002", "MEDIUM", "2.8.0"),
                vuln("CVE-2026-0003", "CRITICAL", "2.8.1", pkg="setuptools"),
            ]
        ),
        tool_version="0.75.0",
    )
    finding = normalized.finding
    assert finding.control_id == SecurityControlId.IMG_1
    assert finding.source == SecurityFindingSource.TRIVY_IMAGE
    assert finding.result == R.FAIL
    assert normalized.reason == Reason.FIXABLE_HIGH_VULNERABILITY
    assert [i.item_id for i in normalized.items] == ["CVE-2026-0003", "CVE-2026-0001"]
    assert normalized.items[1].fixed_version == "2.8.0"
    assert normalized.tool_version == "0.75.0"
    assert finding.policy_version == POLICY_VERSION
    assert finding.collected_at == NOW
    assert finding.expires_at == NOW + timedelta(days=7)
    assert finding.scope == SCOPE


def test_trivy_image_unfixed_high_and_medium_are_warn() -> None:
    normalized = image(
        trivy_image_report(
            [vuln("CVE-2026-0004", "HIGH", None), vuln("CVE-2026-0005", "LOW", "2.8.0")]
        )
    )
    assert normalized.finding.result == R.WARN
    assert normalized.reason == Reason.VULNERABILITY_WARNING
    assert normalized.total_items == 2


def test_trivy_image_without_vulnerabilities_is_pass() -> None:
    normalized = image(trivy_image_report([]))
    assert normalized.finding.result == R.PASS
    assert normalized.reason == Reason.NO_ISSUES
    assert normalized.items == ()


def test_trivy_image_counts_duplicate_vulnerability_once() -> None:
    report = trivy_image_report([vuln("CVE-2026-0001", "HIGH", "2.8.0")] * 3)
    assert image(report).total_items == 1


def test_trivy_image_keeps_at_most_max_items_but_counts_all() -> None:
    many = [vuln(f"CVE-2026-{n:04d}", "HIGH", "9.9.9", pkg=f"p{n}") for n in range(MAX_ITEMS + 7)]
    normalized = image(trivy_image_report(many))
    assert len(normalized.items) == MAX_ITEMS
    assert normalized.total_items == MAX_ITEMS + 7


def test_trivy_image_for_other_digest_is_unknown() -> None:
    normalized = image(trivy_image_report([], digest="sha256:" + "b" * 64))
    assert normalized.finding.result == R.UNKNOWN
    assert normalized.reason == Reason.ARTIFACT_MISMATCH


def test_trivy_image_without_target_digest_is_unknown() -> None:
    normalized = normalize_trivy_image(
        trivy_image_report([]), scope=replace(SCOPE, image_digest=None), collected_at=NOW
    )
    assert normalized.reason == Reason.ARTIFACT_MISMATCH


@pytest.mark.parametrize(
    "report",
    [
        {},
        {"SchemaVersion": 1, "Results": []},
        {"SchemaVersion": 2, "Results": "broken"},
        {"SchemaVersion": 2, "Results": [1, 2]},
    ],
)
def test_trivy_image_malformed_report_is_unknown(report: dict[str, Any]) -> None:
    normalized = image(report)
    assert normalized.finding.result == R.UNKNOWN
    assert normalized.reason == Reason.REPORT_INVALID


def test_trivy_image_report_without_results_is_pass() -> None:
    report = trivy_image_report([])
    del report["Results"]
    assert image(report).finding.result == R.PASS


def test_simulated_mode_is_kept_on_the_finding() -> None:
    normalized = image(trivy_image_report([]), mode=SecurityFindingMode.SIMULATED)
    assert normalized.finding.mode == SecurityFindingMode.SIMULATED


# --- C-IMG-2 시크릿 노출 (AC-2) ---


def test_trivy_secrets_found_is_fail_without_secret_text() -> None:
    report = trivy_image_report(
        secrets=[
            {
                "RuleID": "generic-api-key",
                "Category": "Generic",
                "Severity": "CRITICAL",
                "Title": "Generic API Key",
                "StartLine": 12,
                "Match": f"API_KEY={FAKE_SECRET}",
                "Code": {"Lines": [{"Content": FAKE_SECRET}]},
            }
        ]
    )
    normalized = normalize_trivy_image_secrets(report, scope=SCOPE, collected_at=NOW)
    assert normalized.finding.control_id == SecurityControlId.IMG_2
    assert normalized.finding.source == SecurityFindingSource.TRIVY_SECRET
    assert normalized.finding.result == R.FAIL
    assert normalized.reason == Reason.SECRET_FOUND
    assert normalized.items[0].item_id == "generic-api-key"
    assert normalized.items[0].line == 12
    assert FAKE_SECRET not in repr(normalized)


def test_trivy_secrets_none_found_is_pass() -> None:
    normalized = normalize_trivy_image_secrets(
        trivy_image_report(secrets=[]), scope=SCOPE, collected_at=NOW
    )
    assert normalized.finding.result == R.PASS


def test_gitleaks_leak_is_fail_and_drops_secret_and_author() -> None:
    report = [
        {
            "RuleID": "github-pat",
            "Description": "GitHub Personal Access Token",
            "File": "app/settings.py",
            "StartLine": 7,
            "Commit": "d" * 40,
            "Secret": FAKE_SECRET,
            "Match": f"token = '{FAKE_SECRET}'",
            "Author": "someone",
            "Email": "someone@example.com",
            "Fingerprint": f"{'d' * 40}:app/settings.py:github-pat:7",
        }
    ]
    normalized = normalize_gitleaks(report, scope=SCOPE, collected_at=NOW, tool_version="8.28.0")
    assert normalized.finding.source == SecurityFindingSource.GITLEAKS
    assert normalized.finding.result == R.FAIL
    item = normalized.items[0]
    assert (item.item_id, item.target, item.line, item.commit) == (
        "github-pat",
        "app/settings.py",
        7,
        "d" * 40,
    )
    text = repr(normalized)
    assert FAKE_SECRET not in text
    assert "someone" not in text


def test_gitleaks_empty_report_is_pass() -> None:
    assert normalize_gitleaks([], scope=SCOPE, collected_at=NOW).finding.result == R.PASS


@pytest.mark.parametrize("report", [None, {"leaks": []}, ["x"]])
def test_gitleaks_malformed_report_is_unknown(report: object) -> None:
    normalized = normalize_gitleaks(report, scope=SCOPE, collected_at=NOW)
    assert normalized.finding.result == R.UNKNOWN
    assert normalized.reason == Reason.REPORT_INVALID


# --- C-CFG-1 설정 위반 (AC-3) ---


def config_report(misconfigs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "SchemaVersion": 2,
        "ArtifactName": ".",
        "ArtifactType": "filesystem",
        "Results": [{"Target": "Dockerfile", "Class": "config", "Misconfigurations": misconfigs}],
    }


def misconfig(avd_id: str, severity: str, status: str = "FAIL") -> dict[str, Any]:
    return {
        "ID": avd_id.replace("AVD-", ""),
        "AVDID": avd_id,
        "Severity": severity,
        "Status": status,
    }


@pytest.mark.parametrize(
    ("misconfigs", "result", "reason"),
    [
        (
            [misconfig("AVD-DS-0002", "HIGH"), misconfig("AVD-DS-0026", "LOW")],
            R.FAIL,
            Reason.MISCONFIGURATION_HIGH,
        ),
        ([misconfig("AVD-DS-0026", "LOW")], R.WARN, Reason.MISCONFIGURATION_WARNING),
        ([misconfig("AVD-DS-0002", "HIGH", status="PASS")], R.PASS, Reason.NO_ISSUES),
        ([], R.PASS, Reason.NO_ISSUES),
    ],
)
def test_trivy_config_judges_failed_misconfigurations(
    misconfigs: list[dict[str, Any]], result: SecurityResult, reason: SecurityFindingReason
) -> None:
    normalized = normalize_trivy_config(config_report(misconfigs), scope=SCOPE, collected_at=NOW)
    assert normalized.finding.control_id == SecurityControlId.CFG_1
    assert (normalized.finding.result, normalized.reason) == (result, reason)


def test_trivy_config_without_commit_is_unknown() -> None:
    normalized = normalize_trivy_config(
        config_report([]), scope=replace(SCOPE, config_commit=None), collected_at=NOW
    )
    assert normalized.reason == Reason.ARTIFACT_MISMATCH


# --- C-TLS-1 공개 경로 TLS (AC-5) ---


@pytest.mark.parametrize(
    ("reasons", "result", "reason"),
    [
        ([Reason.TLS_OK, Reason.TLS_OK], R.PASS, Reason.TLS_OK),
        ([Reason.TLS_OK, Reason.TLS_TIMEOUT], R.UNKNOWN, Reason.TLS_TIMEOUT),
        ([Reason.TLS_UNREACHABLE, Reason.TLS_EXPIRES_SOON], R.FAIL, Reason.TLS_EXPIRES_SOON),
        ([Reason.TLS_HOSTNAME_MISMATCH], R.FAIL, Reason.TLS_HOSTNAME_MISMATCH),
        # 사설 대역만 나온 도착 주소는 NA, 대표 주소 결과가 공개 경로를 정한다.
        ([Reason.TLS_OK, Reason.TLS_HOST_NOT_PUBLIC], R.PASS, Reason.TLS_OK),
        ([Reason.TLS_HOST_NOT_PUBLIC], R.NA, Reason.TLS_HOST_NOT_PUBLIC),
        ([Reason.TLS_OK, Reason.TLS_HOST_SUSPICIOUS], R.UNKNOWN, Reason.TLS_HOST_SUSPICIOUS),
        ([Reason.TLS_HOST_NOT_ALLOWED], R.UNKNOWN, Reason.TLS_HOST_NOT_ALLOWED),
    ],
)
def test_tls_probes_use_worst_host_result(
    reasons: list[SecurityFindingReason], result: SecurityResult, reason: SecurityFindingReason
) -> None:
    probes = [TlsProbeResult(f"h{n}.echoport.uk", 443, r) for n, r in enumerate(reasons)]
    normalized = normalize_tls_probes(probes, scope=SCOPE, collected_at=NOW)
    assert normalized.finding.control_id == SecurityControlId.TLS_1
    assert (normalized.finding.result, normalized.reason) == (result, reason)
    assert normalized.finding.expires_at == NOW + timedelta(hours=1)


def test_tls_probes_without_hosts_is_unknown() -> None:
    normalized = normalize_tls_probes([], scope=SCOPE, collected_at=NOW)
    assert normalized.finding.result == R.UNKNOWN


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


@pytest.mark.parametrize(
    ("source", "target", "result", "reason"),
    [
        (DIGEST_A, DIGEST_A, R.PASS, Reason.IMAGE_MATCH),
        (DIGEST_A, DIGEST_B, R.FAIL, Reason.IMAGE_MISMATCH),
        (DIGEST_A, None, R.FAIL, Reason.IMAGE_MISMATCH),
        (None, None, R.FAIL, Reason.IMAGE_MISMATCH),
    ],
)
def test_image_match_compares_current_deployment_digests(
    source: str | None, target: str | None, result: SecurityResult, reason: SecurityFindingReason
) -> None:
    normalized = normalize_image_match(source, target, scope=SCOPE, collected_at=NOW)
    finding = normalized.finding
    assert (finding.control_id, finding.source) == (
        SecurityControlId.IMG_3,
        SecurityFindingSource.WAS_STATE,
    )
    assert (finding.result, normalized.reason) == (result, reason)
    assert finding.expires_at == NOW + timedelta(minutes=2)
    assert [(i.item_id, i.target) for i in normalized.items] == [
        ("source", source),
        ("target", target),
    ]


# --- C-SEC-1 비밀 전달 경로: 기준 연결의 키가 대상 연결에 모두 있는가 (sec-1-2 AC-1) ---


@pytest.mark.parametrize(
    ("reference", "target", "result", "reason", "missing"),
    [
        ({"API_KEY", "DB_URL"}, {"API_KEY", "DB_URL", "EXTRA"}, R.PASS, "SECRETS_READY", []),
        (set(), {"API_KEY"}, R.PASS, "SECRETS_READY", []),
        (set(), set(), R.PASS, "SECRETS_READY", []),
        (
            {"DB_URL", "API_KEY", "TOKEN"},
            {"TOKEN"},
            R.FAIL,
            "SECRETS_MISSING",
            ["API_KEY", "DB_URL"],
        ),
    ],
    ids=["all-present", "reference-empty", "both-empty", "missing-keys"],
)
def test_secret_delivery_compares_variable_keys_only(
    reference: set[str], target: set[str], result: SecurityResult, reason: str, missing: list[str]
) -> None:
    normalized = normalize_secret_delivery(reference, target, scope=SCOPE, collected_at=NOW)
    finding = normalized.finding
    assert (finding.control_id, finding.source, finding.mode) == (
        SecurityControlId.SEC_1,
        SecurityFindingSource.WAS_STATE,
        SecurityFindingMode.REAL,
    )
    assert (finding.result, normalized.reason) == (result, SecurityFindingReason(reason))
    assert finding.scope == SCOPE
    assert finding.policy_version == POLICY_VERSION
    # 근거 줄은 빠진 키 이름뿐이다(값·위치·버전 칸 없음).
    assert [i.item_id for i in normalized.items] == missing
    assert all(i == FindingItem(item_id=i.item_id) for i in normalized.items)
    assert normalized.total_items == len(missing)


def test_secret_delivery_blocks_only_its_service_and_target_environment() -> None:
    failing = normalize_secret_delivery({"API_KEY"}, set(), scope=SCOPE, collected_at=NOW).finding
    same = SecurityTarget(scope=SCOPE, environment_kind="gcp")
    other_env = SecurityTarget(
        scope=replace(SCOPE, environment_id=UUID(int=9)), environment_kind="gcp"
    )
    blocked = evaluate_security(same, SecurityCheckPhase.EXECUTION, [failing], [], NOW)
    unrelated = evaluate_security(other_env, SecurityCheckPhase.EXECUTION, [failing], [], NOW)
    assert blocked.get_control(SecurityControlId.SEC_1).is_blocking
    assert blocked.get_control(SecurityControlId.SEC_1).result == R.FAIL
    assert unrelated.get_control(SecurityControlId.SEC_1).result == R.UNKNOWN


# --- 정규화한 증거를 규칙표가 그대로 쓴다 ---


def test_normalized_findings_feed_rule_table() -> None:
    target = SecurityTarget(scope=SCOPE, environment_kind="gcp")
    failing = image(trivy_image_report([vuln("CVE-2026-0001", "HIGH", "2.8.0")])).finding
    decision = evaluate_security(target, SecurityCheckPhase.PREPARATION, [failing], [], NOW)
    control = decision.get_control(SecurityControlId.IMG_1)
    assert control.result == R.FAIL
    assert control.finding_ids == (failing.finding_id,)
