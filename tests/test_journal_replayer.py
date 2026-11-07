"""요청 기록 재생 실행기 시험(API 서버 21, echo-infra 인프라 12.4 journal 계약).

httpx.MockTransport 두 개로 journal 관리 API(`GET /entries`)와 도착 앱을 흉내 낸다.
"""

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.clients.secret_client import SecretLookupError
from app.core.config import JournalReplayMode, Settings
from app.core.secrets import SecretRef, SecretValue, parse_secret_ref
from app.enums import ConnectionErrorCode, CutoverKind
from app.executors import build_executor_set
from app.executors.base import (
    CutoverRun,
    DisabledJournalReplayer,
    JournalReplayed,
    JournalReplayRequest,
    JournalReplayUnavailable,
)
from app.executors.fake import FakeJournalReplayer
from app.executors.journal import (
    JOURNAL_ERROR,
    JOURNAL_TOKEN_MISSING,
    JOURNAL_UNAUTHORIZED,
    JOURNAL_UNREACHABLE,
    HttpJournalReplayer,
)
from app.services.migration_db_cutover import (
    is_replayable_cutover,
    replay_since,
    replay_to_json,
    run_to_json,
)

JOURNAL_URL = "http://10.60.0.6:8082"
TARGET_URL = "https://shop-api-gcp-a1b2c3d4.gcp-seoul.echo.test"
HOST = "shop-api-a1b2c3d4.echoport.uk"
TOKEN = "journal-test-token"
TOKEN_REF = parse_secret_ref("env:ECHO_SECRET_JOURNAL_TOKEN")
SINCE = datetime(2026, 11, 6, 8, 20, tzinfo=UTC)


class _Secrets:
    def __init__(self, token: str | None = TOKEN) -> None:
        self.token = token

    async def get_secret(self, ref: SecretRef) -> SecretValue:
        if self.token is None:
            raise SecretLookupError(ConnectionErrorCode.SECRET_NOT_FOUND)
        return SecretValue(self.token)


def _entry(
    number: int,
    *,
    method: str = "POST",
    path: str = "/api/writes",
    host: str = HOST,
    key: str | None = None,
    body: bytes = b"{}",
    truncated: bool = False,
) -> dict[str, Any]:
    return {
        "id": number,
        "receivedAt": SINCE.timestamp() + number,
        "method": method,
        "path": path,
        "host": host,
        "requestId": f"r{number}" if key is None else key,
        "contentType": "application/json",
        "body": base64.b64encode(body).decode(),
        "truncated": truncated,
    }


class _Journal:
    """journal 관리 API 대본. 받은 요청을 기억한다."""

    def __init__(self, entries: list[dict[str, Any]] | None = None) -> None:
        self.entries = entries if entries is not None else []
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.payload: Any = None
        self.error: Exception | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": "unauthorized"})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "x"})
        payload = self.payload if self.payload is not None else {"entries": self.entries}
        return httpx.Response(200, json=payload)


class _Target:
    """도착 앱 대본. 멱등 키별로 처음이면 201, 다시 오면 200. fail 에 든 키는 그 상태 코드."""

    def __init__(self, existing: set[str] | None = None) -> None:
        self.seen = set(existing or ())
        self.requests: list[httpx.Request] = []
        self.fail: dict[str, int] = {}
        self.drop: set[str] = set()

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = request.headers["Idempotency-Key"]
        if key in self.drop:
            raise httpx.ConnectError("refused")
        if key in self.fail:
            return httpx.Response(self.fail[key])
        if key in self.seen:
            return httpx.Response(200, json={"n": 1})
        self.seen.add(key)
        return httpx.Response(201, json={"n": 2})


def _replayer(journal: _Journal, target: _Target, secrets: _Secrets | None = None) -> Any:
    return HttpJournalReplayer(
        JOURNAL_URL,
        TOKEN_REF,
        secrets or _Secrets(),
        journal_transport=httpx.MockTransport(journal.handle),
        target_transport=httpx.MockTransport(target.handle),
    )


def _request() -> JournalReplayRequest:
    return JournalReplayRequest(host=HOST, since=SINCE, target_url=TARGET_URL)


def _counts(result: Any) -> tuple[int, int, int, int, int]:
    assert isinstance(result, JournalReplayed)
    return result.sent, result.created, result.existed, result.failed, result.skipped


# --- 읽기·보내기 ---


