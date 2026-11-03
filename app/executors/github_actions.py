"""실제 빌드 실행기: GitHub Actions workflow_dispatch (`executor_mode = real`, API 서버 3-2).

고객 앱 빌드 워크플로(빌드 저장소의 `customer-build.yml`, 본문은 인프라 담당)를 시작하고 결과
아티팩트(`build-result.json`)에서 이미지 저장소·digest 를 읽는다. 계약은 docs/executors.md 의
"GitHub Actions 빌드".

- 인증: GitHub App JWT → 빌드 저장소 설치 → 설치 토큰(actions 쓰기, 만료 5분 전까지 재사용).
- start: 같은 키의 실행(`display_title == "build {키}"`)을 먼저 찾고, 없을 때만 dispatch
  (`return_run_details: true` → 200 + `workflow_run_id`). external_ref = `gha:{run id}`.
- 고객 저장소 읽기 토큰은 받지 않는다. 비공개 저장소는 워크플로가 직접 App 토큰을 받는다.

App 개인키·JWT·설치 토큰은 SecretValue·지역 변수로만 다루고 로그·예외·external_ref 에 넣지 않는다.
시험은 transport(httpx.MockTransport)와 sleep 을 바꾼다(실제 GitHub 호출 금지).
"""

import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
import jwt

from app.clients.secret_client import SecretClient, SecretLookupError
from app.core.clock import Clock
from app.core.log_masking import mask_secrets
from app.core.repo_url import InvalidRepoUrlError, parse_repo_url
from app.core.secrets import SecretRef, SecretValue
from app.enums import DeployRequestErrorCode, LogLevel
from app.executors.base import (
    BuildFailed,
    BuildPollResult,
    BuildRunning,
    BuildSpec,
    BuildStartError,
    BuildSucceeded,
    ExecutorUnavailableError,
    InvalidLogCursor,
    LogChunk,
    LogLine,
    SourceAccess,
)
from app.executors.retry import Sleep, TransientError, real_sleep, retry_transient

logger = logging.getLogger(__name__)

EXTERNAL_REF_PREFIX = "gha:"
RUN_NAME_PREFIX = "build "
RESULT_ARTIFACT = "build-result"
RESULT_FILE = "build-result.json"
_API_VERSION = "2026-03-10"
_APP_JWT_BACKDATE = timedelta(seconds=60)
_APP_JWT_LIFETIME = timedelta(minutes=9)
_TOKEN_REFRESH_MARGIN = timedelta(minutes=5)
_RUNNING_STATUSES = frozenset({"queued", "in_progress", "waiting", "pending", "requested"})
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPOSITORY = re.compile(r"^[a-z0-9][a-z0-9.-]*(?::\d+)?(?:/[a-z0-9._-]+)+$")
_CURSOR = re.compile(r"^(\d{1,6}):(\d{1,9})$")
_LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?Z ?(.*)$")
# 재시작 뒤 찾기·204 뒤 찾기: 5초 간격 3번. 재시작 찾기는 요청함 시각 60초 전부터 만든 실행만 본다.
FIND_RETRY_DELAYS_SECONDS = (5.0, 5.0, 5.0)
_RESUME_LOOKBACK = timedelta(seconds=60)
_LIST_PAGE_SIZE = 50
_LOG_CACHE_LIMIT = 32

# 경고 로그 코드(값·응답 원문 없이 코드만 남긴다).
BUILD_UNREACHABLE = "BUILD_UNREACHABLE"
BUILD_UNAUTHORIZED = "BUILD_UNAUTHORIZED"
BUILD_RUN_NOT_FOUND = "BUILD_RUN_NOT_FOUND"
BUILD_RESULT_INVALID = "BUILD_RESULT_INVALID"
BUILD_REF_UNKNOWN = "BUILD_REF_UNKNOWN"


class _GithubAuthError(Exception):
    """App 인증·권한 문제(개인키 없음·설치 없음·401/403/404). 다시 불러도 소용없다."""


@dataclass(frozen=True)
class _InstallationToken:
    token: SecretValue
    expires_at: datetime


