"""공개 취약점 피드 OSV.dev 클라이언트(SPEC sec-6-cve-watch). 무료·인증 없음.

- `POST /v1/querybatch` 로 패키지(생태계·이름·버전)마다 걸린 취약점 id 를 받고,
  `GET /v1/vulns/{id}` 로 상세(심각도·영향 범위·별칭)를 받는다(querybatch 는 id 만 준다).
- 심각도: GitHub 검토 권고의 `database_specific.severity`(MODERATE → MEDIUM)를 먼저 쓰고,
  없으면 `severity[]` 의 CVSS v3 벡터로 기본 점수를 계산한다(9.0↑ CRITICAL, 7.0↑ HIGH, 4.0↑ MEDIUM).
  둘 다 없으면 UNKNOWN 이다(지어내지 않는다).
- 고칠 버전: 그 패키지 `affected[].ranges` 중 설치 버전이 든 범위의 `fixed`.
- 한 번에 상세를 받는 취약점 수는 `max_details` 로 막는다. 넘은 id 는 미처리 목록
  (`DetailBacklog`, 프로세스 메모리)에 남겨 다음 조회 때 먼저 본다 — 상한이 작아도 돌아가며 모두
  본다. 이번에 못 본 수는 `last_deferred` 로 알린다(일꾼이 다시 뜨면 미처리 목록은 비어 처음부터).
- 고칠 버전: 배포판 생태계(`Debian:12`)는 같은 판의 `affected` 만 쓴다(다른 판의 fixed 를 고르지
  않는다). 판 번호가 없는 `affected` 는 모든 판에 쓴다.
- 같은 취약점을 가리키는 별칭(OSV id·aliases 가 겹치는 것)은 패키지마다 한 건으로 합친다.
- 실패(연결·시간 초과·5xx·최상위 모양 틀림)는 `AdvisoryFeedUnavailableError`(502)다. 결과 한 건·
  상세 한 건의 안쪽 모양이 틀리면 그 건만 건너뛰고 `last_errors` 로 센다.
"""

import asyncio
import math
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.core.exceptions import ExternalError
from app.services.cve_watch_packages import DependencyPackage, package_key
from app.services.cve_watch_versions import fixed_version_for

QUERY_BATCH_SIZE = 500
BACKLOG_MAX = 10_000
DETAIL_CONCURRENCY = 8
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")
_GHSA_SEVERITY = {
    "CRITICAL": "CRITICAL",
    "HIGH": "HIGH",
    "MODERATE": "MEDIUM",
    "MEDIUM": "MEDIUM",
    "LOW": "LOW",
}


class AdvisoryFeedUnavailableError(ExternalError):
    code = "CVE_FEED_UNAVAILABLE"


class MalformedAdvisoryError(ValueError):
    """OSV 결과·상세 한 건의 안쪽 모양이 틀렸다(그 건만 건너뛴다)."""


class DetailBacklog:
    """상세 상한 때문에 못 본 취약점 id(먼저 못 본 순서). 다음 조회 때 앞에 둔다."""

    def __init__(self, max_size: int = BACKLOG_MAX) -> None:
        self._ids: OrderedDict[str, None] = OrderedDict()
        self._max_size = max_size

    def select(self, wanted: Iterable[str], limit: int) -> tuple[list[str], int]:
        """이번에 상세를 받을 id 와 못 본(미뤄진) 수. 미처리 목록의 id 가 먼저다."""
        wanted_set = set(wanted)
        first = [vid for vid in self._ids if vid in wanted_set]
        rest = sorted(wanted_set.difference(first))
        ordered = first + rest
        chosen, deferred = ordered[:limit], ordered[limit:]
        for vid in chosen:
            self._ids.pop(vid, None)
        for vid in deferred:
            self._ids.setdefault(vid, None)
        while len(self._ids) > self._max_size:
            self._ids.popitem(last=False)
        return chosen, len(deferred)

    def __len__(self) -> int:
        return len(self._ids)


# 프로세스 하나(일꾼 또는 API 파드)가 함께 쓰는 미처리 목록.
_SHARED_BACKLOG = DetailBacklog()


@dataclass(frozen=True)
class Advisory:
    """공개 권고 하나(패키지 하나 기준)."""

    advisory_id: str
    aliases: tuple[str, ...]
    summary: str | None
    severity: str
    url: str | None


@dataclass(frozen=True)
class AdvisoryMatch:
    """설치된 의존성 하나에 걸린 권고 하나. fixed_version 이 없으면 아직 고친 버전이 없다."""

    package: DependencyPackage
    advisory: Advisory
    fixed_version: str | None


class AdvisoryFeed(Protocol):
    async def find(self, packages: Sequence[DependencyPackage]) -> list[AdvisoryMatch]: ...


