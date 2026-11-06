"""서비스 저장소에 승인된 패치를 커밋하고 PR 을 여는 클라이언트(SPEC api-19).

러너(샌드박스 VM)는 읽기 자격뿐이라 push 하지 않는다. 저장소 쓰기는 WAS 가 GitHub App 설치
토큰(contents: write, pull_requests: write)으로 한다.

1. App JWT(RS256) → `GET /repos/{o}/{r}/installation` → `POST /app/installations/{id}/access_tokens`
   (`repositories=[이 저장소]`, `permissions={contents: write, pull_requests: write}`).
   App 에 그 권한이 없으면 GitHub 가 422 로 거절한다 → REPO_WRITE_FORBIDDEN.
2. 실패 커밋 tree 의 대상 파일 모드: `GET git/trees/{tree}?recursive=1`. 일반 파일(100644·100755)
   만 쓴다 — 심볼릭 링크(120000)·서브모듈(160000)·디렉터리는 REPO_PATH_REJECTED(SPEC D-13).
3. 실패 커밋의 파일 읽기: `GET /repos/{o}/{r}/contents/{path}?ref={sha}`(raw).
4. Git Data API 로 커밋: blob → tree(base_tree = 실패 커밋의 tree, 원래 모드 보존) →
   commit(parents = [실패 커밋], 작성자 EchoPort bot) → `refs/heads/echoport/fix-<run8>` 만들기.
   브랜치가 이미 있으면 그 커밋의 부모 = [실패 커밋] 이고 tree = 방금 만든 tree 일 때만 쓴다
   (같은 내용). 아니면 REPO_BRANCH_CONFLICT(SPEC D-14 — 남이 만든 브랜치를 배포하지 않는다).
5. `POST /repos/{o}/{r}/pulls`(이미 있으면 그 PR 주소).

개인키·JWT·설치 토큰은 SecretValue·지역 변수로만 다루고 로그·예외에 넣지 않는다. 응답 본문도
로그에 남기지 않는다(상태 코드만). 시험은 httpx.MockTransport 를 transport 로 넣는다.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol
from urllib.parse import quote

import httpx
import jwt

from app.clients.secret_client import SecretClient, SecretLookupError
from app.core.clock import Clock
from app.core.repo_url import RepoLocator
from app.core.secrets import SecretRef, SecretValue

logger = logging.getLogger(__name__)

COMMIT_AUTHOR_NAME = "EchoPort bot"
COMMIT_AUTHOR_EMAIL = "bot@echoport.uk"
WRITE_PERMISSIONS = {"contents": "write", "pull_requests": "write"}
FILE_MODE = "100644"
# 고칠 수 있는 대상(일반 파일). 그 밖의 모드(120000 심볼릭 링크, 160000 서브모듈, 040000 디렉터리)는
# 거절한다.
EDITABLE_MODES = frozenset({"100644", "100755"})

_APP_JWT_BACKDATE = timedelta(seconds=60)
_APP_JWT_LIFETIME = timedelta(minutes=9)
_API_VERSION = "2022-11-28"


class RepoCommitErrorCode:
    """반영 실행 failureCode 로 남는 값(FAILURE_CODE_PATTERN 32자 이하)."""

    APP_NOT_INSTALLED = "REPO_APP_NOT_INSTALLED"
    WRITE_FORBIDDEN = "REPO_WRITE_FORBIDDEN"
    FILE_NOT_FOUND = "REPO_FILE_NOT_FOUND"
    FILE_NOT_TEXT = "REPO_FILE_NOT_TEXT"
    PATCH_CONFLICT = "REPO_PATCH_CONFLICT"
    WRITE_FAILED = "REPO_WRITE_FAILED"
    WRITE_UNAVAILABLE = "REPO_WRITE_UNAVAILABLE"
    PATH_REJECTED = "REPO_PATH_REJECTED"
    BRANCH_CONFLICT = "REPO_BRANCH_CONFLICT"


class RepoCommitError(Exception):
    """저장소 반영 실패. code 가 반영 실행의 failureCode 가 된다(값·토큰은 넣지 않는다)."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if detail is None else f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class RepoCommitRequest:
    repo: RepoLocator
    base_commit_sha: str
    branch: str
    files: Mapping[str, str]
    message: str
    pr_title: str
    pr_body: str
    # PR 을 합칠 브랜치. None 이면 저장소 기본 브랜치.
    pr_base: str | None = None
    # 경로 → 실패 커밋에서의 git 모드(보존한다). 없는 경로는 FILE_MODE(새 파일만 100644).
    modes: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RepoCommitResult:
    commit_sha: str
    branch: str
    pull_request_url: str | None


