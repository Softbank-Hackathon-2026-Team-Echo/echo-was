"""실제 재난 피드 → 재난 이벤트(API 서버 18).

흐름(일꾼, `LIVE_DISASTER_FEED_ENABLED=true` 일 때만, 기본 60초마다)
1. USGS 요약 피드(GeoJSON)를 HTTP 로 읽는다. 실패하면 피드 상태에 오류 코드만 남기고 다음 주기에
   다시 한다.
2. 이미 본 지진 id(disaster_feed_states.seen_event_ids)를 빼고 새 지진만 남긴다.
3. 새 지진마다 살아 있는 환경이 있는 조직 전부를 조직 기준(규모·반경)으로 판정한다.
   맞으면 그 조직에 재난(source = LIVE_FEED, simulated = false, 만든 사람 없음)을 만들고 SYSTEM 감사
   줄
   (DISASTER_DECLARED, component = live-disaster-feed)을 쓴다.
4. 긴급 이전 계획은 만들지 않는다(제안만). 사람이 `POST /disasters/{id}/evacuate` 로 버튼 재난과
   같은
   트리거를 시작한다. 자동 실행은 SYSTEM 이 계획을 만들고 승인해야 해서 #30(사전 승인) 범위로
   남긴다.
5. 피드 상태(마지막 조회·성공 시각, 피드 지진 수, 새 지진 수, 오류 코드)와 기억 목록을 고친다.

시연 주입(`POST /disasters/feed:inject`)은 가짜 지진 한 건을 3·4 단계에 그대로 넣는다(그 조직만,
source = FEED_INJECTED, simulated = true). 피드 상태는 바꾸지 않는다.

같은 조직·같은 피드 지진의 재난은 disasters 부분 유니크 인덱스가 하나로 막는다(일꾼이 여럿이거나
기억 목록 저장 전에 일꾼이 죽어도 겹치지 않는다).
"""

import asyncio
import logging
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.actions import AuditAction, AuditComponent, AuditTargetType
from app.audit.recorder import AuditActor, AuditRecorder, AuditTarget
from app.core.clock import Clock
from app.core.config import Settings
from app.core.places import Coordinates
from app.enums import (
    DisasterSource,
    DisasterStatus,
    DisasterType,
    LiveFeedConnectionStatus,
)
from app.models import Disaster, DisasterFeedState, Organization
from app.repositories.environment_repository import EnvironmentRepository
from app.services.disaster_service import environment_locations
from app.services.live_disaster_feed_rules import (
    INJECT_PROVIDER,
    USGS_PROVIDER,
    FeedEvent,
    FeedFormatError,
    evaluate_event,
    parse_usgs_feed,
    remember,
    unseen_events,
)

logger = logging.getLogger(__name__)

FEED_NAME = "USGS Earthquake Feed (all_hour)"
FEED_UNREACHABLE = "FEED_UNREACHABLE"
FEED_HTTP_ERROR = "FEED_HTTP_ERROR"
FEED_INVALID = "FEED_INVALID"
# 마지막 성공이 이 주기 수보다 오래되면 연결 끊김(ERROR)으로 본다(일꾼이 멈춘 경우 포함).
STALE_AFTER_INTERVALS = 3
DUPLICATE = "DUPLICATE"


@dataclass(frozen=True)
class FeedOutcome:
    """지진 하나 × 조직 하나의 결과. disaster_id 는 새로 만든 재난이다(못 만들었으면 None)."""

    organization_id: uuid.UUID
    disaster_id: uuid.UUID | None
    reason: str | None


@dataclass(frozen=True)
class FeedPollResult:
    feed_count: int
    new_event_count: int
    created_disaster_ids: list[uuid.UUID]
    error_code: str | None


@dataclass(frozen=True)
class FeedStatusView:
    enabled: bool
    connection_status: LiveFeedConnectionStatus
    feed_url: str
    poll_interval_seconds: int
    last_polled_at: datetime | None
    last_success_at: datetime | None
    last_error_code: str | None
    last_feed_count: int
    last_new_event_count: int
    min_magnitude: Decimal
    radius_km: int


@dataclass(frozen=True)
class InjectResult:
    event: FeedEvent
    outcome: FeedOutcome