class OsvAdvisoryFeed:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        max_details: int,
        transport: httpx.AsyncBaseTransport | None = None,
        backlog: DetailBacklog | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_details = max_details
        self._transport = transport
        self._backlog = backlog if backlog is not None else _SHARED_BACKLOG
        # 마지막 조회에서 상한 때문에 미룬 취약점 수·모양이 틀려 건너뛴 건수.
        self.last_deferred = 0
        self.last_errors = 0

    async def find(self, packages: Sequence[DependencyPackage]) -> list[AdvisoryMatch]:
        self.last_deferred = 0
        self.last_errors = 0
        if not packages:
            return []
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout, transport=self._transport
            ) as client:
                ids_by_package = await self._query(client, packages)
                wanted = {vid for ids in ids_by_package.values() for vid in ids}
                chosen, self.last_deferred = self._backlog.select(wanted, self._max_details)
                details = await self._details(client, chosen)
        except httpx.HTTPError as exc:
            raise AdvisoryFeedUnavailableError(
                "OSV request failed", error_type=type(exc).__name__
            ) from exc
        matches: list[AdvisoryMatch] = []
        for package, ids in ids_by_package.items():
            found: list[AdvisoryMatch] = []
            for vid in ids:
                vuln = details.get(vid)
                if vuln is None:
                    continue
                try:
                    found.append(match_from_osv(package, vuln))
                except (MalformedAdvisoryError, TypeError, ValueError, AttributeError):
                    self.last_errors += 1
            matches.extend(merge_aliases(found))
        return matches

    async def _query(
        self, client: httpx.AsyncClient, packages: Sequence[DependencyPackage]
    ) -> dict[DependencyPackage, list[str]]:
        found: dict[DependencyPackage, list[str]] = {}
        for start in range(0, len(packages), QUERY_BATCH_SIZE):
            chunk = list(packages[start : start + QUERY_BATCH_SIZE])
            body = {
                "queries": [
                    {
                        "package": {"name": package.name, "ecosystem": package.ecosystem},
                        "version": package.version,
                    }
                    for package in chunk
                ]
            }
            response = await client.post("/v1/querybatch", json=body)
            response.raise_for_status()
            results = _json_object(response).get("results")
            if not isinstance(results, list) or len(results) != len(chunk):
                raise AdvisoryFeedUnavailableError("OSV querybatch response invalid")
            for package, result in zip(chunk, results, strict=True):
                vulns = result.get("vulns") if isinstance(result, Mapping) else None
                if vulns is not None and not isinstance(vulns, list):
                    self.last_errors += 1
                    continue
                ids = [
                    str(v["id"])
                    for v in vulns or []
                    if isinstance(v, Mapping) and isinstance(v.get("id"), str) and v.get("id")
                ]
                if ids:
                    found[package] = ids
        return found

    async def _details(
        self, client: httpx.AsyncClient, ids: Sequence[str]
    ) -> dict[str, Mapping[str, Any]]:
        semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def fetch(vid: str) -> tuple[str, Mapping[str, Any]]:
            async with semaphore:
                response = await client.get(f"/v1/vulns/{vid}")
                response.raise_for_status()
                return vid, _json_object(response)

        pairs = await asyncio.gather(*(fetch(vid) for vid in ids))
        return dict(pairs)


def _list_field(body: Mapping[str, Any], name: str) -> list[Any]:
    """배열이어야 하는 칸. 없으면 빈 목록, 배열이 아니면 MalformedAdvisoryError."""
    value = body.get(name)
    if value is None:
        return []
    if not isinstance(value, list):
        raise MalformedAdvisoryError(f"OSV field {name} is not a list")
    return value


def _same_release(package_ecosystem: str, affected_ecosystem: str) -> bool:
    """배포판 생태계(`Debian:12`)는 같은 판이어야 한다. 한쪽에 판 번호가 없으면 같다고 본다."""
    package_family, _, package_release = package_ecosystem.partition(":")
    affected_family, _, affected_release = affected_ecosystem.partition(":")
    if package_family.strip().lower() != affected_family.strip().lower():
        return False
    if not package_release or not affected_release:
        return True
    return package_release.strip().lower() == affected_release.strip().lower()


def match_from_osv(package: DependencyPackage, vuln: Mapping[str, Any]) -> AdvisoryMatch:
    """OSV 취약점 상세 하나 → 그 패키지의 권고·고칠 버전. 모양이 틀리면 MalformedAdvisoryError."""
    vid = vuln.get("id")
    if not isinstance(vid, str) or not vid:
        raise MalformedAdvisoryError("OSV vuln id missing")
    aliases = tuple(a for a in _list_field(vuln, "aliases") if isinstance(a, str) and a != vid)
    summary = vuln.get("summary")
    ranges: list[Mapping[str, Any]] = []
    for affected in _list_field(vuln, "affected"):
        if not isinstance(affected, Mapping):
            continue
        affected_package = affected.get("package")
        if not isinstance(affected_package, Mapping):
            continue
        name, ecosystem = affected_package.get("name"), affected_package.get("ecosystem")
        if not isinstance(name, str) or not isinstance(ecosystem, str):
            continue
        if package_key(ecosystem, name) != package.key:
            continue
        if not _same_release(package.ecosystem, ecosystem):
            continue
        ranges.extend(r for r in _list_field(affected, "ranges") if isinstance(r, Mapping))
    advisory = Advisory(
        advisory_id=vid,
        aliases=aliases,
        summary=summary[:300] if isinstance(summary, str) else None,
        severity=severity_of(vuln),
        url=f"https://osv.dev/vulnerability/{vid}",
    )
    return AdvisoryMatch(
        package, advisory, fixed_version_for(package.version, ranges, package.ecosystem)
    )


