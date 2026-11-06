"""고객 앱 환경 변수 API 유스케이스(API 서버 10). 값은 보관함에 넣기만 하고 다시 읽지 않는다.

- 넣기·바꾸기: 서비스 → 연결 행을 잠근 채 보관함에 버전을 더한다. 같은 키의 동시 PUT 순서를 버전
  순서와 맞추려고 잠근 채 외부를 부르는 예외다. 그래서 호출 전체를 10초로 묶는다
  (docs/executors.md "앱 환경 변수"). 호출 뒤 커밋이 실패해도 안 쓰는 버전이 하나 남을 뿐이다.
- 지우기: 행만 삭제 표시한다. 옛 배포 기록·되돌리기가 그 버전을 읽어야 해서 보관함 비밀은 남긴다.
- 앱은 다시 배포해야 바뀐다. 응답 pendingRedeploy 로 보인다.
- 첫 배포 전에도 넣을 수 있다(API 서버 10-4). 연결이 없고 환경이 서비스와 같은 조직에 살아
  있으면 넣기가 배포 없는 연결(비율 0, 현재 배포 없음 = 역할 NONE)을 만들고, 첫 배포 요청은 그
  연결을 그대로 쓴다. 목록·지우기는 연결을 만들지 않는다(목록은 빈 목록, 지우기는
  ENV_VAR_NOT_FOUND). 응답 deployed 는 연결에 현재 배포가 있는지다.

응답·로그·예외에 값·값 일부·길이를 넣지 않는다. 비밀 이름은 정상 응답의 secretRef 에만 나온다.

감사 줄은 DB 행을 바꾸는 블록의 성공 분기에서 쓴다. 누가 어떤 키를 바꿨는지만 적고 값·비밀 참조·
버전은 적지 않는다. 값을 다시 읽지 않으므로 같은 값의 재저장도 새 버전이 생긴 변경이라 한 줄을 쓴다.
"""

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.actions import AuditAction, AuditTargetType
from app.audit.recorder import AuditActor, AuditRecorder, AuditTarget
from app.clients.app_secret_store import (
    AppSecretForbiddenError,
    AppSecretStore,
    AppSecretStoreError,
)
from app.core.clock import Clock
from app.core.config import Settings
from app.core.exceptions import (
    EnvVarKeyInvalidError,
    EnvVarKeyReservedError,
    EnvVarLimitExceededError,
    EnvVarNotFoundError,
    EnvVarValueInvalidError,
    SecretStoreForbiddenError,
    SecretStoreNotConfiguredError,
    SecretStoreUnavailableError,
    ServiceDeletingError,
    ServiceEnvironmentNotFoundError,
)
from app.core.secrets import SecretValue
from app.enums import ServiceStatus
from app.models import (
    Environment,
    Organization,
    Service,
    ServiceEnvironment,
    ServiceEnvironmentVariable,
)
from app.models.service_environment_variable import (
    ENV_VAR_KEY_PATTERN,
    ENV_VAR_SECRET_NAME_MAX_LENGTH,
)
from app.repositories.deployment_repository import DeploymentRepository
from app.repositories.environment_repository import EnvironmentRepository
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.repositories.service_environment_variable_repository import (
    ServiceEnvironmentVariableRepository,
)
from app.repositories.service_repository import ServiceRepository
from app.services.app_env_values import pinned_versions
from app.services.deploy_support import create_service_connection
from app.services.migration_guard import check_no_migration_in_progress

logger = logging.getLogger(__name__)

ENV_VAR_VALUE_MAX_BYTES = 16_384
ENV_VARS_PER_CONNECTION_MAX = 40
RESERVED_ENV_VAR_KEYS = frozenset({"PORT"})
RESERVED_ENV_VAR_PREFIX = "ECHO_"
# 서비스·연결 행을 잠근 채 보관함을 부르는 시간의 상한(재시도 포함).
STORE_CALL_TIMEOUT_SECONDS = 10.0
_ID_PREFIX_LENGTH = 8
_KEY = re.compile(ENV_VAR_KEY_PATTERN)


