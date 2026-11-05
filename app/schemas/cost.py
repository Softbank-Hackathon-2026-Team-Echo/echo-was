"""클라우드 비용 응답(운영 1). 모든 칸은 항상 있고 값이 없으면 null 이다.

환경·공용 항목의 source 는 늘 ESTIMATE 이고, 계정 단위 청구 실측은 billing 블록에만 있다.
"""

import datetime
import uuid
from decimal import Decimal
from typing import Literal

from app.enums import CostComponentKind, CostSource
from app.schemas.response import ApiModel
from app.services.cost_catalog import BillingDay, BillingProvider, PriceUnit
from app.services.cost_service import (
    CloudCostsView,
    CostComponentView,
    EnvironmentCostView,
    ExcludedEnvironmentView,
    SharedCostView,
)


class CostComponentResponse(ApiModel):
    name: str
    kind: CostComponentKind
    quantity: int
    unit: str
    unit_price_usd: float
    price_unit: PriceUnit
    hourly_usd: float
    price_verified: bool
    price_source: str
    price_checked_at: datetime.date
    note: str | None

    @classmethod
    def from_view(cls, view: CostComponentView) -> "CostComponentResponse":
        return cls(
            name=view.name,
            kind=view.kind,
            quantity=view.quantity,
            unit=view.unit,
            unit_price_usd=float(view.unit_price_usd),
            price_unit=view.price_unit,
            hourly_usd=float(view.hourly_usd),
            price_verified=view.price_verified,
            price_source=view.price_source,
            price_checked_at=view.price_checked_at,
            note=view.note,
        )


class EnvironmentCostResponse(ApiModel):
    environment_id: uuid.UUID
    environment_name: str
    provider: str
    region: str | None
    source: Literal[CostSource.ESTIMATE] = CostSource.ESTIMATE
    hourly_usd: float | None
    daily_usd: float | None
    as_of: datetime.datetime
    note: str
    components: list[CostComponentResponse]

    @classmethod
    def from_view(cls, view: EnvironmentCostView) -> "EnvironmentCostResponse":
        return cls(
            environment_id=view.environment_id,
            environment_name=view.environment_name,
            provider=view.provider,
            region=view.region,
            hourly_usd=_to_float(view.hourly_usd),
            daily_usd=_to_float(view.daily_usd),
            as_of=view.as_of,
            note=view.note,
            components=[CostComponentResponse.from_view(item) for item in view.components],
        )


class SharedCostResponse(ApiModel):
    key: str
    name: str
    provider: str
    region: str
    source: Literal[CostSource.ESTIMATE] = CostSource.ESTIMATE
    hourly_usd: float
    daily_usd: float
    as_of: datetime.datetime
    note: str
    components: list[CostComponentResponse]

    @classmethod
    def from_view(cls, view: SharedCostView) -> "SharedCostResponse":
        return cls(
            key=view.key,
            name=view.name,
            provider=view.provider,
            region=view.region,
            hourly_usd=float(view.hourly_usd),
            daily_usd=float(view.daily_usd),
            as_of=view.as_of,
            note=view.note,
            components=[CostComponentResponse.from_view(item) for item in view.components],
        )


class ExcludedEnvironmentResponse(ApiModel):
    environment_id: uuid.UUID
    environment_name: str
    kind: str
    note: str

    @classmethod
    def from_view(cls, view: ExcludedEnvironmentView) -> "ExcludedEnvironmentResponse":
        return cls(
            environment_id=view.environment_id,
            environment_name=view.environment_name,
            kind=view.kind,
            note=view.note,
        )


class BillingDayResponse(ApiModel):
    date: datetime.date
    usage_usd: float
    credits_usd: float
    net_usd: float
    estimated: bool

    @classmethod
    def from_entry(cls, entry: BillingDay) -> "BillingDayResponse":
        return cls(
            date=entry.date,
            usage_usd=entry.usage_usd,
            credits_usd=entry.credits_usd,
            net_usd=entry.net_usd,
            estimated=entry.estimated,
        )


class BillingResponse(ApiModel):
    provider: str
    source: Literal[CostSource.BILLING] = CostSource.BILLING
    available: bool
    scope: str
    label: str
    fetched_at: datetime.datetime | None
    note: str | None
    days: list[BillingDayResponse]

    @classmethod
    def from_entry(cls, entry: BillingProvider) -> "BillingResponse":
        return cls(
            provider=entry.provider,
            available=bool(entry.days),
            scope=entry.scope,
            label=entry.label,
            fetched_at=entry.fetched_at,
            note=entry.note,
            days=[BillingDayResponse.from_entry(day) for day in entry.days],
        )


class CloudCostsResponse(ApiModel):
    as_of: datetime.datetime
    currency: str
    total_hourly_usd: float
    total_daily_usd: float
    environments: list[EnvironmentCostResponse]
    shared: list[SharedCostResponse]
    excluded: list[ExcludedEnvironmentResponse]
    billing: list[BillingResponse]

    @classmethod
    def from_view(cls, view: CloudCostsView) -> "CloudCostsResponse":
        return cls(
            as_of=view.as_of,
            currency=view.currency,
            total_hourly_usd=float(view.total_hourly_usd),
            total_daily_usd=float(view.total_daily_usd),
            environments=[EnvironmentCostResponse.from_view(item) for item in view.environments],
            shared=[SharedCostResponse.from_view(item) for item in view.shared],
            excluded=[ExcludedEnvironmentResponse.from_view(item) for item in view.excluded],
            billing=[BillingResponse.from_entry(item) for item in view.billing],
        )


def _to_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)
