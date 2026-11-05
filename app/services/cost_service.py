"""조직의 클라우드 비용 보기(운영 1): 환경 목록 × 데이터 파일로 ESTIMATE 를 계산한다.

서버는 청구 API 를 부르지 않는다. BILLING 은 스크립트가 파일에 넣은 계정 단위 값을 그대로 보여 주고,
환경 값으로 나누지 않는다. 금액은 Decimal 로 계산하고 합계는 정밀 값의 합에서 반올림한다.
"""

import datetime
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.enums import CostComponentKind
from app.models import Environment
from app.repositories.environment_repository import EnvironmentRepository
from app.services.cost_catalog import (
    BillingProvider,
    CloudPrice,
    CostCatalog,
    PriceUnit,
    SharedInventory,
)

CURRENCY = "USD"
BILLED_KINDS = frozenset({"aws", "gcp"})
ONPREM_KIND = "onprem"
ONPREM_NOTE = "온프레: 자체 장비, 청구 없음"
UNBILLED_KIND_NOTE = "청구를 계산하지 않는 종류"
NO_INVENTORY_NOTE = "자원 목록 없음 — 추정하지 않음"

_HOURS_PER_MONTH = Decimal(730)
_HOURS_PER_DAY = Decimal(24)
_HOURLY_QUANTUM = Decimal("0.0001")
_DAILY_QUANTUM = Decimal("0.01")


@dataclass(frozen=True)
class CostComponentView:
    name: str
    kind: CostComponentKind
    quantity: int
    unit: str
    unit_price_usd: Decimal
    price_unit: PriceUnit
    hourly_usd: Decimal
    price_verified: bool
    price_source: str
    price_checked_at: datetime.date
    note: str | None


@dataclass(frozen=True)
class CostEstimate:
    """exact_hourly 는 합계용 정밀 값, hourly_usd·daily_usd 는 응답용 반올림 값이다."""

    exact_hourly: Decimal
    hourly_usd: Decimal
    daily_usd: Decimal
    components: tuple[CostComponentView, ...]


@dataclass(frozen=True)
class EnvironmentCostView:
    environment_id: uuid.UUID
    environment_name: str
    provider: str
    region: str | None
    hourly_usd: Decimal | None
    daily_usd: Decimal | None
    as_of: datetime.datetime
    note: str
    components: tuple[CostComponentView, ...]


@dataclass(frozen=True)
class SharedCostView:
    key: str
    name: str
    provider: str
    region: str
    hourly_usd: Decimal
    daily_usd: Decimal
    as_of: datetime.datetime
    note: str
    components: tuple[CostComponentView, ...]


@dataclass(frozen=True)
class ExcludedEnvironmentView:
    environment_id: uuid.UUID
    environment_name: str
    kind: str
    note: str


@dataclass(frozen=True)
class CloudCostsView:
    as_of: datetime.datetime
    currency: str
    total_hourly_usd: Decimal
    total_daily_usd: Decimal
    environments: tuple[EnvironmentCostView, ...]
    shared: tuple[SharedCostView, ...]
    excluded: tuple[ExcludedEnvironmentView, ...]
    billing: tuple[BillingProvider, ...]


def estimate_inventory(catalog: CostCatalog, inventory: SharedInventory) -> CostEstimate:
    """자원 목록 하나의 시간·일 금액. GB·월 단가는 730시간으로 나눠 시간 값으로 바꾼다."""
    exact_hourly = Decimal(0)
    components = []
    for component in inventory.components:
        price = catalog.prices[component.price]
        component_hourly = _exact_hourly(price, component.quantity)
        exact_hourly += component_hourly
        components.append(
            CostComponentView(
                name=component.name,
                kind=component.kind,
                quantity=component.quantity,
                unit=component.unit_label,
                unit_price_usd=price.unit_price_usd,
                price_unit=price.unit,
                hourly_usd=_round_hourly(component_hourly),
                price_verified=price.verified,
                price_source=price.source,
                price_checked_at=price.checked_at,
                note=price.note,
            )
        )
    return CostEstimate(
        exact_hourly=exact_hourly,
        hourly_usd=_round_hourly(exact_hourly),
        daily_usd=_round_daily(exact_hourly),
        components=tuple(components),
    )