class GithubActionsBuildExecutor:
    def __init__(
        self,
        *,
        api_base_url: str,
        repository: str,
        workflow: str,
        ref: str,
        app_id: str | None,
        private_key_ref: SecretRef | None,
        secret_client: SecretClient,
        clock: Clock,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = real_sleep,
    ) -> None:
        self._api_base_url = api_base_url.rstrip("/")
        self._repository = repository
        self._workflow = workflow
        self._ref = ref
        self._app_id = app_id
        self._private_key_ref = private_key_ref
        self._secret_client = secret_client
        self._clock = clock
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._sleep = sleep
        self._http: httpx.AsyncClient | None = None
        self._token: _InstallationToken | None = None
        self._log_cache: dict[int, list[tuple[int, int, LogLine]]] = {}

    @property
    def simulated(self) -> bool:
        return False

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # --- start ---

    async def start(self, build: BuildSpec, source: SourceAccess, idempotency_key: str) -> str:
        """같은 키의 실행이 있으면 그 번호, 없으면 새로 시작한다. 읽기 토큰은 쓰지 않는다."""
        try:
            run_id = await retry_transient(
                lambda: self._find_or_dispatch(build, idempotency_key), self._sleep
            )
        except TransientError as exc:
            raise ExecutorUnavailableError(exc.code) from None
        except _GithubAuthError:
            logger.warning(
                "build workflow dispatch rejected",
                extra={"action": "start_build", "build_id": str(build.build_id)},
            )
            raise BuildStartError(DeployRequestErrorCode.BUILD_DISPATCH_FAILED) from None
        logger.info(
            "build workflow run attached",
            extra={"action": "start_build", "build_id": str(build.build_id), "run_id": run_id},
        )
        return f"{EXTERNAL_REF_PREFIX}{run_id}"

    async def _find_or_dispatch(self, build: BuildSpec, key: str) -> int:
        if build.resumed_after is not None:
            # 직전 일꾼이 dispatch 에 성공하고 번호를 저장하기 전에 죽었을 수 있다. 목록에 늦게
            # 보일 수 있어 몇 번 더 찾은 뒤에만 새로 시작한다.
            found = await self._find_with_retries(key, build.resumed_after - _RESUME_LOOKBACK)
        else:
            found = await self._find_run(key, None)
        if found is not None:
            return found
        try:
            repo = parse_repo_url(build.repo_url)
        except InvalidRepoUrlError:
            raise _GithubAuthError from None
        response = await self._request(
            "POST",
            f"/repos/{self._repository}/actions/workflows/{self._workflow}/dispatches",
            json={
                "ref": self._ref,
                "inputs": {
                    "build_id": key,
                    "repo": repo.full_name,
                    "commit_sha": build.commit_sha,
                    "private": "true" if build.repo_private else "false",
                },
                "return_run_details": True,
            },
        )
        if response.status_code in (403, 404, 422) and not _is_rate_limited(response):
            raise _GithubAuthError
        _raise_for_transient(response)
        if response.status_code == 204:
            found = await self._find_with_retries(key, None)
            if found is None:
                raise TransientError(BUILD_RUN_NOT_FOUND)
            return found
        if not response.is_success:
            raise _GithubAuthError
        run_id = _json(response).get("workflow_run_id")
        if not isinstance(run_id, int) or isinstance(run_id, bool):
            raise TransientError(BUILD_RUN_NOT_FOUND)
        return run_id

    async def _find_with_retries(self, key: str, created_after: datetime | None) -> int | None:
        found = await self._find_run(key, created_after)
        for delay in FIND_RETRY_DELAYS_SECONDS:
            if found is not None:
                return found
            await self._sleep(delay)
            found = await self._find_run(key, created_after)
        return found

    async def _find_run(self, key: str, created_after: datetime | None) -> int | None:
        """같은 키의 실행(`display_title == "build {키}"`). 여럿이면 가장 먼저 만든 것."""
        response = await self._request(
            "GET",
            f"/repos/{self._repository}/actions/workflows/{self._workflow}/runs",
            params={
                "event": "workflow_dispatch",
                "branch": self._ref,
                "per_page": str(_LIST_PAGE_SIZE),
            },
        )
        if response.status_code in (401, 403, 404) and not _is_rate_limited(response):
            raise _GithubAuthError
        _raise_for_transient(response)
        runs = _json(response).get("workflow_runs")
        if not isinstance(runs, list):
            raise TransientError(BUILD_UNREACHABLE)
        title = f"{RUN_NAME_PREFIX}{key}"
        candidates: list[tuple[str, int]] = []
        for run in runs:
            if not isinstance(run, dict) or run.get("display_title") != title:
                continue
            run_id, created_at = run.get("id"), run.get("created_at")
            if not isinstance(run_id, int) or not isinstance(created_at, str):
                continue
            if created_after is not None and _parse_time(created_at) < created_after:
                continue
            candidates.append((created_at, run_id))
        return min(candidates)[1] if candidates else None

    # --- poll ---

    async def poll(self, external_ref: str) -> BuildPollResult:
        run_id = _parse_ref(external_ref)
        if run_id is None:
            _warn(BUILD_REF_UNKNOWN, None)
            return BuildFailed()
        try:
            run = await self._get_run(run_id)
            if run is None:
                _warn(BUILD_RUN_NOT_FOUND, run_id)
                return BuildFailed()
            if run.get("status") != "completed":
                return BuildRunning()
            if run.get("conclusion") != "success":
                return BuildFailed()
            result = await self._read_result(run_id)
        except TransientError as exc:
            _warn(exc.code, run_id)
            return BuildRunning()
        except _GithubAuthError:
            _warn(BUILD_UNAUTHORIZED, run_id)
            return BuildFailed()
        # 결과 파일의 build_id 는 이 실행 이름의 키와 같아야 한다(다른 빌드의 결과를 쓰지 않는다).
        if result is None or run.get("display_title") != f"{RUN_NAME_PREFIX}{result[1]}":
            _warn(BUILD_RESULT_INVALID, run_id)
            return BuildFailed()
        return result[0]

    async def _get_run(self, run_id: int) -> dict[str, Any] | None:
        response = await self._request("GET", f"/repos/{self._repository}/actions/runs/{run_id}")
        if response.status_code == 404:
            return None
        _raise_for_poll(response)
        return _json(response)

    async def _read_result(self, run_id: int) -> tuple[BuildSucceeded, str] | None:
        """아티팩트 build-result 의 결과 파일 → (결과, build_id).

        없거나 모양이 다르면 None.
        """
        response = await self._request(
            "GET",
            f"/repos/{self._repository}/actions/runs/{run_id}/artifacts",
            params={"name": RESULT_ARTIFACT},
        )
        _raise_for_poll(response)
        artifacts = _json(response).get("artifacts")
        artifact = next(
            (
                item
                for item in artifacts or []
                if isinstance(item, dict)
                and item.get("name") == RESULT_ARTIFACT
                and not item.get("expired")
            ),
            None,
        )
        url = artifact.get("archive_download_url") if artifact is not None else None
        # 우리 토큰을 실을 주소라 API 주소 아래인지 확인한다.
        if not isinstance(url, str) or not url.startswith(f"{self._api_base_url}/"):
            return None
        archive = await self._download(url)
        if archive is None:
            return None
        return _parse_result(archive)

    # --- 로그 ---

    async def read_logs(self, external_ref: str, cursor: str | None, limit: int) -> LogChunk:
        """잡 로그를 잡 순서·줄 순서로 잇는다. cursor = `{잡 순번}:{줄 번호}`(다음에 읽을 줄).

        실행이 끝나기 전에는 빈 청크(has_more=True, 같은 cursor)다. 줄은 Actions 가 이미 가린
        값 위에 알려진 토큰 모양을 한 번 더 가린다.
        """
        start = _parse_cursor(cursor)
        run_id = _parse_ref(external_ref)
        if run_id is None:
            return LogChunk(lines=(), next_cursor=cursor, has_more=False)
        try:
            lines = await self._run_log_lines(run_id)
        except TransientError as exc:
            _warn(exc.code, run_id)
            return LogChunk(lines=(), next_cursor=cursor, has_more=True)
        except _GithubAuthError:
            _warn(BUILD_UNAUTHORIZED, run_id)
            return LogChunk(lines=(), next_cursor=cursor, has_more=False)
        if lines is None:
            return LogChunk(lines=(), next_cursor=cursor, has_more=True)
        index = next(
            (position for position, (key, _) in enumerate(lines) if key >= start), len(lines)
        )
        page = lines[index : index + limit]
        if not page:
            return LogChunk(lines=(), next_cursor=cursor, has_more=False)
        job, line = page[-1][0]
        return LogChunk(
            lines=tuple(item for _, item in page),
            next_cursor=f"{job}:{line + 1}",
            has_more=index + len(page) < len(lines),
        )

    async def _run_log_lines(self, run_id: int) -> list[tuple[tuple[int, int], LogLine]] | None:
        """끝난 실행의 (위치, 줄) 목록. 아직 진행 중이면 None.

        끝난 실행의 로그는 바뀌지 않아 이 프로세스에 기억한다(최근 32개).
        """
        cached = self._log_cache.get(run_id)
        if cached is None:
            run = await self._get_run(run_id)
            if run is None:
                return []
            if run.get("status") != "completed":
                return None
            cached = await self._collect_logs(run_id, _parse_time(str(run.get("created_at"))))
            while len(self._log_cache) >= _LOG_CACHE_LIMIT:
                self._log_cache.pop(next(iter(self._log_cache)))
            self._log_cache[run_id] = cached
        return [((job, number), line) for job, number, line in cached]

    async def _collect_logs(
        self, run_id: int, run_created_at: datetime
    ) -> list[tuple[int, int, LogLine]]:
        response = await self._request(
            "GET",
            f"/repos/{self._repository}/actions/runs/{run_id}/jobs",
            params={"per_page": "100"},
        )
        _raise_for_poll(response)
        jobs = [job for job in _json(response).get("jobs") or [] if isinstance(job, dict)]
        jobs.sort(key=lambda job: (str(job.get("started_at") or ""), int(job.get("id") or 0)))
        lines: list[tuple[int, int, LogLine]] = []
        for job_index, job in enumerate(jobs):
            raw = await self._download(f"/repos/{self._repository}/actions/jobs/{job['id']}/logs")
            text = raw.decode("utf-8", "replace") if raw else ""
            for number, line in enumerate(_parse_job_log(text, run_created_at)):
                lines.append((job_index, number, line))
        return lines

    # --- HTTP ---

    async def _download(self, url: str) -> bytes | None:
        """GitHub 가 302 로 넘기는 내려받기. 넘겨받은 주소에는 우리 토큰을 보내지 않는다."""
        response = await self._request("GET", url)
        if response.status_code in (404, 410):
            return None
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            if not location:
                return None
            try:
                # 서명된 저장소 주소다. 우리 토큰은 싣지 않는다(요청마다 붙이므로 여기엔 없다).
                response = await self._client().get(location)
            except httpx.TransportError:
                raise TransientError(BUILD_UNREACHABLE) from None
            if response.status_code in (404, 410):
                return None
        _raise_for_poll(response)
        return response.content

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        token = await self._installation_token()
        try:
            return await self._client().request(
                method,
                path,
                json=json,
                params=params,
                headers={"Authorization": f"Bearer {token.get_secret_value()}"},
            )
        except httpx.TransportError:
            raise TransientError(BUILD_UNREACHABLE) from None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self._api_base_url,
                transport=self._transport,
                timeout=self._timeout_seconds,
                follow_redirects=False,
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": _API_VERSION,
                },
            )
        return self._http

    async def _installation_token(self) -> SecretValue:
        """빌드 저장소 설치 토큰(actions 쓰기). 만료 5분 전까지 재사용한다."""
        cached = self._token
        if cached is not None and self._clock.now() < cached.expires_at - _TOKEN_REFRESH_MARGIN:
            return cached.token
        app_jwt = await self._app_jwt()
        headers = {"Authorization": f"Bearer {app_jwt.get_secret_value()}"}
        http = self._client()
        try:
            response = await http.get(f"/repos/{self._repository}/installation", headers=headers)
            _raise_for_auth(response)
            installation_id = _json(response).get("id")
            if not isinstance(installation_id, int):
                raise _GithubAuthError
            response = await http.post(
                f"/app/installations/{installation_id}/access_tokens",
                headers=headers,
                json={
                    "repositories": [self._repository.split("/", 1)[1]],
                    "permissions": {"actions": "write"},
                },
            )
        except httpx.TransportError:
            raise TransientError(BUILD_UNREACHABLE) from None
        _raise_for_auth(response)
        body = _json(response)
        token, expires_at = body.get("token"), body.get("expires_at")
        if not isinstance(token, str) or not token or not isinstance(expires_at, str):
            raise _GithubAuthError
        self._token = _InstallationToken(SecretValue(token), _parse_time(expires_at))
        return self._token.token

    async def _app_jwt(self) -> SecretValue:
        if self._app_id is None or self._private_key_ref is None:
            raise _GithubAuthError
        try:
            private_key = await self._secret_client.get_secret(self._private_key_ref)
        except SecretLookupError:
            raise _GithubAuthError from None
        issued_at = self._clock.now() - _APP_JWT_BACKDATE
        payload = {
            "iat": int(issued_at.timestamp()),
            "exp": int((issued_at + _APP_JWT_LIFETIME).timestamp()),
            "iss": self._app_id,
        }
        try:
            encoded = jwt.encode(payload, private_key.get_secret_value(), algorithm="RS256")
        except (jwt.PyJWTError, ValueError, TypeError):
            # 예외 메시지에 키 내용이 섞일 수 있어 원인을 붙이지 않는다.
            raise _GithubAuthError from None
        return SecretValue(encoded)


