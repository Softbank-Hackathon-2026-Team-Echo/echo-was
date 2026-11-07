"""월비 경계 회귀: 합성 단가이며 실제 절감 근거가 아니다."""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from uuid import uuid4

import pytest

from app.enums import CostComponentKind
from app.models import Environment
from app.services.cost_catalog import (
    CloudPrice,
    CostCatalog,
    CostComponent,
    EnvironmentInventory,
    SharedInventory,
)
from app.services.cost_recommendation import estimate_catalog


def _catalog(storage: Decimal, hourly: Decimal) -> CostCatalog:
    prices = {
        key: CloudPrice(
            key=key,
            unit_price_usd=amount,
            unit=unit,
            description="합성 단가",
            source="synthetic, not a price quote",
            effective_date=date(2026, 11, 5),
            checked_at=date(2026, 11, 5),
            verified=False,
        )
        for key, amount, unit in (
            ("storage", storage, "GiB-month"),
            ("hourly", hourly, "hour"),
        )
    }
    components = (
        CostComponent(
            name="disk",
            kind=CostComponentKind.STORAGE,
            price="storage",
            quantity=1,
            unit_label="GiB",
        ),
        CostComponent(
            name="compute",
            kind=CostComponentKind.COMPUTE,
            price="hourly",
            quantity=2,
            unit_label="instance",
        ),
    )
    inventory = EnvironmentInventory(
        key="environment",
        name="fixture",
        provider="aws",
        region="synthetic",
        note="합성",
        components=components,
        match_kind="aws",
        match_region="synthetic",
    )
    return CostCatalog(prices=prices, environments=(inventory,), shared=(), billing=())


def _environment() -> Environment:
    return Environment(
        id=uuid4(),
        name="fixture",
        kind="aws",
        config={"region": "synthetic"},
        is_deleted=False,
    )


@pytest.mark.parametrize(
    ("storage", "hourly", "expected_exact", "expected_total"),
    [("0.075", "0", "0.075", "0.08"), ("0.075", "0.001", "1.535", "1.54")],
)
def test_catalog_monthly_native_units(
    storage: str,
    hourly: str,
    expected_exact: str,
    expected_total: str,
) -> None:
    result = estimate_catalog(_catalog(Decimal(storage), Decimal(hourly)), [_environment()])
    assert result.inventories[0].exact_monthly_usd == Decimal(expected_exact)
    assert result.monthly_usd == Decimal(expected_total)


def test_catalog_multiple_inventories_round_only_after_dedup_total() -> None:
    catalog = _catalog(Decimal("0.075"), Decimal("0.001"))
    inventory = catalog.environments[0]
    shared = SharedInventory(
        key="shared",
        name="fixture",
        provider="aws",
        region="synthetic",
        note="합성",
        components=inventory.components,
    )
    catalog = CostCatalog(
        prices=catalog.prices,
        environments=catalog.environments,
        shared=(shared, shared),
        billing=(),
    )
    environments = [_environment(), _environment(), _environment()]
    result = estimate_catalog(catalog, environments)
    assert len(result.inventories) == 2
    assert all(item.exact_monthly_usd == Decimal("1.535") for item in result.inventories)
    assert result.monthly_usd == Decimal("3.07")  # 1.54 + 1.54가 아니다.
    matched = next(item for item in result.inventories if item.inventory.key == inventory.key)
    assert matched.environment_ids == tuple(item.id for item in environments)


def test_catalog_monthly_half_cent_sweep() -> None:
    environment = _environment()
    for n in range(1000):
        amount = Decimal(n) / 100 + Decimal("0.005")
        result = estimate_catalog(_catalog(amount, Decimal(0)), [environment])
        assert result.inventories[0].exact_monthly_usd == amount
        assert result.monthly_usd == amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
