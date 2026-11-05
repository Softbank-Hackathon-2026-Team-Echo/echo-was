"""비용 데이터 파일(단가·자원 목록·청구 실측)을 읽어 검사한 카탈로그(운영 1).

프로세스당 한 번 읽는다(get_cost_catalog). 잘못된 표가 조용히 0원이 되지 않게 없는 단가 키·모르는
단위·음수 수량·중복 키는 예외로 올린다. 외부 호출은 없다.
"""

import datetime
import json
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic.alias_generators import to_camel

from app.core.exceptions import AppError
from app.enums import CostComponentKind

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
PRICES_PATH = DATA_DIR / "cloud_prices.toml"
INVENTORY_PATH = DATA_DIR / "cloud_inventory.toml"
BILLING_PATH = DATA_DIR / "cloud_billing.json"

type PriceUnit = Literal["hour", "GiB-month"]


class CostCatalogError(AppError):
    """비용 데이터 파일이 잘못됐다. 사용자가 고칠 수 없는 서버 쪽 문제다(500)."""

    code = "COST_CATALOG_INVALID"
    status_code = 500


class _CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CloudPrice(_CatalogModel):
    key: str = Field(min_length=1)
    unit_price_usd: Decimal = Field(ge=0)
    unit: PriceUnit
    description: str
    source: str
    effective_date: datetime.date
    checked_at: datetime.date
    verified: bool = Field(strict=True)
    note: str | None = None


class CostComponent(_CatalogModel):
    name: str
    kind: CostComponentKind
    price: str
    quantity: int = Field(ge=0, strict=True)
    unit_label: str


class SharedInventory(_CatalogModel):
    key: str = Field(min_length=1)
    name: str
    provider: str
    region: str
    note: str
    components: tuple[CostComponent, ...] = Field(min_length=1)


class EnvironmentInventory(SharedInventory):
    match_kind: str
    match_region: str


class _PricesFile(_CatalogModel):
    prices: tuple[CloudPrice, ...]


class _InventoryFile(_CatalogModel):
    environments: tuple[EnvironmentInventory, ...]
    shared: tuple[SharedInventory, ...]


class _BillingModel(BaseModel):
    """청구 파일은 스크립트가 쓰고 응답과 같은 camelCase 키를 쓴다."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
    )


class BillingDay(_BillingModel):
    date: datetime.date
    usage_usd: float
    credits_usd: float
    net_usd: float
    estimated: bool


class BillingProvider(_BillingModel):
    provider: str
    scope: Literal["ACCOUNT"]
    label: str
    source: str | None
    fetched_at: AwareDatetime | None
    note: str | None
    days: tuple[BillingDay, ...]

    @model_validator(mode="after")
    def _days_are_sorted_and_unique(self) -> "BillingProvider":
        dates = [day.date for day in self.days]
        if dates != sorted(set(dates)):
            raise ValueError("days must be sorted by date without duplicates")
        return self


class BillingFile(_BillingModel):
    providers: tuple[BillingProvider, ...]


@dataclass(frozen=True)
class CostCatalog:
    prices: Mapping[str, CloudPrice]
    environments: tuple[EnvironmentInventory, ...]
    shared: tuple[SharedInventory, ...]
    billing: tuple[BillingProvider, ...]

    def find_environment_inventory(
        self, kind: str, region: str | None
    ) -> EnvironmentInventory | None:
        for inventory in self.environments:
            if (inventory.match_kind, inventory.match_region) == (kind, region):
                return inventory
        return None


def load_billing_file(path: Path) -> BillingFile:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CostCatalogError(
            "cost data file unreadable", file=path.name, exc_type=type(exc).__name__
        ) from exc
    billing = _validated(BillingFile, document, path)
    _reject_duplicates((entry.provider for entry in billing.providers), path)
    return billing


def load_cost_catalog(
    prices_path: Path = PRICES_PATH,
    inventory_path: Path = INVENTORY_PATH,
    billing_path: Path = BILLING_PATH,
) -> CostCatalog:
    prices_file = _validated(_PricesFile, _read_toml(prices_path), prices_path)
    inventory_file = _validated(_InventoryFile, _read_toml(inventory_path), inventory_path)
    _reject_duplicates((price.key for price in prices_file.prices), prices_path)
    inventories = (*inventory_file.environments, *inventory_file.shared)
    _reject_duplicates((inventory.key for inventory in inventories), inventory_path)
    _reject_duplicates(
        (f"{entry.match_kind}:{entry.match_region}" for entry in inventory_file.environments),
        inventory_path,
    )
    prices = {price.key: price for price in prices_file.prices}
    for inventory in inventories:
        for component in inventory.components:
            if component.price not in prices:
                raise CostCatalogError(
                    "cost component refers to an unknown price",
                    file=inventory_path.name,
                    inventory_key=inventory.key,
                    price_key=component.price,
                )
    return CostCatalog(
        prices=prices,
        environments=inventory_file.environments,
        shared=inventory_file.shared,
        billing=load_billing_file(billing_path).providers,
    )


@lru_cache
def get_cost_catalog() -> CostCatalog:
    """실제 데이터 파일로 만든 카탈로그. 시험은 이 의존성을 작은 카탈로그로 바꾼다."""
    return load_cost_catalog()


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        # float 대신 Decimal 로 읽어 단가 표기를 그대로 계산에 쓴다.
        return tomllib.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    except (OSError, ValueError) as exc:
        raise CostCatalogError(
            "cost data file unreadable", file=path.name, exc_type=type(exc).__name__
        ) from exc


def _validated[T: BaseModel](model: type[T], document: object, path: Path) -> T:
    try:
        return model.model_validate(document)
    except ValidationError as exc:
        invalid_fields = [".".join(str(part) for part in error["loc"]) for error in exc.errors()]
        raise CostCatalogError(
            "cost data file invalid", file=path.name, invalid_fields=invalid_fields
        ) from None


def _reject_duplicates(keys: Iterable[str], path: Path) -> None:
    seen: set[str] = set()
    for key in keys:
        if key in seen:
            raise CostCatalogError("cost data file has a duplicate key", file=path.name, key=key)
        seen.add(key)