_SEVERITY_RANK = {name: rank for rank, name in enumerate(reversed(SEVERITIES))}


def canonical_advisory_id(advisory: Advisory) -> str:
    """별칭까지 본 대표 id: CVE 가 있으면 가장 작은 CVE, 없으면 가장 작은 id."""
    ids = {advisory.advisory_id, *advisory.aliases}
    cves = sorted(vid for vid in ids if vid.upper().startswith("CVE-"))
    return cves[0] if cves else min(ids)


def merge_aliases(matches: Sequence[AdvisoryMatch]) -> list[AdvisoryMatch]:
    """한 패키지에 걸린 권고 중 id·별칭이 겹치는 것을 한 건으로 합친다.

    대표: 심각도가 높은 것 → 고친 버전이 있는 것 → id 가 작은 것. 별칭은 모두 모은다.
    """
    groups: list[tuple[set[str], list[AdvisoryMatch]]] = []
    for match in matches:
        ids = {match.advisory.advisory_id, *match.advisory.aliases}
        joined = [group for group in groups if group[0] & ids]
        merged_ids, merged_matches = set(ids), [match]
        for group in joined:
            merged_ids |= group[0]
            merged_matches.extend(group[1])
            groups.remove(group)
        groups.append((merged_ids, merged_matches))
    result: list[AdvisoryMatch] = []
    for ids, members in groups:
        best = min(
            members,
            key=lambda m: (
                -_SEVERITY_RANK.get(m.advisory.severity, 0),
                m.fixed_version is None,
                m.advisory.advisory_id,
            ),
        )
        aliases = tuple(sorted(ids - {best.advisory.advisory_id}))
        advisory = Advisory(
            advisory_id=best.advisory.advisory_id,
            aliases=aliases,
            summary=best.advisory.summary,
            severity=best.advisory.severity,
            url=best.advisory.url,
        )
        result.append(AdvisoryMatch(best.package, advisory, best.fixed_version))
    return result


def severity_of(vuln: Mapping[str, Any]) -> str:
    specific = vuln.get("database_specific")
    if isinstance(specific, Mapping):
        label = specific.get("severity")
        if isinstance(label, str) and label.upper() in _GHSA_SEVERITY:
            return _GHSA_SEVERITY[label.upper()]
    best: float | None = None
    for entry in _list_field(vuln, "severity"):
        if not isinstance(entry, Mapping) or entry.get("type") != "CVSS_V3":
            continue
        score = cvss3_base_score(str(entry.get("score") or ""))
        if score is not None and (best is None or score > best):
            best = score
    if best is None:
        return "UNKNOWN"
    if best >= 9.0:
        return "CRITICAL"
    if best >= 7.0:
        return "HIGH"
    if best >= 4.0:
        return "MEDIUM"
    return "LOW" if best > 0 else "UNKNOWN"


_AV = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}
_AC = {"L": 0.77, "H": 0.44}
_UI = {"N": 0.85, "R": 0.62}
_CIA = {"H": 0.56, "L": 0.22, "N": 0.0}


def cvss3_base_score(vector: str) -> float | None:
    """CVSS v3.0·v3.1 벡터의 기본 점수(FIRST 명세 공식). 모양이 틀리면 None."""
    if not vector.startswith("CVSS:3."):
        return None
    metrics = dict(part.split(":", 1) for part in vector.split("/")[1:] if ":" in part)
    try:
        scope_changed = metrics["S"] == "C"
        privileges = {
            "N": 0.85,
            "L": 0.68 if scope_changed else 0.62,
            "H": 0.5 if scope_changed else 0.27,
        }
        exploitability = (
            8.22
            * _AV[metrics["AV"]]
            * _AC[metrics["AC"]]
            * privileges[metrics["PR"]]
            * _UI[metrics["UI"]]
        )
        isc_base = 1 - (
            (1 - _CIA[metrics["C"]]) * (1 - _CIA[metrics["I"]]) * (1 - _CIA[metrics["A"]])
        )
    except KeyError:
        return None
    if scope_changed:
        impact = 7.52 * (isc_base - 0.029) - 3.25 * (isc_base - 0.02) ** 15
    else:
        impact = 6.42 * isc_base
    if impact <= 0:
        return 0.0
    raw = 1.08 * (impact + exploitability) if scope_changed else impact + exploitability
    return _round_up(min(raw, 10.0))


def _round_up(value: float) -> float:
    as_int = round(value * 100000)
    if as_int % 10000 == 0:
        return as_int / 100000.0
    return (math.floor(as_int / 10000) + 1) / 10.0


def _json_object(response: httpx.Response) -> Mapping[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise AdvisoryFeedUnavailableError("OSV response is not JSON") from exc
    if not isinstance(body, Mapping):
        raise AdvisoryFeedUnavailableError("OSV response is not an object")
    return body
