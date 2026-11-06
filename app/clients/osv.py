"""공개 취약점 피드 OSV.dev 클라이언트(SPEC sec-6-cve-watch). 무료·인증 없음.

- `POST /v1/querybatch` 로 패키지(생태계·이름·버전)마다 걸린 취약점 id 를 받고,
  `GET /v1/vulns/{id}` 로 상세(심각도·영향 범위·별칭)를 받는다(querybatch 는 id 만 준다).
- 심각도: GitHub 검토 권고의 `database_specific.severity`(MODERATE → MEDIUM)를 먼저 쓰고,
  없으면 `severity[]` 의 CVSS v3 벡터로 기본 점수를 계산한다(9.0↑ CRITICAL, 7.0↑ HIGH, 4.0↑ MEDIUM).
  둘 다 없으면 UNKNOWN 이다(지어내지 않는다).
- 고칠 버전: 그 패키지 `affected[].ranges` 중 설치 버전이 든 범위의 `fixed`.
- 한 번에 상세를 받는 취약점 수는 `max_details` 로 막는다. 넘으면 나머지는 다음 주기에 본다.
- 실패(연결·시간 초과·5xx·모양 틀림)는 `AdvisoryFeedUnavailableError`(502)다.
"""

import asyncio
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.core.exceptions import ExternalError
from app.services.cve_watch_packages import DependencyPackage, package_key
from app.services.cve_watch_versions import fixed_version_for

QUERY_BATCH_SIZE = 500
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
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_details = max_details
        self._transport = transport

    async def find(self, packages: Sequence[DependencyPackage]) -> list[AdvisoryMatch]:
        if not packages:
            return []
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout, transport=self._transport
            ) as client:
                ids_by_package = await self._query(client, packages)
                wanted = sorted({vid for ids in ids_by_package.values() for vid in ids})
                details = await self._details(client, wanted[: self._max_details])
        except httpx.HTTPError as exc:
            raise AdvisoryFeedUnavailableError(
                "OSV request failed", error_type=type(exc).__name__
            ) from exc
        matches: list[AdvisoryMatch] = []
        for package, ids in ids_by_package.items():
            for vid in ids:
                vuln = details.get(vid)
                if vuln is not None:
                    matches.append(match_from_osv(package, vuln))
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
                ids = [str(v["id"]) for v in vulns or [] if isinstance(v, Mapping) and v.get("id")]
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


def match_from_osv(package: DependencyPackage, vuln: Mapping[str, Any]) -> AdvisoryMatch:
    """OSV 취약점 상세 하나 → 그 패키지에 대한 권고·고칠 버전."""
    vid = str(vuln.get("id") or "UNKNOWN")
    aliases = tuple(str(a) for a in vuln.get("aliases") or [] if isinstance(a, str))
    summary = vuln.get("summary")
    ranges: list[Mapping[str, Any]] = []
    for affected in vuln.get("affected") or []:
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
        ranges.extend(r for r in affected.get("ranges") or [] if isinstance(r, Mapping))
    advisory = Advisory(
        advisory_id=vid,
        aliases=aliases,
        summary=summary[:300] if isinstance(summary, str) else None,
        severity=severity_of(vuln),
        url=f"https://osv.dev/vulnerability/{vid}",
    )
    return AdvisoryMatch(package, advisory, fixed_version_for(package.version, ranges))


def severity_of(vuln: Mapping[str, Any]) -> str:
    specific = vuln.get("database_specific")
    if isinstance(specific, Mapping):
        label = specific.get("severity")
        if isinstance(label, str) and label.upper() in _GHSA_SEVERITY:
            return _GHSA_SEVERITY[label.upper()]
    best: float | None = None
    for entry in vuln.get("severity") or []:
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
