"""고객 앱 환경 변수 보관함(API 서버 10). 원본은 GCP Secret Manager 한 곳이다.

- GcpAppSecretStore: Secret Manager REST 를 httpx 로 부른다. 토큰은 관제탑 GSA 의 메타데이터 토큰.
- FakeAppSecretStore: 프로세스 메모리. 시험은 이름 목록으로 실패를 넣는다.

값은 SecretValue 로만 다룬다. 로그·예외에 값·토큰·비밀 이름을 넣지 않는다(예외는 메시지 없는 고정
종류다). 삭제 메서드는 두지 않는다: 옛 배포 기록·되돌리기가 그 버전을 계속 읽어야 해서 echo-was 는
보관함 비밀을 지우지 않는다.

프로세스마다 하나를 쓴다(API 는 lifespan 보관함, 일꾼은 시작 때). 만들 때 네트워크 호출이 없다.
"""

import base64
import binascii
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any, Protocol

import httpx

from app.core.clock import Clock
from app.core.config import AppSecretStoreMode, Settings
from app.core.secrets import SecretValue
from app.executors.google_metadata import GoogleMetadataClient, MetadataAuthError
from app.executors.retry import Sleep, TransientError, real_sleep, retry_transient

SECRET_MANAGER_BASE_URL = "https://secretmanager.googleapis.com/v1"
# 요청 하나의 제한 시간. API 는 잠근 채 부르므로 호출 전체를 10초로 다시 묶는다.
REQUEST_TIMEOUT_SECONDS = 5.0
SECRET_STORE_UNREACHABLE = "SECRET_STORE_UNREACHABLE"
FAKE_REF_PREFIX = "fake-sm:"
GCP_REF_PREFIX = "gcp-sm:"
VERSION_SEGMENT = "/versions/"


class AppSecretStoreError(Exception):
    """보관함 호출 실패. 메시지·속성에 값·비밀 이름을 담지 않는다."""


class AppSecretNotFoundError(AppSecretStoreError):
    """그 비밀·버전이 없다(404)."""


class AppSecretForbiddenError(AppSecretStoreError):
    """보관함이 거절했다(401·403, 메타데이터 서버가 토큰을 주지 않음)."""


class AppSecretRejectedError(AppSecretStoreError):
    """그 밖의 4xx(사용 중지·파기된 버전의 FAILED_PRECONDITION 등). 다시 불러도 같다."""


class AppSecretUnavailableError(AppSecretStoreError):
    """닿지 못했다(연결 실패·5xx·429·메타데이터 일시 오류). 재시도 뒤에만 올린다."""


class AppSecretStore(Protocol):
    @property
    def simulated(self) -> bool: ...

    async def add_version(self, name: str, value: SecretValue, labels: Mapping[str, str]) -> int:
        """없으면 비밀을 만들고 버전을 더한다. 새 버전 번호를 돌려준다."""
        ...

    async def access(self, name: str, version: int) -> SecretValue:
        """고정 버전을 읽는다."""
        ...

    def secret_ref(self, name: str, version: int) -> str:
        """배포 기록·응답에 적는 참조(버전 고정)."""
        ...

    async def aclose(self) -> None: ...


class FakeAppSecretStore:
    """프로세스 메모리 보관함. 없는 이름·버전은 AppSecretNotFoundError 다.

    일꾼과 API 가 다른 프로세스면 일꾼에는 값이 없다. EXECUTOR_MODE=fake 일 때만 부르는 쪽이 이
    실패를 자리 표시 값으로 대신한다.
    """

    def __init__(
        self,
        *,
        fail_names: Iterable[str] = (),
        forbidden_names: Iterable[str] = (),
        unavailable_names: Iterable[str] = (),
    ) -> None:
        self._versions: dict[str, list[SecretValue]] = {}
        self.fail_names = set(fail_names)
        self.forbidden_names = set(forbidden_names)
        self.unavailable_names = set(unavailable_names)
        self.access_calls: list[tuple[str, int]] = []

    @property
    def simulated(self) -> bool:
        return True

    @property
    def names(self) -> list[str]:
        return list(self._versions)

    async def add_version(self, name: str, value: SecretValue, labels: Mapping[str, str]) -> int:
        self._raise_injected(name)
        versions = self._versions.setdefault(name, [])
        versions.append(value)
        return len(versions)

    async def access(self, name: str, version: int) -> SecretValue:
        self.access_calls.append((name, version))
        self._raise_injected(name)
        versions = self._versions.get(name, [])
        if not 1 <= version <= len(versions):
            raise AppSecretNotFoundError
        return versions[version - 1]

    def secret_ref(self, name: str, version: int) -> str:
        return f"{FAKE_REF_PREFIX}{name}{VERSION_SEGMENT}{version}"

    async def aclose(self) -> None:
        return None

    def _raise_injected(self, name: str) -> None:
        if name in self.unavailable_names:
            raise AppSecretUnavailableError
        if name in self.forbidden_names:
            raise AppSecretForbiddenError
        if name in self.fail_names:
            raise AppSecretRejectedError