def build_cloud_costs(
    catalog: CostCatalog, environments: Sequence[Environment], now: datetime.datetime
) -> CloudCostsView:
    environment_views: list[EnvironmentCostView] = []
    excluded: list[ExcludedEnvironmentView] = []
    total_hourly = Decimal(0)
    has_match = False
    for environment in environments:
        if environment.kind not in BILLED_KINDS:
            excluded.append(_excluded_view(environment))
            continue
        region = environment.config.get("region")
        inventory = catalog.find_environment_inventory(environment.kind, region)
        if inventory is None:
            environment_views.append(_unmatched_view(environment, region, now))
            continue
        has_match = True
        estimate = estimate_inventory(catalog, inventory)
        total_hourly += estimate.exact_hourly
        environment_views.append(
            EnvironmentCostView(
                environment_id=environment.id,
                environment_name=environment.name,
                provider=environment.kind,
                region=region,
                hourly_usd=estimate.hourly_usd,
                daily_usd=estimate.daily_usd,
                as_of=now,
                note=inventory.note,
                components=estimate.components,
            )
        )
    # 공용 자원은 aws·gcp 환경이 목록에 맞은 조직에만 보인다(개인 조직에 플랫폼 비용이 보이지 않게).
    shared_views: list[SharedCostView] = []
    if has_match:
        for shared_inventory in catalog.shared:
            estimate = estimate_inventory(catalog, shared_inventory)
            total_hourly += estimate.exact_hourly
            shared_views.append(_shared_view(shared_inventory, estimate, now))
    return CloudCostsView(
        as_of=now,
        currency=CURRENCY,
        total_hourly_usd=_round_hourly(total_hourly),
        total_daily_usd=_round_daily(total_hourly),
        environments=tuple(environment_views),
        shared=tuple(shared_views),
        excluded=tuple(excluded),
        billing=catalog.billing,
    )


class CostService:
    def __init__(self, session: AsyncSession, clock: Clock, catalog: CostCatalog) -> None:
        self._session = session
        self._clock = clock
        self._catalog = catalog
        self._environment_repository = EnvironmentRepository(session)

    async def get_cloud_costs(self, organization_id: uuid.UUID) -> CloudCostsView:
        """조직의 비용 보기. 읽기만 하고 외부를 부르지 않는다."""
        now = self._clock.now()
        async with self._session.begin():
            environments = await self._environment_repository.search_all_live(organization_id)
        return build_cloud_costs(self._catalog, environments, now)


def _exact_hourly(price: CloudPrice, quantity: int) -> Decimal:
    amount = quantity * price.unit_price_usd
    return amount / _HOURS_PER_MONTH if price.unit == "GiB-month" else amount


def _round_hourly(exact_hourly: Decimal) -> Decimal:
    return exact_hourly.quantize(_HOURLY_QUANTUM, rounding=ROUND_HALF_UP)


def _round_daily(exact_hourly: Decimal) -> Decimal:
    return (exact_hourly * _HOURS_PER_DAY).quantize(_DAILY_QUANTUM, rounding=ROUND_HALF_UP)


def _excluded_view(environment: Environment) -> ExcludedEnvironmentView:
    return ExcludedEnvironmentView(
        environment_id=environment.id,
        environment_name=environment.name,
        kind=environment.kind,
        note=ONPREM_NOTE if environment.kind == ONPREM_KIND else UNBILLED_KIND_NOTE,
    )


def _unmatched_view(
    environment: Environment, region: str | None, now: datetime.datetime
) -> EnvironmentCostView:
    return EnvironmentCostView(
        environment_id=environment.id,
        environment_name=environment.name,
        provider=environment.kind,
        region=region,
        hourly_usd=None,
        daily_usd=None,
        as_of=now,
        note=NO_INVENTORY_NOTE,
        components=(),
    )


def _shared_view(
    inventory: SharedInventory, estimate: CostEstimate, now: datetime.datetime
) -> SharedCostView:
    return SharedCostView(
        key=inventory.key,
        name=inventory.name,
        provider=inventory.provider,
        region=inventory.region,
        hourly_usd=estimate.hourly_usd,
        daily_usd=estimate.daily_usd,
        as_of=now,
        note=inventory.note,
        components=estimate.components,
    )
