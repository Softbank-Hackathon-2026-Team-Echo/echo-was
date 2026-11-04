"""앱 환경 변수 보관함 클라이언트(SPEC api-10 AC-8). 메타데이터·Secret Manager 는 가짜 전송이다."""

import base64
import json
import logging
import uuid
from dataclasses import dataclass

import httpx
import pytest
from pydantic import ValidationError

from app.clients.app_secret_store import (
    AppSecretForbiddenError,
    AppSecretNotFoundError,
    AppSecretRejectedError,
    AppSecretUnavailableError,
    FakeAppSecretStore,
    GcpAppSecretStore,
    build_app_secret_store,
)
from app.core.config import AppSecretStoreMode, Settings
from app.core.secrets import SecretValue
from app.executors.google_metadata import GoogleMetadataClient
from tests.real_executor_fakes import (
    GOOGLE_ACCESS_TOKEN,
    METADATA_BASE,
    SECRET_MANAGER_PROJECT_NUMBER,
    FakeCloud,
    FakeSecretManager,
    RecordingSleep,
    TestClock,
)

PROJECT = "team-echo-final"
NAME = "echo-app-team-echo-aaaa1111-bbbb2222-database-url"
LABELS = {"echo-org": "cccc3333", "managed-by": "echo-was"}
VALUE = f"sv-{uuid.uuid4()}"


@dataclass
class World:
    cloud: FakeCloud
    manager: FakeSecretManager
    sleep: RecordingSleep
    store: GcpAppSecretStore

    def paths(self) -> list[tuple[str, str]]:
        return [(request.method, request.url.path) for request in self.manager.requests]


def _world() -> World:
    clock = TestClock()
    cloud = FakeCloud(clock)
    manager = FakeSecretManager()
    sleep = RecordingSleep()
    metadata = GoogleMetadataClient(METADATA_BASE, clock, transport=cloud.transport())
    store = GcpAppSecretStore(PROJECT, metadata, transport=manager.transport(), sleep=sleep)
    return World(cloud, manager, sleep, store)


def _status(status: int, error: str = "X") -> httpx.Response:
    return httpx.Response(status, json={"error": {"status": error}})


async def test_add_version_creates_missing_secret_then_adds_version() -> None:
    world = _world()

    version = await world.store.add_version(NAME, SecretValue(VALUE), LABELS)

    create, add = world.manager.requests
    assert version == 1
    assert world.paths() == [
        ("POST", f"/v1/projects/{PROJECT}/secrets"),
        ("POST", f"/v1/projects/{PROJECT}/secrets/{NAME}:addVersion"),
    ]
    assert create.url.host == "secretmanager.googleapis.com"
    assert create.url.params["secretId"] == NAME
    assert json.loads(create.content) == {
        "replication": {"automatic": {}},
        "labels": LABELS,
    }
    assert json.loads(add.content) == {
        "payload": {"data": base64.b64encode(VALUE.encode()).decode()}
    }
    assert VALUE.encode() not in create.content


async def test_add_version_treats_already_exists_as_success_and_continues_versions() -> None:
    world = _world()
    await world.store.add_version(NAME, SecretValue("first"), LABELS)

    version = await world.store.add_version(NAME, SecretValue(VALUE), LABELS)

    assert version == 2
    assert [request.url.path.rsplit(":", 1)[-1] for request in world.manager.requests].count(
        "addVersion"
    ) == 2


async def test_version_number_comes_only_from_last_segment_and_ref_uses_project_id() -> None:
    world = _world()
    world.manager.secrets[NAME] = ["a", "b"]

    version = await world.store.add_version(NAME, SecretValue(VALUE), LABELS)

    assert version == 3
    assert SECRET_MANAGER_PROJECT_NUMBER not in world.store.secret_ref(NAME, version)
    assert world.store.secret_ref(NAME, version) == (
        f"gcp-sm:projects/{PROJECT}/secrets/{NAME}/versions/3"
    )


