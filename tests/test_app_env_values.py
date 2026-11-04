"""배포 기록 참조 → 실행기 값 표, 키·값 규칙, 보관함 이름(SPEC api-10 5-4, AD-2·AD-3·AD-4)."""

import uuid
from typing import Any

import pytest

from app.clients.app_secret_store import FakeAppSecretStore
from app.core.config import AppSecretStoreMode, ExecutorMode, Settings
from app.core.exceptions import (
    EnvVarKeyInvalidError,
    EnvVarKeyReservedError,
    EnvVarValueInvalidError,
)
from app.core.secrets import SecretValue
from app.executors.base import ExecutorUnavailableError
from app.models.deploy_request_log import DEPLOY_LOG_MESSAGE_MAX_LENGTH
from app.services.app_env_values import (
    SIMULATED_ENV_VALUE,
    AppEnvResolver,
    EnvSecretUnavailableError,
    pinned_versions,
)
from app.services.deploy_log_messages import env_vars_applied_message
from app.services.service_variable_service import (
    app_secret_labels,
    app_secret_name,
    check_env_var_key,
    check_env_var_value,
)

PROJECT = "team-echo-final"
GCP_REF = f"gcp-sm:projects/{PROJECT}/secrets/name-a/versions/2"
FAKE_REF = "fake-sm:name-a/versions/1"


def _settings(**changes: Any) -> Settings:
    settings = Settings(database_url="postgresql+asyncpg://x:y@127.0.0.1:1/z")
    return settings.model_copy(update=changes)


def _gcp_settings() -> Settings:
    return _settings(
        app_secret_store_mode=AppSecretStoreMode.GCP,
        app_secret_project_id=PROJECT,
        executor_mode=ExecutorMode.REAL,
    )


class _RealLookingStore(FakeAppSecretStore):
    @property
    def simulated(self) -> bool:
        return False


async def _store_with(*values: str, name: str = "name-a") -> _RealLookingStore:
    store = _RealLookingStore()
    for value in values:
        await store.add_version(name, SecretValue(value), {})
    return store


# --- 5-4 표 ---


async def test_record_without_env_vars_key_touches_nothing() -> None:
    store = FakeAppSecretStore()

    assert await AppEnvResolver(store, _gcp_settings()).resolve({"containerPort": 1}) is None
    assert store.access_calls == []


async def test_fake_ref_in_gcp_mode_touches_nothing() -> None:
    store = FakeAppSecretStore()
    snapshot = {"envVars": {"A": FAKE_REF, "B": GCP_REF}}

    assert await AppEnvResolver(store, _gcp_settings()).resolve(snapshot) is None
    assert store.access_calls == []


@pytest.mark.parametrize("refs", [{}, {"A": FAKE_REF}, {"A": GCP_REF}])
async def test_fake_store_with_real_executor_touches_nothing(refs: dict[str, str]) -> None:
    store = FakeAppSecretStore()
    settings = _settings(executor_mode=ExecutorMode.REAL)

    assert await AppEnvResolver(store, settings).resolve({"envVars": refs}) is None
    assert store.access_calls == []


async def test_empty_env_vars_is_empty_dict() -> None:
    resolver = AppEnvResolver(FakeAppSecretStore(), _gcp_settings())

    assert await resolver.resolve({"envVars": {}}) == {}


async def test_values_are_read_at_the_pinned_version() -> None:
    store = await _store_with("old", "new")
    resolver = AppEnvResolver(store, _gcp_settings())

    values = await resolver.resolve({"envVars": {"A": GCP_REF.replace("/2", "/1")}})

    assert values is not None
    assert values["A"].get_secret_value() == "old"
    assert store.access_calls == [("name-a", 1)]


# --- 못 꺼낼 때 ---


async def test_permanent_failures_report_all_key_names_only() -> None:
    store = await _store_with("v", "v")
    store.fail_names.add("broken")
    snapshot = {
        "envVars": {
            "OK": GCP_REF,
            "MISSING": f"gcp-sm:projects/{PROJECT}/secrets/name-a/versions/9",
            "BROKEN": f"gcp-sm:projects/{PROJECT}/secrets/broken/versions/1",
            "OTHER_PROJECT": "gcp-sm:projects/other/secrets/name-a/versions/1",
            "GARBAGE": "not-a-ref",
        }
    }

    with pytest.raises(EnvSecretUnavailableError) as failure:
        await AppEnvResolver(store, _gcp_settings()).resolve(snapshot)

    assert failure.value.keys == ("BROKEN", "GARBAGE", "MISSING", "OTHER_PROJECT")


