"""기한부 예외 요청·승인·철회·조회 유스케이스(보안·거버넌스 3).

기한부 예외 = 통제 하나를 범위 안에서 최대 7일 막지 않게 하는 기록. 판정에서는 PASS 가 아니라
WARN 으로 보인다(규칙표). 규칙
- 요청·승인·철회는 개발자 이상 사람만 한다(검사기가 막는다, 에이전트 불가).
- 범위 칸은 그 통제가 정한 것만, 모두 채운다(규칙표 `scope_keys`). 정한 칸이 없는 통제는
  조직 전체다. 서비스·환경은 이 조직의 살아 있는 것이어야 한다.
- **만든 시각은 앱 시계로 직접 넣는다.** 만료는 만든 시각 뒤 7일 이하(정확히 7일 포함)이고
  DB CHECK 도 같은 만든 시각을 본다(DB `now()` 와 앱 시계 차이로 경계에서 실패하지 않게).
- 승인: 승인 대기(승인 전·철회 전·만료 전)만. 요청한 사람의 승인은 조직 `allow_self_approval` 이
  켜져 있을 때만(꺼져 있으면 403 `SELF_APPROVAL_FORBIDDEN`, 거부 기록).
- 철회: 철회 전·만료 전이면 승인 전이어도 된다. 철회한 예외는 판정에 쓰지 않는다. 지우지 않는다.
- 승인·철회는 조건부 UPDATE 로만 바꾸고, 0행이면 다시 읽어 409 를 올린다.
- 상태(`SecurityExceptionStatus`)는 저장하지 않고 조회 때 칸과 지금 시각으로 정한다.
- 요청·승인·철회는 같은 트랜잭션에서 감사 줄(USER, 대상 `security_exception`, 이름표 = 통제 ID)을
  남긴다. 요청 줄 details 는 만료 시각만(사유 원문은 예외 행에 있다).
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.actions import AuditAction, AuditTargetType
from app.audit.recorder import AuditActor, AuditRecorder, AuditTarget
from app.core.clock import Clock
from app.core.exceptions import (
    ErrorField,
    OrganizationNotFoundError,
    RequestFieldsInvalidError,
    SecurityExceptionClosedError,
    SecurityExceptionNotFoundError,
    SecurityExceptionNotPendingError,
    SelfApprovalForbiddenError,
)
from app.enums import ActorType, SecurityControlId, SecurityExceptionStatus
from app.models import SecurityException, User
from app.repositories.environment_repository import EnvironmentRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.security_exception_repository import (
    SecurityExceptionConditions,
    SecurityExceptionRepository,
    exception_status,
)
from app.repositories.service_repository import ServiceRepository
from app.repositories.user_repository import UserRepository
from app.services.security_rules import (
    EXCEPTION_MAX_DURATION,
    ScopeKey,
    SecurityScope,
    get_rule,
)
from app.services.security_rules import SecurityException as SecurityExceptionInput

logger = logging.getLogger(__name__)

# 422 details 의 칸 이름(요청 JSON camelCase)과 사유 코드.
SCOPE_FIELD_BY_KEY = {
    ScopeKey.SERVICE: "serviceId",
    ScopeKey.ENVIRONMENT: "environmentId",
    ScopeKey.IMAGE_DIGEST: "imageDigest",
    ScopeKey.CONFIG_COMMIT: "configCommit",
    ScopeKey.WAS_VERSION: "wasVersion",
}
REQUIRED_FOR_CONTROL_REASON = "REQUIRED_FOR_CONTROL"
NOT_ALLOWED_FOR_CONTROL_REASON = "NOT_ALLOWED_FOR_CONTROL"
EXPIRES_NOT_AFTER_NOW_REASON = "NOT_AFTER_NOW"
EXPIRES_TOO_LATE_REASON = "MAX_7_DAYS"
EXPIRES_AT_FIELD = "expiresAt"


def to_security_exception(model: SecurityException) -> SecurityExceptionInput:
    """DB 행 → 판정 입력(게이트·매트릭스 공용). 요청·승인은 users FK 라 사람(USER)이다."""
    return SecurityExceptionInput(
        exception_id=model.id,
        control_id=SecurityControlId(model.control_id),
        scope=SecurityScope(
            organization_id=model.organization_id,
            service_id=model.service_id,
            environment_id=model.environment_id,
            image_digest=model.image_digest,
            config_commit=model.config_commit,
            was_version=model.was_version,
        ),
        reason=model.reason,
        requested_by_user_id=model.requested_by_user_id,
        requested_by_actor_type=ActorType.USER,
        approved_by_user_id=model.approved_by_user_id,
        approved_by_actor_type=ActorType.USER if model.approved_by_user_id else None,
        created_at=model.created_at,
        expires_at=model.expires_at,
    )


@dataclass(frozen=True)
class CreateSecurityExceptionCommand:
    control_id: SecurityControlId
    reason: str
    expires_at: datetime
    service_id: uuid.UUID | None = None
    environment_id: uuid.UUID | None = None
    image_digest: str | None = None
    config_commit: str | None = None
    was_version: str | None = None

    def value_of(self, key: ScopeKey) -> uuid.UUID | str | None:
        match key:
            case ScopeKey.SERVICE:
                return self.service_id
            case ScopeKey.ENVIRONMENT:
                return self.environment_id
            case ScopeKey.IMAGE_DIGEST:
                return self.image_digest
            case ScopeKey.CONFIG_COMMIT:
                return self.config_commit
            case ScopeKey.WAS_VERSION:
                return self.was_version


@dataclass(frozen=True)
class SecurityExceptionView:
    exception: SecurityException
    status: SecurityExceptionStatus
    users_by_id: dict[uuid.UUID, User]

    def user(self, user_id: uuid.UUID | None) -> User | None:
        return self.users_by_id.get(user_id) if user_id is not None else None


@dataclass(frozen=True)
class SecurityExceptionSearchResult:
    items: list[SecurityExceptionView]
    total: int


class SecurityExceptionService:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock
        self._exceptions = SecurityExceptionRepository(session)
        self._services = ServiceRepository(session)
        self._environments = EnvironmentRepository(session)
        self._organizations = OrganizationRepository(session)
        self._users = UserRepository(session)
        self._audit = AuditRecorder(session, clock)

    async def create_exception(
        self,
        organization_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        command: CreateSecurityExceptionCommand,
    ) -> SecurityExceptionView:
        """승인 대기(PENDING) 예외를 만든다. 범위·만료 규칙에 맞지 않으면 422."""
        now = self._clock.now()
        _check_scope(command)
        _check_expiry(command.expires_at, now)
        async with self._session.begin():
            if command.service_id is not None:
                await self._services.get_live_by_id(organization_id, command.service_id)
            if command.environment_id is not None:
                await self._environments.get_live_by_id(organization_id, command.environment_id)
            exception = SecurityException(
                organization_id=organization_id,
                control_id=command.control_id,
                service_id=command.service_id,
                environment_id=command.environment_id,
                image_digest=command.image_digest,
                config_commit=command.config_commit,
                was_version=command.was_version,
                reason=command.reason,
                requested_by_user_id=actor_user_id,
                expires_at=command.expires_at,
                created_at=now,
                updated_at=now,
            )
            self._exceptions.add(exception)
            await self._session.flush()
            self._record_audit(
                AuditAction.SECURITY_EXCEPTION_REQUESTED,
                exception,
                actor_user_id,
                {"expiresAt": exception.expires_at.isoformat()},
            )
            view = await self._view(organization_id, exception.id, now)
        _log_exception("security exception requested", "create_security_exception", view)
        return view

    async def search_exceptions(
        self,
        organization_id: uuid.UUID,
        *,
        control_ids: Sequence[SecurityControlId],
        service_id: uuid.UUID | None,
        environment_id: uuid.UUID | None,
        statuses: Sequence[SecurityExceptionStatus],
        page: int,
        size: int,
    ) -> SecurityExceptionSearchResult:
        now = self._clock.now()
        conditions = SecurityExceptionConditions(
            now=now,
            control_ids=tuple(control_ids),
            service_id=service_id,
            environment_id=environment_id,
            statuses=tuple(statuses),
        )
        async with self._session.begin():
            exceptions = await self._exceptions.search_by_organization(
                organization_id, conditions, offset=page * size, limit=size
            )
            total = await self._exceptions.count_by_organization(organization_id, conditions)
            users_by_id = await self._users_by_id(exceptions)
        return SecurityExceptionSearchResult(
            items=[
                SecurityExceptionView(item, exception_status(item, now), users_by_id)
                for item in exceptions
            ],
            total=total,
        )

    async def get_exception(
        self, organization_id: uuid.UUID, exception_id: uuid.UUID
    ) -> SecurityExceptionView:
        now = self._clock.now()
        async with self._session.begin():
            return await self._view(organization_id, exception_id, now)

    async def approve_exception(
        self, organization_id: uuid.UUID, exception_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> SecurityExceptionView:
        now = self._clock.now()
        async with self._session.begin():
            organization = await self._organizations.find_by_id(organization_id)
            if organization is None:
                raise OrganizationNotFoundError
            exception = await self._get(organization_id, exception_id)
            if exception_status(exception, now) is not SecurityExceptionStatus.PENDING:
                raise _not_pending(exception)
            if (
                exception.requested_by_user_id == actor_user_id
                and not organization.allow_self_approval
            ):
                raise SelfApprovalForbiddenError
            if not await self._exceptions.approve(exception.id, actor_user_id, now):
                raise _not_pending(exception)
            self._record_audit(AuditAction.SECURITY_EXCEPTION_APPROVED, exception, actor_user_id)
            view = await self._view(organization_id, exception.id, now)
        _log_exception("security exception approved", "approve_security_exception", view)
        return view

    async def revoke_exception(
        self, organization_id: uuid.UUID, exception_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> SecurityExceptionView:
        now = self._clock.now()
        async with self._session.begin():
            exception = await self._get(organization_id, exception_id)
            if not await self._exceptions.revoke(exception.id, actor_user_id, now):
                raise SecurityExceptionClosedError(
                    "security exception is already revoked or expired",
                    exception_id=str(exception.id),
                )
            self._record_audit(AuditAction.SECURITY_EXCEPTION_REVOKED, exception, actor_user_id)
            view = await self._view(organization_id, exception.id, now)
        _log_exception("security exception revoked", "revoke_security_exception", view)
        return view

    def _record_audit(
        self,
        action: AuditAction,
        exception: SecurityException,
        actor_user_id: uuid.UUID,
        details: dict[str, str] | None = None,
    ) -> None:
        self._audit.record(
            organization_id=exception.organization_id,
            actor=AuditActor.user(actor_user_id),
            action=action,
            target=AuditTarget(
                AuditTargetType.SECURITY_EXCEPTION, exception.id, str(exception.control_id)
            ),
            details=details,
        )

    async def _get(self, organization_id: uuid.UUID, exception_id: uuid.UUID) -> SecurityException:
        exception = await self._exceptions.find_in_organization(organization_id, exception_id)
        if exception is None:
            raise SecurityExceptionNotFoundError(
                "security exception not found", exception_id=str(exception_id)
            )
        return exception

    async def _view(
        self, organization_id: uuid.UUID, exception_id: uuid.UUID, now: datetime
    ) -> SecurityExceptionView:
        exception = await self._get(organization_id, exception_id)
        users_by_id = await self._users_by_id([exception])
        return SecurityExceptionView(exception, exception_status(exception, now), users_by_id)

    async def _users_by_id(self, exceptions: Sequence[SecurityException]) -> dict[uuid.UUID, User]:
        user_ids = {
            user_id
            for item in exceptions
            for user_id in (
                item.requested_by_user_id,
                item.approved_by_user_id,
                item.revoked_by_user_id,
            )
            if user_id is not None
        }
        return {user.id: user for user in await self._users.search_by_ids(user_ids)}


def _check_scope(command: CreateSecurityExceptionCommand) -> None:
    """통제가 정한 범위 칸은 모두 있어야 하고, 그 밖의 칸은 없어야 한다."""
    required = get_rule(command.control_id).scope_keys
    details = []
    for key, field_name in SCOPE_FIELD_BY_KEY.items():
        has_value = command.value_of(key) is not None
        if key in required and not has_value:
            details.append(ErrorField(field=field_name, reason=REQUIRED_FOR_CONTROL_REASON))
        elif key not in required and has_value:
            details.append(ErrorField(field=field_name, reason=NOT_ALLOWED_FOR_CONTROL_REASON))
    if details:
        raise RequestFieldsInvalidError(details)


def _check_expiry(expires_at: datetime, now: datetime) -> None:
    if expires_at <= now:
        raise RequestFieldsInvalidError(
            [ErrorField(field=EXPIRES_AT_FIELD, reason=EXPIRES_NOT_AFTER_NOW_REASON)]
        )
    if expires_at - now > EXCEPTION_MAX_DURATION:
        raise RequestFieldsInvalidError(
            [ErrorField(field=EXPIRES_AT_FIELD, reason=EXPIRES_TOO_LATE_REASON)]
        )


def _not_pending(exception: SecurityException) -> SecurityExceptionNotPendingError:
    return SecurityExceptionNotPendingError(
        "security exception is not pending approval", exception_id=str(exception.id)
    )


def _log_exception(message: str, action: str, view: SecurityExceptionView) -> None:
    exception = view.exception
    logger.info(
        message,
        extra={
            "action": action,
            "security_exception_id": str(exception.id),
            "organization_id": str(exception.organization_id),
            "control_id": exception.control_id,
            "status": view.status,
        },
    )
