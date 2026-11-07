"""CVE 감시의 버전 비교·영향 범위 판정(순수 계산, SPEC sec-6-cve-watch).

OSV 의 `affected[].ranges[].events`(introduced·fixed·last_affected)를 설치 버전과 맞춘다.
버전 규칙은 생태계(또는 OSV 범위 종류)에 따라 고른다.

- semver(기본, npm·Go·crates.io 등과 OSV `SEMVER` 범위): `1.2.3-pre+build`. `-` 뒤는 사전 배포
  (`1.0.0-1` < `1.0.0`, 숫자 식별자는 숫자로, 글자 식별자보다 앞), `+` 뒤 빌드 메타는 순위
  비교에서 뺀다(`1.0.0+build.1` == `1.0.0`). semver.org 규칙.
- debian(Debian·Ubuntu): dpkg 비교(epoch → upstream → revision, `~` 은 무엇보다 앞).
  `-1~deb12u2` 같은 개정 번호는 사전 배포가 아니라 뒤에 붙는 꼬리다.
- generic(PyPI·Alpine 등): 숫자 조각 + 꼬리표. `~`·`-rc`·`a`·`b`·`dev` 꼬리표가 붙은 버전은
  같은 숫자의 정식 버전보다 앞이다(`1.0rc1` < `1.0`). PEP 440 전부를 구현하지는 않는다.
- 비교할 수 없는 버전(빈 값·`*`)은 영향 없음으로 본다. 판정을 지어내지 않는다. OSV `GIT` 범위
  (커밋 해시)는 버전으로 비교하지 않는다.

OSV 서버는 질의 때 생태계 규칙으로 이미 맞춰 준다. 이 비교는 ① 가짜 권고(inject)를 맞출 때와
② OSV 응답에서 "고칠 버전"(설치 버전이 든 범위의 fixed)을 고를 때만 쓴다.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

SEMVER = "semver"
DEBIAN = "debian"
GENERIC = "generic"
_DEBIAN_FAMILIES = frozenset({"debian", "ubuntu"})
_SEMVER_FAMILIES = frozenset(
    {"npm", "go", "crates.io", "nuget", "packagist", "pub", "hex", "swifturl", "rubygems"}
)

_EPOCH = re.compile(r"^\d+:")
_TOKEN = re.compile(r"\d+|[a-zA-Z]+")
# 정식 버전보다 앞인 꼬리표. 숫자 조각 뒤에 오면 그 버전을 앞으로 당긴다.
_PRE_RELEASE = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "pre": 3, "c": 3, "rc": 3}
_POST_RELEASE = {"post": 5, "p": 5, "deb": 5, "ubuntu": 5, "build": 5}


def scheme_for(ecosystem: str | None, range_type: str | None = None) -> str:
    """비교 규칙 이름. OSV 범위 종류 SEMVER 가 생태계보다 먼저다. 모르면 semver."""
    if range_type is not None and range_type.upper() == "SEMVER":
        return SEMVER
    family = (ecosystem or "").split(":", 1)[0].strip().lower()
    if family in _DEBIAN_FAMILIES:
        return DEBIAN
    if family in _SEMVER_FAMILIES or not family:
        return SEMVER
    return GENERIC


def _split(version: str) -> tuple[list[int], list[tuple[int, int | str]], bool]:
    """(숫자 조각, 꼬리, 사전 배포 여부). 사전 배포는 정식보다 앞이다."""
    text = _EPOCH.sub("", version.strip()).removeprefix("v").removeprefix("V")
    numbers: list[int] = []
    tail: list[tuple[int, int | str]] = []
    pre_release = False
    index = 0
    # 숫자 조각: 맨 앞의 "1.2.3" 부분.
    head = re.match(r"\d+(?:\.\d+)*", text)
    if head:
        numbers = [int(part) for part in head.group(0).split(".")]
        index = head.end()
    rest = text[index:]
    if rest.startswith("~"):
        pre_release = True
    for token in _TOKEN.findall(rest):
        if token.isdigit():
            tail.append((1, int(token)))
            continue
        lowered = token.lower()
        if lowered in _PRE_RELEASE and not tail:
            pre_release = True
            tail.append((0, _PRE_RELEASE[lowered]))
        elif lowered in _POST_RELEASE:
            tail.append((0, _POST_RELEASE[lowered]))
        else:
            tail.append((2, lowered))
    return numbers, tail, pre_release


def is_comparable(version: str | None) -> bool:
    if not version:
        return False
    numbers, _, _ = _split(version)
    return bool(numbers)


def compare_versions(left: str, right: str, scheme: str = SEMVER) -> int:
    """-1 · 0 · 1. 비교할 수 없는 값은 ValueError."""
    if not is_comparable(left) or not is_comparable(right):
        raise ValueError("version not comparable")
    if scheme == DEBIAN:
        return _compare_debian(left, right)
    if scheme == SEMVER and _looks_semver(left) and _looks_semver(right):
        return _compare_semver(left, right)
    return _compare_generic(left, right)


def _compare_generic(left: str, right: str) -> int:
    left_numbers, left_tail, left_pre = _split(left)
    right_numbers, right_tail, right_pre = _split(right)
    width = max(len(left_numbers), len(right_numbers))
    left_padded = left_numbers + [0] * (width - len(left_numbers))
    right_padded = right_numbers + [0] * (width - len(right_numbers))
    if left_padded != right_padded:
        return -1 if left_padded < right_padded else 1
    if left_pre != right_pre:
        return -1 if left_pre else 1
    if left_tail == right_tail:
        return 0
    return -1 if _tail_key(left_tail) < _tail_key(right_tail) else 1


_SEMVER = re.compile(
    r"^[vV]?(?P<core>\d+(?:\.\d+)*)(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]*)?$"
)


def _looks_semver(version: str) -> bool:
    return _SEMVER.match(version.strip()) is not None


def _compare_semver(left: str, right: str) -> int:
    """semver.org 11절: 숫자 조각 → 사전 배포(없는 쪽이 뒤) → 식별자별. 빌드 메타는 뺀다."""
    left_match, right_match = _SEMVER.match(left.strip()), _SEMVER.match(right.strip())
    assert left_match is not None and right_match is not None
    left_core = [int(part) for part in left_match["core"].split(".")]
    right_core = [int(part) for part in right_match["core"].split(".")]
    width = max(len(left_core), len(right_core))
    left_core += [0] * (width - len(left_core))
    right_core += [0] * (width - len(right_core))
    if left_core != right_core:
        return -1 if left_core < right_core else 1
    left_pre, right_pre = left_match["pre"], right_match["pre"]
    if left_pre == right_pre:
        return 0
    if left_pre is None:
        return 1
    if right_pre is None:
        return -1
    left_ids, right_ids = left_pre.split("."), right_pre.split(".")
    for left_id, right_id in zip(left_ids, right_ids, strict=False):
        if left_id == right_id:
            continue
        left_num, right_num = left_id.isdigit(), right_id.isdigit()
        if left_num and right_num:
            return -1 if int(left_id) < int(right_id) else 1
        if left_num != right_num:
            # 숫자 식별자는 글자 식별자보다 앞이다.
            return -1 if left_num else 1
        return -1 if left_id < right_id else 1
    if len(left_ids) == len(right_ids):
        return 0
    return -1 if len(left_ids) < len(right_ids) else 1


def _compare_debian(left: str, right: str) -> int:
    """dpkg 의 버전 비교(epoch:upstream-revision)."""
    left_epoch, left_upstream, left_revision = _debian_parts(left)
    right_epoch, right_upstream, right_revision = _debian_parts(right)
    if left_epoch != right_epoch:
        return -1 if left_epoch < right_epoch else 1
    result = _verrevcmp(left_upstream, right_upstream)
    if result != 0:
        return result
    return _verrevcmp(left_revision, right_revision)


def _debian_parts(version: str) -> tuple[int, str, str]:
    text = version.strip()
    epoch = 0
    head, sep, rest = text.partition(":")
    if sep and head.isdigit():
        epoch, text = int(head), rest
    upstream, sep, revision = text.rpartition("-")
    if not sep:
        upstream, revision = text, "0"
    return epoch, upstream, revision


def _debian_order(char: str) -> int:
    if char == "~":
        return -1
    if char.isdigit():
        return 0
    if char.isalpha():
        return ord(char)
    return ord(char) + 256


def _verrevcmp(left: str, right: str) -> int:
    i = j = 0
    while i < len(left) or j < len(right):
        first_diff = 0
        while (i < len(left) and not left[i].isdigit()) or (
            j < len(right) and not right[j].isdigit()
        ):
            left_order = _debian_order(left[i]) if i < len(left) else 0
            right_order = _debian_order(right[j]) if j < len(right) else 0
            if left_order != right_order:
                return -1 if left_order < right_order else 1
            i += 1
            j += 1
        while i < len(left) and left[i] == "0":
            i += 1
        while j < len(right) and right[j] == "0":
            j += 1
        while i < len(left) and left[i].isdigit() and j < len(right) and right[j].isdigit():
            if not first_diff:
                first_diff = ord(left[i]) - ord(right[j])
            i += 1
            j += 1
        if i < len(left) and left[i].isdigit():
            return 1
        if j < len(right) and right[j].isdigit():
            return -1
        if first_diff:
            return -1 if first_diff < 0 else 1
    return 0


def _tail_key(tail: list[tuple[int, int | str]]) -> list[tuple[int, int, str]]:
    return [(kind, value if isinstance(value, int) else 0, str(value)) for kind, value in tail]


def is_affected(version: str, events: Sequence[Mapping[str, Any]], scheme: str = SEMVER) -> bool:
    """OSV 범위 하나(events)에 설치 버전이 드나. introduced ≤ v < fixed (또는 ≤ last_affected)."""
    if not is_comparable(version):
        return False
    introduced: str | None = None
    # OSV 범위는 introduced 뒤에 그 구간을 닫는 fixed·last_affected 가 온다(적힌 순서대로 짝짓는다).
    for event in events:
        if "introduced" in event:
            introduced = str(event["introduced"])
            continue
        if introduced is None:
            continue
        if "fixed" in event:
            fixed = str(event["fixed"])
            if _at_or_after(version, introduced, scheme) and _before(version, fixed, scheme):
                return True
            introduced = None
        elif "last_affected" in event:
            last = str(event["last_affected"])
            if _at_or_after(version, introduced, scheme) and not _before(last, version, scheme):
                return True
            introduced = None
    # 닫히지 않은 범위(고친 버전 없음).
    return introduced is not None and _at_or_after(version, introduced, scheme)


def fixed_version_for(
    version: str, ranges: Iterable[Mapping[str, Any]], ecosystem: str | None = None
) -> str | None:
    """설치 버전이 든 범위의 fixed. 여럿이면 가장 낮은 것, 없으면 None(아직 고친 버전 없음).

    범위마다 비교 규칙을 고른다(`type: SEMVER` 면 semver, 아니면 생태계 규칙). `GIT` 범위는 버전이
    아니라 커밋이라 건너뛴다.
    """
    candidates: list[tuple[str, str]] = []
    for affected_range in ranges:
        range_type = affected_range.get("type")
        if isinstance(range_type, str) and range_type.upper() == "GIT":
            continue
        events = affected_range.get("events")
        if not isinstance(events, list):
            continue
        scheme = scheme_for(ecosystem, range_type if isinstance(range_type, str) else None)
        dict_events = [event for event in events if isinstance(event, Mapping)]
        if not is_affected(version, dict_events, scheme):
            continue
        for event in dict_events:
            fixed = event.get("fixed")
            if isinstance(fixed, str) and is_comparable(fixed) and _before(version, fixed, scheme):
                candidates.append((fixed, scheme))
    if not candidates:
        return None
    lowest = candidates[0][0]
    for candidate, scheme in candidates[1:]:
        if compare_versions(candidate, lowest, scheme) < 0:
            lowest = candidate
    return lowest


def _at_or_after(version: str, bound: str, scheme: str) -> bool:
    if bound == "0":
        return True
    return is_comparable(bound) and compare_versions(version, bound, scheme) >= 0


def _before(version: str, bound: str, scheme: str) -> bool:
    return is_comparable(bound) and compare_versions(version, bound, scheme) < 0
