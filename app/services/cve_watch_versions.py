"""CVE 감시의 버전 비교·영향 범위 판정(순수 계산, SPEC sec-6-cve-watch).

OSV 의 `affected[].ranges[].events`(introduced·fixed·last_affected)를 설치 버전과 맞춘다.
생태계마다 버전 규칙(semver·PEP 440·Debian)이 다르지만 이 감시는 "점으로 나눈 숫자 + 꼬리표"의
공통 부분만 비교한다.

- 숫자 조각은 숫자로, 글자 조각은 글자로 비교한다(`1.10` > `1.9`).
- 앞의 `v`, Debian 의 epoch(`1:`)는 뗀다. `~`(Debian 사전 배포)·`-rc`·`a`·`b`·`dev` 꼬리표가
  붙은 버전은 같은 숫자의 정식 버전보다 앞이다(`1.0rc1` < `1.0`).
- Debian 개정 번호(`-1~deb12u2`)와 semver 빌드 메타(`+build`)는 숫자 조각 뒤에 오는 꼬리로 본다.
- 비교할 수 없는 버전(빈 값·`*`)은 영향 없음으로 본다. 판정을 지어내지 않는다.

OSV 서버는 질의 때 생태계 규칙으로 이미 맞춰 준다. 이 비교는 ① 가짜 권고(inject)를 맞출 때와
② OSV 응답에서 "고칠 버전"(설치 버전이 든 범위의 fixed)을 고를 때만 쓴다.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

_EPOCH = re.compile(r"^\d+:")
_TOKEN = re.compile(r"\d+|[a-zA-Z]+")
# 정식 버전보다 앞인 꼬리표. 숫자 조각 뒤에 오면 그 버전을 앞으로 당긴다.
_PRE_RELEASE = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "pre": 3, "c": 3, "rc": 3}
_POST_RELEASE = {"post": 5, "p": 5, "deb": 5, "ubuntu": 5, "build": 5}


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


def compare_versions(left: str, right: str) -> int:
    """-1 · 0 · 1. 비교할 수 없는 값은 ValueError."""
    if not is_comparable(left) or not is_comparable(right):
        raise ValueError("version not comparable")
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


def _tail_key(tail: list[tuple[int, int | str]]) -> list[tuple[int, int, str]]:
    return [(kind, value if isinstance(value, int) else 0, str(value)) for kind, value in tail]


def is_affected(version: str, events: Sequence[Mapping[str, Any]]) -> bool:
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
            if _at_or_after(version, introduced) and _before(version, fixed):
                return True
            introduced = None
        elif "last_affected" in event:
            last = str(event["last_affected"])
            if _at_or_after(version, introduced) and not _before(last, version):
                return True
            introduced = None
    # 닫히지 않은 범위(고친 버전 없음).
    return introduced is not None and _at_or_after(version, introduced)


def fixed_version_for(version: str, ranges: Iterable[Mapping[str, Any]]) -> str | None:
    """설치 버전이 든 범위의 fixed. 여럿이면 가장 낮은 것, 없으면 None(아직 고친 버전 없음)."""
    candidates: list[str] = []
    for affected_range in ranges:
        events = affected_range.get("events")
        if not isinstance(events, list):
            continue
        dict_events = [event for event in events if isinstance(event, Mapping)]
        if not is_affected(version, dict_events):
            continue
        for event in dict_events:
            fixed = event.get("fixed")
            if isinstance(fixed, str) and is_comparable(fixed) and _before(version, fixed):
                candidates.append(fixed)
    if not candidates:
        return None
    lowest = candidates[0]
    for candidate in candidates[1:]:
        if compare_versions(candidate, lowest) < 0:
            lowest = candidate
    return lowest


def _at_or_after(version: str, bound: str) -> bool:
    if bound == "0":
        return True
    return is_comparable(bound) and compare_versions(version, bound) >= 0


def _before(version: str, bound: str) -> bool:
    return is_comparable(bound) and compare_versions(version, bound) < 0