class RepoCommitWriter(Protocol):
    async def file_modes(
        self, repo: RepoLocator, commit_sha: str, paths: Sequence[str]
    ) -> dict[str, str]:
        """커밋 tree 에서 각 경로의 git 모드(`100644`·`100755`·`120000`…).

        없으면 REPO_FILE_NOT_FOUND. 판정(일반 파일만)은 부르는 쪽이 한다.
        """
        ...

    async def read_files(
        self, repo: RepoLocator, commit_sha: str, paths: Sequence[str]
    ) -> dict[str, str]:
        """커밋의 텍스트 파일 내용.

        없으면 REPO_FILE_NOT_FOUND, 텍스트가 아니면 REPO_FILE_NOT_TEXT.
        """
        ...

    async def commit_and_open_pull_request(self, request: RepoCommitRequest) -> RepoCommitResult:
        """브랜치 + 커밋 + PR.

        같은 브랜치가 이미 있으면 그 커밋이 같은 내용(부모·tree 일치)일 때만 그 커밋·PR 을
        돌려주고, 다르면 REPO_BRANCH_CONFLICT.
        """
        ...


@dataclass
class FakeRepoCommitWriter:
    """시험용. 파일 내용을 정해 두고, 부른 요청을 쌓고, 정해 둔 오류를 낸다(네트워크 없음)."""

    files: dict[str, str] = field(default_factory=dict)
    fail_code: str | None = None
    commit_sha: str = "f1" * 20
    pull_request_url: str | None = "https://github.com/team-echo/shop-api/pull/1"
    # 경로 → git 모드. 없으면 100644.
    modes: dict[str, str] = field(default_factory=dict)
    reads: list[tuple[str, str, tuple[str, ...]]] = field(default_factory=list)
    requests: list[RepoCommitRequest] = field(default_factory=list)

    async def file_modes(
        self, repo: RepoLocator, commit_sha: str, paths: Sequence[str]
    ) -> dict[str, str]:
        if self.fail_code is not None:
            raise RepoCommitError(self.fail_code)
        if any(path not in self.files and path not in self.modes for path in paths):
            raise RepoCommitError(RepoCommitErrorCode.FILE_NOT_FOUND)
        return {path: self.modes.get(path, FILE_MODE) for path in paths}

    async def read_files(
        self, repo: RepoLocator, commit_sha: str, paths: Sequence[str]
    ) -> dict[str, str]:
        self.reads.append((repo.full_name, commit_sha, tuple(paths)))
        if self.fail_code is not None:
            raise RepoCommitError(self.fail_code)
        missing = [path for path in paths if path not in self.files]
        if missing:
            raise RepoCommitError(RepoCommitErrorCode.FILE_NOT_FOUND)
        return {path: self.files[path] for path in paths}

    async def commit_and_open_pull_request(self, request: RepoCommitRequest) -> RepoCommitResult:
        self.requests.append(request)
        return RepoCommitResult(self.commit_sha, request.branch, self.pull_request_url)