def check_env_var_key(key: str) -> None:
    if not _KEY.fullmatch(key):
        raise EnvVarKeyInvalidError
    if key in RESERVED_ENV_VAR_KEYS or key.startswith(RESERVED_ENV_VAR_PREFIX):
        raise EnvVarKeyReservedError


def check_env_var_value(value: SecretValue) -> None:
    try:
        size = len(value.get_secret_value().encode())
    except UnicodeEncodeError:
        raise EnvVarValueInvalidError from None
    if not 1 <= size <= ENV_VAR_VALUE_MAX_BYTES:
        raise EnvVarValueInvalidError


def app_secret_name(
    prefix: str, slug: str, service_id: uuid.UUID, environment_id: uuid.UUID, key: str
) -> str:
    """`{접두어}{slug}-{서비스 8자}-{환경 8자}-{키 소문자 _→-}`. 255자가 넘으면 slug 를 줄인다."""
    service_part = str(service_id)[:_ID_PREFIX_LENGTH]
    environment_part = str(environment_id)[:_ID_PREFIX_LENGTH]
    rest = f"-{service_part}-{environment_part}-{key.lower().replace('_', '-')}"
    room = ENV_VAR_SECRET_NAME_MAX_LENGTH - len(prefix) - len(rest)
    return f"{prefix}{slug[:room]}{rest}"


def app_secret_labels(
    organization_id: uuid.UUID, service_id: uuid.UUID, environment_id: uuid.UUID
) -> dict[str, str]:
    """정리 일이 라벨로 찾게 남긴다(값은 id 앞 8자)."""
    return {
        "echo-org": str(organization_id)[:_ID_PREFIX_LENGTH],
        "echo-service": str(service_id)[:_ID_PREFIX_LENGTH],
        "echo-environment": str(environment_id)[:_ID_PREFIX_LENGTH],
        "managed-by": "echo-was",
    }


@dataclass(frozen=True)
class ServiceVariableView:
    key: str
    version: int
    secret_ref: str
    updated_at: datetime
    updated_by_id: uuid.UUID
    updated_by_github_login: str
    applied_version: int | None

    @property
    def pending_redeploy(self) -> bool:
        return self.applied_version != self.version


@dataclass(frozen=True)
class ServiceVariableList:
    items: list[ServiceVariableView]
    simulated: bool
    # 연결에 현재 배포가 있는가. 첫 배포 전(연결 없음·배포 없는 연결)이면 False.
    deployed: bool


@dataclass(frozen=True)
class SavedServiceVariable:
    variable: ServiceVariableView
    created: bool
    simulated: bool
    deployed: bool