@pytest.mark.parametrize(
    "name",
    [None, 7, "projects/x/secrets/y", "projects/x/secrets/y/versions/latest", "/versions/0"],
)
async def test_unreadable_version_name_is_unavailable(name: object) -> None:
    world = _world()
    response = httpx.Response(200, json={"name": name})
    world.manager.script = [
        _status(200),
        response,
        *[
            item
            for _ in range(3)
            for item in (_status(200), httpx.Response(200, json={"name": name}))
        ],
    ]

    with pytest.raises(AppSecretUnavailableError):
        await world.store.add_version(NAME, SecretValue(VALUE), LABELS)


async def test_access_reads_pinned_version() -> None:
    world = _world()
    await world.store.add_version(NAME, SecretValue("old"), LABELS)
    await world.store.add_version(NAME, SecretValue("새 값\nline2"), LABELS)
    world.manager.requests.clear()

    old = await world.store.access(NAME, 1)
    new = await world.store.access(NAME, 2)

    assert (old.get_secret_value(), new.get_secret_value()) == ("old", "새 값\nline2")
    assert world.paths()[0] == ("GET", f"/v1/projects/{PROJECT}/secrets/{NAME}/versions/1:access")


async def test_requests_carry_metadata_token_header() -> None:
    world = _world()

    await world.store.add_version(NAME, SecretValue(VALUE), LABELS)

    assert {request.headers["authorization"] for request in world.manager.requests} == {
        f"Bearer {GOOGLE_ACCESS_TOKEN}1"
    }
    assert world.cloud.metadata_token_calls == 1


async def test_401_refreshes_token_once_and_succeeds() -> None:
    world = _world()
    world.manager.secrets[NAME] = [base64.b64encode(b"x").decode()]
    world.manager.script = [_status(401)]

    value = await world.store.access(NAME, 1)

    assert value.get_secret_value() == "x"
    assert [request.headers["authorization"] for request in world.manager.requests] == [
        f"Bearer {GOOGLE_ACCESS_TOKEN}1",
        f"Bearer {GOOGLE_ACCESS_TOKEN}2",
    ]
    assert world.sleep.delays == []


@pytest.mark.parametrize("statuses", [[403], [401, 401], [401, 403]])
async def test_denied_is_forbidden_without_backoff(statuses: list[int]) -> None:
    world = _world()
    world.manager.script = [_status(status) for status in statuses]

    with pytest.raises(AppSecretForbiddenError):
        await world.store.access(NAME, 1)

    assert world.sleep.delays == []
    assert len(world.manager.requests) == len(statuses)


async def test_metadata_refusal_is_forbidden() -> None:
    clock = TestClock()
    metadata = GoogleMetadataClient(
        METADATA_BASE,
        clock,
        transport=httpx.MockTransport(lambda _: httpx.Response(404)),
    )
    store = GcpAppSecretStore(PROJECT, metadata, transport=FakeSecretManager().transport())

    with pytest.raises(AppSecretForbiddenError):
        await store.access(NAME, 1)


@pytest.mark.parametrize("status", [500, 503, 429])
async def test_server_errors_retry_with_backoff_then_succeed(status: int) -> None:
    world = _world()
    world.manager.secrets[NAME] = [base64.b64encode(b"x").decode()]
    world.manager.script = [_status(status), _status(status), _status(status)]

    value = await world.store.access(NAME, 1)

    assert value.get_secret_value() == "x"
    assert world.sleep.delays == [1.0, 2.0, 4.0]


async def test_server_errors_exhausted_are_unavailable() -> None:
    world = _world()
    world.manager.script = [_status(503) for _ in range(4)]

    with pytest.raises(AppSecretUnavailableError):
        await world.store.access(NAME, 1)

    assert world.sleep.delays == [1.0, 2.0, 4.0]


async def test_connection_failure_is_retried_then_unavailable() -> None:
    world = _world()
    world.manager.script = [httpx.ConnectError("boom") for _ in range(4)]

    with pytest.raises(AppSecretUnavailableError):
        await world.store.add_version(NAME, SecretValue(VALUE), LABELS)

    assert world.sleep.delays == [1.0, 2.0, 4.0]


async def test_metadata_temporary_failure_is_unavailable() -> None:
    clock = TestClock()
    sleep = RecordingSleep()
    metadata = GoogleMetadataClient(
        METADATA_BASE,
        clock,
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
    )
    store = GcpAppSecretStore(
        PROJECT, metadata, transport=FakeSecretManager().transport(), sleep=sleep
    )

    with pytest.raises(AppSecretUnavailableError):
        await store.access(NAME, 1)

    assert sleep.delays == [1.0, 2.0, 4.0]


