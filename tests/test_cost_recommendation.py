"""합성 가격은 계산 검증 fixture이며 운영 절감 근거가 아니다."""

from datetime import date
from decimal import Decimal
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
from app.services.cost_recommendation import calculate_savings


@pytest.mark.parametrize("amount", [Decimal("-1"), Decimal("NaN"), Decimal("Infinity")])
def test_savings_rejects_invalid_cost_evidence(amount: Decimal) -> None:
    with pytest.raises(ValueError):
        calculate_savings(
            current_fixed=amount,
            alternative_fixed=Decimal("0"),
            current_recurring=Decimal("0"),
            alternative_recurring=Decimal("0"),
            one_time=None,
            comparable=True,
        )


@pytest.mark.parametrize(
    ("current", "alternative", "recurring", "one_time", "comparable", "net", "break_even"),
    [
        ("100", "120", "5", "30", True, "-25", None),
        ("100", "100", "0", "30", True, "0", None),
        ("100", "80", "5", None, True, "15", None),
        ("100", "80", "5", "30", True, "15", "2"),
        ("100", "80", "5", "30", False, None, None),
        (None, "80", "5", "30", True, None, None),
    ],
)
def test_savings_preserves_sign_null_and_break_even(
    current: str | None,
    alternative: str,
    recurring: str,
    one_time: str | None,
    comparable: bool,
    net: str | None,
    break_even: str | None,
) -> None:
    result = calculate_savings(
        current_fixed=Decimal(current) if current is not None else None,
        alternative_fixed=Decimal(alternative),
        current_recurring=Decimal("0"),
        alternative_recurring=Decimal(recurring),
        one_time=Decimal(one_time) if one_time is not None else None,
        comparable=comparable,
    )
    assert result.net_savings == (Decimal(net) if net is not None else None)
    assert result.break_even_months == (Decimal(break_even) if break_even is not None else None)


def test_traffic_only_preserves_fixed_cost_and_unknown_transfer() -> None:
    from app.services.cost_recommendation import calculate_savings

    result = calculate_savings(
        current_fixed=Decimal("100"),
        alternative_fixed=Decimal("100"),
        current_recurring=None,
        alternative_recurring=None,
        one_time=None,
        comparable=True,
    )

    assert result.fixed_savings == Decimal("0")
    assert result.net_savings is None
    assert result.one_time is None
    assert result.break_even_months is None


def test_catalog_monthly_storage_shared_dedup_and_round_after_total() -> None:
    from app.services.cost_recommendation import estimate_catalog

    today = date(2026, 11, 5)
    price = CloudPrice(
        key="fixture-storage",
        unit_price_usd=Decimal("0.004"),
        unit="GiB-month",
        description="합성 fixture",
        source="unit-test fixture, not a cloud quote",
        effective_date=today,
        checked_at=today,
        verified=False,
    )
    component = CostComponent(
        name="fixture-disk",
        kind=CostComponentKind.STORAGE,
        price=price.key,
        quantity=1,
        unit_label="GiB",
    )
    inventory = EnvironmentInventory(
        key="shared-fixture",
        name="fixture",
        provider="aws",
        region="fixture",
        note="자원 ID가 아닌 카탈로그 키",
        components=(component,),
        match_kind="aws",
        match_region="fixture",
    )
    catalog = CostCatalog(
        prices={price.key: price},
        environments=(inventory,),
        shared=(
            SharedInventory(
                key="fixture-platform",
                name="platform",
                provider="aws",
                region="fixture",
                note="합성 공용 자원",
                components=(component,),
            ),
        ),
        billing=(),
    )
    organization_id = uuid4()
    environments = [
        Environment(
            id=uuid4(),
            organization_id=organization_id,
            name=f"fixture-{i}",
            kind="aws",
            config={"region": "fixture"},
            is_deleted=False,
        )
        for i in range(2)
    ]
    result = estimate_catalog(catalog, environments)
    assert result.monthly_usd == Decimal("0.01")  # .004 + .004, 마지막 한 번 반올림
    assert len(result.inventories) == 2
    environment_estimate = next(
        item for item in result.inventories if item.inventory.key == inventory.key
    )
    assert environment_estimate.environment_ids == tuple(item.id for item in environments)
    assert all(item.exact_monthly_usd == Decimal("0.004") for item in result.inventories)


def test_traffic_only_unknown_total_still_has_symbolic_zero_fixed_savings() -> None:
    result = calculate_savings(
        current_fixed=None,
        alternative_fixed=None,
        current_recurring=None,
        alternative_recurring=None,
        one_time=None,
        comparable=True,
        all_fixed_resources_retained=True,
    )
    assert result.fixed_savings == Decimal(0)
    assert result.net_savings is None


def test_catalog_unmatched_or_onprem_is_not_free() -> None:
    from app.services.cost_recommendation import estimate_catalog

    environment = Environment(id=uuid4(), name="lab", kind="onprem", config={}, is_deleted=False)
    result = estimate_catalog(
        CostCatalog(prices={}, environments=(), shared=(), billing=()), [environment]
    )
    assert result.monthly_usd is None
    assert result.unpriced_environment_ids == (environment.id,)
