"""승인 패치 저장소 반영(SPEC api-19) 단위 시험: 브랜치 이름·커밋 메시지·diff 적용·GitHub 쓰기.

GitHub 는 httpx.MockTransport 로만 흉내 낸다(실제 호출 없음).
"""

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.clients.repo_commit_writer import (
    COMMIT_AUTHOR_EMAIL,
    COMMIT_AUTHOR_NAME,
    GithubAppRepoCommitWriter,
    RepoCommitError,
    RepoCommitRequest,
)
from app.clients.secret_client import EnvVarSecretClient
from app.core.repo_url import RepoLocator
from app.core.secrets import parse_secret_ref
from app.services.apply_commit_service import (
    CommitContext,
    commit_message,
    fix_branch_name,
    pull_request_body,
    pull_request_title,
)
from app.services.patch_text import PatchApplyError, apply_patch, diff_paths
from tests.github_fake import API_BASE, APP_ID, TEST_APP_PRIVATE_KEY_PEM

KEY_ENV = "ECHO_SECRET_GITHUB_APP_PRIVATE_KEY"
TOKEN = "ghs_write_token_do_not_log_91c2e0"
REPO = RepoLocator(owner="team-echo", name="demo-app")
BASE_SHA = "a" * 40
BASE_TREE = "b" * 40
BLOB_SHA = "c" * 40
TREE_SHA = "d" * 40
NEW_SHA = "e" * 40
APPLY_RUN_ID = uuid.UUID("1234abcd-0000-4000-8000-000000000001")

HTML = "    <script>\n      renderStatus(data;\n    </script>\n"
DIFF = (
    "--- a/app/static/index.html\n"
    "+++ b/app/static/index.html\n"
    "@@ -1,3 +1,3 @@\n"
    "     <script>\n"
    "-      renderStatus(data;\n"
    "+      renderStatus(data);\n"
    "     </script>\n"
)


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 11, 7, 3, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def private_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY_ENV, TEST_APP_PRIVATE_KEY_PEM)


def _context(**changes: Any) -> CommitContext:
    values: dict[str, Any] = {
        "service_name": "demo",
        "cause": "app/static/index.html 120번째 줄의 괄호가 닫히지 않았습니다.",
        "category": "CODE",
        "approver": "@approver-b",
        "diagnosis_run_id": uuid.UUID("00000000-0000-4000-8000-0000000000d1"),
        "apply_run_id": APPLY_RUN_ID,
        "deploy_request_id": uuid.UUID("00000000-0000-4000-8000-0000000000f1"),
        "source_commit_sha": BASE_SHA,
        "patch_id": "ai-fix-1",
        "patch_hash": "sha256:" + "9" * 64,
    }
    values.update(changes)
    return CommitContext(**values)


# --- 브랜치 이름·커밋 메시지 ---


def test_fix_branch_name_uses_first_eight_hex_of_apply_run() -> None:
    assert fix_branch_name(APPLY_RUN_ID) == "echoport/fix-1234abcd"


def test_commit_message_has_diagnosis_approver_and_run_ids() -> None:
    message = commit_message(_context())
    title, _, body = message.partition("\n\n")
    assert title.startswith("fix: EchoPort AI 진단 수정 반영 — app/static/index.html 120번째")
    assert "\n" not in title
    assert "진단(CODE): app/static/index.html 120번째 줄의 괄호가 닫히지 않았습니다." in body
    assert "승인: @approver-b" in body
    assert f"반영 실행: {APPLY_RUN_ID}" in body
    assert "진단 실행: 00000000-0000-4000-8000-0000000000d1" in body
    assert f"(커밋 {BASE_SHA[:12]})" in body
    assert "패치: ai-fix-1 sha256:" in body


def test_commit_texts_mask_secrets_and_cut_long_cause() -> None:
    leaked = "ghp_" + "Q" * 36
    context = _context(cause=f"토큰 {leaked} 이 로그에 있습니다. " + "가" * 600)
    for text in (commit_message(context), pull_request_title(context), pull_request_body(context)):
        assert leaked not in text
    assert len(pull_request_title(context)) < 120
    assert pull_request_title(_context()).startswith("[demo] EchoPort AI 진단 수정:")


