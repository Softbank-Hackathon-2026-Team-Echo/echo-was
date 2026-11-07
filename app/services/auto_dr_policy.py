"""사람의 명시 동의를 정확한 서비스·환경·설정·보안 정책에 결속한다."""

import hashlib
import json
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.actions import AuditAction, AuditTargetType
from app.audit.recorder import AuditActor, AuditRecorder, AuditTarget
from app.core.clock import Clock
from app.core.config import EntrypointExecutorMode, Settings
from app.core.exceptions import ConflictError, ErrorField
from app.enums import ActorType, DisasterSource, DisasterStatus, OrganizationRole
from app.models import AuditLog, Disaster, MigrationPlan, Organization, OrganizationMember
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.repositories.service_repository import ServiceRepository
from app.services import security_rules
from app.services.migration_plan_support import read_fingerprint, single_source

CONSENT = "DISASTER_AUTO_DR_CONSENTED"
REVOKED = "DISASTER_AUTO_DR_REVOKED"
BINDING = "autoDr"


class AutoDrRefusedError(ConflictError):
    code = "AUTO_DR_REFUSED"

    def __init__(self, reason: str) -> None:
        super().__init__(
            "automatic disaster recovery refused",
            details=[ErrorField(field="automatic", reason=reason)],
            reason=reason,
        )
        self.reason = reason


def _json_default(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, set | frozenset):
        return sorted(value, key=str)
    if isinstance(value, timedelta):
        return value.total_seconds()
    return str(value)


def digest(value: Any) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_default).encode()
        ).hexdigest()
    )


def security_policy_digest() -> str:
    # API와 worker의 프로세스 hash seed가 달라도 같은 규칙표는 같은 지문이다.
    return digest({"version": security_rules.POLICY_VERSION, "rules": security_rules.RULES})


def material_fingerprint(value: dict[str, Any]) -> dict[str, Any]:
    # 실행 중 비율만 변한다. 배포 id·환경 설정·열린 배포 요청 변화는 허용하지 않는다.
    return {
        key: ([[*row[:2]] for row in item] if key == "connections" else item)
        for key, item in value.items()
        if key != BINDING
    }


async def snapshot(
    session: AsyncSession,
    organization_id: uuid.UUID,
    service_id: uuid.UUID,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
) -> dict[str, Any]:
    service = await ServiceRepository(session).get_live_by_id(organization_id, service_id)
    connections = await ServiceEnvironmentRepository(session).search_live_by_service(service_id)
    source = single_source(connections)
    target = next((row for row in connections if row.environment_id == target_id), None)
    if source.environment_id != source_id or target is None or source.id == target.id:
        raise AutoDrRefusedError("SCOPE_MISMATCH")
    return await read_fingerprint(session, service, connections, source.id, target.id)


async def configuration_digest(
    session: AsyncSession, organization_id: uuid.UUID, service_id: uuid.UUID
) -> str:
    from app.models import ServiceEnvironmentVariable

    service = await ServiceRepository(session).get_live_by_id(organization_id, service_id)
    connections = await ServiceEnvironmentRepository(session).search_live_by_service(service_id)
    variables = list(
        await session.scalars(
            select(ServiceEnvironmentVariable).where(
                ServiceEnvironmentVariable.service_environment_id.in_(
                    [row.id for row in connections]
                ),
                ServiceEnvironmentVariable.is_deleted.is_(False),
            )
        )
    )
    return digest(
        {
            "service": [
                service.repo_url,
                service.health_check_path,
                service.health_check_timeout_seconds,
                service.container_port,
            ],
            "variables": sorted([str(row.id), row.key, row.latest_version] for row in variables),
        }
    )