async def process_feed_event(
    session: AsyncSession,
    clock: Clock,
    event: FeedEvent,
    source: DisasterSource,
    organization_ids: Collection[uuid.UUID] | None = None,
) -> list[FeedOutcome]:
    """지진 하나를 조직마다 판정해 맞는 조직에 재난을 만든다. 한 트랜잭션, 조직마다 savepoint.

    organization_ids 를 주면 그 조직만 본다(시연 주입). 안 주면 살아 있는 환경이 있는 조직 전부다.
    """
    now = clock.now()
    outcomes: list[FeedOutcome] = []
    environments = EnvironmentRepository(session)
    audit = AuditRecorder(session, clock)
    async with session.begin():
        target_ids = (
            sorted(organization_ids)
            if organization_ids is not None
            else await environments.search_organization_ids_with_live()
        )
        for organization_id in target_ids:
            organization = await session.get(Organization, organization_id)
            if organization is None:
                continue
            live = await environments.search_all_live(organization_id)
            radius = organization.live_feed_radius_km
            min_magnitude = organization.live_feed_min_magnitude
            decision = evaluate_event(event, environment_locations(live), min_magnitude, radius)
            if not decision.matched:
                outcomes.append(FeedOutcome(organization_id, None, decision.reason))
                continue
            classification = decision.classification
            disaster = Disaster(
                organization_id=organization_id,
                type=DisasterType.EARTHQUAKE,
                source=source,
                magnitude=event.magnitude,
                epicenter_lat=event.epicenter.lat,
                epicenter_lng=event.epicenter.lng,
                radius_km=radius,
                meets_threshold=True,
                status=DisasterStatus.ACTIVE,
                affected_environment_ids=[str(item) for item in classification.affected],
                safe_environment_ids=[str(item) for item in classification.safe],
                unknown_environment_ids=[str(item) for item in classification.unknown],
                created_by_user_id=None,
                feed_event_id=event.key,
                feed_event=event.summary(decision.nearest_distance_km),
                started_at=now,
                created_at=now,
                updated_at=now,
            )
            try:
                async with session.begin_nested():
                    session.add(disaster)
                    await session.flush()
            except IntegrityError:
                # 다른 일꾼이 같은 지진으로 이미 만들었다(부분 유니크 인덱스).
                outcomes.append(FeedOutcome(organization_id, None, DUPLICATE))
                continue
            audit.record(
                organization_id=organization_id,
                actor=AuditActor.system(AuditComponent.LIVE_DISASTER_FEED),
                action=AuditAction.DISASTER_DECLARED,
                target=AuditTarget(
                    AuditTargetType.DISASTER, disaster.id, f"EARTHQUAKE M{event.magnitude:.1f}"
                ),
                details={
                    "source": str(source),
                    "feedEventId": event.key,
                    "magnitude": float(event.magnitude),
                    "place": event.place,
                    "radiusKm": radius,
                    "minMagnitude": float(min_magnitude),
                    "nearestDistanceKm": decision.nearest_distance_km,
                    "affectedEnvironmentIds": disaster.affected_environment_ids,
                    "evacuation": "SUGGESTED",
                },
            )
            outcomes.append(FeedOutcome(organization_id, disaster.id, None))
            logger.info(
                "live feed disaster declared",
                extra={
                    "action": "declare_feed_disaster",
                    "disaster_id": str(disaster.id),
                    "organization_id": str(organization_id),
                    "feed_event_id": event.key,
                    "source": str(source),
                },
            )
    return outcomes


class LiveDisasterFeedService:
    """API 쪽: 피드 상태 조회와 시연 주입."""

    def __init__(self, session: AsyncSession, clock: Clock, settings: Settings) -> None:
        self._session = session
        self._clock = clock
        self._settings = settings
        self._audit = AuditRecorder(session, clock)

    async def get_status(self, organization_id: uuid.UUID) -> FeedStatusView:
        async with self._session.begin():
            state = await self._session.get(DisasterFeedState, USGS_PROVIDER)
            organization = await self._session.get(Organization, organization_id)
        assert organization is not None
        return FeedStatusView(
            enabled=self._settings.live_disaster_feed_enabled,
            connection_status=self._connection_status(state),
            feed_url=self._settings.live_disaster_feed_url,
            poll_interval_seconds=self._settings.live_disaster_feed_interval_seconds,
            last_polled_at=state.last_polled_at if state else None,
            last_success_at=state.last_success_at if state else None,
            last_error_code=state.last_error_code if state else None,
            last_feed_count=state.last_feed_count if state else 0,
            last_new_event_count=state.last_new_event_count if state else 0,
            min_magnitude=organization.live_feed_min_magnitude,
            radius_km=organization.live_feed_radius_km,
        )

    def _connection_status(self, state: DisasterFeedState | None) -> LiveFeedConnectionStatus:
        if not self._settings.live_disaster_feed_enabled:
            return LiveFeedConnectionStatus.DISABLED
        if state is None or state.last_polled_at is None:
            return LiveFeedConnectionStatus.WAITING
        if state.last_error_code is not None or state.last_success_at is None:
            return LiveFeedConnectionStatus.ERROR
        stale_after = timedelta(
            seconds=self._settings.live_disaster_feed_interval_seconds * STALE_AFTER_INTERVALS
        )
        if self._clock.now() - state.last_success_at > stale_after:
            return LiveFeedConnectionStatus.ERROR
        return LiveFeedConnectionStatus.CONNECTED

    async def inject(
        self,
        organization_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        magnitude: Decimal,
        epicenter: Coordinates,
        place: str | None,
    ) -> InjectResult:
        """가짜 지진 한 건을 피드와 같은 경로로 그 조직에만 흘린다(source = FEED_INJECTED)."""
        now = self._clock.now()
        event = FeedEvent(
            provider=INJECT_PROVIDER,
            event_id=uuid.uuid4().hex,
            aliases=(),
            magnitude=magnitude,
            epicenter=epicenter,
            depth_km=None,
            place=place,
            occurred_at=now,
            url=None,
        )
        (outcome,) = await process_feed_event(
            self._session,
            self._clock,
            event,
            DisasterSource.FEED_INJECTED,
            organization_ids=[organization_id],
        )
        async with self._session.begin():
            organization = await self._session.get(Organization, organization_id)
            assert organization is not None
            target = (
                AuditTarget(
                    AuditTargetType.DISASTER,
                    outcome.disaster_id,
                    f"EARTHQUAKE M{magnitude:.1f}",
                )
                if outcome.disaster_id is not None
                else AuditTarget(AuditTargetType.ORGANIZATION, organization_id, organization.name)
            )
            self._audit.record(
                organization_id=organization_id,
                actor=AuditActor.user(actor_user_id),
                action=AuditAction.DISASTER_FEED_INJECTED,
                target=target,
                details={
                    "feedEventId": event.key,
                    "magnitude": float(magnitude),
                    "epicenter": {"lat": epicenter.lat, "lng": epicenter.lng},
                    "matched": outcome.disaster_id is not None,
                    "reason": outcome.reason,
                },
            )
        return InjectResult(event=event, outcome=outcome)


