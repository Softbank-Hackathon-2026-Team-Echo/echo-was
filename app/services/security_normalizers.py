"""검사 원문(Trivy·gitleaks·TLS 접속 시험)을 공통 증거(SecurityFinding)로 바꾼다(순수 계산).

- **비밀값 원문은 남기지 않는다.** gitleaks 의 `Secret`·`Match`·작성자 정보, Trivy 비밀 결과의
  `Match`·`Code` 는 읽지 않는다. 근거로 남기는 것은 규칙 ID·파일·줄·커밋뿐이다.
- 결과 판단은 규칙표 버전에 묶인다. 만든 증거에 `POLICY_VERSION` 을 적는다.
- 원문이 기대한 대상(digest·설정 커밋)의 것이 아니거나 모양이 틀리면 UNKNOWN 증거를 만든다.
- 만료는 수집 시각 + 그 통제의 유효기간이다.
- 근거 줄은 심각도 순으로 최대 `MAX_ITEMS` 개만 남기고 전체 개수는 따로 적는다.

판단 기준
- C-IMG-1(이미지 취약점): HIGH 이상 + 수정 버전 있음 → FAIL.
  그 밖의 취약점이 있으면 WARN. 없으면 PASS.
- C-IMG-2(시크릿 노출, gitleaks·Trivy secret): 하나라도 있으면 FAIL. 없으면 PASS.
- C-CFG-1(설정 위반): 실패한 HIGH 이상 → FAIL. 그 밖의 실패 → WARN. 없으면 PASS.
- C-TLS-1(공개 경로 TLS): 주소마다 접속 시험 결과의 가장 나쁜 값. 시간 초과·연결 불가는 UNKNOWN.
  사설 대역만 나온 주소는 NA(공개 경로가 아님), 의심 주소·Echo 가 만들지 않은 주소는 UNKNOWN.
- C-IMG-3(출발·도착 이미지 일치, WAS 상태): 두 현재 배포의 digest 가 같으면 PASS, 다르거나 한쪽에
  현재 배포가 없으면 FAIL.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from app.clients.tls_probe import TlsProbeResult
from app.enums import (
    SecurityControlId,
    SecurityFindingMode,
    SecurityFindingReason,
    SecurityFindingSource,
    SecurityResult,
)
from app.services.security_rules import (
    POLICY_VERSION,
    SecurityFinding,
    SecurityScope,
    get_rule,
)

MAX_ITEMS = 50

TRIVY_SCHEMA_VERSION = 2

_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNKNOWN": 4}
_HIGH_OR_ABOVE = frozenset({"CRITICAL", "HIGH"})

_TLS_RESULT = {
    SecurityFindingReason.TLS_OK: SecurityResult.PASS,
    SecurityFindingReason.TLS_TIMEOUT: SecurityResult.UNKNOWN,
    SecurityFindingReason.TLS_UNREACHABLE: SecurityResult.UNKNOWN,
    SecurityFindingReason.TLS_HOST_NOT_PUBLIC: SecurityResult.NA,
    SecurityFindingReason.TLS_HOST_SUSPICIOUS: SecurityResult.UNKNOWN,
    SecurityFindingReason.TLS_HOST_NOT_ALLOWED: SecurityResult.UNKNOWN,
}

_RESULT_ORDER = {
    SecurityResult.NA: 0,
    SecurityResult.PASS: 1,
    SecurityResult.WARN: 2,
    SecurityResult.UNKNOWN: 3,
    SecurityResult.FAIL: 4,
}


@dataclass(frozen=True)
class FindingItem:
    """증거 근거 한 줄(취약점·설정 위반·비밀 발견·TLS 주소). 비밀값 원문은 담지 않는다."""

    item_id: str
    severity: str | None = None
    target: str | None = None
    installed_version: str | None = None
    fixed_version: str | None = None
    line: int | None = None
    commit: str | None = None
    reason: SecurityFindingReason | None = None


@dataclass(frozen=True)
class NormalizedFinding:
    finding: SecurityFinding
    reason: SecurityFindingReason
    items: tuple[FindingItem, ...]
    total_items: int
    tool_version: str | None


def normalize_trivy_image(
    report: Mapping[str, Any],
    *,
    scope: SecurityScope,
    collected_at: datetime,
    tool_version: str | None = None,
    mode: SecurityFindingMode = SecurityFindingMode.REAL,
) -> NormalizedFinding:
    """`trivy image --format json` 결과 → C-IMG-1."""
    control = SecurityControlId.IMG_1
    source = SecurityFindingSource.TRIVY_IMAGE
    results = _trivy_results(report)
    if results is None:
        return _unknown(control, source, scope, collected_at, mode, tool_version)
    if not _is_same_image(report, scope.image_digest):
        return _unknown(
            control,
            source,
            scope,
            collected_at,
            mode,
            tool_version,
            SecurityFindingReason.ARTIFACT_MISMATCH,
        )

    vulnerabilities: dict[tuple[str, str, str], FindingItem] = {}
    for result in results:
        for vulnerability in _list_of_dicts(result.get("Vulnerabilities")):
            item = FindingItem(
                item_id=_text(vulnerability.get("VulnerabilityID")) or "UNKNOWN",
                severity=_severity(vulnerability.get("Severity")),
                target=_text(vulnerability.get("PkgName")),
                installed_version=_text(vulnerability.get("InstalledVersion")),
                fixed_version=_text(vulnerability.get("FixedVersion")),
            )
            key = (item.item_id, item.target or "", item.installed_version or "")
            vulnerabilities.setdefault(key, item)

    items = list(vulnerabilities.values())
    fixable_high = [i for i in items if i.severity in _HIGH_OR_ABOVE and i.fixed_version]
    if fixable_high:
        result_value, reason, shown = (
            SecurityResult.FAIL,
            SecurityFindingReason.FIXABLE_HIGH_VULNERABILITY,
            fixable_high,
        )
    elif items:
        result_value, reason, shown = (
            SecurityResult.WARN,
            SecurityFindingReason.VULNERABILITY_WARNING,
            items,
        )
    else:
        result_value, reason, shown = SecurityResult.PASS, SecurityFindingReason.NO_ISSUES, []
    return _build(
        control, source, scope, collected_at, mode, tool_version, result_value, reason, shown
    )


def normalize_trivy_image_secrets(
    report: Mapping[str, Any],
    *,
    scope: SecurityScope,
    collected_at: datetime,
    tool_version: str | None = None,
    mode: SecurityFindingMode = SecurityFindingMode.REAL,
) -> NormalizedFinding:
    """비밀 검사(`--scanners secret`)를 켠 `trivy image` 결과 → C-IMG-2. 끈 결과를 넣지 않는다."""
    control = SecurityControlId.IMG_2
    source = SecurityFindingSource.TRIVY_SECRET
    results = _trivy_results(report)
    if results is None:
        return _unknown(control, source, scope, collected_at, mode, tool_version)
    if not _is_same_image(report, scope.image_digest):
        return _unknown(
            control,
            source,
            scope,
            collected_at,
            mode,
            tool_version,
            SecurityFindingReason.ARTIFACT_MISMATCH,
        )
    items = [
        FindingItem(
            item_id=_text(secret.get("RuleID")) or "UNKNOWN",
            severity=_severity(secret.get("Severity")),
            target=_text(result.get("Target")),
            line=_int(secret.get("StartLine")),
        )
        for result in results
        for secret in _list_of_dicts(result.get("Secrets"))
    ]
    return _secret_finding(control, source, scope, collected_at, mode, tool_version, items)


def normalize_gitleaks(
    report: object,
    *,
    scope: SecurityScope,
    collected_at: datetime,
    tool_version: str | None = None,
    mode: SecurityFindingMode = SecurityFindingMode.REAL,
) -> NormalizedFinding:
    """`gitleaks --report-format json` 결과(발견 목록) → C-IMG-2. 빈 목록이면 PASS."""
    control = SecurityControlId.IMG_2
    source = SecurityFindingSource.GITLEAKS
    if not isinstance(report, list) or not all(isinstance(leak, dict) for leak in report):
        return _unknown(control, source, scope, collected_at, mode, tool_version)
    items = [
        FindingItem(
            item_id=_text(leak.get("RuleID")) or "UNKNOWN",
            target=_text(leak.get("File")),
            line=_int(leak.get("StartLine")),
            commit=_text(leak.get("Commit")),
        )
        for leak in report
    ]
    return _secret_finding(control, source, scope, collected_at, mode, tool_version, items)


def normalize_trivy_config(
    report: Mapping[str, Any],
    *,
    scope: SecurityScope,
    collected_at: datetime,
    tool_version: str | None = None,
    mode: SecurityFindingMode = SecurityFindingMode.REAL,
) -> NormalizedFinding:
    """`trivy config --format json` 결과 → C-CFG-1. 설정 커밋은 검사 워크플로가 알려 준 값이다."""
    control = SecurityControlId.CFG_1
    source = SecurityFindingSource.TRIVY_CONFIG
    results = _trivy_results(report)
    if results is None:
        return _unknown(control, source, scope, collected_at, mode, tool_version)
    if not scope.config_commit:
        return _unknown(
            control,
            source,
            scope,
            collected_at,
            mode,
            tool_version,
            SecurityFindingReason.ARTIFACT_MISMATCH,
        )
    items = [
        FindingItem(
            item_id=_text(misconfig.get("AVDID")) or _text(misconfig.get("ID")) or "UNKNOWN",
            severity=_severity(misconfig.get("Severity")),
            target=_text(result.get("Target")),
        )
        for result in results
        for misconfig in _list_of_dicts(result.get("Misconfigurations"))
        if misconfig.get("Status") == "FAIL"
    ]
    high = [i for i in items if i.severity in _HIGH_OR_ABOVE]
    if high:
        result_value, reason, shown = (
            SecurityResult.FAIL,
            SecurityFindingReason.MISCONFIGURATION_HIGH,
            high,
        )
    elif items:
        result_value, reason, shown = (
            SecurityResult.WARN,
            SecurityFindingReason.MISCONFIGURATION_WARNING,
            items,
        )
    else:
        result_value, reason, shown = SecurityResult.PASS, SecurityFindingReason.NO_ISSUES, []
    return _build(
        control, source, scope, collected_at, mode, tool_version, result_value, reason, shown
    )


def normalize_tls_probes(
    probes: Sequence[TlsProbeResult],
    *,
    scope: SecurityScope,
    collected_at: datetime,
    control: SecurityControlId = SecurityControlId.TLS_1,
    mode: SecurityFindingMode = SecurityFindingMode.REAL,
) -> NormalizedFinding:
    """주소별 TLS 접속 시험 → C-TLS-1(또는 C-POST-1 의 TLS 부분). 주소가 없으면 UNKNOWN."""
    source = SecurityFindingSource.TLS_PROBE
    if not probes:
        return _unknown(control, source, scope, collected_at, mode, None)
    items = [
        FindingItem(item_id=f"{p.host}:{p.port}", target=p.host, reason=p.reason) for p in probes
    ]
    worst = max(probes, key=lambda p: _RESULT_ORDER[_tls_result(p.reason)])
    result_value = _tls_result(worst.reason)
    shown = [i for i in items if i.reason != SecurityFindingReason.TLS_OK] or items
    return _build(
        control, source, scope, collected_at, mode, None, result_value, worst.reason, shown
    )


def _tls_result(reason: SecurityFindingReason) -> SecurityResult:
    return _TLS_RESULT.get(reason, SecurityResult.FAIL)


def normalize_image_match(
    source_digest: str | None,
    target_digest: str | None,
    *,
    scope: SecurityScope,
    collected_at: datetime,
) -> NormalizedFinding:
    """출발·도착 현재 배포의 digest(WAS 상태) → C-IMG-3. 실행 시점에 WAS 가 DB 에서 읽은 값이다."""
    is_same = source_digest is not None and source_digest == target_digest
    items = [
        FindingItem(item_id="source", target=source_digest),
        FindingItem(item_id="target", target=target_digest),
    ]
    return _build(
        SecurityControlId.IMG_3,
        SecurityFindingSource.WAS_STATE,
        scope,
        collected_at,
        SecurityFindingMode.REAL,
        None,
        SecurityResult.PASS if is_same else SecurityResult.FAIL,
        SecurityFindingReason.IMAGE_MATCH if is_same else SecurityFindingReason.IMAGE_MISMATCH,
        items,
    )


def _secret_finding(
    control: SecurityControlId,
    source: SecurityFindingSource,
    scope: SecurityScope,
    collected_at: datetime,
    mode: SecurityFindingMode,
    tool_version: str | None,
    items: list[FindingItem],
) -> NormalizedFinding:
    if items:
        return _build(
            control,
            source,
            scope,
            collected_at,
            mode,
            tool_version,
            SecurityResult.FAIL,
            SecurityFindingReason.SECRET_FOUND,
            items,
        )
    return _build(
        control,
        source,
        scope,
        collected_at,
        mode,
        tool_version,
        SecurityResult.PASS,
        SecurityFindingReason.NO_ISSUES,
        [],
    )


def _unknown(
    control: SecurityControlId,
    source: SecurityFindingSource,
    scope: SecurityScope,
    collected_at: datetime,
    mode: SecurityFindingMode,
    tool_version: str | None,
    reason: SecurityFindingReason = SecurityFindingReason.REPORT_INVALID,
) -> NormalizedFinding:
    return _build(
        control, source, scope, collected_at, mode, tool_version, SecurityResult.UNKNOWN, reason, []
    )


def _build(
    control: SecurityControlId,
    source: SecurityFindingSource,
    scope: SecurityScope,
    collected_at: datetime,
    mode: SecurityFindingMode,
    tool_version: str | None,
    result: SecurityResult,
    reason: SecurityFindingReason,
    items: Iterable[FindingItem],
) -> NormalizedFinding:
    ordered = sorted(items, key=lambda i: (_SEVERITY_ORDER.get(i.severity or "", 5), i.item_id))
    finding = SecurityFinding(
        finding_id=uuid4(),
        control_id=control,
        result=result,
        mode=mode,
        source=source,
        scope=scope,
        collected_at=collected_at,
        expires_at=collected_at + get_rule(control).max_age,
        policy_version=POLICY_VERSION,
    )
    return NormalizedFinding(
        finding=finding,
        reason=reason,
        items=tuple(ordered[:MAX_ITEMS]),
        total_items=len(ordered),
        tool_version=tool_version,
    )


def _trivy_results(report: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    """모양이 맞으면 Results 목록(없으면 빈 목록), 틀리면 None."""
    if not isinstance(report, Mapping) or report.get("SchemaVersion") != TRIVY_SCHEMA_VERSION:
        return None
    results = report.get("Results", [])
    if results is None:
        return []
    if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
        return None
    return results


def _is_same_image(report: Mapping[str, Any], image_digest: str | None) -> bool:
    if not image_digest:
        return False
    metadata = report.get("Metadata")
    repo_digests = metadata.get("RepoDigests") if isinstance(metadata, Mapping) else None
    names = [*(repo_digests if isinstance(repo_digests, list) else []), report.get("ArtifactName")]
    return any(isinstance(name, str) and name.endswith(f"@{image_digest}") for name in names)


def _list_of_dicts(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _severity(value: object) -> str | None:
    text = _text(value)
    return text.upper() if text else None
