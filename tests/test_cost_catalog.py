"""비용 카탈로그·추정 계산 시험(운영 1 AC-1·AC-2). 실제 데이터 파일과 임시 파일을 읽는다."""

import datetime
import uuid
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import pytest

from app.enums import CostComponentKind
from app.models import Environment
from app.services.cost_catalog import (
    CloudPrice,
    CostCatalog,
    CostCatalogError,
    CostComponent,
    EnvironmentInventory,
    load_billing_file,
    load_cost_catalog,
)
from app.services.cost_service import (
    NO_INVENTORY_NOTE,
    ONPREM_NOTE,
    UNBILLED_KIND_NOTE,
    build_cloud_costs,
    estimate_inventory,
)

NOW = datetime.datetime(2026, 11, 5, 6, 0, tzinfo=datetime.UTC)
EIGHT_PLACES = Decimal("0.00000001")

# SPEC 4절 단가표: (단가, 단위, 유효일, 확인 여부).
SPEC_PRICES = {
    "aws.ap-northeast-1.eks-cluster": ("0.10", "hour", "2026-10-01", True),
    "aws.ap-northeast-1.ec2.m7i-flex.large": ("0.12369", "hour", "2026-11-01", True),
    "aws.ap-northeast-1.ebs.gp3": ("0.096", "GiB-month", "2026-11-01", True),
    "aws.ap-northeast-1.nat-gateway": ("0.062", "hour", "2026-11-01", True),
    "aws.ap-northeast-1.alb": ("0.0243", "hour", "2026-08-01", True),
    "aws.ap-northeast-1.nlb": ("0.0243", "hour", "2026-08-01", True),
    "aws.ap-northeast-1.public-ipv4": ("0.005", "hour", "2026-11-01", True),
    "gcp.gke-zonal-cluster": ("0.10", "hour", "2026-11-04", True),
    "gcp.asia-northeast3.e2-standard-2": ("0.085964", "hour", "2026-11-04", True),
    "gcp.asia-northeast3.e2-small": ("0.021491", "hour", "2026-11-04", True),
    "gcp.asia-northeast3.pd-balanced": ("0.13", "GiB-month", "2026-11-04", True),
    "gcp.external-ip-standard-vm": ("0.005", "hour", "2026-11-04", True),
    "gcp.cloud-nat-vm": ("0.0014", "hour", "2026-11-05", True),
    "gcp.cloud-nat-ip": ("0.005", "hour", "2026-11-05", True),
    "gcp.forwarding-rules-first-5": ("0.025", "hour", "2026-11-05", False),
}

VALID_PRICES = """
[[prices]]
key = "p.hour"
unit_price_usd = 0.5
unit = "hour"
description = "시간 단가"
source = "시험"
effective_date = "2026-11-01"
checked_at = "2026-11-05"
verified = true

[[prices]]
key = "p.disk"
unit_price_usd = 0.1
unit = "GiB-month"
description = "용량 단가"
source = "시험"
effective_date = "2026-11-01"
checked_at = "2026-11-05"
verified = false
"""

VALID_INVENTORY = """
[[environments]]
key = "env-a"
name = "env-a"
provider = "aws"
region = "r-1"
match_kind = "aws"
match_region = "r-1"
note = "환경 메모"

[[environments.components]]
name = "노드"
kind = "COMPUTE"
price = "p.hour"
quantity = 2
unit_label = "대"

[[shared]]
key = "shared-a"
name = "공용"
provider = "gcp"
region = "r-2"
note = "공용 메모"

[[shared.components]]
name = "디스크"
kind = "STORAGE"
price = "p.disk"
quantity = 73
unit_label = "GiB"
"""

VALID_BILLING = '{"providers": []}'

DUPLICATE_PRICE = """
[[prices]]
key = "p.hour"
unit_price_usd = 0.7
unit = "hour"
description = "겹치는 키"
source = "시험"
effective_date = "2026-11-01"
checked_at = "2026-11-05"
verified = true
"""

_BILLING_AWS_EMPTY = (
    '{"provider": "aws", "scope": "ACCOUNT", "label": "l", "source": null,'
    ' "fetchedAt": null, "note": null, "days": []}'
)
_BILLING_DAY_5 = (
    '{"date": "2026-11-05", "usageUsd": 1, "creditsUsd": 0, "netUsd": 1, "estimated": true}'
)
_BILLING_DAY_4 = (
    '{"date": "2026-11-04", "usageUsd": 1, "creditsUsd": 0, "netUsd": 1, "estimated": true}'
)


