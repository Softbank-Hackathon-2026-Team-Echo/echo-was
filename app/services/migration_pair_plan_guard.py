"""계획 생성·승인 경계에서 같은 DB쌍 snapshot을 고정/재검사한다."""

from datetime import datetime

from app.enums import MigrationPlanMode
from app.models import MigrationPlan, Service, ServiceEnvironment
from app.schemas.migration_capability import MigrationPairContext, MigrationPairSnapshot, Operation
from app.services.migration_pair_registry import MigrationPairRegistry, blocked
from app.services.migration_plan_support import has_db_cutover

PAIR_FINGERPRINT_KEY = "migrationPair"


class MigrationPairPlanGuard:
    def __init__(self, registry: MigrationPairRegistry) -> None:
        self._registry = registry

    def snapshot(
        self,
        service: Service,
        source: ServiceEnvironment,
        target: ServiceEnvironment,
        plan: MigrationPlan,
        now: datetime,
    ) -> MigrationPairSnapshot:
        if (
            source.service_id != service.id
            or target.service_id != service.id
            or source.environment.organization_id != service.organization_id
            or target.environment.organization_id != service.organization_id
        ):
            raise blocked("DB_BINDING_SCOPE_MISMATCH")
        context = MigrationPairContext(
            organization_id=service.organization_id,
            service_id=service.id,
            source_environment_id=source.environment_id,
            target_environment_id=target.environment_id,
            source_deployment_id=source.current_deployment_id,
            target_deployment_id=target.current_deployment_id,
        )
        operation: Operation = "appTraffic"
        if has_db_cutover(plan.steps):
            operation = "dbCutover"
            if plan.mode == MigrationPlanMode.EMERGENCY:
                operation = "disasterPromote"
            elif plan.mode == MigrationPlanMode.FAILBACK:
                operation = "failback"
        return self._registry.snapshot(context, operation, now)

    def check(
        self,
        service: Service,
        source: ServiceEnvironment,
        target: ServiceEnvironment,
        plan: MigrationPlan,
        now: datetime,
    ) -> None:
        current = self.snapshot(service, source, target, plan, now)
        if plan.fingerprint.get(PAIR_FINGERPRINT_KEY) != current.model_dump(mode="json"):
            raise blocked("DB_PAIR_SNAPSHOT_CHANGED")

    def check_execution(
        self,
        service: Service,
        source: ServiceEnvironment,
        target: ServiceEnvironment,
        plan: MigrationPlan,
        now: datetime,
    ) -> None:
        self.check(service, source, target, plan, now)
        # 단계1은 gate 연결까지다. 현 엔진·이미지·VAP에는 명시 쌍 계약이 없다.
        raise blocked("DB_PAIR_WORKER_NOT_READY")