async def test_access_missing_version_is_not_found() -> None:
    world = _world()

    with pytest.raises(AppSecretNotFoundError):
        await world.store.access(NAME, 9)


@pytest.mark.parametrize(
    "response",
    [
        _status(400, "FAILED_PRECONDITION"),
        httpx.Response(200, json={"payload": {}}),
        httpx.Response(200, json={"payload": {"data": "@@not-base64@@"}}),
        httpx.Response(200, json={"payload": {"data": base64.b64encode(b"\xff").decode()}}),
    ],
    ids=["failed-precondition", "no-data", "bad-base64", "not-utf8"],
)
async def test_access_permanent_failures_are_rejected(response: httpx.Response) -> None:
    world = _world()
    world.manager.script = [response]

    with pytest.raises(AppSecretRejectedError):
        await world.store.access(NAME, 1)

    assert world.sleep.delays == []


async def test_logs_and_errors_never_contain_value_or_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    world = _world()
    await world.store.add_version(NAME, SecretValue(VALUE), LABELS)
    await world.store.access(NAME, 1)
    world.manager.script = [_status(403)]

    with pytest.raises(AppSecretForbiddenError) as denied:
        await world.store.access(NAME, 1)

    assert VALUE not in caplog.text
    assert GOOGLE_ACCESS_TOKEN not in caplog.text
    assert NAME not in str(denied.value)
    assert VALUE not in repr(denied.value)


async def test_aclose_closes_store_and_metadata_clients() -> None:
    world = _world()
    await world.store.add_version(NAME, SecretValue(VALUE), LABELS)

    await world.store.aclose()
    await world.store.aclose()


# --- 가짜 보관함·설정 ---


async def test_fake_store_versions_and_failures() -> None:
    store = FakeAppSecretStore(forbidden_names={"denied"}, unavailable_names={"down"})

    first = await store.add_version("a", SecretValue("one"), {})
    second = await store.add_version("a", SecretValue("two"), {})

    assert (first, second) == (1, 2)
    assert (await store.access("a", 1)).get_secret_value() == "one"
    assert store.secret_ref("a", 2) == "fake-sm:a/versions/2"
    assert store.simulated is True
    with pytest.raises(AppSecretNotFoundError):
        await store.access("a", 3)
    with pytest.raises(AppSecretNotFoundError):
        await store.access("missing", 1)
    with pytest.raises(AppSecretForbiddenError):
        await store.add_version("denied", SecretValue("x"), {})
    with pytest.raises(AppSecretUnavailableError):
        await store.access("down", 1)


def _settings(**changes: object) -> Settings:
    return Settings(database_url="postgresql+asyncpg://x:y@127.0.0.1:1/z", **changes)  # type: ignore[arg-type]


def test_settings_default_to_fake_store() -> None:
    settings = _settings()

    assert settings.app_secret_store_mode is AppSecretStoreMode.FAKE
    assert settings.app_secret_name_prefix == "echo-app-"
    assert isinstance(build_app_secret_store(settings, TestClock()), FakeAppSecretStore)


def test_gcp_store_requires_project_id() -> None:
    with pytest.raises(ValidationError):
        _settings(app_secret_store_mode="gcp")


@pytest.mark.parametrize("prefix", ["", "Echo-", "echo_app", "a" * 33])
def test_name_prefix_must_be_lowercase_label(prefix: str) -> None:
    with pytest.raises(ValidationError):
        _settings(app_secret_name_prefix=prefix)


async def test_building_gcp_store_makes_no_network_call() -> None:
    settings = _settings(
        app_secret_store_mode="gcp",
        app_secret_project_id=PROJECT,
        google_metadata_base_url=METADATA_BASE,
    )
    cloud = FakeCloud(TestClock())

    store = build_app_secret_store(settings, TestClock(), metadata_transport=cloud.transport())

    assert isinstance(store, GcpAppSecretStore)
    assert store.simulated is False
    assert cloud.metadata_token_calls == 0
    await store.aclose()
