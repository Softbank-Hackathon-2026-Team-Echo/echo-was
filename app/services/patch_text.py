"""승인된 unified diff 를 파일 내용에 엄격히 적용한다(SPEC api-19).

echo-ai-agent 반영 러너(`apply_runner.apply_file_diff`)와 같은 규칙이다: 덩어리 머리의 줄
번호 자리에서 문맥·지울 줄이 한 글자라도 다르면 실패한다. 러너가 샌드박스에서 같은 diff 를
같은 커밋에 적용해 검증했으므로, WAS 가 GitHub 의 같은 커밋 파일에 적용한 결과도 같아야 한다.

이름 바꾸기·이진 패치·`\\ No newline at end of file`·파일 추가/삭제는 받지 않는다(진단 수정안은
텍스트 파일 고치기뿐이다). 첫 `---` 머리 앞의 `diff --git` 줄은 러너와 같이 무시한다.

저장소에 쓰기 전에 `check_diff_paths` 로 경로를 검사한다(SPEC api-19 D-13): 정규화된 상대 경로만,
`.github/`·`.git/` 아래 금지, 모드 바꾸기·이름 바꾸기 머리 금지, 승인 화면의 `files` 와 diff 경로
집합이 같아야 한다.
"""

import re
from collections.abc import Mapping, Sequence

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# git 확장 머리 중 모드·이름·복사를 바꾸는 줄(통합 diff 에서는 덩어리 밖에서만 나온다).
_EXTENDED_HEADERS = (
    "old mode ",
    "new mode ",
    "new file mode ",
    "deleted file mode ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "similarity index ",
    "dissimilarity index ",
)
# 맨 앞 경로 조각이 이것이면 거절한다(CI 워크플로). `.git` 은 어느 깊이든 거절한다.
_PROTECTED_TOP = frozenset({".github"})
_PROTECTED_ANY = frozenset({".git"})


class PatchApplyError(Exception):
    """diff 를 적용할 수 없다. reason 은 짧은 사유 코드다."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnsafePathError(Exception):
    """diff 의 경로를 저장소에 쓸 수 없다(REPO_PATH_REJECTED). reason 은 짧은 사유 코드다."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def check_path(path: str) -> str:
    """저장소 안의 정규화된 상대 경로인지 본다. 그대로 돌려준다.

    거절: 빈 경로·NUL/제어 문자·백슬래시·따옴표(git 이 이스케이프한 경로), 절대 경로, 빈 조각
    (`a//b`·끝 `/`), `.`·`..` 조각, 맨 앞 `.github`, 어느 깊이든 `.git`(대소문자 무시).
    """
    if not path:
        raise UnsafePathError("EMPTY_PATH")
    if any(ch in path for ch in ("\x00", "\\", '"')) or any(ord(ch) < 0x20 for ch in path):
        raise UnsafePathError("UNSAFE_CHAR")
    if path.startswith("/"):
        raise UnsafePathError("ABSOLUTE_PATH")
    parts = path.split("/")
    if any(part == "" for part in parts):
        raise UnsafePathError("EMPTY_SEGMENT")
    if any(part in {".", ".."} for part in parts):
        raise UnsafePathError("DOT_SEGMENT")
    if parts[0].casefold() in _PROTECTED_TOP or any(
        part.casefold() in _PROTECTED_ANY for part in parts
    ):
        raise UnsafePathError("PROTECTED_PATH")
    return path


def _header_path(raw: str) -> str:
    """`split_file_diffs` 와 같은 방식으로 머리 경로를 뗀다(탭 뒤 시각 버림, 앞뒤 공백 버림)."""
    return raw.split("\t", 1)[0].strip()


def check_diff_paths(diff: str, declared: Sequence[str]) -> list[str]:
    """diff 의 대상 경로를 검사하고 나온 순서대로 돌려준다(중복 없음).

    - 모드·이름 바꾸기·복사 머리 줄이 있으면 거절(MODE_OR_RENAME): 실행 비트를 바꾸는 diff 를 막는다
    - 파일마다 `--- a/X`·`+++ b/X` 가 같은 경로여야 한다(RENAME). 추가·삭제(/dev/null)는 거절
    - 각 경로·`declared`(승인 화면의 files)는 `check_path` 통과
    - diff 경로 집합 = `declared` 집합(FILES_MISMATCH) — 사람이 본 파일만 바뀐다
    """
    lines = diff.splitlines()
    paths: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(_EXTENDED_HEADERS):
            raise UnsafePathError("MODE_OR_RENAME")
        if (
            line.startswith("--- ")
            and index + 1 < len(lines)
            and lines[index + 1].startswith("+++ ")
        ):
            old = _header_path(line[4:])
            new = _header_path(lines[index + 1][4:])
            if "/dev/null" in (old, new):
                raise UnsafePathError("ADD_OR_DELETE")
            if old.removeprefix("a/") != new.removeprefix("b/"):
                raise UnsafePathError("RENAME")
            path = check_path(new.removeprefix("b/"))
            if path not in paths:
                paths.append(path)
            index += 2
            continue
        index += 1
    if not paths:
        raise UnsafePathError("NO_FILE")
    for name in declared:
        check_path(name)
    if set(paths) != set(declared):
        raise UnsafePathError("FILES_MISMATCH")
    return paths