async def test_forbidden_is_permanent() -> None:
    store = await _store_with("v", "v")
    store.forbidden_names.add("name-a")

    with pytest.raises(EnvSecretUnavailableError):
        await AppEnvResolver(store, _gcp_settings()).resolve({"envVars": {"A": GCP_REF}})


async def test_temporary_failure_is_executor_unavailable() -> None:
    store = await _store_with("v", "v")
    store.unavailable_names.add("name-a")

    with pytest.raises(ExecutorUnavailableError):
        await AppEnvResolver(store, _gcp_settings()).resolve({"envVars": {"A": GCP_REF}})


async def test_fake_executor_substitutes_only_for_values_missing_in_this_process() -> None:
    store = FakeAppSecretStore()
    resolver = AppEnvResolver(store, _settings())

    values = await resolver.resolve({"envVars": {"A": FAKE_REF}})

    assert values is not None
    assert values["A"].get_secret_value() == SIMULATED_ENV_VALUE
    store.fail_names.add("name-a")
    with pytest.raises(EnvSecretUnavailableError):
        await resolver.resolve({"envVars": {"A": FAKE_REF}})


async def test_real_executor_never_substitutes_missing_value() -> None:
    store = _RealLookingStore()

    with pytest.raises(EnvSecretUnavailableError):
        await AppEnvResolver(store, _gcp_settings()).resolve({"envVars": {"A": GCP_REF}})


def test_pinned_versions_reads_only_known_refs() -> None:
    assert pinned_versions({}) == {}
    assert pinned_versions({"envVars": {"A": GCP_REF, "B": FAKE_REF, "C": "garbage", "D": 3}}) == {
        "A": 2,
        "B": 1,
    }


# --- 키·값 규칙 ---


@pytest.mark.parametrize("key", ["port", "1A", "A-B", "", "a", "A" * 65, "Ａ", "A\n"])
def test_invalid_keys(key: str) -> None:
    with pytest.raises(EnvVarKeyInvalidError):
        check_env_var_key(key)


@pytest.mark.parametrize("key", ["PORT", "ECHO_X", "ECHO_"])
def test_reserved_keys(key: str) -> None:
    with pytest.raises(EnvVarKeyReservedError):
        check_env_var_key(key)


@pytest.mark.parametrize("key", ["A", "DATABASE_URL", "A1_B2", "A" * 64, "PORTS", "ECHO"])
def test_valid_keys(key: str) -> None:
    check_env_var_key(key)


@pytest.mark.parametrize("value", ["", "a" * 16385, "가" * 5462, "\ud800"])
def test_invalid_values(value: str) -> None:
    with pytest.raises(EnvVarValueInvalidError):
        check_env_var_value(SecretValue(value))


@pytest.mark.parametrize("value", ["x", "a" * 16384, "가" * 5461, "여러\n줄\n값"])
def test_valid_values(value: str) -> None:
    check_env_var_value(SecretValue(value))


# --- 보관함 이름·라벨 ---


def test_secret_name_format_and_labels() -> None:
    service_id = uuid.UUID("aaaa1111-0000-4000-8000-000000000000")
    environment_id = uuid.UUID("bbbb2222-0000-4000-8000-000000000000")
    organization_id = uuid.UUID("cccc3333-0000-4000-8000-000000000000")

    name = app_secret_name("echo-app-", "team-echo", service_id, environment_id, "DATABASE_URL")

    assert name == "echo-app-team-echo-aaaa1111-bbbb2222-database-url"
    assert app_secret_labels(organization_id, service_id, environment_id) == {
        "echo-org": "cccc3333",
        "echo-service": "aaaa1111",
        "echo-environment": "bbbb2222",
        "managed-by": "echo-was",
    }


def test_secret_name_truncates_slug_to_255() -> None:
    service_id = uuid.uuid4()
    environment_id = uuid.uuid4()

    name = app_secret_name("echo-app-", "s" * 400, service_id, environment_id, "A" * 64)

    assert len(name) == 255
    assert name.endswith(f"-{str(environment_id)[:8]}-{'a' * 64}")


# --- 배포 작업 로그 문장 ---


def test_applied_message_lists_keys_only() -> None:
    assert env_vars_applied_message(["A", "B"], DEPLOY_LOG_MESSAGE_MAX_LENGTH) == (
        "App environment variables applied: 2 (keys: A, B)."
    )


def test_applied_message_fits_column_with_forty_long_keys() -> None:
    keys = [f"K{index:02d}{'X' * 61}" for index in range(40)]

    message = env_vars_applied_message(keys, DEPLOY_LOG_MESSAGE_MAX_LENGTH)

    assert len(message) <= DEPLOY_LOG_MESSAGE_MAX_LENGTH
    assert message.startswith("App environment variables applied: 40 (keys: K00")
    assert "more)." in message
