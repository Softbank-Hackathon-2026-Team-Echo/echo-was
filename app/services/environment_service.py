"""환경 등록·조회·수정·삭제·다시 확인 유스케이스.

모든 유스케이스는 조직 하나 안에서만 본다(다른 조직의 환경은 없는 것과 같다). 이름은 조직 안에서
유일하고, env 비밀 참조는 조직 열쇠 이름(`ECHO_SECRET_<조직 키>__`)으로 시작해야 한다.

트랜잭션은 이 서비스가 `async with session.begin()` 블록 단위로 연다. 접속 확인은 DB 트랜잭션·행
잠금 없이 실행하고(네트워크 대기 중 잠금 방지), 결과는 새 트랜잭션에서 행을 다시 잠가 저장한다.
그 사이에 config·secret_ref 가 바뀌었으면 결과를 버린다(바꾼 쪽이 자기 확인을 따로 돈다).
"""

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.adapters.base import (
    ClusterInfo,
    EnvironmentConfig,
    InvalidEnvironmentConfigError,
    UnknownEnvironmentKindError,
)
from app.adapters.registry import REASON_INVALID_FORMAT, validate_environment_config
from app.audit.actions import AuditAction, AuditTargetType
from app.audit.recorder import AuditActor, AuditRecorder, AuditTarget
from app.core.clock import Clock
from app.core.exceptions import (
    EnvironmentInUseError,
    EnvironmentKindImmutableError,
    EnvironmentNameConflictError,
    EnvironmentNotFoundError,
    ErrorField,
    SecretRefNotInOrganizationError,
)
from app.core.secrets import (
    InvalidSecretRefError,
    is_secret_ref_in_organization,
    parse_secret_ref,
)
from app.enums import ConnectionErrorCode
from app.models import Environment, Organization
from app.repositories.environment_repository import EnvironmentRepository
from app.services.environment_connection_service import (
    ConnectionTarget,
    EnvironmentConnectionService,
)

logger = logging.getLogger(__name__)

NAME_UNIQUE_INDEX = "uq_environments_org_name_live"


@dataclass(frozen=True)
class EnvironmentView:
    """응답으로 바꿀 환경 한 건. config 는 저장 값을 지금 양식으로 읽은 것이고, 읽지 못하면 None."""

    environment: Environment
    config: EnvironmentConfig | None


@dataclass(frozen=True)
class EnvironmentDetailView(EnvironmentView):
    cluster_info: ClusterInfo | None
    cluster_info_error: ConnectionErrorCode | None


@dataclass(frozen=True)
class EnvironmentSearchResult:
    items: list[EnvironmentView]
    total: int


@dataclass(frozen=True)
class EnvironmentChanges:
    """수정 요청에서 들어온 칸. None 은 '들어오지 않음'이다(null 값은 요청 검사가 막는다)."""

    name: str | None = None
    kind: str | None = None
    config: Mapping[str, object] | None = None
    secret_ref: str | None = None