def _parse_job_log(text: str, fallback_at: datetime) -> list[LogLine]:
    """`<ISO 시각>Z <문장>` 줄들. 시각이 없는 줄은 앞 줄 시각을 쓴다. `##[error]` 는 ERROR."""
    lines: list[LogLine] = []
    at = fallback_at
    for raw in text.lstrip("﻿").splitlines():
        if not raw.strip():
            continue
        match = _LOG_LINE.match(raw)
        message = raw
        if match is not None:
            fraction = (match[2] or ".0")[:7]
            at = max(at, _parse_time(f"{match[1]}{fraction}Z"))
            message = match[3]
        level = LogLevel.INFO
        if message.startswith("##[error]"):
            level, message = LogLevel.ERROR, message.removeprefix("##[error]")
        elif message.startswith("##[warning]"):
            level, message = LogLevel.WARN, message.removeprefix("##[warning]")
        lines.append(LogLine(at=at, level=level, message=mask_secrets(message)))
    return lines


def _parse_result(archive: bytes) -> tuple[BuildSucceeded, str] | None:
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            raw = bundle.read(RESULT_FILE)
        body = json.loads(raw)
    except (zipfile.BadZipFile, KeyError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    digest, repository, build_id = (
        body.get("image_digest"),
        body.get("ar_repository"),
        body.get("build_id"),
    )
    if (
        not isinstance(digest, str)
        or not _DIGEST.fullmatch(digest)
        or not isinstance(repository, str)
        or not _REPOSITORY.fullmatch(repository)
        or not isinstance(build_id, str)
    ):
        return None
    return BuildSucceeded(image_repository=repository, image_digest=digest), build_id


def _parse_ref(external_ref: str) -> int | None:
    value = external_ref.removeprefix(EXTERNAL_REF_PREFIX)
    if value == external_ref or not value.isascii() or not value.isdigit():
        return None
    return int(value)


def _parse_cursor(cursor: str | None) -> tuple[int, int]:
    if cursor is None:
        return (0, 0)
    match = _CURSOR.fullmatch(cursor)
    if match is None:
        raise InvalidLogCursor
    return (int(match[1]), int(match[2]))


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_rate_limited(response: httpx.Response) -> bool:
    return response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers


def _raise_for_transient(response: httpx.Response) -> None:
    if (
        response.status_code >= 500
        or response.status_code == 429
        or (response.status_code == 403 and _is_rate_limited(response))
    ):
        raise TransientError(BUILD_UNREACHABLE)


def _raise_for_auth(response: httpx.Response) -> None:
    _raise_for_transient(response)
    if not response.is_success:
        raise _GithubAuthError


def _raise_for_poll(response: httpx.Response) -> None:
    _raise_for_transient(response)
    if response.status_code in (401, 403):
        raise _GithubAuthError
    if not response.is_success:
        raise TransientError(BUILD_UNREACHABLE)


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise TransientError(BUILD_UNREACHABLE) from None
    if not isinstance(body, dict):
        raise TransientError(BUILD_UNREACHABLE)
    return body


def _warn(code: str, run_id: int | None) -> None:
    logger.warning(
        "build executor call failed",
        extra={"action": "poll_build", "run_id": run_id, "error_code": code},
    )
