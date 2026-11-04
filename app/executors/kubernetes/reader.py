"""환경 접속 확인 어댑터가 클러스터에서 버전·노드 수를 읽는 구현(API 서버 12).

- 접근 경로는 배포 실행기와 같다(`ClusterAccessProvider`): aws 는 구글 신분증 → STS 역할 → EKS 토큰,
  gcp 는 워크로드 신원 액세스 토큰 + GKE API, onprem 은 `apiUrl` + 서비스가 넘긴 서비스 계정 토큰.
- 읽기 전용 GET 세 가지뿐이다: `/version`, `/api`(인증이 있어야 열리는 API 목록, 출입증 증명),
  `/api/v1/nodes`.
- 실패는 `ConnectionCheckError(reason)` 로만 낸다. 원문(주소·헤더·본문)은 버린다.
  권한(401·403) AUTH_FAILED / 설정(역할 없음·404·쿠버네티스가 아닌 본문) CONFIG_INVALID /
  연결 실패·TLS·5xx·429(0.5초 뒤 한 번 더 시도한 뒤) UNREACHABLE. TIMEOUT 은 호출 쪽 전체
  제한 시간(서비스)이 판정한다.
- 401 이면 토큰을 다시 얻어 한 번 더 부른다(캐시한 토큰이 만료·취소됐을 수 있다).
"""

import logging
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.adapters.base import (
    KUBERNETES_VERSION_PATTERN,
    ConnectionCheckError,
    EnvironmentConfig,
)
from app.core.secrets import SecretValue
from app.enums import ConnectionErrorCode
from app.executors.kubernetes.access import (
    CLUSTER_UNREACHABLE,
    ClusterAccessProvider,
    ClusterAuthError,
    ClusterConfigError,
)
from app.executors.kubernetes.api import KubeHttpClients
from app.executors.retry import Sleep, TransientError, real_sleep

logger = logging.getLogger(__name__)

RETRY_DELAY_SECONDS = 0.5
# 실패한 시도 하나가 이보다 오래 걸렸으면 다시 시도하지 않는다(전체 5초 안에서 원인을 TIMEOUT 에
# 가리지 않으려고. 남은 시간 5 - 1.5 - 0.5 = 3초가 온프레 터널 새 연결 상한과 맞는다).
RETRY_MAX_ATTEMPT_SECONDS = 1.5
_VERSION = re.compile(KUBERNETES_VERSION_PATTERN)


class ClusterReader:
    """`ClusterReadPort` 구현. 프로세스당 하나를 만들어 재사용한다."""

    def __init__(
        self,
        provider: ClusterAccessProvider,
        clients: KubeHttpClients,
        *,
        sleep: Sleep = real_sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider = provider
        self._clients = clients
        self._sleep = sleep
        self._monotonic = monotonic

    async def read_version(self, config: EnvironmentConfig, token: SecretValue | None) -> str:
        body = await self._get_json(config, token, "/version")
        version = body.get("gitVersion")
        if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
            raise self._failed(config, ConnectionErrorCode.CONFIG_INVALID)
        return version

    async def verify_identity(self, config: EnvironmentConfig, token: SecretValue | None) -> None:
        # GET /api (system:discovery)는 로그인한 사용자에게만 열린다: 익명 403, 틀린 토큰 401.
        body = await self._get_json(config, token, "/api")
        if not isinstance(body.get("versions"), list):
            raise self._failed(config, ConnectionErrorCode.CONFIG_INVALID)

    async def count_nodes(self, config: EnvironmentConfig, token: SecretValue | None) -> int:
        body = await self._get_json(config, token, "/api/v1/nodes")
        items = body.get("items")
        if not isinstance(items, list):
            raise self._failed(config, ConnectionErrorCode.CONFIG_INVALID)
        return len(items)

    async def _get_json(
        self, config: EnvironmentConfig, token: SecretValue | None, path: str
    ) -> dict[str, Any]:
        try:
            response = await self._get_with_one_retry(config, token, path)
        except ClusterAuthError:
            raise self._failed(config, ConnectionErrorCode.AUTH_FAILED) from None
        except ClusterConfigError:
            raise self._failed(config, ConnectionErrorCode.CONFIG_INVALID) from None
        except TransientError:
            raise self._failed(config, ConnectionErrorCode.UNREACHABLE) from None
        if not response.is_success:
            # 404 등: 주소가 쿠버네티스 창구가 아니거나 경로가 없다.
            raise self._failed(config, ConnectionErrorCode.CONFIG_INVALID)
        try:
            body = response.json()
        except ValueError:
            raise self._failed(config, ConnectionErrorCode.CONFIG_INVALID) from None
        if not isinstance(body, dict):
            raise self._failed(config, ConnectionErrorCode.CONFIG_INVALID)
        return body

    async def _get_with_one_retry(
        self, config: EnvironmentConfig, token: SecretValue | None, path: str
    ) -> httpx.Response:
        started = self._monotonic()
        try:
            return await self._get(config, token, path)
        except TransientError:
            if self._monotonic() - started > RETRY_MAX_ATTEMPT_SECONDS:
                raise
        await self._sleep(RETRY_DELAY_SECONDS)
        return await self._get(config, token, path)

    async def _get(
        self, config: EnvironmentConfig, token: SecretValue | None, path: str
    ) -> httpx.Response:
        response = await self._send(config, token, path, refresh=False)
        if response.status_code == 401:
            response = await self._send(config, token, path, refresh=True)
        if response.status_code in (401, 403):
            raise ClusterAuthError
        if response.status_code >= 500 or response.status_code == 429:
            raise TransientError(CLUSTER_UNREACHABLE)
        return response

    async def _send(
        self, config: EnvironmentConfig, token: SecretValue | None, path: str, *, refresh: bool
    ) -> httpx.Response:
        access = await self._provider.access_for_config(config, onprem_token=token, refresh=refresh)
        headers = {
            "Authorization": f"Bearer {access.token.get_secret_value()}",
            "Accept": "application/json",
        }
        try:
            return await self._clients.get(access).get(path, headers=headers)
        except httpx.TransportError:
            raise TransientError(CLUSTER_UNREACHABLE) from None

    @staticmethod
    def _failed(config: EnvironmentConfig, reason: ConnectionErrorCode) -> ConnectionCheckError:
        # 주소·이름·본문은 남기지 않는다. 종류와 이유 코드만.
        logger.warning(
            "cluster read failed",
            extra={"action": "read_cluster", "kind": config.kind, "reason": reason.value},
        )
        return ConnectionCheckError(reason)
