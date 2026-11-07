from typing import Annotated

from fastapi import APIRouter, Body, Depends, status

from app.auth.guards import AdminAccess, HumanAdminAccess
from app.dependencies import get_demo_snapshot_service
from app.schemas.demo_snapshot import (
    DemoRestoreResponse,
    DemoSnapshotResponse,
    RestoreDemoRequest,
)
from app.schemas.response import ApiResponse
from app.services.demo_snapshot_service import DemoSnapshotService

# 시연 조직 스냅샷·복귀. 조직 어드민 전용(만들기·복귀는 사람만).
router = APIRouter(prefix="/api/v1/orgs/{org_id}/demo", tags=["demo"])

DemoSnapshots = Annotated[DemoSnapshotService, Depends(get_demo_snapshot_service)]


@router.post(
    "/snapshot",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[DemoSnapshotResponse],
)
async def take_demo_snapshot(
    access: HumanAdminAccess, snapshots: DemoSnapshots
) -> ApiResponse[DemoSnapshotResponse]:
    """지금 상태를 시연 기준으로 기록한다. 열린 계획·재난·배포 요청이 있으면 409."""
    view = await snapshots.take_snapshot(access.organization_id, access.user.id)
    return ApiResponse(data=DemoSnapshotResponse.from_view(view))


@router.get("/snapshot", response_model=ApiResponse[DemoSnapshotResponse])
async def get_demo_snapshot(
    access: AdminAccess, snapshots: DemoSnapshots
) -> ApiResponse[DemoSnapshotResponse]:
    """가장 최근 스냅샷."""
    view = await snapshots.get_snapshot(access.organization_id)
    return ApiResponse(data=DemoSnapshotResponse.from_view(view))


@router.post("/restore", response_model=ApiResponse[DemoRestoreResponse])
async def restore_demo_snapshot(
    access: HumanAdminAccess,
    snapshots: DemoSnapshots,
    request: Annotated[RestoreDemoRequest | None, Body()] = None,
) -> ApiResponse[DemoRestoreResponse]:
    """스냅샷으로 되돌린다(단계별 결과). 여러 번 불러도 안전하고, callAgain 이면 다시 부른다."""
    result = await snapshots.restore(
        access.organization_id,
        access.user.id,
        dry_run=request.dry_run if request is not None else False,
    )
    return ApiResponse(data=DemoRestoreResponse.from_result(result))