async def test_replay_sends_entries_in_order_and_counts_created_and_existing() -> None:
    journal = _Journal([_entry(1), _entry(2), _entry(3)])
    target = _Target(existing={"r1"})

    result = await _replayer(journal, target).replay(_request())

    assert _counts(result) == (3, 2, 1, 0, 0)
    read = journal.requests[0]
    assert (read.method, read.url.path) == ("GET", "/entries")
    assert float(read.url.params["since"]) == pytest.approx(SINCE.timestamp())
    assert [item.headers["Idempotency-Key"] for item in target.requests] == ["r1", "r2", "r3"]
    first = target.requests[0]
    assert (first.method, str(first.url)) == ("POST", f"{TARGET_URL}/api/writes")
    assert first.headers["Content-Type"] == "application/json"
    assert first.content == b"{}"
    # 토큰은 journal 에만 보낸다(도착 앱에 넘기지 않는다).
    assert all("Authorization" not in item.headers for item in target.requests)


async def test_replay_keeps_method_and_body_bytes() -> None:
    body = json.dumps({"sku": "apple", "qty": 2}).encode()
    journal = _Journal([_entry(1, method="put", path="/api/orders/7", body=body)])
    target = _Target()

    await _replayer(journal, target).replay(_request())

    sent = target.requests[0]
    assert (sent.method, sent.url.path, sent.content) == ("PUT", "/api/orders/7", body)


async def test_replay_uses_only_entries_for_the_service_host() -> None:
    journal = _Journal(
        [
            _entry(1),
            _entry(2, host="other-svc-x9y8z7w6.echoport.uk"),
            _entry(3, host="SHOP-API-A1B2C3D4.echoport.uk:443"),
        ]
    )
    target = _Target()

    result = await _replayer(journal, target).replay(_request())

    assert _counts(result) == (2, 2, 0, 0, 0)
    assert [item.headers["Idempotency-Key"] for item in target.requests] == ["r1", "r3"]


@pytest.mark.parametrize(
    "entry",
    [
        _entry(1, key=""),
        _entry(1, truncated=True),
        _entry(1, method="GET"),
        _entry(1, path="api/writes"),
        _entry(1, path="//evil.example/steal"),
        {**_entry(1), "body": "not base64!"},
        {**_entry(1), "truncated": None},
    ],
)
async def test_replay_skips_entries_it_must_not_send(entry: dict[str, Any]) -> None:
    target = _Target()

    result = await _replayer(_Journal([entry]), target).replay(_request())

    assert _counts(result) == (0, 0, 0, 0, 1)
    assert target.requests == []


async def test_replay_counts_error_status_and_unreachable_target_as_failed() -> None:
    journal = _Journal([_entry(1), _entry(2), _entry(3)])
    target = _Target()
    target.fail["r1"] = 503
    target.drop.add("r2")

    result = await _replayer(journal, target).replay(_request())

    assert _counts(result) == (3, 1, 0, 2, 0)


async def test_replay_twice_only_creates_once() -> None:
    journal = _Journal([_entry(1), _entry(2)])
    target = _Target()
    replayer = _replayer(journal, target)

    first = await replayer.replay(_request())
    second = await replayer.replay(_request())

    assert (_counts(first), _counts(second)) == ((2, 2, 0, 0, 0), (2, 0, 2, 0, 0))


async def test_replay_with_no_entries_sends_nothing() -> None:
    target = _Target()

    result = await _replayer(_Journal([]), target).replay(_request())

    assert _counts(result) == (0, 0, 0, 0, 0)
    assert target.requests == []


# --- 읽지 못함 ---


async def test_replay_without_token_does_not_call_journal() -> None:
    journal = _Journal([_entry(1)])

    result = await _replayer(journal, _Target(), _Secrets(token=None)).replay(_request())

    assert result == JournalReplayUnavailable(JOURNAL_TOKEN_MISSING)
    assert journal.requests == []


@pytest.mark.parametrize(
    ("status", "code"),
    [(503, JOURNAL_TOKEN_MISSING), (500, JOURNAL_ERROR), (404, JOURNAL_ERROR)],
)
async def test_replay_maps_journal_error_status(status: int, code: str) -> None:
    journal = _Journal([_entry(1)])
    journal.status = status
    target = _Target()

    result = await _replayer(journal, target).replay(_request())

    assert result == JournalReplayUnavailable(code)
    assert target.requests == []


async def test_replay_wrong_token_is_unauthorized() -> None:
    target = _Target()

    result = await _replayer(_Journal([_entry(1)]), target, _Secrets("wrong")).replay(_request())

    assert result == JournalReplayUnavailable(JOURNAL_UNAUTHORIZED)
    assert target.requests == []


async def test_replay_unreachable_journal() -> None:
    journal = _Journal()
    journal.error = httpx.ConnectTimeout("timeout")

    result = await _replayer(journal, _Target()).replay(_request())

    assert result == JournalReplayUnavailable(JOURNAL_UNREACHABLE)