class ServiceVariableService:
    def __init__(
        self, session: AsyncSession, store: AppSecretStore, clock: Clock, settings: Settings
    ) -> None:
        self._session = session
        self._store = store
        self._clock = clock
        self._settings = settings
        self._services = ServiceRepository(session)
        self._environments = EnvironmentRepository(session)
        self._connections = ServiceEnvironmentRepository(session)
        self._variables = ServiceEnvironmentVariableRepository(session)
        self._deployments = DeploymentRepository(session)
        self._audit = AuditRecorder(session, clock)

    async def search_variables(
        self, organization_id: uuid.UUID, service_id: uuid.UUID, environment_id: uuid.UUID
    ) -> ServiceVariableList:
        async with self._session.begin():
            service = await self._services.get_live_by_id(organization_id, service_id)
            connection = await self._connections.find_live_by_service_and_environment(
                service.id, environment_id
            )
            if connection is None:
                # 첫 배포 전: 같은 조직의 살아 있는 환경이면 빈 목록(읽기는 연결을 만들지 않는다).
                if (
                    await self._environments.find_live_by_id(organization_id, environment_id)
                    is None
                ):
                    raise ServiceEnvironmentNotFoundError
                return ServiceVariableList(
                    items=[], simulated=self._store.simulated, deployed=False
                )
            items = await self._views(connection)
            deployed = connection.current_deployment_id is not None
        return ServiceVariableList(items=items, simulated=self._store.simulated, deployed=deployed)

    async def put_variable(
        self,
        organization: Organization,
        service_id: uuid.UUID,
        environment_id: uuid.UUID,
        key: str,
        value: SecretValue,
        user_id: uuid.UUID,
    ) -> SavedServiceVariable:
        """새 키면 created. 연결이 없으면 배포 없는 연결을 만든다(API 서버 10-4).

        다른 조직·없는·지운 환경, 삭제 중 서비스, 한도, 보관함 오류는 예외다.
        """
        check_env_var_key(key)
        check_env_var_value(value)
        self._check_store_configured()
        now = self._clock.now()
        async with self._session.begin():
            service, connection, connection_created = await self._lock_connection(
                organization.id, service_id, environment_id, create=True
            )
            existing = await self._variables.find_live(connection.id, key)
            created = existing is None
            if existing is None:
                if await self._variables.count_live(connection.id) >= ENV_VARS_PER_CONNECTION_MAX:
                    raise EnvVarLimitExceededError
                secret_name = await self._variables.find_previous_secret_name(
                    connection.id, key
                ) or app_secret_name(
                    self._settings.app_secret_name_prefix,
                    organization.slug,
                    service.id,
                    environment_id,
                    key,
                )
            else:
                secret_name = existing.secret_name
            version = await self._add_version(
                secret_name,
                value,
                app_secret_labels(organization.id, service.id, environment_id),
                connection.id,
            )
            if existing is None:
                self._variables.add(
                    ServiceEnvironmentVariable(
                        service_environment_id=connection.id,
                        key=key,
                        secret_name=secret_name,
                        latest_version=version,
                        updated_by_user_id=user_id,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                existing.latest_version = version
                existing.updated_by_user_id = user_id
                existing.updated_at = now
            await self._session.flush()
            await self._record_audit(
                AuditAction.SERVICE_VARIABLE_SET, service, environment_id, key, user_id
            )
            view = next(item for item in await self._views(connection) if item.key == key)
        logger.info(
            "service variable saved",
            extra={
                "action": "put_service_variable",
                "service_environment_id": str(connection.id),
                "env_key": key,
                "version": version,
                "connection_created": connection_created,
            },
        )
        return SavedServiceVariable(
            variable=view,
            created=created,
            simulated=self._store.simulated,
            deployed=connection.current_deployment_id is not None,
        )

    async def delete_variable(
        self,
        organization_id: uuid.UUID,
        service_id: uuid.UUID,
        environment_id: uuid.UUID,
        key: str,
        user_id: uuid.UUID,
    ) -> None:
        self._check_store_configured()
        now = self._clock.now()
        async with self._session.begin():
            service, connection, _ = await self._lock_connection(
                organization_id, service_id, environment_id, create=False
            )
            variable = await self._variables.find_live(connection.id, key)
            if variable is None:
                raise EnvVarNotFoundError
            variable.mark_as_deleted(now)
            variable.updated_by_user_id = user_id
            variable.updated_at = now
            await self._record_audit(
                AuditAction.SERVICE_VARIABLE_DELETED, service, environment_id, key, user_id
            )
        logger.info(
            "service variable deleted",
            extra={
                "action": "delete_service_variable",
                "service_environment_id": str(connection.id),
                "env_key": key,
            },
        )

    async def _record_audit(
        self,
        action: AuditAction,
        service: Service,
        environment_id: uuid.UUID,
        key: str,
        user_id: uuid.UUID,
    ) -> None:
        environment = await self._session.get_one(Environment, environment_id)
        self._audit.record(
            organization_id=service.organization_id,
            actor=AuditActor.user(user_id),
            action=action,
            target=AuditTarget(
                AuditTargetType.SERVICE,
                service.id,
                f"{service.name} · {environment.name} · {key}",
            ),
            details={"environmentId": str(environment_id), "key": key},
        )

    def _check_store_configured(self) -> None:
        if not self._settings.is_app_secret_store_configured:
            raise SecretStoreNotConfiguredError

    async def _lock_connection(
        self,
        organization_id: uuid.UUID,
        service_id: uuid.UUID,
        environment_id: uuid.UUID,
        *,
        create: bool,
    ) -> tuple[Service, ServiceEnvironment, bool]:
        """서비스 → 연결 순서로 잠근다. 세 번째 값은 연결을 새로 만들었는가.

        연결이 이미 있으면 예전과 같다(환경 행을 잠그지 않는다). 미리 읽은 연결이 없을 때만 환경을
        FOR SHARE 로 먼저 잡는다(배포 접수와 같은 환경 → 서비스 → 연결 순서). 환경 삭제(FOR UPDATE,
        살아 있는 연결이 있으면 거절)와 줄을 세워, 지워지는 환경에 연결을 만들지 않게 한다.
        잠근 뒤에도 연결이 없을 때: 환경이 같은 조직에 살아 있지 않으면 404
        SERVICE_ENVIRONMENT_NOT_FOUND, create 면 배포 없는 연결을 만들고, 아니면(지우기) 지울 키가
        없으므로 404 ENV_VAR_NOT_FOUND. 서비스 없음(404)·삭제 중(409)이 환경 판정보다 먼저다.
        """
        seen = await self._connections.find_live_by_service_and_environment(
            service_id, environment_id
        )
        environment = (
            await self._environments.find_live_by_id(
                organization_id, environment_id, for_share=True
            )
            if seen is None
            else None
        )
        service = await self._services.get_live_by_id(organization_id, service_id, lock=True)
        if service.status == ServiceStatus.DELETING:
            raise ServiceDeletingError("service is being deleted", service_id=str(service.id))
        connection = await self._connections.lock_live_by_service_and_environment(
            service.id, environment_id
        )
        if connection is not None:
            return service, connection, False
        if environment is None:
            raise ServiceEnvironmentNotFoundError
        if not create:
            raise EnvVarNotFoundError
        # 새 연결은 첫 배포 접수와 같은 가드를 받는다(실행 중 이전이 있으면 409). 이전 지문은 배포
        # 없는 연결을 보지 않지만, 이전이 도는 동안 서비스의 연결 집합을 바꾸지 않는다.
        await check_no_migration_in_progress(self._session, service.id)
        connection = await create_service_connection(
            self._session, service, environment, self._settings.public_base_domain
        )
        return service, connection, True

    async def _add_version(
        self,
        secret_name: str,
        value: SecretValue,
        labels: dict[str, str],
        service_environment_id: uuid.UUID,
    ) -> int:
        try:
            async with asyncio.timeout(STORE_CALL_TIMEOUT_SECONDS):
                return await self._store.add_version(secret_name, value, labels)
        except AppSecretForbiddenError:
            raise SecretStoreForbiddenError from None
        except (AppSecretStoreError, TimeoutError) as exc:
            logger.warning(
                "app secret store call failed",
                extra={
                    "action": "put_service_variable",
                    "service_environment_id": str(service_environment_id),
                    "exc_type": type(exc).__name__,
                },
            )
            raise SecretStoreUnavailableError from None

    async def _views(self, connection: ServiceEnvironment) -> list[ServiceVariableView]:
        rows = await self._variables.search_live_by_connection(connection.id)
        current = (
            await self._deployments.find_by_id(connection.current_deployment_id)
            if connection.current_deployment_id
            else None
        )
        applied = pinned_versions(current.config_snapshot) if current is not None else {}
        return [
            ServiceVariableView(
                key=row.key,
                version=row.latest_version,
                secret_ref=self._store.secret_ref(row.secret_name, row.latest_version),
                updated_at=row.updated_at,
                updated_by_id=row.updated_by.id,
                updated_by_github_login=row.updated_by.github_login,
                applied_version=applied.get(row.key),
            )
            for row in rows
        ]
