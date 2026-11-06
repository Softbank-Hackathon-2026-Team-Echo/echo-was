"""승인된 unified diff 를 파일 내용에 엄격히 적용한다(SPEC api-19).

echo-ai-agent 반영 러너(`apply_runner.apply_file_diff`)와 같은 규칙이다: 덩어리 머리의 줄
번호 자리에서 문맥·지울 줄이 한 글자라도 다르면 실패한다. 러너가 샌드박스에서 같은 diff 를
같은 커밋에 적용해 검증했으므로, WAS 가 GitHub 의 같은 커밋 파일에 적용한 결과도 같아야 한다.

`diff --git`·이름 바꾸기·이진 패치·`\\ No newline at end of file`·파일 추가/삭제는 받지 않는다
(진단 수정안은 텍스트 파일 고치기뿐이다).
"""

import re
from collections.abc import Mapping

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchApplyError(Exception):
    """diff 를 적용할 수 없다. reason 은 짧은 사유 코드다."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