@pytest.mark.parametrize("payload", [[], {"entries": "x"}, {"entries": [1]}, {"other": []}])
async def test_replay_rejects_unexpected_journal_shape(payload: Any) -> None:
    journal = _Journal()
    journal.payload = payload

    result = await _replayer(journal, _Target()).replay(_request())

    assert result == JournalReplayUnavailable(JOURNAL_ERROR)


# --- 꺼짐·가짜·설정 ---


async def test_disabled_and_fake_replayers() -> None:
    disabled = DisabledJournalReplayer()
    fake = FakeJournalReplayer()

    assert disabled.enabled is False
    assert await disabled.replay(_request()) == JournalReplayUnavailable("JOURNAL_REPLAY_DISABLED")
    assert fake.enabled is True
    assert _counts(await fake.replay(_request())) == (0, 0, 0, 0, 0)
    assert fake.requests == [_request()]


def _settings(**changes: object) -> Settings:
    return Settings(database_url="postgresql+asyncpg://x:y@127.0.0.1:1/z").model_copy(
        update=changes
    )


class _Clock:
    def now(self) -> datetime:
        return SINCE


def test_build_executor_set_picks_journal_replayer_by_mode() -> None:
    clock = _Clock()

    disabled = build_executor_set(_settings(), clock).journal_replayer
    fake = build_executor_set(_settings(journal_replay_mode=JournalReplayMode.FAKE), clock)
    http = build_executor_set(
        _settings(
            journal_replay_mode=JournalReplayMode.HTTP,
            journal_base_url=JOURNAL_URL,
            journal_token_ref="env:ECHO_SECRET_JOURNAL_TOKEN",
        ),
        clock,
    )

    assert isinstance(disabled, DisabledJournalReplayer)
    assert isinstance(fake.journal_replayer, FakeJournalReplayer)
    assert isinstance(http.journal_replayer, HttpJournalReplayer)


def test_http_mode_requires_base_url_and_token_ref() -> None:
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+asyncpg://x:y@127.0.0.1:1/z",
            journal_replay_mode=JournalReplayMode.HTTP,
        )


def test_journal_token_ref_must_be_a_secret_reference() -> None:
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql+asyncpg://x:y@127.0.0.1:1/z", journal_token_ref="plain")


# --- 단계 규칙 ---


def _run(mode: str | None, rpo_ms: int | None = 1200) -> CutoverRun:
    return CutoverRun(
        kind=CutoverKind.CUTOVER,
        paused_at=SINCE,
        resumed_at=SINCE + timedelta(seconds=2),
        write_pause_ms=2000,
        drain_ms=None,
        catchup_ms=None,
        switch_ms=None,
        rows_total=181,
        missing_rows=0,
        duplicate_rows=0,
        mode=mode,
        rpo_estimate_ms=rpo_ms,
    )


def _result(status: str, run: CutoverRun | None) -> dict[str, Any]:
    cutover: dict[str, Any] = {"jobName": "echo-cutover-x-1"}
    if run is not None:
        cutover["run"] = run_to_json(run)
    return {"status": status, "cutover": cutover}


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_result("PASSED", _run("PROMOTE")), True),
        (_result("PASSED", _run("NORMAL")), False),
        (_result("PASSED", _run(None)), False),
        (_result("FAILED", _run("PROMOTE")), False),
        (_result("PASSED", None), False),
        ({"status": "PASSED"}, False),
    ],
)
def test_is_replayable_cutover(result: dict[str, Any], expected: bool) -> None:
    assert is_replayable_cutover(result) is expected


def test_replay_since_subtracts_rpo_and_margin() -> None:
    cutover = _result("PASSED", _run("PROMOTE", rpo_ms=1500))["cutover"]
    no_rpo = _result("PASSED", _run("PROMOTE", rpo_ms=None))["cutover"]

    assert replay_since(cutover, 30) == SINCE - timedelta(seconds=31.5)
    assert replay_since(no_rpo, 30) == SINCE - timedelta(seconds=30)


def test_replay_to_json_marks_partial_when_any_failed() -> None:
    done = JournalReplayed(sent=2, created=2, existed=0, failed=0, skipped=1, duration_ms=530)
    partial = JournalReplayed(sent=2, created=1, existed=0, failed=1, skipped=0, duration_ms=10)
    moment = SINCE + timedelta(minutes=5)

    assert replay_to_json(done, SINCE, moment, 1) == {
        "status": "DONE",
        "sent": 2,
        "created": 2,
        "existed": 0,
        "failed": 0,
        "skipped": 1,
        "durationMs": 530,
        "since": SINCE.isoformat(),
        "replayedAt": moment.isoformat(),
        "attempts": 1,
    }
    assert replay_to_json(partial, SINCE, moment, 2)["status"] == "PARTIAL"
