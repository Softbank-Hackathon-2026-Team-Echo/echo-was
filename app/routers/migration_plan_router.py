import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.auth.guards import HumanDeveloperAccess, ViewerAccess
from app.dependencies import get_migration_plan_result_service, get_migration_plan_service
from app.enums import MigrationPlanStatus
from app.schemas.migration_plan import (
    CreateMigrationPlanRequest,
    MigrationLogPageResponse,
    MigrationPlanDetailResponse,
    MigrationPlanSummaryResponse,
)
from app.schemas.response import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, ApiResponse, Page
from app.services.migration_plan_result import MigrationPlanResultService
from app.services.migration_plan_service import CreateMigrationPlanCommand, MigrationPlanService

# 조회·로그는 뷰어(에이전트 가능). 만들기·승인·거절·취소·중단은 개발자 이상 사람
# (에이전트는 승인할 수 없다).
router = APIRouter(prefix="/api/v1/orgs/{org_id}", tags=["migration-plan"])

MigrationPlans = Annotated[MigrationPlanService, Depends(get_migration_plan_service)]
MigrationPlanResults = Annotated[
    MigrationPlanResultService, Depends(get_migration_plan_result_service)
]

DEFAULT_LOG_PAGE_SIZE = 200
MAX_LOG_PAGE_SIZE = 500


@router.post(
    "/services/{service_id}/migration-plans",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[MigrationPlanDetailResponse],
)
async def create_migration_plan(
    access: HumanDeveloperAccess,
    service_id: uuid.UUID,
    request: CreateMigrationPlanRequest,
    migration_plans: MigrationPlans,
) -> ApiResponse[MigrationPlanDetailResponse]:
    conditions = request.rollback_conditions
    simulate = request.simulate
    view = await migration_plans.create_plan(
        access.organization_id,
        service_id,
        access.user.id,
        CreateMigrationPlanCommand(
            target_environment_id=request.target_environment_id,
            required_requests=request.required_requests,
            max_wait_seconds=request.max_wait_seconds,
            max_error_rate_percent=conditions.max_error_rate_percent,
            max_p95_ms=conditions.max_p95_ms,
            max_replication_lag_seconds=conditions.max_replication_lag_seconds,
            simulate_fail_at_step=simulate.fail_at_step if simulate is not None else None,
            simulate_reason=simulate.reason if simulate is not None else None,
            include_db_cutover=request.include_cutover,
        ),
    )
    return ApiResponse(data=MigrationPlanDetailResponse.from_view(view))


@router.get(
    "/services/{service_id}/migration-plans",
    response_model=ApiResponse[Page[MigrationPlanSummaryResponse]],
)
async def search_migration_plans(
    access: ViewerAccess,
    service_id: uuid.UUID,
    migration_plans: MigrationPlans,
    page: Annotated[int, Query(ge=0)] = 0,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    status_: Annotated[list[MigrationPlanStatus] | None, Query(alias="status")] = None,
) -> ApiResponse[Page[MigrationPlanSummaryResponse]]:
    result = await migration_plans.search_plans(
        access.organization_id, service_id, status_ or (), page, size
    )
    return ApiResponse(
        data=Page(
            items=[MigrationPlanSummaryResponse.from_view(view) for view in result.items],
            total=result.total,
            page=page,
            size=size,
        )
    )


@router.get("/migration-plans/{plan_id}", response_model=ApiResponse[MigrationPlanDetailResponse])
async def get_migration_plan(
    access: ViewerAccess,
    plan_id: uuid.UUID,
    migration_plans: MigrationPlans,
    results: MigrationPlanResults,
) -> ApiResponse[MigrationPlanDetailResponse]:
    view = await migration_plans.get_plan(access.organization_id, plan_id)
    cutover_run = await results.find_cutover_run(view)
    return ApiResponse(data=MigrationPlanDetailResponse.from_view(view, cutover_run))


@router.post(
    "/migration-plans/{plan_id}/approve",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ApiResponse[MigrationPlanDetailResponse],
)
async def approve_migration_plan(
    access: HumanDeveloperAccess, plan_id: uuid.UUID, migration_plans: MigrationPlans
) -> ApiResponse[MigrationPlanDetailResponse]:
    """승인하면 바로 실행이 시작된다(별도 실행 버튼 없음)."""
    view = await migration_plans.approve_plan(access.organization_id, plan_id, access.user.id)
    return ApiResponse(data=MigrationPlanDetailResponse.from_view(view))


@router.post(
    "/migration-plans/{plan_id}/reject",
    response_model=ApiResponse[MigrationPlanDetailResponse],
)
async def reject_migration_plan(
    access: HumanDeveloperAccess, plan_id: uuid.UUID, migration_plans: MigrationPlans
) -> ApiResponse[MigrationPlanDetailResponse]:
    view = await migration_plans.reject_plan(access.organization_id, plan_id, access.user.id)
    return ApiResponse(data=MigrationPlanDetailResponse.from_view(view))


@router.post(
    "/migration-plans/{plan_id}/cancel",
    response_model=ApiResponse[MigrationPlanDetailResponse],
)
async def cancel_migration_plan(
    access: HumanDeveloperAccess, plan_id: uuid.UUID, migration_plans: MigrationPlans
) -> ApiResponse[MigrationPlanDetailResponse]:
    view = await migration_plans.cancel_plan(access.organization_id, plan_id, access.user.id)
    return ApiResponse(data=MigrationPlanDetailResponse.from_view(view))


@router.post(
    "/migration-plans/{plan_id}/abort",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ApiResponse[MigrationPlanDetailResponse],
)
async def abort_migration_plan(
    access: HumanDeveloperAccess, plan_id: uuid.UUID, migration_plans: MigrationPlans
) -> ApiResponse[MigrationPlanDetailResponse]:
    """중단을 받는다(멱등). 출발 100% 로 되돌리는 것은 일꾼이 한다."""
    view = await migration_plans.abort_plan(access.organization_id, plan_id, access.user.id)
    return ApiResponse(data=MigrationPlanDetailResponse.from_view(view))


@router.get("/migration-plans/{plan_id}/logs", response_model=ApiResponse[MigrationLogPageResponse])
async def get_migration_plan_logs(
    access: ViewerAccess,
    plan_id: uuid.UUID,
    migration_plans: MigrationPlans,
    cursor: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LOG_PAGE_SIZE)] = DEFAULT_LOG_PAGE_SIZE,
) -> ApiResponse[MigrationLogPageResponse]:
    view = await migration_plans.get_plan_logs(access.organization_id, plan_id, cursor, limit)
    return ApiResponse(data=MigrationLogPageResponse.from_view(view))
