"""합성 GitHub 응답유실·GET-only 정합성. 실 GitHub 접근0."""

import hashlib

import httpx
import pytest

from app.clients.repo_commit_writer import RepoCommitError
from app.core.secrets import SecretValue
from tests.test_apply_commit import (
    BASE_SHA,
    BASE_TREE,
    NEW_SHA,
    REPO,
    TOKEN,
    TREE_SHA,
    _GithubWrite,
    _request,
    _writer,
)


def blob_sha(content: str) -> str:
    body = content.encode()
    return hashlib.sha1(f"blob {len(body)}\0".encode() + body).hexdigest()


async def test_repository_mutation_response_loss_is_unknown() -> None:
    def lose(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("synthetic response lost")

    writer = _writer(_GithubWrite())
    async with httpx.AsyncClient(transport=httpx.MockTransport(lose)) as client:
        with pytest.raises(RepoCommitError) as caught:
            await writer._send(
                client, "POST", "https://github.invalid/repos/o/r/git/refs", SecretValue(TOKEN)
            )
    assert caught.value.uncertain is True
    assert caught.value.code == "REPO_WRITE_FAILED"


class ReadOnlyGithub(_GithubWrite):
    def __init__(self, fault: str | None = None) -> None:
        super().__init__(branch_exists=True)
        self.fault = fault
        self.request = _request("main")

    def _handle(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "reconciliation must never send POST"
        self.calls.append((request.method, request.url.path, None))
        base = "/repos/team-echo/demo-app"
        path = request.url.path
        if path.endswith("/git/ref/heads/echoport/fix-1234abcd"):
            if self.fault == "missing":
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"object": {"sha": NEW_SHA}})
        if path.endswith("/git/commits/" + BASE_SHA):
            return httpx.Response(200, json={"tree": {"sha": BASE_TREE}})
        if path.endswith("/git/commits/" + NEW_SHA):
            parents = [] if self.fault == "parent" else [{"sha": BASE_SHA}]
            return httpx.Response(200, json={"tree": {"sha": TREE_SHA}, "parents": parents})
        if "/git/trees/" in path:
            changed = path.endswith(TREE_SHA)
            entries = [
                {
                    "path": "app/static/index.html",
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob_sha(self.request.files["app/static/index.html"])
                    if changed
                    else "1" * 40,
                },
                {"path": "keep.txt", "mode": "100644", "type": "blob", "sha": "2" * 40},
            ]
            if changed and self.fault == "extra":
                entries[1]["sha"] = "3" * 40
            if changed and self.fault == "mode":
                entries[0]["mode"] = "100755"
            return httpx.Response(
                200, json={"tree": entries, "truncated": self.fault == "truncated"}
            )
        if path == base + "/pulls":
            pr = {
                "number": 7,
                "html_url": "https://github.com/team-echo/demo-app/pull/7",
                "head": {
                    "ref": self.request.branch,
                    "sha": NEW_SHA,
                    "repo": {"full_name": REPO.full_name},
                },
                "base": {"ref": "main", "repo": {"full_name": REPO.full_name}},
            }
            if self.fault == "repo":
                pr["head"]["repo"]["full_name"] = "attacker/other"
            return httpx.Response(200, json=[pr, pr] if self.fault == "multiple" else [pr])
        raise AssertionError(path)


async def test_reconcile_full_effect_uses_only_get_and_preserves_files() -> None:
    github = ReadOnlyGithub()
    writer = _writer(github)
    writer._tokens[REPO.full_name] = SecretValue(TOKEN)
    result = await writer.reconcile(github.request)
    assert result.commit_sha == NEW_SHA
    assert result.pull_request_url.endswith("/pull/7")
    assert len(github.calls) >= 6
    assert all(method == "GET" for method, _, _ in github.calls)


@pytest.mark.parametrize(
    "fault", ["missing", "parent", "extra", "mode", "truncated", "repo", "multiple"]
)
async def test_reconcile_ambiguous_effect_never_writes(fault: str) -> None:
    github = ReadOnlyGithub(fault)
    writer = _writer(github)
    writer._tokens[REPO.full_name] = SecretValue(TOKEN)
    with pytest.raises(RepoCommitError):
        await writer.reconcile(github.request)
    assert all(method == "GET" for method, _, _ in github.calls)