class LiveDisasterFeedPoller:
    """일꾼 쪽: 피드를 주기로 읽는다. 스위치가 꺼져 있으면 아무것도 하지 않는다(HTTP 도 안 부름)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._settings = settings
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            timeout=settings.live_disaster_feed_timeout_seconds,
            headers={"User-Agent": "echo-was live-disaster-feed"},
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def poll_once(self) -> FeedPollResult | None:
        """한 번 읽는다. 꺼져 있으면 None."""
        if not self._settings.live_disaster_feed_enabled:
            return None
        polled_at = self._clock.now()
        try:
            events = await self._fetch()
        except _FeedFetchError as exc:
            await self._record_state(polled_at, error_code=exc.code)
            logger.warning(
                "live disaster feed fetch failed",
                extra={"action": "poll_disaster_feed", "error_code": exc.code},
            )
            return FeedPollResult(0, 0, [], exc.code)
        async with self._session_factory() as session, session.begin():
            state = await session.get(DisasterFeedState, USGS_PROVIDER)
            seen = list(state.seen_event_ids) if state is not None else []
        fresh = unseen_events(events, seen)
        created: list[uuid.UUID] = []
        for event in fresh:
            async with self._session_factory() as session:
                outcomes = await process_feed_event(
                    session, self._clock, event, DisasterSource.LIVE_FEED
                )
            created.extend(item.disaster_id for item in outcomes if item.disaster_id is not None)
        await self._record_state(
            polled_at, error_code=None, feed_count=len(events), fresh=fresh, success=True
        )
        logger.info(
            "live disaster feed polled",
            extra={
                "action": "poll_disaster_feed",
                "feed_count": len(events),
                "new_event_count": len(fresh),
                "created_disaster_count": len(created),
            },
        )
        return FeedPollResult(len(events), len(fresh), created, None)

    async def _fetch(self) -> list[FeedEvent]:
        try:
            response = await self._http.get(self._settings.live_disaster_feed_url)
        except httpx.HTTPError as exc:
            raise _FeedFetchError(FEED_UNREACHABLE) from exc
        if response.status_code != httpx.codes.OK:
            raise _FeedFetchError(FEED_HTTP_ERROR)
        try:
            payload: Any = response.json()
            return parse_usgs_feed(payload)
        except (ValueError, FeedFormatError) as exc:
            raise _FeedFetchError(FEED_INVALID) from exc

    async def _record_state(
        self,
        polled_at: datetime,
        *,
        error_code: str | None,
        feed_count: int = 0,
        fresh: list[FeedEvent] | None = None,
        success: bool = False,
    ) -> None:
        """피드 상태 행을 잠그고 고친다(없으면 만든다). 실패 때는 기억 목록·수를 그대로 둔다."""
        async with self._session_factory() as session, session.begin():
            await session.execute(
                insert(DisasterFeedState)
                .values(
                    provider=USGS_PROVIDER,
                    last_feed_count=0,
                    last_new_event_count=0,
                    seen_event_ids=[],
                )
                .on_conflict_do_nothing(index_elements=["provider"])
            )
            state = (
                await session.scalars(
                    select(DisasterFeedState)
                    .where(DisasterFeedState.provider == USGS_PROVIDER)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).one()
            state.last_polled_at = polled_at
            state.last_error_code = error_code
            state.updated_at = polled_at
            if success:
                state.last_success_at = polled_at
                state.last_feed_count = feed_count
                state.last_new_event_count = len(fresh or [])
                state.seen_event_ids = remember(state.seen_event_ids, fresh or [])

    async def run(self, stop: asyncio.Event) -> None:
        interval = self._settings.live_disaster_feed_interval_seconds
        while not stop.is_set():
            try:
                await self.poll_once()
            except Exception:
                # 피드 처리 실패로 일꾼이 멈추지 않게 한다. 다음 주기에 다시 한다.
                logger.exception("live disaster feed crashed", extra={"action": "run_worker"})
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass


class _FeedFetchError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