# --- diff 적용(러너 엄격 적용기와 같은 규칙) ---


def test_apply_patch_produces_fixed_file() -> None:
    assert diff_paths(DIFF) == ["app/static/index.html"]
    patched = apply_patch({"app/static/index.html": HTML}, DIFF)
    assert patched == {"app/static/index.html": HTML.replace("data;", "data);")}


@pytest.mark.parametrize(
    ("originals", "diff", "reason"),
    [
        ({"app/static/index.html": HTML.replace("data;", "x;")}, DIFF, "CONTEXT_MISMATCH"),
        ({}, DIFF, "TARGET_MISSING"),
        ({"a": "x\n"}, "--- /dev/null\n+++ b/a\n@@ -0,0 +1 @@\n+x\n", "ADD_OR_DELETE"),
        ({"a": "x\n"}, "not a diff", "NO_FILE"),
        (
            {"app/static/index.html": HTML},
            DIFF.replace("@@ -1,3 +1,3 @@", "@@ -9,3 +9,3 @@"),
            "HUNK_OUT_OF_RANGE",
        ),
    ],
)
def test_apply_patch_rejects_what_runner_would_reject(
    originals: dict[str, str], diff: str, reason: str
) -> None:
    with pytest.raises(PatchApplyError) as caught:
        apply_patch(originals, diff)
    assert caught.value.reason == reason


# --- GitHub App 쓰기 (MockTransport) ---


class _GithubWrite:
    """경로별 가짜 GitHub. 받은 요청을 쌓는다."""

    def __init__(self, **overrides: Any) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.token_status = overrides.get("token_status", 201)
        self.installed = overrides.get("installed", True)
        self.branch_exists = overrides.get("branch_exists", False)
        self.pr_exists = overrides.get("pr_exists", False)
        self.file_missing = overrides.get("file_missing", False)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        self.calls.append((request.method, path, body))
        repo = "/repos/team-echo/demo-app"
        if path == f"{repo}/installation":
            return httpx.Response(200 if self.installed else 404, json={"id": 77})
        if path == "/app/installations/77/access_tokens":
            if self.token_status != 201:
                return httpx.Response(self.token_status, json={"message": "permissions"})
            return httpx.Response(201, json={"token": TOKEN})
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if path == f"{repo}/contents/app/static/index.html":
            assert request.url.params["ref"] == BASE_SHA
            if self.file_missing:
                return httpx.Response(404, json={})
            return httpx.Response(200, content=HTML.encode())
        if path == f"{repo}/git/ref/heads/echoport/fix-1234abcd":
            if self.branch_exists:
                return httpx.Response(200, json={"object": {"sha": NEW_SHA}})
            return httpx.Response(404, json={})
        if path == f"{repo}/git/commits/{BASE_SHA}":
            return httpx.Response(200, json={"tree": {"sha": BASE_TREE}})
        if path == f"{repo}/git/blobs":
            return httpx.Response(201, json={"sha": BLOB_SHA})
        if path == f"{repo}/git/trees":
            return httpx.Response(201, json={"sha": TREE_SHA})
        if path == f"{repo}/git/commits":
            return httpx.Response(201, json={"sha": NEW_SHA})
        if path == f"{repo}/git/refs":
            return httpx.Response(201, json={"ref": "x"})
        if path == repo:
            return httpx.Response(200, json={"default_branch": "main"})
        if path == f"{repo}/pulls" and request.method == "POST":
            if self.pr_exists:
                return httpx.Response(422, json={"message": "exists"})
            return httpx.Response(
                201, json={"html_url": "https://github.com/team-echo/demo-app/pull/7"}
            )
        if path == f"{repo}/pulls":
            return httpx.Response(200, json=[{"html_url": "https://github.com/x/pull/3"}])
        raise AssertionError(f"unexpected {request.method} {path}")

    def paths(self) -> list[str]:
        return [f"{method} {path}" for method, path, _ in self.calls]