def _write(
    tmp_path: Path,
    prices: str = VALID_PRICES,
    inventory: str = VALID_INVENTORY,
    billing: str = VALID_BILLING,
) -> tuple[Path, Path, Path]:
    prices_path = tmp_path / "prices.toml"
    inventory_path = tmp_path / "inventory.toml"
    billing_path = tmp_path / "billing.json"
    prices_path.write_text(prices, encoding="utf-8")
    inventory_path.write_text(inventory, encoding="utf-8")
    billing_path.write_text(billing, encoding="utf-8")
    return prices_path, inventory_path, billing_path


def _environment(name: str, kind: str, config: dict[str, object]) -> Environment:
    return Environment(
        id=uuid.uuid4(), organization_id=uuid.uuid4(), name=name, kind=kind, config=config
    )


def _aws(name: str, region: str) -> Environment:
    return _environment(name, "aws", {"region": region, "cluster_name": "eks"})


def _gcp(name: str, region: str) -> Environment:
    return _environment(
        name, "gcp", {"project_id": "echo-hack-123", "region": region, "cluster_name": "gke"}
    )


# --- AC-1: 실제 데이터 파일 ---


def test_real_catalog_estimates_match_spec_table() -> None:
    # given
    catalog = load_cost_catalog()
    environments = [_aws("aws-tokyo", "ap-northeast-1"), _gcp("gcp-seoul", "asia-northeast3")]

    # when
    costs = build_cloud_costs(catalog, environments, NOW)

    # then
    aws, gcp = costs.environments
    assert (aws.hourly_usd, aws.daily_usd) == (Decimal("0.4896"), Decimal("11.75"))
    assert (gcp.hourly_usd, gcp.daily_usd) == (Decimal("0.3243"), Decimal("7.78"))
    assert [(s.key, s.hourly_usd, s.daily_usd) for s in costs.shared] == [
        ("gcp-platform", Decimal("0.1846"), Decimal("4.43"))
    ]
    assert (costs.total_hourly_usd, costs.total_daily_usd) == (Decimal("0.9985"), Decimal("23.96"))


def test_real_catalog_estimates_keep_exact_values_from_spec() -> None:
    catalog = load_cost_catalog()

    exact = {
        inventory.key: estimate_inventory(catalog, inventory).exact_hourly
        for inventory in (*catalog.environments, *catalog.shared)
    }

    # SPEC 은 뒤를 잘라 적었다(0.48955534…).
    def cut(value: Decimal) -> Decimal:
        return value.quantize(EIGHT_PLACES, rounding=ROUND_DOWN)

    assert cut(exact["aws-tokyo"]) == Decimal("0.48955534")
    assert cut(exact["gcp-seoul"]) == Decimal("0.32431704")
    assert cut(exact["gcp-platform"]) == Decimal("0.18459257")
    assert cut(sum(exact.values(), Decimal(0))) == Decimal("0.99846495")


def test_real_prices_match_spec_table() -> None:
    catalog = load_cost_catalog()

    actual = {
        key: (str(price.unit_price_usd), price.unit, str(price.effective_date), price.verified)
        for key, price in catalog.prices.items()
    }

    assert actual == SPEC_PRICES
    assert {price.checked_at for price in catalog.prices.values()} == {datetime.date(2026, 11, 5)}


def test_real_billing_is_the_november_4_cost_explorer_values() -> None:
    catalog = load_cost_catalog()

    aws, gcp = catalog.billing

    assert aws.provider == "aws"
    assert aws.fetched_at == datetime.datetime(2026, 11, 5, 5, 50, tzinfo=datetime.UTC)
    assert [(d.date.isoformat(), d.usage_usd, d.credits_usd, d.net_usd) for d in aws.days] == [
        ("2026-11-04", 6.7374, -6.7374, 0.0)
    ]
    assert aws.days[0].estimated is True
    assert (gcp.provider, gcp.source, gcp.fetched_at, gcp.days) == ("gcp", None, None, ())


def test_estimate_component_uses_price_unit_and_rounds_for_display() -> None:
    catalog = load_cost_catalog()
    inventory = catalog.find_environment_inventory("aws", "ap-northeast-1")
    assert inventory is not None

    estimate = estimate_inventory(catalog, inventory)

    by_name = {component.name: component for component in estimate.components}
    ec2 = by_name["EC2 노드 m7i-flex.large (온디맨드)"]
    assert (ec2.quantity, ec2.unit, ec2.unit_price_usd, ec2.hourly_usd) == (
        2,
        "대",
        Decimal("0.12369"),
        Decimal("0.2474"),
    )
    assert ec2.price_unit == "hour"
    # 40 GiB × 0.096 ÷ 730
    root_volume = by_name["노드 루트 볼륨 gp3"]
    assert (root_volume.price_unit, root_volume.hourly_usd) == ("GiB-month", Decimal("0.0053"))


