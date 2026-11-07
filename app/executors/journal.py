"""요청 기록 재생 실행기 (`journal_replay_mode = http`, API 서버 21).

echo-infra 인프라 12.4 의 진입점 요청 기록(journal) 관리 API 에서 기록을 읽고, 승격된 도착 환경에
받은 순서대로 다시 보낸다. `replication/k8s/scripts/replay.sh` 와 같은 규칙이다.

- 읽기: `GET {journal}/entries?since=<UNIX 초>`(Bearer). 응답 `entries[]` 의 host 가 요청의 host 와
  같은 기록만 쓴다(진입점 하나가 모든 서비스의 쓰기를 기록한다).
- 보내기: `{target_url}{path}` 에 같은 메서드·본문·Content-Type, 헤더 `Idempotency-Key` = 기록의
  requestId. 201 = 새로 들어감, 200 = 이미 있음(앱의 멱등 재응답), 그 밖 = 실패.
- 보내지 않음(skipped): 멱등 키가 없음(다시 보내면 중복될 수 있다)·본문이 잘림·쓰기 메서드가
  아님·경로가 `/` 로 시작하지 않음(도착 주소 밖으로 보내지 않는다).

진입점 쓰기 막기(hold)는 하지 않는다. 승격 직후 자동 재생(hold → 승격 → 재생 → release)은 후속이고,
이 실행기는 사람이 화면에서 누르는 재생·다시 시도용이다. 이미 들어간 쓰기는 200 으로 건너뛰므로
몇 번을 불러도 안전하다.

토큰은 비밀 참조로만 받아 호출마다 읽는다. 토큰·본문·응답 원문은 로그·실패 코드에 넣지 않는다.
"""

import base64
import binascii
import logging
import time
from typing import Any

import httpx

from app.clients.secret_client import SecretClient, SecretLookupError
from app.core.config import Settings
from app.core.secrets import SecretRef, parse_secret_ref
from app.executors.base import (
    JournalReplayed,
    JournalReplayRequest,
    JournalReplayResult,
    JournalReplayUnavailable,
)

logger = logging.getLogger(__name__)

JOURNAL_TOKEN_MISSING = "JOURNAL_TOKEN_MISSING"
JOURNAL_UNAUTHORIZED = "JOURNAL_UNAUTHORIZED"
JOURNAL_UNREACHABLE = "JOURNAL_UNREACHABLE"
JOURNAL_ERROR = "JOURNAL_ERROR"

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
STATUS_CREATED = 201
STATUS_EXISTED = 200


def build_journal_replayer(
    settings: Settings, secret_client: SecretClient
) -> "HttpJournalReplayer":
    """설정으로 만든다. 주소·토큰 참조가 없으면 설정 검증이 먼저 막는다."""
    if not settings.journal_base_url or not settings.journal_token_ref:
        raise ValueError("journal_base_url and journal_token_ref are required")
    return HttpJournalReplayer(
        settings.journal_base_url,
        parse_secret_ref(settings.journal_token_ref),
        secret_client,
        timeout_seconds=settings.journal_request_timeout_seconds,
    )