def diff_paths(diff: str) -> list[str]:
    """`--- a/…`·`+++ b/…` 머리의 경로(`/dev/null` 제외, 나온 순서, 중복 없음)."""
    paths: list[str] = []
    for line in diff.splitlines():
        for prefix, strip in (("--- ", "a/"), ("+++ ", "b/")):
            if line.startswith(prefix):
                path = line[len(prefix) :].split("\t", 1)[0].strip()
                if path == "/dev/null":
                    continue
                path = path.removeprefix(strip)
                if path not in paths:
                    paths.append(path)
    return paths


def split_file_diffs(diff: str) -> dict[str, str]:
    """파일마다 덩어리 부분(머리 `---`/`+++` 다음부터)만 나눈다. 추가·삭제(/dev/null)는 거절."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    lines = diff.splitlines(keepends=True)
    index = 0
    while index < len(lines):
        line = lines[index]
        if (
            line.startswith("--- ")
            and index + 1 < len(lines)
            and lines[index + 1].startswith("+++ ")
        ):
            old = line[4:].split("\t", 1)[0].strip()
            new = lines[index + 1][4:].split("\t", 1)[0].strip()
            if "/dev/null" in (old, new):
                raise PatchApplyError("ADD_OR_DELETE")
            current = new.removeprefix("b/")
            sections.setdefault(current, [])
            index += 2
            continue
        if current is not None:
            sections[current].append(line)
        index += 1
    if not sections:
        raise PatchApplyError("NO_FILE")
    return {name: "".join(body) for name, body in sections.items()}


def apply_file_diff(original: str, hunks: str) -> str:
    """덩어리를 원본에 엄격히 적용한다."""
    source = original.splitlines(keepends=True)
    output: list[str] = []
    cursor = 0
    lines = hunks.splitlines(keepends=True)
    index = 0
    saw_hunk = False
    while index < len(lines):
        header = _HUNK_HEADER.match(lines[index])
        if header is None:
            if lines[index].strip() == "":
                index += 1
                continue
            raise PatchApplyError("MALFORMED_DIFF")
        saw_hunk = True
        old_start = int(header.group(1))
        old_count = int(header.group(2) or "1")
        new_count = int(header.group(4) or "1")
        start = old_start - 1 if old_count else old_start
        if start < cursor or start > len(source):
            raise PatchApplyError("HUNK_OUT_OF_RANGE")
        output.extend(source[cursor:start])
        cursor = start
        index += 1
        seen_old = seen_new = 0
        while index < len(lines) and (seen_old < old_count or seen_new < new_count):
            line = lines[index]
            tag, text = line[:1], line[1:]
            if line.startswith("\\"):
                raise PatchApplyError("NO_NEWLINE_MARKER")
            if tag in {" ", "-"}:
                if cursor >= len(source) or source[cursor] != text:
                    raise PatchApplyError("CONTEXT_MISMATCH")
                cursor += 1
                seen_old += 1
                if tag == " ":
                    output.append(text)
                    seen_new += 1
            elif tag == "+":
                output.append(text)
                seen_new += 1
            else:
                raise PatchApplyError("MALFORMED_DIFF")
            index += 1
        if seen_old != old_count or seen_new != new_count:
            raise PatchApplyError("HUNK_LENGTH")
    if not saw_hunk:
        raise PatchApplyError("NO_HUNK")
    output.extend(source[cursor:])
    return "".join(output)


def apply_patch(originals: Mapping[str, str], diff: str) -> dict[str, str]:
    """파일 경로 → 원본 내용에 diff 를 적용한 새 내용. 대상 파일이 없으면 실패."""
    patched: dict[str, str] = {}
    for name, hunks in split_file_diffs(diff).items():
        if name not in originals:
            raise PatchApplyError("TARGET_MISSING")
        patched[name] = apply_file_diff(originals[name], hunks)
    return patched