class EnvironmentService:
    def __init__(
        self,
        session: AsyncSession,
        environment_repository: EnvironmentRepository,
        connection_service: EnvironmentConnectionService,
        public_base_domain: str,
        clock: Clock,
    ) -> None:
        self._session = session
        self._environment_repository = environment_repository
        self._connection_service = connection_service
        self._public_base_domain = public_base_domain
        self._audit = AuditRecorder(session, clock)

    def _record_audit(
        self,
        action: AuditAction,
        environment: Environment,
        actor_user_id: uuid.UUID,
        details: dict[str, object] | None = None,
    ) -> None:
        self._audit.record(
            organization_id=environment.organization_id,
            actor=AuditActor.user(actor_user_id),
            action=action,
            target=AuditTarget(AuditTargetType.ENVIRONMENT, environment.id, environment.name),
            details=details,
        )

    def _check_ingress_domain(self, config: EnvironmentConfig) -> None:
        """환경별 하위 도메인은 대표 주소 도메인 바로 아래 한 단계여야 한다.

        와일드카드 인증서·DNS(`*.{하위 도메인}`)가 환경별 주소 한 단계만 덮기 때문이다. 양식은
        도메인 모양만 보고(설정에 닿지 않는다) 여기서 설정값과 비교한다.
        """
        domain = config.environment_host_domain()
        if domain is None:
            return
        label, _, parent = domain.partition(".")
        if parent != self._public_base_domain or not label:
            raise InvalidEnvironmentConfigError(
                config.kind,
                ["ingress_domain"],
                [ErrorField(field="config.ingressDomain", reason=REASON_INVALID_FORMAT)],
            )

    async def create_environment(
        self,
        organization: Organization,
        actor_user_id: uuid.UUID,
        name: str,
        kind: str,
        config: Mapping[str, object],
        secret_ref: str,
    ) -> EnvironmentDetailView:
        """환경을 UNCHECKED 로 저장·커밋한 뒤 접속을 확인하고 결과를 저장한다.

        config 는 API 표기(camelCase)다. 접속 확인이 실패해도 등록은 된다. 감사 줄은 등록 한 줄이고
        뒤따르는 접속 확인은 따로 쓰지 않는다.
        """
        parsed_config = validate_environment_config(kind, config, by_alias=True)
        self._check_ingress_domain(parsed_config)
        _check_secret_ref_in_organization(secret_ref, organization)
        environment = Environment(
            organization_id=organization.id,
            name=name,
            kind=kind,
            config=parsed_config.model_dump(mode="json", exclude_none=True),
            secret_ref=secret_ref,
        )
        async with self._session.begin():
            await self._check_name_available(organization.id, name)
            self._environment_repository.add(environment)
            await self._flush()
            self._record_audit(
                AuditAction.ENVIRONMENT_CREATED, environment, actor_user_id, {"kind": kind}
            )
        self._log("environment created", "create_environment", environment)
        return await self._check_and_record(environment)

    async def search_environments(
        self, organization_id: uuid.UUID, kind: str | None, page: int, size: int
    ) -> EnvironmentSearchResult:
        """저장된 값만 읽는다. 어댑터·비밀값 조회기는 부르지 않는다."""
        async with self._session.begin():
            environments = await self._environment_repository.search_live(
                organization_id, kind, offset=page * size, limit=size
            )
            total = await self._environment_repository.count_live(organization_id, kind)
        return EnvironmentSearchResult(
            items=[self._view(environment) for environment in environments], total=total
        )

    async def get_environment(
        self, organization_id: uuid.UUID, environment_id: uuid.UUID
    ) -> EnvironmentDetailView:
        """저장된 값 + 그 자리에서 읽은 클러스터 정보. 저장된 접속 상태는 바꾸지 않는다."""
        async with self._session.begin():
            environment = await self._environment_repository.get_live_by_id(
                organization_id, environment_id
            )
        return await self._detail_with_cluster_info(environment)

    async def update_environment(
        self,
        organization: Organization,
        environment_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        changes: EnvironmentChanges,
    ) -> EnvironmentDetailView:
        """name·config(전체 교체)·secret_ref 를 바꾼다. kind 는 바꿀 수 없다.

        config·secret_ref 가 실제로 바뀌면 같은 커밋에서 접속 상태를 UNCHECKED 로 되돌리고,
        커밋 뒤 다시 확인한다. name 만 바뀌면 다시 확인하지 않는다. 바뀐 칸이 없으면 감사 줄을 쓰지
        않고, 줄에는 바뀐 칸 이름만 적는다(값은 적지 않는다).
        """
        if changes.secret_ref is not None:
            _check_secret_ref_in_organization(changes.secret_ref, organization)
        async with self._session.begin():
            environment = await self._environment_repository.get_live_by_id(
                organization.id, environment_id, for_update=True
            )
            if changes.kind is not None and changes.kind != environment.kind:
                raise EnvironmentKindImmutableError(
                    "environment kind cannot be changed", environment_id=str(environment_id)
                )
            needs_check = False
            changed_fields: list[str] = []
            if changes.config is not None:
                parsed_config = validate_environment_config(
                    environment.kind, changes.config, by_alias=True
                )
                self._check_ingress_domain(parsed_config)
                new_config = parsed_config.model_dump(mode="json", exclude_none=True)
                if new_config != environment.config:
                    environment.config = new_config
                    needs_check = True
                    changed_fields.append("config")
            if changes.secret_ref is not None and changes.secret_ref != environment.secret_ref:
                environment.secret_ref = changes.secret_ref
                needs_check = True
                changed_fields.append("secretRef")
            if changes.name is not None and changes.name != environment.name:
                await self._check_name_available(organization.id, changes.name)
                environment.name = changes.name
                changed_fields.append("name")
            if needs_check:
                environment.mark_connection_unchecked()
                # 확인 중인 다른 요청이 결과를 버리도록 반드시 UPDATE 를 낸다.
                flag_modified(environment, "config")
            await self._flush()
            if changed_fields:
                self._record_audit(
                    AuditAction.ENVIRONMENT_UPDATED,
                    environment,
                    actor_user_id,
                    {"changedFields": changed_fields},
                )
        self._log("environment updated", "update_environment", environment)
        if needs_check:
            return await self._check_and_record(environment)
        return await self._detail_with_cluster_info(environment)

    async def delete_environment(
        self, organization_id: uuid.UUID, environment_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> None:
        """살아 있는 서비스-환경 연결이 없으면 소프트 삭제한다. 있으면 EnvironmentInUseError.

        연결을 만드는 쪽(배포 요청 접수)도 같은 환경 행을 FOR SHARE 로 잠근 뒤 연결을 만든다
        (이 FOR UPDATE 와 부딪혀 둘 중 하나가 기다린다).
        """
        async with self._session.begin():
            environment = await self._environment_repository.get_live_by_id(
                organization_id, environment_id, for_update=True
            )
            if await self._environment_repository.has_live_service_environments(environment_id):
                raise EnvironmentInUseError(
                    "environment has live service environments",
                    environment_id=str(environment_id),
                )
            environment.mark_as_deleted(datetime.now(UTC))
            self._record_audit(AuditAction.ENVIRONMENT_DELETED, environment, actor_user_id)
        self._log("environment deleted", "delete_environment", environment)

    async def check_environment_connection(
        self, organization_id: uuid.UUID, environment_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> EnvironmentDetailView:
        """커밋된 행으로 접속을 다시 확인하고 결과를 저장한다."""
        async with self._session.begin():
            environment = await self._environment_repository.get_live_by_id(
                organization_id, environment_id
            )
        return await self._check_and_record(environment, audit_actor_user_id=actor_user_id)

    async def _check_and_record(
        self, environment: Environment, *, audit_actor_user_id: uuid.UUID | None = None
    ) -> EnvironmentDetailView:
        """audit_actor_user_id 는 사람이 누른 '다시 확인'일 때만 준다.

        결과가 낡아 버려져도 확인 행동은 일어났으므로 discarded 와 함께 기록한다.
        """
        target = _connection_target(environment)
        result = await self._connection_service.check_connection(target)

        async with self._session.begin():
            stored = await self._environment_repository.find_live_by_id(
                environment.organization_id, target.environment_id, for_update=True
            )
            if stored is None:
                raise EnvironmentNotFoundError(
                    "environment deleted during connection check",
                    environment_id=str(target.environment_id),
                )
            is_current = stored.config == target.config and stored.secret_ref == target.secret_ref
            if is_current:
                stored.record_connection_check(datetime.now(UTC), result.connection_error)
            if audit_actor_user_id is not None:
                details: dict[str, object] = {"connectionStatus": str(stored.connection_status)}
                if not is_current:
                    details["discarded"] = True
                self._record_audit(
                    AuditAction.ENVIRONMENT_CONNECTION_CHECKED,
                    stored,
                    audit_actor_user_id,
                    details,
                )

        if not is_current:
            self._log("connection check result discarded", "check_environment_connection", stored)
            return EnvironmentDetailView(
                environment=stored,
                config=self._read_config(stored),
                cluster_info=None,
                cluster_info_error=None,
            )
        self._log("environment connection checked", "check_environment_connection", stored)
        return EnvironmentDetailView(
            environment=stored,
            config=self._read_config(stored),
            cluster_info=result.cluster_info,
            cluster_info_error=result.cluster_info_error,
        )

    async def _detail_with_cluster_info(self, environment: Environment) -> EnvironmentDetailView:
        result = await self._connection_service.read_cluster_info(_connection_target(environment))
        return EnvironmentDetailView(
            environment=environment,
            config=self._read_config(environment),
            cluster_info=result.cluster_info,
            cluster_info_error=result.error,
        )

    async def _check_name_available(self, organization_id: uuid.UUID, name: str) -> None:
        if await self._environment_repository.find_live_by_name(organization_id, name) is not None:
            raise EnvironmentNameConflictError("environment name already in use")

    async def _flush(self) -> None:
        """사전 조회를 지나친 동시 등록·수정의 이름 중복도 409 로 바꾼다.

        다른 무결성 오류는 그대로 올린다.
        """
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if _violated_constraint_name(exc) == NAME_UNIQUE_INDEX:
                raise EnvironmentNameConflictError("environment name already in use") from None
            raise

    def _view(self, environment: Environment) -> EnvironmentView:
        return EnvironmentView(environment=environment, config=self._read_config(environment))

    @staticmethod
    def _read_config(environment: Environment) -> EnvironmentConfig | None:
        """저장 값을 지금 양식으로 읽는다.

        읽지 못하는 행은 목록 전체를 깨지 않고 None 을 돌려주며 경고 로그를 남긴다.
        """
        if environment.secret_ref is not None:
            try:
                parse_secret_ref(environment.secret_ref)
            except InvalidSecretRefError:
                logger.warning(
                    "stored environment secret_ref is invalid",
                    extra={"action": "read_environment", "environment_id": str(environment.id)},
                )
        try:
            return environment.parsed_config()
        except (InvalidEnvironmentConfigError, UnknownEnvironmentKindError):
            logger.warning(
                "stored environment config is unreadable",
                extra={
                    "action": "read_environment",
                    "environment_id": str(environment.id),
                    "kind": environment.kind,
                },
            )
            return None

    @staticmethod
    def _log(message: str, action: str, environment: Environment) -> None:
        logger.info(
            message,
            extra={
                "action": action,
                "environment_id": str(environment.id),
                "kind": environment.kind,
                "connection_status": environment.connection_status,
                "connection_error": environment.connection_error,
            },
        )


def _check_secret_ref_in_organization(secret_ref: str, organization: Organization) -> None:
    """형식은 요청 스키마가 이미 검사했다. 여기서는 조직 열쇠 이름으로 묶였는지만 본다."""
    try:
        ref = parse_secret_ref(secret_ref)
    except InvalidSecretRefError:
        return
    if not is_secret_ref_in_organization(ref, organization.slug):
        raise SecretRefNotInOrganizationError


def _connection_target(environment: Environment) -> ConnectionTarget:
    """확인 기준값. 커밋된 행의 config 사본과 secret_ref 를 기억한다."""
    return ConnectionTarget(
        environment_id=environment.id,
        kind=environment.kind,
        config=dict(environment.config),
        secret_ref=environment.secret_ref,
    )


def _violated_constraint_name(exc: IntegrityError) -> str | None:
    # asyncpg 예외는 DBAPI 어댑터 예외의 원인(__cause__)으로 붙어 있고 constraint_name 을 가진다.
    driver_error = getattr(exc.orig, "__cause__", None)
    name = getattr(driver_error, "constraint_name", None)
    return name if isinstance(name, str) else None