def _writer(github: _GithubWrite) -> GithubAppRepoCommitWriter:
    return GithubAppRepoCommitWriter(
        api_base_url=API_BASE,
        app_id=APP_ID,
        private_key_ref=parse_secret_ref(f"env:{KEY_ENV}"),
        secret_client=EnvVarSecretClient(),
        timeout_seconds=5,
        clock=_Clock(),
        transport=github.transport(),
    )


def _request(pr_base: str | None = None) -> RepoCommitRequest:
    return RepoCommitRequest(
        repo=REPO,
        base_commit_sha=BASE_SHA,
        branch="echoport/fix-1234abcd",
        files={"app/static/index.html": HTML.replace("data;", "data);")},
        message="fix: x",
        pr_title="t",
        pr_body="b",
        pr_base=pr_base,
    )


async def test_github_writer_reads_commits_branches_and_opens_pr(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    github = _GithubWrite()
    writer = _writer(github)

    files = await writer.read_files(REPO, BASE_SHA, ["app/static/index.html"])
    result = await writer.commit_and_open_pull_request(_request())

    assert files == {"app/static/index.html": HTML}
    assert (result.commit_sha, result.branch) == (NEW_SHA, "echoport/fix-1234abcd")
    assert result.pull_request_url == "https://github.com/team-echo/demo-app/pull/7"
    bodies = {f"{m} {p}": b for m, p, b in github.calls}
    assert bodies["POST /app/installations/77/access_tokens"] == {
        "repositories": ["demo-app"],
        "permissions": {"contents": "write", "pull_requests": "write"},
    }
    assert bodies["POST /repos/team-echo/demo-app/git/trees"]["base_tree"] == BASE_TREE
    commit = bodies["POST /repos/team-echo/demo-app/git/commits"]
    assert commit["parents"] == [BASE_SHA] and commit["tree"] == TREE_SHA
    assert commit["author"] == {"name": COMMIT_AUTHOR_NAME, "email": COMMIT_AUTHOR_EMAIL}
    assert bodies["POST /repos/team-echo/demo-app/git/refs"] == {
        "ref": "refs/heads/echoport/fix-1234abcd",
        "sha": NEW_SHA,
    }
    assert bodies["POST /repos/team-echo/demo-app/pulls"]["base"] == "main"
    # 설치 토큰은 한 번만 받는다(같은 인스턴스 안 재사용).
    assert github.paths().count("POST /app/installations/77/access_tokens") == 1
    assert TOKEN not in caplog.text


async def test_github_writer_reuses_existing_branch_and_pr() -> None:
    github = _GithubWrite(branch_exists=True, pr_exists=True)
    result = await _writer(github).commit_and_open_pull_request(_request(pr_base="release"))
    assert result.commit_sha == NEW_SHA
    assert result.pull_request_url == "https://github.com/x/pull/3"
    assert "POST /repos/team-echo/demo-app/git/commits" not in github.paths()
    assert (
        "GET /repos/team-echo/demo-app" not in github.paths()
    )  # pr_base 가 있으면 기본 브랜치 안 봄


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"token_status": 422}, "REPO_WRITE_FORBIDDEN"),
        ({"token_status": 403}, "REPO_WRITE_FORBIDDEN"),
        ({"installed": False}, "REPO_APP_NOT_INSTALLED"),
        ({"file_missing": True}, "REPO_FILE_NOT_FOUND"),
    ],
)
async def test_github_writer_failures_map_to_codes(overrides: dict[str, Any], code: str) -> None:
    github = _GithubWrite(**overrides)
    with pytest.raises(RepoCommitError) as caught:
        await _writer(github).read_files(REPO, BASE_SHA, ["app/static/index.html"])
    assert caught.value.code == code
    assert TOKEN not in str(caught.value)


async def test_github_writer_without_private_key_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(KEY_ENV)
    with pytest.raises(RepoCommitError) as caught:
        await _writer(_GithubWrite()).read_files(REPO, BASE_SHA, ["app/static/index.html"])
    assert caught.value.code == "REPO_WRITE_UNAVAILABLE"