class GcpAppSecretStore:
    """GCP Secret Manager. 일시 오류는 1·2·4초 재시도 뒤 AppSecretUnavailableError."""

    def __init__(
        self,
        project_id: str,
        metadata: GoogleMetadataClient,
        *,
        base_url: str = SECRET_MANAGER_BASE_URL,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = real_sleep,
    ) -> None:
        self._project_id = project_id
        self._metadata = metadata
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._sleep = sleep
        self._http: httpx.AsyncClient | None = None

    @property
    def simulated(self) -> bool:
        return False

    def secret_ref(self, name: str, version: int) -> str:
        # 응답 name 에는 프로젝트 번호가 올 수 있어 늘 설정한 프로젝트 id 로 조립한다.
        return (
            f"{GCP_REF_PREFIX}projects/{self._project_id}/secrets/{name}{VERSION_SEGMENT}{version}"
        )

    async def add_version(self, name: str, value: SecretValue, labels: Mapping[str, str]) -> int:
        return await self._retrying(lambda: self._add_version_once(name, value, labels))

    async def access(self, name: str, version: int) -> SecretValue:
        return await self._retrying(lambda: self._access_once(name, version))

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        await self._metadata.aclose()

    async def _retrying[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        try:
            return await retry_transient(operation, self._sleep)
        except TransientError:
            raise AppSecretUnavailableError from None

    async def _add_version_once(
        self, name: str, value: SecretValue, labels: Mapping[str, str]
    ) -> int:
        created = await self._send(
            "POST",
            self._secrets_path(),
            params={"secretId": name},
            body={"replication": {"automatic": {}}, "labels": dict(labels)},
        )
        # 409 ALREADY_EXISTS 는 이미 만든 비밀이라 성공으로 본다.
        if created.status_code != 409:
            _check(created)
        data = base64.b64encode(value.get_secret_value().encode()).decode()
        added = await self._send(
            "POST",
            f"{self._secrets_path()}/{name}:addVersion",
            body={"payload": {"data": data}},
        )
        _check(added)
        return _version_number(_json(added))

    async def _access_once(self, name: str, version: int) -> SecretValue:
        response = await self._send(
            "GET", f"{self._secrets_path()}/{name}{VERSION_SEGMENT}{version}:access"
        )
        _check(response)
        payload = _json(response).get("payload")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, str):
            raise AppSecretRejectedError
        try:
            return SecretValue(base64.b64decode(data, validate=True).decode())
        except (binascii.Error, UnicodeDecodeError):
            raise AppSecretRejectedError from None

    def _secrets_path(self) -> str:
        return f"/projects/{self._project_id}/secrets"

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        response = await self._send_once(method, path, params, body, force=False)
        if response.status_code == 401:
            # 캐시한 토큰이 만료·취소됐을 수 있다. 새로 받아 한 번만 다시 부른다.
            response = await self._send_once(method, path, params, body, force=True)
        if response.status_code in (401, 403):
            raise AppSecretForbiddenError
        if response.status_code >= 500 or response.status_code == 429:
            raise TransientError(SECRET_STORE_UNREACHABLE)
        return response

    async def _send_once(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None,
        body: dict[str, Any] | None,
        *,
        force: bool,
    ) -> httpx.Response:
        try:
            token = await self._metadata.access_token(force=force)
        except MetadataAuthError:
            raise AppSecretForbiddenError from None
        headers = {"Authorization": f"Bearer {token.token.get_secret_value()}"}
        try:
            return await self._client().request(
                method, path, params=params, json=body, headers=headers
            )
        except httpx.TransportError:
            raise TransientError(SECRET_STORE_UNREACHABLE) from None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                transport=self._transport,
                timeout=self._timeout_seconds,
                follow_redirects=False,
            )
        return self._http


def build_app_secret_store(
    settings: Settings,
    clock: Clock,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    metadata_transport: httpx.AsyncBaseTransport | None = None,
    sleep: Sleep = real_sleep,
) -> AppSecretStore:
    """설정에 맞는 보관함. 각자 메타데이터 클라이언트를 갖고 aclose 때 닫는다."""
    if settings.app_secret_store_mode is AppSecretStoreMode.FAKE:
        return FakeAppSecretStore()
    assert settings.app_secret_project_id is not None
    return GcpAppSecretStore(
        settings.app_secret_project_id,
        GoogleMetadataClient(
            settings.google_metadata_base_url, clock, transport=metadata_transport
        ),
        transport=transport,
        sleep=sleep,
    )


def _check(response: httpx.Response) -> None:
    if response.is_success:
        return
    if response.status_code == 404:
        raise AppSecretNotFoundError
    raise AppSecretRejectedError


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise TransientError(SECRET_STORE_UNREACHABLE) from None
    if not isinstance(body, dict):
        raise TransientError(SECRET_STORE_UNREACHABLE)
    return body


def _version_number(body: dict[str, Any]) -> int:
    """응답 name 의 마지막 `/versions/<n>` 만 읽는다."""
    name = body.get("name")
    _, separator, number = (
        name.rpartition(VERSION_SEGMENT) if isinstance(name, str) else ("", "", "")
    )
    if not separator or not number.isascii() or not number.isdigit() or int(number) < 1:
        raise TransientError(SECRET_STORE_UNREACHABLE)
    return int(number)
