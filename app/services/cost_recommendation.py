"""조회 전용 비용 시나리오. 금액은 Decimal, 실행·자원 정리는 하지 않는다."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.enums import DeploymentStatus, ServiceEnvironmentRole
from app.models import Environment
from app.repositories.deployment_repository import DeploymentRepository
from app.repositories.environment_repository import EnvironmentRepository
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.repositories.service_repository import ServiceRepository
from app.services.cost_catalog import BillingProvider, CostCatalog, SharedInventory
from app.services.cost_service import BILLED_KINDS
from app.services.service_view import role_of

HOURS_PER_MONTH = Decimal(730)
MONEY_QUANTUM = Decimal("0.01")


@dataclass(frozen=True)
class InventoryEstimateView:
    inventory: SharedInventory
    environment_ids: tuple[uuid.UUID, ...]
    exact_monthly_usd: Decimal


@dataclass(frozen=True)
class CatalogEstimateView:
    monthly_usd: Decimal | None
    inventories: tuple[InventoryEstimateView, ...]
    unpriced_environment_ids: tuple[uuid.UUID, ...]


def estimate_catalog(
    catalog: CostCatalog, environments: Sequence[Environment]
) -> CatalogEstimateView:
    """조직 목록가 부분 추정. 키는 자원 ID가 아니며 서비스 귀속·정리 근거로 쓰지 않는다."""
    inventories_by_key: dict[str, SharedInventory] = {}
    environment_ids_by_key: dict[str, list[uuid.UUID]] = {}
    unpriced = []
    for environment in environments:
        inventory = (
            catalog.find_environment_inventory(environment.kind, environment.config.get("region"))
            if environment.kind in BILLED_KINDS
            else None
        )
        if inventory is None:
            unpriced.append(environment.id)
            continue
        inventories_by_key[inventory.key] = inventory
        environment_ids_by_key.setdefault(inventory.key, []).append(environment.id)
    if inventories_by_key:
        for shared_inventory in catalog.shared:
            inventories_by_key.setdefault(shared_inventory.key, shared_inventory)
    estimates = tuple(
        InventoryEstimateView(
            inventory=inventory,
            environment_ids=tuple(environment_ids_by_key.get(key, ())),
            # 월 단가의 시간 환산 왕복은 반 센트 경계를 훼손하므로 원 단위에서 계산한다.
            exact_monthly_usd=sum(
                (
                    component.quantity
                    * catalog.prices[component.price].unit_price_usd
                    * (HOURS_PER_MONTH if catalog.prices[component.price].unit == "hour" else 1)
                    for component in inventory.components
                ),
                Decimal(0),
            ),
        )
        for key, inventory in sorted(inventories_by_key.items())
    )
    return CatalogEstimateView(
        monthly_usd=sum((item.exact_monthly_usd for item in estimates), Decimal(0)).quantize(
            MONEY_QUANTUM, rounding=ROUND_HALF_UP
        )
        if estimates
        else None,
        inventories=estimates,
        unpriced_environment_ids=tuple(unpriced),
    )


@dataclass(frozen=True)
class SavingsView:
    fixed_savings: Decimal | None
    net_savings: Decimal | None
    one_time: Decimal | None
    break_even_months: Decimal | None


def calculate_savings(
    *,
    current_fixed: Decimal | None,
    alternative_fixed: Decimal | None,
    current_recurring: Decimal | None,
    alternative_recurring: Decimal | None,
    one_time: Decimal | None,
    comparable: bool,
    all_fixed_resources_retained: bool = False,
) -> SavingsView:
    """동일 기간·범위·단가 기준의 비용만 비교한다. 미상은 0으로 대체하지 않는다."""
    amounts = (current_fixed, alternative_fixed, current_recurring, alternative_recurring, one_time)
    if any(value is not None and (not value.is_finite() or value < 0) for value in amounts):
        raise ValueError("cost evidence must be finite and non-negative")
    fixed = (
        current_fixed - alternative_fixed
        if comparable and current_fixed is not None and alternative_fixed is not None
        else None
    )
    if comparable and all_fixed_resources_retained:
        # 동일 자원을 유지하는 가정의 차액은 총원가를 몰라도 0이다. 무료라는 뜻은 아니다.
        fixed = Decimal(0)
    net = (
        fixed + current_recurring - alternative_recurring
        if fixed is not None and current_recurring is not None and alternative_recurring is not None
        else None
    )
    return SavingsView(
        fixed_savings=fixed,
        net_savings=net,
        one_time=one_time,
        break_even_months=one_time / net
        if comparable and net is not None and net > 0 and one_time is not None
        else None,
    )


MISSING_REASONS = (
    "SERVICE_RESOURCE_ATTRIBUTION_UNKNOWN",
    "INVENTORY_OBSERVED_AT_UNKNOWN",
    "RECURRING_TRANSFER_COST_UNKNOWN",
    "ONE_TIME_MIGRATION_COST_UNKNOWN",
    "PAIR_SUPPORT_NOT_EVALUATED",
    "PAIR_SECURITY_NOT_EVALUATED",
    "DATA_RESIDENCY_NOT_EVALUATED",
    "RECOVERY_TIME_NOT_MEASURED",
    "CATALOG_FRESHNESS_UNVERIFIED",
)


@dataclass(frozen=True)
class PlacementView:
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


@dataclass(frozen=True)
class ConstraintView:
    kind: str
    status: str
    reason: str
    observed_at: datetime | None


@dataclass(frozen=True)
class AlternativeView:
    environment_id: uuid.UUID
    environment_name: str
    kind: str
    scenario: str
    status: str
    has_current_deployment: bool
    current_monthly_usd: Decimal | None
    alternative_monthly_usd: Decimal | None
    fixed_cost_savings_monthly_usd: Decimal
    net_savings_monthly_usd: Decimal | None
    one_time_migration_usd: Decimal | None
    break_even_months: Decimal | None
    recovery_seconds: Decimal | None
    is_safe_recommendation: bool
    retained_inventory_keys: tuple[str, ...]
    added_resource_ids: tuple[str, ...]
    removed_resource_ids: tuple[str, ...]
    constraints: tuple[ConstraintView, ...]
    assumptions: tuple[str, ...]
    missing_reasons: tuple[str, ...]


@dataclass(frozen=True)
class CostRecommendationView:
    organization_id: uuid.UUID
    service_id: uuid.UUID
    service_status: str
    computed_at: datetime
    placement_read_at: datetime
    inventory_observed_at: datetime | None
    currency: str
    hours_per_month: int
    freshness: str
    confidence: str
    current_monthly_usd: Decimal | None
    current_placement: tuple[PlacementView, ...]
    alternatives: tuple[AlternativeView, ...]
    catalog_estimate: CatalogEstimateView
    catalog: CostCatalog
    billing: tuple[BillingProvider, ...]
    missing_reasons: tuple[str, ...]


class CostRecommendationService:
    def __init__(self, session: AsyncSession, clock: Clock, catalog: CostCatalog) -> None:
        self._session = session
        self._clock = clock
        self._catalog = catalog
        self._service_repository = ServiceRepository(session)
        self._environment_repository = EnvironmentRepository(session)
        self._connection_repository = ServiceEnvironmentRepository(session)
        self._deployment_repository = DeploymentRepository(session)

    async def get_recommendations(
        self, organization_id: uuid.UUID, service_id: uuid.UUID
    ) -> CostRecommendationView:
        """조직 소유 서비스의 DB 기록과 저장 단가만 읽는다. 게이트·실행기는 호출하지 않는다."""
        async with self._session.begin():
            service = await self._service_repository.get_live_by_id(organization_id, service_id)
            environments = await self._environment_repository.search_all_live(organization_id)
            live_ids = {environment.id for environment in environments}
            connections = [
                item
                for item in await self._connection_repository.search_live_by_service(service.id)
                if item.environment_id in live_ids
            ]
            deployments = {
                item.id: item
                for item in await self._deployment_repository.search_by_ids(
                    [
                        item.current_deployment_id
                        for item in connections
                        if item.current_deployment_id
                    ]
                )
            }
            placements = tuple(
                PlacementView(
                    environment_id=item.environment_id,
                    environment_name=item.environment.name,
                    kind=item.environment.kind,
                    service_environment_id=item.id,
                    role=role_of(item),
                    traffic_weight=item.traffic_weight,
                    traffic_applied_at=item.traffic_applied_at,
                    current_deployment_id=item.current_deployment_id,
                    deployment_status=DeploymentStatus(deployment.status)
                    if (
                        deployment := deployments.get(item.current_deployment_id)
                        if item.current_deployment_id
                        else None
                    )
                    else None,
                    deployment_recorded_at=deployment.created_at if deployment else None,
                )
                for item in connections
            )
            service_status = service.status
        placement_read_at = self._clock.now()
        estimate = estimate_catalog(self._catalog, environments)
        serving_ids = {item.environment_id for item in placements if item.traffic_weight > 0}
        deployed_ids = {item.environment_id for item in placements if item.current_deployment_id}
        constraints = tuple(
            ConstraintView(kind=kind, status="UNKNOWN", reason=reason, observed_at=None)
            for kind, reason in (
                ("SUPPORT", "PAIR_SUPPORT_NOT_EVALUATED"),
                ("SECURITY", "PAIR_SECURITY_NOT_EVALUATED"),
                ("DATA_RESIDENCY", "DATA_RESIDENCY_NOT_EVALUATED"),
                ("RECOVERY", "RECOVERY_TIME_NOT_MEASURED"),
            )
        )
        traffic_only = calculate_savings(
            current_fixed=None,
            alternative_fixed=None,
            current_recurring=None,
            alternative_recurring=None,
            one_time=None,
            comparable=True,
            all_fixed_resources_retained=True,
        )
        assert traffic_only.fixed_savings is not None
        alternatives = tuple(
            AlternativeView(
                environment_id=environment.id,
                environment_name=environment.name,
                kind=environment.kind,
                scenario="TRAFFIC_ONLY",
                status="NEEDS_EVIDENCE",
                has_current_deployment=environment.id in deployed_ids,
                current_monthly_usd=None,
                alternative_monthly_usd=None,
                fixed_cost_savings_monthly_usd=traffic_only.fixed_savings,
                net_savings_monthly_usd=traffic_only.net_savings,
                one_time_migration_usd=traffic_only.one_time,
                break_even_months=traffic_only.break_even_months,
                recovery_seconds=None,
                is_safe_recommendation=False,
                retained_inventory_keys=tuple(item.inventory.key for item in estimate.inventories),
                added_resource_ids=(),
                removed_resource_ids=(),
                constraints=constraints,
                assumptions=("TRAFFIC_ONLY_ALL_RESOURCES_RETAINED", "NO_RESOURCE_CLEANUP_ASSUMED"),
                missing_reasons=(
                    *MISSING_REASONS,
                    *(("TARGET_NOT_DEPLOYED",) if environment.id not in deployed_ids else ()),
                    *(("ONPREM_COST_UNASSESSED",) if environment.kind == "onprem" else ()),
                    *(("NO_SERVING_PLACEMENT",) if not serving_ids else ()),
                ),
            )
            for environment in environments
            if environment.id not in serving_ids
        )
        return CostRecommendationView(
            organization_id=organization_id,
            service_id=service_id,
            service_status=service_status,
            computed_at=self._clock.now(),
            placement_read_at=placement_read_at,
            inventory_observed_at=None,
            currency="USD",
            hours_per_month=730,
            freshness="UNVERIFIED",
            confidence="INSUFFICIENT_DATA",
            current_monthly_usd=None,
            current_placement=placements,
            alternatives=alternatives,
            catalog_estimate=estimate,
            catalog=self._catalog,
            billing=self._catalog.billing,
            missing_reasons=(
                *MISSING_REASONS,
                *(("NO_ALTERNATIVE_ENVIRONMENT",) if not alternatives else ()),
                *(("NO_SERVING_PLACEMENT",) if not serving_ids else ()),
            ),
        )