class HttpJournalReplayer:
    """journal 읽기와 다시 보내기. 요청마다 토큰을 읽고 AsyncClient 를 새로 연다."""

    def __init__(
        self,
        base_url: str,
        token_ref: SecretRef,
        secret_client: SecretClient,
        *,
        timeout_seconds: float = 5.0,
        journal_transport: httpx.AsyncBaseTransport | None = None,
        target_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token_ref = token_ref
        self._secret_client = secret_client
        self._timeout_seconds = timeout_seconds
        self._journal_transport = journal_transport
        self._target_transport = target_transport

    @property
    def enabled(self) -> bool:
        return True

    async def replay(self, request: JournalReplayRequest) -> JournalReplayResult:
        started = time.perf_counter()
        entries = await self._read_entries(request.since.timestamp())
        if isinstance(entries, JournalReplayUnavailable):
            logger.warning(
                "journal entries unavailable", extra={"action": "replay", "code": entries.code}
            )
            return entries
        host = _bare_host(request.host)
        sent = created = existed = failed = skipped = 0
        async with httpx.AsyncClient(
            base_url=request.target_url.rstrip("/"),
            transport=self._target_transport,
            timeout=self._timeout_seconds,
            follow_redirects=False,
        ) as target:
            for entry in entries:
                if _bare_host(entry.get("host")) != host:
                    continue
                outgoing = _outgoing(entry)
                if outgoing is None:
                    skipped += 1
                    continue
                method, path, headers, body = outgoing
                sent += 1
                try:
                    response = await target.request(method, path, headers=headers, content=body)
                except httpx.TransportError:
                    failed += 1
                    continue
                if response.status_code == STATUS_CREATED:
                    created += 1
                elif response.status_code == STATUS_EXISTED:
                    existed += 1
                else:
                    failed += 1
        return JournalReplayed(
            sent=sent,
            created=created,
            existed=existed,
            failed=failed,
            skipped=skipped,
            duration_ms=round((time.perf_counter() - started) * 1000),
        )

    async def _read_entries(self, since: float) -> list[dict[str, Any]] | JournalReplayUnavailable:
        try:
            token = await self._secret_client.get_secret(self._token_ref)
        except SecretLookupError:
            return JournalReplayUnavailable(JOURNAL_TOKEN_MISSING)
        async with httpx.AsyncClient(
            base_url=self._base_url,
            transport=self._journal_transport,
            timeout=self._timeout_seconds,
            follow_redirects=False,
        ) as http:
            try:
                response = await http.get(
                    "/entries",
                    params={"since": f"{since:.3f}"},
                    headers={"Authorization": f"Bearer {token.get_secret_value()}"},
                )
            except httpx.TransportError:
                return JournalReplayUnavailable(JOURNAL_UNREACHABLE)
        if response.status_code == httpx.codes.UNAUTHORIZED:
            return JournalReplayUnavailable(JOURNAL_UNAUTHORIZED)
        if response.status_code == httpx.codes.SERVICE_UNAVAILABLE:
            # journal 에 관리 토큰이 설정되지 않았다(관리 경로 503).
            return JournalReplayUnavailable(JOURNAL_TOKEN_MISSING)
        if not response.is_success:
            return JournalReplayUnavailable(JOURNAL_ERROR)
        try:
            data = response.json()
        except ValueError:
            return JournalReplayUnavailable(JOURNAL_ERROR)
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
            return JournalReplayUnavailable(JOURNAL_ERROR)
        return entries


def _bare_host(value: Any) -> str:
    """비교용 Host: 소문자, 포트 없음. 문자열이 아니면 빈 값(어떤 요청과도 맞지 않는다)."""
    if not isinstance(value, str):
        return ""
    return value.strip().lower().split(":", 1)[0]


def _outgoing(entry: dict[str, Any]) -> tuple[str, str, dict[str, str], bytes] | None:
    """다시 보낼 요청(메서드·경로·헤더·본문). 보내지 않을 기록이면 None."""
    method = entry.get("method")
    path = entry.get("path")
    key = entry.get("requestId")
    if not isinstance(method, str) or method.upper() not in WRITE_METHODS:
        return None
    # 경로만 받는다: `//host/…`·`http://…` 처럼 도착 주소 밖을 가리키는 값은 보내지 않는다.
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        return None
    if not isinstance(key, str) or not key.strip() or entry.get("truncated") is not False:
        return None
    try:
        body = base64.b64decode(entry.get("body") or "", validate=True)
    except (binascii.Error, ValueError):
        return None
    headers = {"Idempotency-Key": key.strip()}
    content_type = entry.get("contentType")
    if isinstance(content_type, str) and content_type.strip():
        headers["Content-Type"] = content_type.strip()
    return method.upper(), path, headers, body
