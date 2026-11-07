"""USGS 조직별 커밋 직후 소비. 예약은 재난 행에 영구 보존, 실패도 자동 재실행하지 않는다."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.tls_probe import PublicHostTlsChecker
from app.core.clock import Clock
from app.core.config import Settings
from app.core.exceptions import AppError
from app.models import AuditLog, Disaster
from app.services.auto_dr_policy import CONSENT, AutoDrAuthorization
from app.services.disaster_service import DisasterService, _emergency_command
from app.services.live_disaster_feed_rules import FeedEvent
from app.services.migration_plan_service import DisasterTrigger, MigrationPlanService
from app.services.security_gate import SecurityGate


class DisasterAutomation:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        settings: Settings,
        tls_checker: PublicHostTlsChecker | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._settings = settings
        self._tls_checker = tls_checker

    async def consume(
        self, organization_id: uuid.UUID, disaster_id: uuid.UUID, event: FeedEvent
    ) -> None:
        if not self._settings.auto_dr_enabled:
            await self._record(disaster_id, "status", {"reason": "AUTO_DR_DISABLED"})
            return
        async with self._session_factory() as session, session.begin():
            rows = list(
                await session.scalars(
                    select(AuditLog).where(
                        AuditLog.organization_id == organization_id,
                        AuditLog.action == CONSENT,
                        AuditLog.id.in_(
                            [uuid.UUID(item) for item in self._settings.auto_dr_consent_ids]
                        ),
                    )
                )
            )
            policies = [(row.id, row.actor_user_id, dict(row.details)) for row in rows]
        if not policies:
            await self._record(disaster_id, "status", {"reason": "CONSENT_MISSING"})
        for consent_id, actor_id, policy in policies:
            assert actor_id is not None
            service_id = uuid.UUID(policy["serviceId"])
            if not await self._reserve(disaster_id, service_id):
                continue
            plan_id: uuid.UUID | None = None
            try:
                if (
                    not 0
                    <= (self._clock.now() - event.occurred_at).total_seconds()
                    <= self._settings.auto_dr_max_event_age_seconds
                ):
                    await self._record(disaster_id, str(service_id), {"reason": "EVENT_STALE"})
                    continue
                async with self._session_factory() as session:
                    automatic = AutoDrAuthorization(
                        session, self._clock, self._settings, consent_id, disaster_id
                    )
                    plans = MigrationPlanService(
                        session,
                        self._clock,
                        self._settings.executor_mode,
                        self._settings.migration_required_requests_default,
                        self._settings.migration_max_wait_seconds_default,
                        self._settings.migration_expected_rps,
                        self._settings.migration_step_overhead_seconds,
                        self._settings.migration_plan_approval_ttl_seconds,
                        security_gate=SecurityGate(
                            session, self._clock, self._tls_checker, self._settings.was_version
                        )
                        if self._tls_checker is not None
                        else None,
                        db_cutover_mode=self._settings.db_cutover_executor_mode,
                        db_cutover_on_disaster=self._settings.migration_db_cutover_on_disaster,
                        automatic=automatic,
                    )
                    disasters = DisasterService(
                        session,
                        self._clock,
                        plans,
                        [
                            item.partition("=")[0].strip()
                            for item in self._settings.entrypoint_environment_map
                        ],
                    )
                    choice = await disasters.choose_automatic_target(
                        organization_id,
                        disaster_id,
                        service_id,
                        uuid.UUID(policy["targetEnvironmentId"]),
                        actor_id,
                    )
                    if isinstance(choice, str):
                        await self._record(disaster_id, str(service_id), {"reason": choice})
                        continue
                    view = await plans.create_plan(
                        organization_id,
                        service_id,
                        actor_id,
                        _emergency_command(choice[0]),
                        DisasterTrigger(disaster_id, float(event.magnitude), choice[1]),
                    )
                    plan_id = view.plan.id
                    await plans.approve_plan(organization_id, plan_id, actor_id)
                await self._record(
                    disaster_id,
                    str(service_id),
                    {"reason": None, "status": "APPROVED", "planId": str(plan_id)},
                )
            except AppError as exc:
                await self._record(
                    disaster_id,
                    str(service_id),
                    {
                        "reason": getattr(exc, "reason", exc.code),
                        "planId": str(plan_id) if plan_id else None,
                    },
                )

    async def _reserve(self, disaster_id: uuid.UUID, service_id: uuid.UUID) -> bool:
        async with self._session_factory() as session, session.begin():
            row = (
                await session.scalars(
                    select(Disaster).where(Disaster.id == disaster_id).with_for_update()
                )
            ).one()
            data: dict[str, Any] = dict(row.feed_event or {})
            attempts = dict(data.get("autoDrAttempts", {}))
            if str(service_id) in attempts:
                return False
            attempts[str(service_id)] = {"status": "RESERVED", "reason": "RESERVATION_RETAINED"}
            row.feed_event = {**data, "autoDrAttempts": attempts}
            return True

    async def _record(self, disaster_id: uuid.UUID, key: str, value: dict[str, Any]) -> None:
        async with self._session_factory() as session, session.begin():
            row = (
                await session.scalars(
                    select(Disaster).where(Disaster.id == disaster_id).with_for_update()
                )
            ).one()
            data: dict[str, Any] = dict(row.feed_event or {})
            row.feed_event = {
                **data,
                "autoDrAttempts": {**data.get("autoDrAttempts", {}), key: value},
            }
