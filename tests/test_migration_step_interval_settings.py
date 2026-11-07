"""이전 비율 단계 간격 설정: 기다리는 시간만 줄이고 판정 조건(필요 건수·최대 대기)은 그대로."""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import DEFAULT_MIGRATION_REQUIRED_REQUESTS, Settings
from app.worker.runner import idle_poll_seconds

DATABASE_URL = "postgresql+asyncpg://x:y@127.0.0.1:1/z"
BASE_YAML = Path(__file__).resolve().parents[1] / "deploy" / "base.yaml"
INTERVAL_KEYS = (
    "MIGRATION_MIN_OBSERVE_SECONDS",
    "MIGRATION_COUNT_POLL_SECONDS",
    "WORKER_IDLE_POLL_SECONDS",
    "MIGRATION_DB_CUTOVER_POLL_SECONDS",
)


def _configmap_values() -> dict[str, str]:
    text = BASE_YAML.read_text(encoding="utf-8")
    values: dict[str, str] = {}
    for key in INTERVAL_KEYS:
        found = re.search(rf'^  {key}: "([^"]+)"$', text, flags=re.MULTILINE)
        assert found is not None, key
        values[key.lower()] = found.group(1)
    return values


def test_defaults_are_unchanged() -> None:
    settings = Settings(_env_file=None, database_url=DATABASE_URL)

    assert settings.migration_min_observe_seconds == 3
    assert settings.migration_count_poll_seconds == 1.0
    assert settings.worker_idle_poll_seconds is None
    assert idle_poll_seconds(settings) == settings.worker_poll_interval_seconds == 1.0
    assert settings.migration_db_cutover_poll_seconds == 3


def test_count_poll_accepts_sub_second() -> None:
    settings = Settings(_env_file=None, database_url=DATABASE_URL, migration_count_poll_seconds=0.5)

    assert settings.migration_count_poll_seconds == 0.5


@pytest.mark.parametrize("value", [0, 0.1, 11])
def test_count_poll_outside_range_is_rejected(value: float) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url=DATABASE_URL, migration_count_poll_seconds=value)


def test_idle_poll_overrides_only_the_idle_wait() -> None:
    settings = Settings(_env_file=None, database_url=DATABASE_URL, worker_idle_poll_seconds=0.2)

    assert idle_poll_seconds(settings) == 0.2
    # 배포·내리기의 실행기 재조회 주기는 그대로다.
    assert settings.worker_poll_interval_seconds == 1.0


@pytest.mark.parametrize("value", [0, -1, 11])
def test_idle_poll_outside_range_is_rejected(value: float) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url=DATABASE_URL, worker_idle_poll_seconds=value)


def test_production_configmap_shortens_waits_but_keeps_judgement() -> None:
    settings = Settings(_env_file=None, database_url=DATABASE_URL, **_configmap_values())

    assert settings.migration_min_observe_seconds == 1
    assert settings.migration_count_poll_seconds == 0.5
    assert idle_poll_seconds(settings) == 0.2
    assert settings.migration_db_cutover_poll_seconds == 1
    # 판정 조건은 기본값 그대로(ConfigMap 이 건드리지 않는다).
    assert tuple(settings.migration_required_requests_default) == (
        DEFAULT_MIGRATION_REQUIRED_REQUESTS
    )
    assert settings.migration_max_wait_seconds_default == 30
    assert settings.migration_counts_stale_seconds == 5
    text = BASE_YAML.read_text(encoding="utf-8")
    for untouched in ("MIGRATION_REQUIRED_REQUESTS_DEFAULT", "MIGRATION_MAX_WAIT_SECONDS_DEFAULT"):
        assert f"  {untouched}:" not in text
