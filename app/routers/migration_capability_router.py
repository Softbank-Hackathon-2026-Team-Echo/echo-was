from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.auth.guards import ViewerAccess
from app.dependencies import get_migration_capability_service
from app.schemas.migration_capability import MigrationPairCapabilitiesResponse
from app.schemas.response import ApiResponse
from app.services.migration_capability_service import MigrationCapabilityService

router = APIRouter(prefix="/api/v1/orgs/{org_id}/services", tags=["migration-capability"])
Capabilities = Annotated[MigrationCapabilityService, Depends(get_migration_capability_service)]


@router.get(
    "/{service_id}/migration-capabilities",
    response_model=ApiResponse[MigrationPairCapabilitiesResponse],
)
async def get_migration_capabilities(
    access: ViewerAccess,
    service_id: UUID,
    capabilities: Capabilities,
    source_environment_id: Annotated[UUID, Query(alias="sourceEnvironmentId")],
    target_environment_id: Annotated[UUID, Query(alias="targetEnvironmentId")],
) -> ApiResponse[MigrationPairCapabilitiesResponse]:
    result = await capabilities.get_capabilities(
        access.organization_id, service_id, source_environment_id, target_environment_id
    )
    return ApiResponse(data=result)