class AutoDrConsentService:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    async def create_consent(
        self,
        organization_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        service_id: uuid.UUID,
        source_id: uuid.UUID,
        target_id: uuid.UUID,
        expires_at: datetime,
    ) -> uuid.UUID:
        now = self._clock.now()
        if expires_at.tzinfo is None or not now < expires_at <= now + timedelta(days=7):
            raise AutoDrRefusedError("CONSENT_EXPIRY_INVALID")
        async with self._session.begin():
            member = await self._session.scalar(
                select(OrganizationMember).where(
                    OrganizationMember.organization_id == organization_id,
                    OrganizationMember.user_id == actor_user_id,
                )
            )
            if member is None or member.role != OrganizationRole.ADMIN:
                raise AutoDrRefusedError("CONSENT_ADMIN_REQUIRED")
            fingerprint = await snapshot(
                self._session, organization_id, service_id, source_id, target_id
            )
            organization = await self._session.get_one(Organization, organization_id)
            row = AuditRecorder(self._session, self._clock).record(
                organization_id=organization_id,
                actor=AuditActor.user(actor_user_id),
                action=AuditAction.DISASTER_AUTO_DR_CONSENTED,
                target=AuditTarget(AuditTargetType.SERVICE, service_id),
                details={
                    "serviceId": str(service_id),
                    "sourceEnvironmentId": str(source_id),
                    "targetEnvironmentId": str(target_id),
                    "fingerprint": fingerprint,
                    "configurationDigest": await configuration_digest(
                        self._session, organization_id, service_id
                    ),
                    "policyVersion": security_rules.POLICY_VERSION,
                    "policyDigest": security_policy_digest(),
                    "minMagnitude": str(organization.live_feed_min_magnitude),
                    "radiusKm": organization.live_feed_radius_km,
                    "expiresAt": expires_at.isoformat(),
                    "allowDbCutover": False,
                    "acknowledgeAutomaticExecution": True,
                },
            )
            await self._session.flush()
            return row.id

    async def revoke_consent(
        self, organization_id: uuid.UUID, actor_user_id: uuid.UUID, consent_id: uuid.UUID
    ) -> None:
        async with self._session.begin():
            row = await self._session.get(AuditLog, consent_id)
            if row is None or row.organization_id != organization_id or row.action != CONSENT:
                raise AutoDrRefusedError("CONSENT_NOT_FOUND")
            revoked = await self._session.scalar(
                select(AuditLog.id).where(
                    AuditLog.organization_id == organization_id,
                    AuditLog.action == REVOKED,
                    AuditLog.details["consentId"].astext == str(consent_id),
                )
            )
            if revoked is None:
                AuditRecorder(self._session, self._clock).record(
                    organization_id=organization_id,
                    actor=AuditActor.user(actor_user_id),
                    action=AuditAction.DISASTER_AUTO_DR_REVOKED,
                    target=AuditTarget(
                        AuditTargetType.SERVICE, uuid.UUID(row.details["serviceId"])
                    ),
                    details={"consentId": str(consent_id)},
                )


