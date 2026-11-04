"""배포 기록의 앱 환경 변수 참조(`config_snapshot.envVars`)를 실행기에 넘길 값으로 바꾼다.

표(docs/executors.md "앱 환경 변수"):

| 배포 기록 | 결과 | 실행기의 `{ns}-env` |
|---|---|---|
| envVars 키 없음(이 기능 이전 기록) | None | 읽지도 쓰지도 않음 |
| 참조가 fake-sm: 인데 보관함이 gcp | None | 읽지도 쓰지도 않음 |
| 가짜 보관함 + 실제 실행기 | None | 읽지도 쓰지도 않음 |
| envVars: {} | {} | echo-was 소유일 때만 빈 데이터로 교체 |
| envVars 1개 이상 | 값 사전 | 통째로 교체 |

값은 트랜잭션 밖에서 꺼내고 메모리로만 넘긴다. 로그·예외에는 키 이름만 넣는다.
"""

import asyncio
import re
from collections.abc import Mapping
from typing import Any

from app.clients.app_secret_store import (
    FAKE_REF_PREFIX,
    AppSecretForbiddenError,
    AppSecretNotFoundError,
    AppSecretRejectedError,
    AppSecretStore,
    AppSecretUnavailableError,
)
from app.core.config import AppSecretStoreMode, ExecutorMode, Settings
from app.core.secrets import SecretValue
from app.executors.base import ExecutorUnavailableError
from app.services.deploy_support import SNAPSHOT_ENV_VARS

SECRET_STORE_UNAVAILABLE = "SECRET_STORE_UNAVAILABLE"
# 가짜 보관함은 프로세스 메모리라 일꾼이 API 가 넣은 값을 볼 수 없다. 이 값으로 대신한다.
SIMULATED_ENV_VALUE = "simulated"
# 한 번에 보관함을 부르는 수. 연결당 키가 40개까지라 임대 안에 끝나게 나눠 부른다.
ACCESS_CONCURRENCY = 8

_VERSION = r"(?P<version>[1-9][0-9]*)"
_GCP_REF = re.compile(
    rf"^gcp-sm:projects/(?P<project>[^/]+)/secrets/(?P<name>[^/]+)/versions/{_VERSION}$"
)
_FAKE_REF = re.compile(rf"^fake-sm:(?P<name>[^/]+)/versions/{_VERSION}$")


class EnvSecretUnavailableError(Exception):
    """고정 버전을 영구히 꺼내지 못했다(404·403·FAILED_PRECONDITION). keys 는 키 이름만."""

    def __init__(self, keys: tuple[str, ...]) -> None:
        super().__init__(keys)
        self.keys = keys


def pinned_versions(snapshot: Mapping[str, Any]) -> dict[str, int]:
    """배포 기록에 고정된 키별 버전. 없거나 읽지 못하는 참조는 뺀다."""
    refs = snapshot.get(SNAPSHOT_ENV_VARS)
    if not isinstance(refs, dict):
        return {}
    versions: dict[str, int] = {}
    for key, ref in refs.items():
        match = (
            (_GCP_REF.fullmatch(ref) or _FAKE_REF.fullmatch(ref)) if isinstance(ref, str) else None
        )
        if match is not None:
            versions[key] = int(match["version"])
    return versions


class AppEnvResolver:
    def __init__(self, store: AppSecretStore, settings: Settings) -> None:
        self._store = store
        self._settings = settings

    async def resolve(self, snapshot: Mapping[str, Any]) -> dict[str, SecretValue] | None:
        """위 표의 결과. 영구 실패는 EnvSecretUnavailableError, 일시는 ExecutorUnavailableError."""
        refs = snapshot.get(SNAPSHOT_ENV_VARS)
        if not isinstance(refs, dict) or not self._settings.is_app_secret_store_configured:
            return None
        if self._settings.app_secret_store_mode is AppSecretStoreMode.GCP and any(
            isinstance(ref, str) and ref.startswith(FAKE_REF_PREFIX) for ref in refs.values()
        ):
            return None
        if not refs:
            return {}
        semaphore = asyncio.Semaphore(ACCESS_CONCURRENCY)
        outcomes = await asyncio.gather(
            *(self._read(semaphore, ref) for ref in refs.values()), return_exceptions=True
        )
        values: dict[str, SecretValue] = {}
        failed: list[str] = []
        for key, outcome in zip(refs, outcomes, strict=True):
            if isinstance(outcome, SecretValue):
                values[key] = outcome
            elif isinstance(outcome, AppSecretUnavailableError):
                raise ExecutorUnavailableError(SECRET_STORE_UNAVAILABLE) from None
            elif isinstance(outcome, _PERMANENT_FAILURES):
                failed.append(key)
            else:
                assert isinstance(outcome, BaseException)
                raise outcome
        if failed:
            raise EnvSecretUnavailableError(tuple(sorted(failed)))
        return values

    async def _read(self, semaphore: asyncio.Semaphore, ref: object) -> SecretValue:
        name, version = self._parse(ref)
        async with semaphore:
            try:
                return await self._store.access(name, version)
            except AppSecretNotFoundError:
                if self._settings.executor_mode is ExecutorMode.FAKE and self._store.simulated:
                    return SecretValue(SIMULATED_ENV_VALUE)
                raise

    def _parse(self, ref: object) -> tuple[str, int]:
        """(비밀 이름, 버전). 형식이 틀리거나 다른 프로젝트 참조면 읽을 수 없는 참조다."""
        match = _GCP_REF.fullmatch(ref) if isinstance(ref, str) else None
        if match is not None and (
            self._store.simulated or match["project"] == self._settings.app_secret_project_id
        ):
            return match["name"], int(match["version"])
        match = _FAKE_REF.fullmatch(ref) if isinstance(ref, str) else None
        if match is not None:
            return match["name"], int(match["version"])
        raise AppSecretRejectedError


_PERMANENT_FAILURES = (AppSecretNotFoundError, AppSecretForbiddenError, AppSecretRejectedError)
