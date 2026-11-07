"""조직·서비스 조회를 통과한 환경쌍만 지원 능력을 조회한다. 네트워크 수집 없음."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.repositories.environment_repository import EnvironmentRepository
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.repositories.service_repository import ServiceRepository
from app.schemas.migration_capability import MigrationPairCapabilitiesResponse, MigrationPairContext
from app.services.migration_pair_registry import MigrationPairRegistry


class MigrationCapabilityService:
    def __init__(
        self,
        session: AsyncSession,
        registry: MigrationPairRegistry,
        clock: Clock,
        *,
        guard_enabled: bool = False,
    ) -> None:
        self._session = session
        self._registry = registry
        self._clock = clock
        self._guard_enabled = guard_enabled

    async def get_capabilities(
        self,
        organization_id: UUID,
        service_id: UUID,
        source_environment_id: UUID,
        target_environment_id: UUID,
    ) -> MigrationPairCapabilitiesResponse:
        """미등록·미확인은 supported=false다. 다른 조직 자원은 기존 404를 유지한다."""
        async with self._session.begin():
            service = await ServiceRepository(self._session).get_live_by_id(
                organization_id, service_id
            )
            environments = EnvironmentRepository(self._session)
            await environments.get_live_by_id(organization_id, source_environment_id)
            await environments.get_live_by_id(organization_id, target_environment_id)
            connections = await ServiceEnvironmentRepository(self._session).search_live_by_service(
                service.id
            )
            deployment_by_environment = {
                item.environment_id: item.current_deployment_id for item in connections
            }
            context = MigrationPairContext(
                organization_id=organization_id,
                service_id=service_id,
                source_environment_id=source_environment_id,
                target_environment_id=target_environment_id,
                source_deployment_id=deployment_by_environment.get(source_environment_id),
                target_deployment_id=deployment_by_environment.get(target_environment_id),
            )
        result = self._registry.capabilities(context, self._clock.now())
        # 등록표의 잠재 지원과 실제 실행 준비를 혼동하지 않는다.
        for capability in (
            result.app_traffic,
            result.db_cutover,
            result.disaster_promote,
            result.failback,
        ):
            if capability.supported:
                capability.supported = False
                capability.status = "UNSUPPORTED"
                capability.reasons = ["DB_PAIR_WORKER_NOT_READY"]
        return result.model_copy(
            update={
                "creation_gate_enforced": self._guard_enabled,
                "approval_gate_enforced": self._guard_enabled,
                "worker_gate_enforced": self._guard_enabled,
                "execution_ready": False,
            }
        )