class AutoDrAuthorization:
    """외부 요청이 만들 수 없는 worker 내부 능력. 감사 행·설정 allowlist를 매번 검증한다."""

    def __init__(
        self,
        session: AsyncSession,
        clock: Clock,
        settings: Settings,
        consent_id: uuid.UUID,
        disaster_id: uuid.UUID,
    ) -> None:
        self._session = session
        self._clock = clock
        self._settings = settings
        self.consent_id = consent_id
        self.disaster_id = disaster_id

    async def validate(self, plan: MigrationPlan, *, starting: bool = False) -> None:
        if not self._settings.auto_dr_enabled or not self._settings.live_disaster_feed_enabled:
            raise AutoDrRefusedError("AUTOMATION_DISABLED")
        # WAS #117의 서비스 격리 송신 계약이 미병합인 main에서는 전역 weights 경로를 열지 않는다.
        if self._settings.entrypoint_executor_mode is not EntrypointExecutorMode.FAKE:
            raise AutoDrRefusedError("SERVICE_SCOPED_ROUTING_NOT_READY")
        if str(self.consent_id) not in self._settings.auto_dr_consent_ids:
            raise AutoDrRefusedError("CONSENT_NOT_ALLOWLISTED")
        consent = await self._session.get(AuditLog, self.consent_id, populate_existing=True)
        if consent is None or consent.action != CONSENT or consent.actor_type != ActorType.USER:
            raise AutoDrRefusedError("CONSENT_MISSING")
        policy = consent.details
        if consent.target_id != plan.service_id or policy.get("serviceId") != str(plan.service_id):
            raise AutoDrRefusedError("SCOPE_MISMATCH")
        if consent.organization_id is None:
            raise AutoDrRefusedError("SCOPE_MISMATCH")
        service = await ServiceRepository(self._session).get_live_by_id(
            consent.organization_id, plan.service_id
        )
        if service.organization_id != consent.organization_id:
            raise AutoDrRefusedError("SCOPE_MISMATCH")
        member = await self._session.scalar(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == consent.organization_id,
                OrganizationMember.user_id == consent.actor_user_id,
            )
        )
        if member is None or member.role != OrganizationRole.ADMIN:
            raise AutoDrRefusedError("CONSENT_AUTHORITY_LOST")
        if consent.actor_user_id != plan.created_by_user_id:
            raise AutoDrRefusedError("CONSENT_ACTOR_MISMATCH")
        if await self._session.scalar(
            select(AuditLog.id).where(
                AuditLog.organization_id == consent.organization_id,
                AuditLog.action == REVOKED,
                AuditLog.details["consentId"].astext == str(self.consent_id),
            )
        ):
            raise AutoDrRefusedError("CONSENT_REVOKED")
        now = self._clock.now()
        if datetime.fromisoformat(policy["expiresAt"]) <= now:
            raise AutoDrRefusedError("CONSENT_EXPIRED")
        if (
            policy.get("policyVersion") != security_rules.POLICY_VERSION
            or policy.get("policyDigest") != security_policy_digest()
        ):
            raise AutoDrRefusedError("POLICY_CHANGED")
        if not policy.get("acknowledgeAutomaticExecution"):
            raise AutoDrRefusedError("CONSENT_MISSING")
        if any(step.get("kind") == "DB_CUTOVER" for step in plan.steps):
            raise AutoDrRefusedError("DB_PAIR_NOT_READY")
        disaster = await self._session.get(Disaster, self.disaster_id, populate_existing=True)
        if (
            disaster is None
            or disaster.organization_id != consent.organization_id
            or disaster.id != plan.disaster_id
            or disaster.source != DisasterSource.LIVE_FEED
            or disaster.status != DisasterStatus.ACTIVE
        ):
            raise AutoDrRefusedError("LIVE_EVENT_REQUIRED")
        event: dict[str, Any] = dict(disaster.feed_event or {})
        occurred_at = datetime.fromisoformat(event["occurredAt"])
        if occurred_at > now or (
            starting
            and (now - occurred_at).total_seconds() > self._settings.auto_dr_max_event_age_seconds
        ):
            raise AutoDrRefusedError("EVENT_STALE")
        organization = await self._session.get_one(Organization, consent.organization_id)
        if (
            policy["minMagnitude"] != str(organization.live_feed_min_magnitude)
            or policy["radiusKm"] != organization.live_feed_radius_km
        ):
            raise AutoDrRefusedError("THRESHOLD_POLICY_CHANGED")
        from app.core.places import Coordinates
        from app.repositories.environment_repository import EnvironmentRepository
        from app.services.disaster_service import environment_locations
        from app.services.live_disaster_feed_rules import FeedEvent, evaluate_event

        locations = environment_locations(
            await EnvironmentRepository(self._session).search_all_live(organization.id)
        )
        decision = evaluate_event(
            FeedEvent(
                "usgs",
                event["eventId"],
                (),
                disaster.magnitude,
                Coordinates(disaster.epicenter_lat, disaster.epicenter_lng),
                None,
                None,
                occurred_at,
                None,
            ),
            locations,
            organization.live_feed_min_magnitude,
            organization.live_feed_radius_km,
        )
        connections = await ServiceEnvironmentRepository(self._session).search_live_by_service(
            plan.service_id
        )
        source = next(
            (row for row in connections if row.id == plan.source_service_environment_id), None
        )
        target = next(
            (row for row in connections if row.id == plan.target_service_environment_id), None
        )
        if (
            source is None
            or target is None
            or str(source.environment_id) != policy["sourceEnvironmentId"]
            or str(target.environment_id) != policy["targetEnvironmentId"]
            or not decision.matched
            or source.environment_id not in decision.classification.affected
            or target.environment_id not in decision.classification.safe
        ):
            raise AutoDrRefusedError("NO_SAFE_TARGET")
        current = await read_fingerprint(self._session, service, connections, source.id, target.id)
        if material_fingerprint(current) != material_fingerprint(
            policy["fingerprint"]
        ) or policy.get("configurationDigest") != await configuration_digest(
            self._session, organization.id, plan.service_id
        ):
            raise AutoDrRefusedError("SCOPE_CHANGED")
        from app.repositories.disaster_repository import DisasterRepository

        if await DisasterRepository(self._session).search_blocking_affecting(
            [target.environment_id]
        ):
            raise AutoDrRefusedError("TARGET_AT_RISK")

    def binding(self) -> dict[str, str]:
        return {"consentId": str(self.consent_id), "disasterId": str(self.disaster_id)}