def test_estimate_unverified_price_is_visible_on_component() -> None:
    catalog = load_cost_catalog()
    inventory = catalog.find_environment_inventory("gcp", "asia-northeast3")
    assert inventory is not None

    estimate = estimate_inventory(catalog, inventory)

    rule = next(c for c in estimate.components if c.name.startswith("지역 전달 규칙"))
    assert rule.price_verified is False
    assert rule.note == "서울 지역 표 값 미확인"
    assert all(c.price_verified for c in estimate.components if c is not rule)


def test_build_totals_round_exact_sum_not_rounded_items() -> None:
    # given: 항목 하나는 0.00004 라 표시 값이 0.0000 이지만 셋을 더하면 0.00012 다.
    today = datetime.date(2026, 11, 5)
    regions = ("r-1", "r-2", "r-3")
    catalog = CostCatalog(
        prices={
            "tiny": CloudPrice(
                key="tiny",
                unit_price_usd=Decimal("0.00004"),
                unit="hour",
                description="시험",
                source="시험",
                effective_date=today,
                checked_at=today,
                verified=True,
            )
        },
        environments=tuple(
            EnvironmentInventory(
                key=f"env-{region}",
                name=region,
                provider="aws",
                region=region,
                note="메모",
                components=(
                    CostComponent(
                        name="작은 자원",
                        kind=CostComponentKind.OTHER,
                        price="tiny",
                        quantity=1,
                        unit_label="개",
                    ),
                ),
                match_kind="aws",
                match_region=region,
            )
            for region in regions
        ),
        shared=(),
        billing=(),
    )

    # when
    costs = build_cloud_costs(catalog, [_aws(f"env-{r}", r) for r in regions], NOW)

    # then
    assert [view.hourly_usd for view in costs.environments] == [Decimal("0.0000")] * 3
    assert costs.total_hourly_usd == Decimal("0.0001")


def test_build_excludes_unbilled_kinds_with_their_notes() -> None:
    catalog = load_cost_catalog()
    onprem = _environment("onprem", "onprem", {"location": "lab", "kube_context": "k3d"})
    other = _environment("azure-a", "azure", {"region": "koreacentral"})

    costs = build_cloud_costs(catalog, [onprem, other], NOW)

    assert costs.environments == ()
    assert costs.shared == ()
    assert [(item.environment_name, item.kind, item.note) for item in costs.excluded] == [
        ("onprem", "onprem", ONPREM_NOTE),
        ("azure-a", "azure", UNBILLED_KIND_NOTE),
    ]
    assert ONPREM_NOTE == "온프레: 자체 장비, 청구 없음"


def test_build_unmatched_environment_has_null_amounts_and_no_shared() -> None:
    catalog = load_cost_catalog()

    costs = build_cloud_costs(catalog, [_aws("aws-virginia", "us-east-1")], NOW)

    view = costs.environments[0]
    assert (view.hourly_usd, view.daily_usd, view.components) == (None, None, ())
    assert view.note == NO_INVENTORY_NOTE
    assert costs.shared == ()
    assert (costs.total_hourly_usd, costs.total_daily_usd) == (Decimal(0), Decimal(0))


def test_build_with_no_environments_keeps_billing() -> None:
    catalog = load_cost_catalog()

    costs = build_cloud_costs(catalog, [], NOW)

    assert (costs.environments, costs.shared, costs.excluded) == ((), (), ())
    assert costs.total_hourly_usd == 0
    assert costs.billing == catalog.billing


# --- AC-2: 잘못된 표는 예외 ---


def test_load_valid_small_catalog_succeeds(tmp_path: Path) -> None:
    catalog = load_cost_catalog(*_write(tmp_path))

    assert set(catalog.prices) == {"p.hour", "p.disk"}
    assert catalog.prices["p.hour"].unit_price_usd == Decimal("0.5")
    # 73 GiB × 0.1 ÷ 730
    assert estimate_inventory(catalog, catalog.shared[0]).exact_hourly == Decimal("0.01")


