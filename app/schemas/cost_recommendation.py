"""비용 시나리오 응답. 내부 Decimal을 마지막 JSON 경계에서만 number로 변환한다."""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import ConfigDict

from app.enums import DeploymentStatus, ServiceEnvironmentRole
from app.schemas.cost import BillingResponse
from app.schemas.response import ApiModel
from app.services.cost_catalog import PriceUnit
from app.services.cost_recommendation import CostRecommendationView


class _ReadModel(ApiModel):
    model_config = ConfigDict(from_attributes=True)


class CostPlacementResponse(_ReadModel):
    environment_id: uuid.UUID
    environment_name: str
    kind: str
    service_environment_id: uuid.UUID
    role: ServiceEnvironmentRole
    traffic_weight: int
    traffic_applied_at: datetime | None
    current_deployment_id: uuid.UUID | None
    deployment_status: DeploymentStatus | None
    deployment_recorded_at: datetime | None


class CostConstraintResponse(_ReadModel):
    kind: str
    status: Literal["UNKNOWN"]
    reason: str
    observed_at: datetime | None


class CostAlternativeResponse(_ReadModel):
    environment_id: uuid.UUID
    environment_name: str
    kind: str
    scenario: Literal["TRAFFIC_ONLY"]
    status: Literal["NEEDS_EVIDENCE"]
    has_current_deployment: bool
    current_monthly_usd: float | None
    alternative_monthly_usd: float | None
    fixed_cost_savings_monthly_usd: float
    net_savings_monthly_usd: float | None
    one_time_migration_usd: float | None
    break_even_months: float | None
    recovery_seconds: float | None
    is_safe_recommendation: Literal[False]
    retained_inventory_keys: list[str]
    added_resource_ids: list[str]
    removed_resource_ids: list[str]
    constraints: list[CostConstraintResponse]
    assumptions: list[str]
    missing_reasons: list[str]


class CostPriceEvidenceResponse(ApiModel):
    name: str
    price_key: str
    quantity: int
    unit: str
    unit_price_usd: float
    price_unit: PriceUnit
    price_source: str
    price_checked_at: date
    price_effective_date: date
    price_verified: bool
    formula: str


class CostInventoryEvidenceResponse(ApiModel):
    inventory_key: str
    environment_ids: list[uuid.UUID]
    monthly_usd: float
    note: str
    components: list[CostPriceEvidenceResponse]


class CostCatalogEstimateResponse(ApiModel):
    source: Literal["ESTIMATE"] = "ESTIMATE"
    scope: Literal["ORGANIZATION_CATALOG_PARTIAL"] = "ORGANIZATION_CATALOG_PARTIAL"
    monthly_usd: float | None
    inventories: list[CostInventoryEvidenceResponse]
    unpriced_environment_ids: list[uuid.UUID]
    note: str
    revision: str | None = None


class CostRecommendationsResponse(ApiModel):
    organization_id: uuid.UUID
    service_id: uuid.UUID
    service_status: str
    computed_at: datetime
    placement_read_at: datetime
    inventory_observed_at: datetime | None
    currency: Literal["USD"] = "USD"
    hours_per_month: int
    freshness: Literal["UNVERIFIED"] = "UNVERIFIED"
    confidence: Literal["INSUFFICIENT_DATA"] = "INSUFFICIENT_DATA"
    current_monthly_usd: float | None
    current_placement: list[CostPlacementResponse]
    alternatives: list[CostAlternativeResponse]
    catalog_estimate: CostCatalogEstimateResponse
    billing: list[BillingResponse]
    missing_reasons: list[str]

    @classmethod
    def from_view(cls, view: CostRecommendationView) -> "CostRecommendationsResponse":
        inventories = []
        for estimate in view.catalog_estimate.inventories:
            components = []
            for component in estimate.inventory.components:
                price = view.catalog.prices[component.price]
                components.append(
                    CostPriceEvidenceResponse(
                        name=component.name,
                        price_key=component.price,
                        quantity=component.quantity,
                        unit=component.unit_label,
                        unit_price_usd=float(price.unit_price_usd),
                        price_unit=price.unit,
                        price_source=price.source,
                        price_checked_at=price.checked_at,
                        price_effective_date=price.effective_date,
                        price_verified=price.verified,
                        formula="quantity * unitPriceUsd * 730"
                        if price.unit == "hour"
                        else "quantity * unitPriceUsd",
                    )
                )
            inventories.append(
                CostInventoryEvidenceResponse(
                    inventory_key=estimate.inventory.key,
                    environment_ids=list(estimate.environment_ids),
                    monthly_usd=float(estimate.exact_monthly_usd),
                    note=estimate.inventory.note,
                    components=components,
                )
            )
        return cls(
            organization_id=view.organization_id,
            service_id=view.service_id,
            service_status=view.service_status,
            computed_at=view.computed_at,
            placement_read_at=view.placement_read_at,
            inventory_observed_at=view.inventory_observed_at,
            hours_per_month=view.hours_per_month,
            current_monthly_usd=None
            if view.current_monthly_usd is None
            else float(view.current_monthly_usd),
            current_placement=[
                CostPlacementResponse.model_validate(item) for item in view.current_placement
            ],
            alternatives=[
                CostAlternativeResponse.model_validate(item) for item in view.alternatives
            ],
            catalog_estimate=CostCatalogEstimateResponse(
                monthly_usd=float(view.catalog_estimate.monthly_usd)
                if view.catalog_estimate.monthly_usd is not None
                else None,
                inventories=inventories,
                unpriced_environment_ids=list(view.catalog_estimate.unpriced_environment_ids),
                note="과거 목록가 snapshot의 조직 부분 추정. 서비스 귀속·실자원 ID·현재성 미확인. "
                "공용/같은 inventory key는 한 번만 산입. 온프레 미산정은 무료가 아님. "
                "계정 청구·크레딧과 합산하거나 서비스 절감액으로 쓰지 않음.",
            ),
            billing=[BillingResponse.from_entry(item) for item in view.billing],
            missing_reasons=list(view.missing_reasons),
        )