class GithubAppRepoCommitWriter:
    """GitHub App 설치 토큰으로 저장소에 쓴다(SPEC api-19). 인스턴스는 요청 하나 동안만 쓴다."""

    def __init__(
        self,
        *,
        api_base_url: str,
        app_id: str,
        private_key_ref: SecretRef,
        secret_client: SecretClient,
        timeout_seconds: float,
        clock: Clock,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_base_url = api_base_url.rstrip("/")
        self._app_id = app_id
        self._private_key_ref = private_key_ref
        self._secret_client = secret_client
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._transport = transport
        self._tokens: dict[str, SecretValue] = {}

    async def file_modes(
        self, repo: RepoLocator, commit_sha: str, paths: Sequence[str]
    ) -> dict[str, str]:
        base = f"/repos/{repo.owner}/{repo.name}"
        async with self._http() as http:
            token = await self._write_token(http, repo)
            tree_sha = await self._commit_tree(http, token, base, commit_sha)
            listing = await self._send(
                http, "GET", f"{base}/git/trees/{tree_sha}", token, params={"recursive": "1"}
            )
            _raise_for_unexpected(listing, "get_tree")
            body = _json(listing)
            entries: dict[str, Any] = {}
            raw_entries = body.get("tree")
            for entry in raw_entries if isinstance(raw_entries, list) else []:
                if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                    entries[entry["path"]] = entry
            modes: dict[str, str] = {}
            for path in paths:
                found = entries.get(path)
                if found is None:
                    if body.get("truncated"):
                        # 큰 저장소라 목록이 잘렸다. 모드를 모르면 쓰지 않는다.
                        raise RepoCommitError(RepoCommitErrorCode.WRITE_FAILED, "tree_truncated")
                    raise RepoCommitError(RepoCommitErrorCode.FILE_NOT_FOUND)
                mode = found.get("mode")
                if not isinstance(mode, str):
                    raise RepoCommitError(RepoCommitErrorCode.WRITE_FAILED, "tree_mode")
                modes[path] = mode
            return modes

    async def read_files(
        self, repo: RepoLocator, commit_sha: str, paths: Sequence[str]
    ) -> dict[str, str]:
        async with self._http() as http:
            token = await self._write_token(http, repo)
            files: dict[str, str] = {}
            for path in paths:
                response = await self._send(
                    http,
                    "GET",
                    f"/repos/{repo.owner}/{repo.name}/contents/{quote(path)}",
                    token,
                    params={"ref": commit_sha},
                    accept="application/vnd.github.raw+json",
                )
                if response.status_code == 404:
                    raise RepoCommitError(RepoCommitErrorCode.FILE_NOT_FOUND)
                _raise_for_unexpected(response, "read_file")
                try:
                    files[path] = response.content.decode("utf-8")
                except UnicodeDecodeError:
                    raise RepoCommitError(RepoCommitErrorCode.FILE_NOT_TEXT) from None
            return files

    async def commit_and_open_pull_request(self, request: RepoCommitRequest) -> RepoCommitResult:
        repo = request.repo
        base = f"/repos/{repo.owner}/{repo.name}"
        async with self._http() as http:
            token = await self._write_token(http, repo)
            # 기대 tree 를 먼저 만든다(ref 를 바꾸지 않는 객체 쓰기). 이미 있는 브랜치가 이 tree
            # 와 같은 내용인지 대조하는 기준이다.
            base_tree = await self._commit_tree(http, token, base, request.base_commit_sha)
            tree_sha = await self._create_tree(http, token, base, base_tree, request)
            existing = await self._find_branch(http, token, base, request.branch)
            if existing is not None:
                await self._verify_branch_commit(http, token, base, existing, request, tree_sha)
                commit_sha = existing
            else:
                commit_sha = await self._create_commit(http, token, base, request, tree_sha)
                created = await self._send(
                    http,
                    "POST",
                    f"{base}/git/refs",
                    token,
                    json={"ref": f"refs/heads/{request.branch}", "sha": commit_sha},
                )
                if created.status_code == 422:
                    # 그 사이 누가 같은 이름으로 만들었다. 같은 내용일 때만 그 커밋을 쓴다.
                    existing = await self._find_branch(http, token, base, request.branch)
                    if existing is None:
                        raise RepoCommitError(RepoCommitErrorCode.WRITE_FAILED, "create_ref")
                    await self._verify_branch_commit(http, token, base, existing, request, tree_sha)
                    commit_sha = existing
                else:
                    _raise_for_unexpected(created, "create_ref")
            url = await self._open_pull_request(http, token, base, request)
        logger.info(
            "repo commit created",
            extra={
                "action": "repo_commit",
                "repo": repo.full_name,
                "branch": request.branch,
                "commit_sha": commit_sha,
                "pull_request": url is not None,
            },
        )
        return RepoCommitResult(commit_sha, request.branch, url)

    async def _commit_tree(
        self, http: httpx.AsyncClient, token: SecretValue, base: str, commit_sha: str
    ) -> str:
        parent = await self._send(http, "GET", f"{base}/git/commits/{commit_sha}", token)
        _raise_for_unexpected(parent, "get_base_commit")
        tree = _json(parent).get("tree")
        tree_sha = tree.get("sha") if isinstance(tree, dict) else None
        if not isinstance(tree_sha, str):
            raise RepoCommitError(RepoCommitErrorCode.WRITE_FAILED, "base_tree")
        return tree_sha

    async def _create_tree(
        self,
        http: httpx.AsyncClient,
        token: SecretValue,
        base: str,
        base_tree: str,
        request: RepoCommitRequest,
    ) -> str:
        entries: list[dict[str, str]] = []
        for path, content in sorted(request.files.items()):
            mode = request.modes.get(path, FILE_MODE)
            if mode not in EDITABLE_MODES:
                raise RepoCommitError(RepoCommitErrorCode.PATH_REJECTED, "mode")
            blob = await self._send(
                http,
                "POST",
                f"{base}/git/blobs",
                token,
                json={"content": content, "encoding": "utf-8"},
            )
            _raise_for_unexpected(blob, "create_blob")
            entries.append({"path": path, "mode": mode, "type": "blob", "sha": _sha(_json(blob))})
        tree = await self._send(
            http, "POST", f"{base}/git/trees", token, json={"base_tree": base_tree, "tree": entries}
        )
        _raise_for_unexpected(tree, "create_tree")
        return _sha(_json(tree))

    async def _create_commit(
        self,
        http: httpx.AsyncClient,
        token: SecretValue,
        base: str,
        request: RepoCommitRequest,
        tree_sha: str,
    ) -> str:
        commit = await self._send(
            http,
            "POST",
            f"{base}/git/commits",
            token,
            json={
                "message": request.message,
                "tree": tree_sha,
                "parents": [request.base_commit_sha],
                "author": {"name": COMMIT_AUTHOR_NAME, "email": COMMIT_AUTHOR_EMAIL},
            },
        )
        _raise_for_unexpected(commit, "create_commit")
        return _sha(_json(commit))

    async def _verify_branch_commit(
        self,
        http: httpx.AsyncClient,
        token: SecretValue,
        base: str,
        commit_sha: str,
        request: RepoCommitRequest,
        tree_sha: str,
    ) -> None:
        """이미 있는 브랜치 head 가 승인된 패치 커밋과 같은 내용인지 본다.

        부모 = [실패 커밋] 이고 tree = 이번에 만든 tree 여야 한다(내용이 바이트 단위로 같다).
        아니면 REPO_BRANCH_CONFLICT — 남이 먼저 만든 브랜치를 배포하지 않는다.
        """
        found = await self._send(http, "GET", f"{base}/git/commits/{commit_sha}", token)
        _raise_for_unexpected(found, "get_branch_commit")
        body = _json(found)
        tree = body.get("tree")
        parents = body.get("parents")
        head_tree = tree.get("sha") if isinstance(tree, dict) else None
        parent_shas = (
            [item.get("sha") for item in parents if isinstance(item, dict)]
            if isinstance(parents, list)
            else None
        )
        if head_tree != tree_sha or parent_shas != [request.base_commit_sha]:
            logger.warning(
                "existing fix branch does not match approved patch",
                extra={"action": "verify_branch", "branch": request.branch},
            )
            raise RepoCommitError(RepoCommitErrorCode.BRANCH_CONFLICT)

    async def _find_branch(
        self, http: httpx.AsyncClient, token: SecretValue, base: str, branch: str
    ) -> str | None:
        response = await self._send(
            http, "GET", f"{base}/git/ref/heads/{quote(branch, safe='/')}", token
        )
        if response.status_code == 404:
            return None
        _raise_for_unexpected(response, "get_ref")
        sha = _json(response).get("object", {}).get("sha")
        if not isinstance(sha, str):
            raise RepoCommitError(RepoCommitErrorCode.WRITE_FAILED, "ref_sha")
        return sha

    async def _open_pull_request(
        self, http: httpx.AsyncClient, token: SecretValue, base: str, request: RepoCommitRequest
    ) -> str | None:
        pr_base = request.pr_base
        if pr_base is None:
            info = await self._send(http, "GET", base, token)
            _raise_for_unexpected(info, "get_repository")
            default_branch = _json(info).get("default_branch")
            if not isinstance(default_branch, str):
                raise RepoCommitError(RepoCommitErrorCode.WRITE_FAILED, "default_branch")
            pr_base = default_branch
        created = await self._send(
            http,
            "POST",
            f"{base}/pulls",
            token,
            json={
                "title": request.pr_title,
                "head": request.branch,
                "base": pr_base,
                "body": request.pr_body,
            },
        )
        if created.status_code == 422:
            # 같은 브랜치의 PR 이 이미 있다(재시도). 그 주소를 쓴다.
            found = await self._send(
                http,
                "GET",
                f"{base}/pulls",
                token,
                params={"head": f"{request.repo.owner}:{request.branch}", "state": "all"},
            )
            _raise_for_unexpected(found, "find_pull_request")
            items = found.json()
            if isinstance(items, list) and items and isinstance(items[0], dict):
                url = items[0].get("html_url")
                return url if isinstance(url, str) else None
            raise RepoCommitError(RepoCommitErrorCode.WRITE_FAILED, "create_pull_request")
        _raise_for_unexpected(created, "create_pull_request")
        url = _json(created).get("html_url")
        return url if isinstance(url, str) else None

    async def _write_token(self, http: httpx.AsyncClient, repo: RepoLocator) -> SecretValue:
        """이 저장소만, contents·pull_requests 쓰기 권한의 설치 토큰(1시간)."""
        cached = self._tokens.get(repo.full_name)
        if cached is not None:
            return cached
        app_jwt = await self._app_jwt()
        found = await self._send(
            http, "GET", f"/repos/{repo.owner}/{repo.name}/installation", app_jwt
        )
        if found.status_code == 404:
            raise RepoCommitError(RepoCommitErrorCode.APP_NOT_INSTALLED)
        _raise_for_unexpected(found, "find_installation")
        installation_id = _json(found).get("id")
        if not isinstance(installation_id, int):
            raise RepoCommitError(RepoCommitErrorCode.WRITE_FAILED, "installation_id")
        issued = await self._send(
            http,
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            app_jwt,
            json={"repositories": [repo.name], "permissions": WRITE_PERMISSIONS},
        )
        if issued.status_code in (403, 422):
            # App 에 contents·pull_requests 쓰기 권한이 없거나 설치 대상에 이 저장소가 없다.
            logger.warning(
                "github app write permission missing",
                extra={"action": "issue_write_token", "status_code": issued.status_code},
            )
            raise RepoCommitError(RepoCommitErrorCode.WRITE_FORBIDDEN)
        _raise_for_unexpected(issued, "issue_write_token")
        token = _json(issued).get("token")
        if not isinstance(token, str) or not token:
            raise RepoCommitError(RepoCommitErrorCode.WRITE_FAILED, "token")
        value = SecretValue(token)
        self._tokens[repo.full_name] = value
        return value

    async def _app_jwt(self) -> SecretValue:
        try:
            private_key = await self._secret_client.get_secret(self._private_key_ref)
        except SecretLookupError as exc:
            logger.error(
                "github app private key unavailable",
                extra={"action": "sign_app_jwt", "reason": exc.reason},
            )
            raise RepoCommitError(RepoCommitErrorCode.WRITE_UNAVAILABLE) from None
        issued_at = self._clock.now() - _APP_JWT_BACKDATE
        payload = {
            "iat": int(issued_at.timestamp()),
            "exp": int((issued_at + _APP_JWT_LIFETIME).timestamp()),
            "iss": self._app_id,
        }
        try:
            encoded = jwt.encode(payload, private_key.get_secret_value(), algorithm="RS256")
        except (jwt.PyJWTError, ValueError, TypeError):
            logger.error("github app jwt signing failed", extra={"action": "sign_app_jwt"})
            raise RepoCommitError(RepoCommitErrorCode.WRITE_UNAVAILABLE) from None
        return SecretValue(encoded)

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._api_base_url,
            transport=self._transport,
            timeout=self._timeout_seconds,
            follow_redirects=False,
            headers={"X-GitHub-Api-Version": _API_VERSION},
        )

    @staticmethod
    async def _send(
        http: httpx.AsyncClient,
        method: str,
        path: str,
        token: SecretValue,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        accept: str = "application/vnd.github+json",
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token.get_secret_value()}", "Accept": accept}
        try:
            return await http.request(method, path, headers=headers, json=json, params=params)
        except httpx.TransportError as exc:
            logger.warning(
                "github unreachable",
                extra={"action": "repo_commit", "exc_type": type(exc).__name__},
            )
            raise RepoCommitError(RepoCommitErrorCode.WRITE_FAILED, "transport") from None


def _raise_for_unexpected(response: httpx.Response, action: str) -> None:
    """2xx 가 아니면 실패 코드로 바꾼다. 403·404 는 권한·설치 대상 문제로 본다."""
    if response.is_success:
        return
    logger.warning(
        "github returned an error",
        extra={"action": action, "status_code": response.status_code},
    )
    if response.status_code in (403, 404):
        raise RepoCommitError(RepoCommitErrorCode.WRITE_FORBIDDEN, action)
    raise RepoCommitError(RepoCommitErrorCode.WRITE_FAILED, action)


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise RepoCommitError(RepoCommitErrorCode.WRITE_FAILED, "json") from None
    if not isinstance(body, dict):
        raise RepoCommitError(RepoCommitErrorCode.WRITE_FAILED, "json")
    return body


def _sha(body: Mapping[str, Any]) -> str:
    sha = body.get("sha")
    if not isinstance(sha, str) or len(sha) != 40:
        raise RepoCommitError(RepoCommitErrorCode.WRITE_FAILED, "sha")
    return sha.lower()
