"""의존성 목록 뽑기(순수 계산, SPEC sec-6-cve-watch).

- Trivy 이미지 원문(`trivy image --format json`): `Results[].Packages[]`
  (`--list-all-pkgs` 일 때 전체)를
  읽고, 없으면 `Results[].Vulnerabilities[]` 의 패키지(취약점이 이미 있는 것만)로 대신한다.
- CycloneDX SBOM(`trivy image --format cyclonedx`): `components[].purl` 을 읽는다.
- 생태계 이름은 OSV 표기(PyPI·npm·Go·Maven·crates.io·RubyGems·NuGet·Packagist·Debian:12·
  Alpine:v3.20·Ubuntu:22.04)로 바꾼다. 모르는 종류는 버린다(질의할 수 없다).
- OS 패키지는 OSV 가 소스 패키지 단위라 Trivy 의 `SrcName`·`SrcVersion` 이 있으면 그것을 쓴다
  (예: 바이너리 `libssl3` → 소스 `openssl`).
- 같은 (생태계, 이름, 버전)은 하나로 친다. 목록은 `MAX_PACKAGES` 개까지만 남긴다.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

MAX_PACKAGES = 5000
_NAME_MAX = 200
_VERSION_MAX = 100

# Trivy 언어 결과의 Type → OSV 생태계.
_TRIVY_LANG_TYPES = {
    "python-pkg": "PyPI",
    "pip": "PyPI",
    "pipenv": "PyPI",
    "poetry": "PyPI",
    "uv": "PyPI",
    "conda-pkg": "PyPI",
    "node-pkg": "npm",
    "npm": "npm",
    "yarn": "npm",
    "pnpm": "npm",
    "bun": "npm",
    "gomod": "Go",
    "gobinary": "Go",
    "jar": "Maven",
    "pom": "Maven",
    "gradle": "Maven",
    "sbt": "Maven",
    "cargo": "crates.io",
    "rust-binary": "crates.io",
    "gemspec": "RubyGems",
    "bundler": "RubyGems",
    "composer": "Packagist",
    "composer-vendor": "Packagist",
    "nuget": "NuGet",
    "dotnet-core": "NuGet",
    "packages-props": "NuGet",
    "pub": "Pub",
    "hex": "Hex",
    "swift": "SwiftURL",
}
# purl type → OSV 생태계(OS 배포판은 따로).
_PURL_TYPES = {
    "pypi": "PyPI",
    "npm": "npm",
    "golang": "Go",
    "maven": "Maven",
    "cargo": "crates.io",
    "gem": "RubyGems",
    "nuget": "NuGet",
    "composer": "Packagist",
    "pub": "Pub",
    "hex": "Hex",
}
_PURL = re.compile(
    r"^pkg:(?P<type>[a-zA-Z0-9.+-]+)/(?P<path>[^@?#]+)(?:@(?P<version>[^?#]+))?(?:\?(?P<qualifiers>[^#]*))?"
)


@dataclass(frozen=True, order=True)
class DependencyPackage:
    """의존성 하나. ecosystem 은 OSV 표기다.

    같음·정렬은 (생태계, 이름, 버전)만 본다. purl 은 보조 정보라 있는 목록·없는 목록이 섞여도
    같은 패키지로 치고, None 과 문자열을 비교하다 멈추지 않는다.
    """

    ecosystem: str
    name: str
    version: str
    purl: str | None = field(default=None, compare=False)

    def to_json(self) -> dict[str, str]:
        body = {"ecosystem": self.ecosystem, "name": self.name, "version": self.version}
        if self.purl:
            body["purl"] = self.purl
        return body

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "DependencyPackage | None":
        ecosystem, name, version = raw.get("ecosystem"), raw.get("name"), raw.get("version")
        if not (isinstance(ecosystem, str) and isinstance(name, str) and isinstance(version, str)):
            return None
        purl = raw.get("purl")
        return cls(ecosystem, name, version, purl if isinstance(purl, str) else None)

    @property
    def key(self) -> tuple[str, str]:
        """(생태계, 정규화한 이름). 권고·의존성 맞출 때 쓴다."""
        return package_key(self.ecosystem, self.name)


def package_key(ecosystem: str, name: str) -> tuple[str, str]:
    """생태계 이름의 판 번호(`Debian:12`)는 떼고, PyPI 이름은 PEP 503 으로 맞춘다."""
    base = ecosystem.split(":", 1)[0].lower()
    normalized = name.strip().lower()
    if base == "pypi":
        normalized = re.sub(r"[-_.]+", "-", normalized)
    return base, normalized


def packages_from_trivy_report(report: Mapping[str, Any]) -> list[DependencyPackage]:
    """Trivy 이미지 원문 → 의존성 목록. 모양이 틀리면 빈 목록."""
    results = report.get("Results")
    if not isinstance(results, list):
        return []
    os_ecosystem = _os_ecosystem(report.get("Metadata"))
    found: list[DependencyPackage] = []
    for result in results:
        if not isinstance(result, Mapping):
            continue
        ecosystem = _result_ecosystem(result, os_ecosystem)
        if ecosystem is None:
            continue
        is_os = result.get("Class") == "os-pkgs"
        packages = _list_of_dicts(result.get("Packages"))
        if packages:
            for package in packages:
                found.extend(_from_trivy_package(package, ecosystem, is_os=is_os))
        else:
            for vulnerability in _list_of_dicts(result.get("Vulnerabilities")):
                found.extend(_from_trivy_vulnerability(vulnerability, ecosystem))
    return _unique(found)


def packages_from_cyclonedx(sbom: Mapping[str, Any]) -> list[DependencyPackage]:
    """CycloneDX JSON → 의존성 목록(`components[].purl`, 중첩 components 포함)."""
    found: list[DependencyPackage] = []
    stack = list(_list_of_dicts(sbom.get("components")))
    while stack:
        component = stack.pop()
        stack.extend(_list_of_dicts(component.get("components")))
        purl = component.get("purl")
        if isinstance(purl, str):
            package = package_from_purl(purl)
            if package is not None:
                found.append(package)
    return _unique(found)


def package_from_purl(purl: str) -> DependencyPackage | None:
    match = _PURL.match(purl.strip())
    if match is None or not match["version"]:
        return None
    purl_type = match["type"].lower()
    path = unquote(match["path"])
    version = unquote(match["version"])
    qualifiers = _qualifiers(match["qualifiers"])
    ecosystem: str | None
    if purl_type in _PURL_TYPES:
        ecosystem = _PURL_TYPES[purl_type]
        parts = path.split("/")
        if purl_type == "maven" and len(parts) >= 2:
            name = f"{parts[-2]}:{parts[-1]}"
        elif purl_type in ("npm", "golang", "composer"):
            name = path
        else:
            name = parts[-1]
    elif purl_type in ("deb", "apk"):
        namespace, _, name = path.rpartition("/")
        ecosystem = _distro_ecosystem(namespace, qualifiers.get("distro"))
    else:
        ecosystem = None
        name = path
    if ecosystem is None:
        return None
    return _package(ecosystem, name, version, purl)


def _from_trivy_package(
    package: Mapping[str, Any], ecosystem: str, *, is_os: bool
) -> list[DependencyPackage]:
    name, version = package.get("Name"), package.get("Version")
    if is_os and isinstance(package.get("SrcName"), str) and package.get("SrcName"):
        name = package.get("SrcName")
        source_version = package.get("SrcVersion")
        release = package.get("SrcRelease")
        if isinstance(source_version, str) and source_version:
            version = (
                f"{source_version}-{release}"
                if isinstance(release, str) and release
                else (source_version)
            )
    elif is_os and isinstance(package.get("Release"), str) and package.get("Release"):
        version = f"{version}-{package.get('Release')}"
    epoch = package.get("Epoch") if is_os else None
    if isinstance(epoch, int) and epoch > 0 and isinstance(version, str):
        version = f"{epoch}:{version}"
    purl = _purl_of(package)
    result = _package(ecosystem, name, version, purl)
    return [result] if result is not None else []


def _from_trivy_vulnerability(
    vulnerability: Mapping[str, Any], ecosystem: str
) -> list[DependencyPackage]:
    identifier = vulnerability.get("PkgIdentifier")
    purl = identifier.get("PURL") if isinstance(identifier, Mapping) else None
    result = _package(
        ecosystem,
        vulnerability.get("PkgName"),
        vulnerability.get("InstalledVersion"),
        purl if isinstance(purl, str) else None,
    )
    return [result] if result is not None else []


def _package(
    ecosystem: str, name: object, version: object, purl: str | None
) -> DependencyPackage | None:
    if not isinstance(name, str) or not isinstance(version, str):
        return None
    name, version = name.strip(), version.strip()
    if not name or not version or len(name) > _NAME_MAX or len(version) > _VERSION_MAX:
        return None
    return DependencyPackage(ecosystem, name, version, purl[:500] if purl else None)


def _purl_of(package: Mapping[str, Any]) -> str | None:
    identifier = package.get("Identifier")
    if isinstance(identifier, Mapping) and isinstance(identifier.get("PURL"), str):
        return str(identifier["PURL"])
    return None


def _result_ecosystem(result: Mapping[str, Any], os_ecosystem: str | None) -> str | None:
    result_type = result.get("Type")
    if not isinstance(result_type, str):
        return None
    if result.get("Class") == "os-pkgs":
        return os_ecosystem
    return _TRIVY_LANG_TYPES.get(result_type.lower())


def _os_ecosystem(metadata: object) -> str | None:
    if not isinstance(metadata, Mapping):
        return None
    os_info = metadata.get("OS")
    if not isinstance(os_info, Mapping):
        return None
    family, name = os_info.get("Family"), os_info.get("Name")
    if not isinstance(family, str) or not isinstance(name, str):
        return None
    return _distro_ecosystem(family, f"{family}-{name}")


def _distro_ecosystem(family: str, distro: str | None) -> str | None:
    """`debian` + `debian-12.5` → `Debian:12`, `alpine` + `3.20.3` → `Alpine:v3.20`."""
    family = family.lower()
    release = (distro or "").lower().removeprefix(f"{family}-")
    if family == "debian":
        major = release.split(".", 1)[0]
        return f"Debian:{major}" if major.isdigit() else "Debian"
    if family == "alpine":
        parts = release.removeprefix("v").split(".")
        return f"Alpine:v{parts[0]}.{parts[1]}" if len(parts) >= 2 else "Alpine"
    if family == "ubuntu":
        parts = release.split(".")
        return f"Ubuntu:{parts[0]}.{parts[1]}" if len(parts) >= 2 else "Ubuntu"
    return None


def _qualifiers(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    pairs = (part.split("=", 1) for part in raw.split("&") if "=" in part)
    return {key.lower(): unquote(value) for key, value in pairs}


def _list_of_dicts(value: object) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _unique(packages: Iterable[DependencyPackage]) -> list[DependencyPackage]:
    """같은 (생태계, 정규화한 이름, 버전)은 하나로 친다(PyPI `Zope.Interface`·`zope-interface`)."""
    seen: dict[tuple[str, str, str], DependencyPackage] = {}
    for package in packages:
        seen.setdefault((package.ecosystem, package.key[1], package.version), package)
    return sorted(seen.values())[:MAX_PACKAGES]