@pytest.mark.parametrize(
    ("prices", "inventory", "expected_field"),
    [
        (
            VALID_PRICES,
            VALID_INVENTORY.replace('price = "p.hour"', 'price = "p.none"'),
            "price_key",
        ),
        (VALID_PRICES.replace('"hour"', '"minute"'), VALID_INVENTORY, "invalid_fields"),
        (VALID_PRICES, VALID_INVENTORY.replace("quantity = 2", "quantity = -1"), "invalid_fields"),
        (VALID_PRICES, VALID_INVENTORY.replace("quantity = 2", "quantity = 1.5"), "invalid_fields"),
        (VALID_PRICES.replace("0.5", "-0.5"), VALID_INVENTORY, "invalid_fields"),
        (VALID_PRICES + DUPLICATE_PRICE, VALID_INVENTORY, "key"),
        (VALID_PRICES, VALID_INVENTORY.replace('key = "shared-a"', 'key = "env-a"'), "key"),
        (
            VALID_PRICES,
            VALID_INVENTORY.replace('kind = "STORAGE"', 'kind = "GPU"'),
            "invalid_fields",
        ),
        (
            VALID_PRICES.replace('unit = "hour"', 'unit = "hour"\nextra = 1'),
            VALID_INVENTORY,
            "invalid_fields",
        ),
        (
            VALID_PRICES.replace("verified = true", "verified = 1"),
            VALID_INVENTORY,
            "invalid_fields",
        ),
        (
            VALID_PRICES.replace('key = "p.hour"', 'key = "p.hour"\nkey = "p.hour"'),
            VALID_INVENTORY,
            "exc_type",
        ),
        ("[[prices]\n", VALID_INVENTORY, "exc_type"),
    ],
    ids=[
        "unknown-price-key",
        "unknown-unit",
        "negative-quantity",
        "fractional-quantity",
        "negative-price",
        "duplicate-price-key",
        "duplicate-inventory-key",
        "unknown-component-kind",
        "unknown-field",
        "verified-not-bool",
        "toml-duplicate-key",
        "toml-syntax-error",
    ],
)
def test_load_invalid_tables_raise(
    tmp_path: Path, prices: str, inventory: str, expected_field: str
) -> None:
    paths = _write(tmp_path, prices=prices, inventory=inventory)

    with pytest.raises(CostCatalogError) as caught:
        load_cost_catalog(*paths)

    assert expected_field in caught.value.fields
    assert caught.value.status_code == 500


def test_load_rejects_two_inventories_matching_the_same_environment(tmp_path: Path) -> None:
    second = VALID_INVENTORY.split("[[shared]]")[0].replace('key = "env-a"', 'key = "env-b"')
    paths = _write(tmp_path, inventory=VALID_INVENTORY + second)

    with pytest.raises(CostCatalogError) as caught:
        load_cost_catalog(*paths)

    assert caught.value.fields["key"] == "aws:r-1"


def test_load_rejects_inventory_without_components(tmp_path: Path) -> None:
    without_components = VALID_INVENTORY.split("[[shared.components]]")[0]
    paths = _write(tmp_path, inventory=without_components)

    with pytest.raises(CostCatalogError):
        load_cost_catalog(*paths)


def test_load_missing_file_raises(tmp_path: Path) -> None:
    prices_path, inventory_path, _ = _write(tmp_path)

    with pytest.raises(CostCatalogError):
        load_cost_catalog(prices_path, inventory_path, tmp_path / "none.json")


@pytest.mark.parametrize(
    "billing",
    [
        "{not json",
        '{"providers": [], "extra": 1}',
        '{"providers": [' + _BILLING_AWS_EMPTY.replace("ACCOUNT", "PROJECT") + "]}",
        '{"providers": ['
        + _BILLING_AWS_EMPTY.replace("[]", f"[{_BILLING_DAY_5},{_BILLING_DAY_4}]")
        + "]}",
        '{"providers": ['
        + _BILLING_AWS_EMPTY.replace("[]", f"[{_BILLING_DAY_4},{_BILLING_DAY_4}]")
        + "]}",
        '{"providers": [' + _BILLING_AWS_EMPTY + "," + _BILLING_AWS_EMPTY + "]}",
    ],
    ids=[
        "bad-json",
        "unknown-field",
        "unknown-scope",
        "unsorted-days",
        "duplicate-day",
        "duplicate-provider",
    ],
)
def test_load_billing_file_rejects_invalid_content(tmp_path: Path, billing: str) -> None:
    path = tmp_path / "billing.json"
    path.write_text(billing, encoding="utf-8")

    with pytest.raises(CostCatalogError):
        load_billing_file(path)
