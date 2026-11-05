import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.auth.guards import HumanDeveloperAccess, ViewerAccess
from app.dependencies import get_precheck_patch_service, get_precheck_service
from app.schemas.precheck import (
    DEFAULT_LOG_PAGE_SIZE,
    MAX_LOG_PAGE_SIZE,
    CreatePrecheckRequest,
    DecidePrecheckPatchRequest,
    PrecheckDetailResponse,
    PrecheckLogPageResponse,
    PrecheckPatchResponse,
)
from app.schemas.response import ApiResponse
from app.services.precheck_patch_service import PrecheckPatchService
from app.services.precheck_service import PrecheckService

# 조회·이벤트는 뷰어(에이전트 가능). 만들기·취소·패치 승인·거절은 개발자 이상 사람.
router = APIRouter(prefix="/api/v1/orgs/{org_id}/prechecks", tags=["precheck"])

Prechecks = Annotated[PrecheckService, Depends(get_precheck_service)]
PrecheckPatches = Annotated[PrecheckPatchService, Depends(get_precheck_patch_service)]


@router.post(
    "", status_code=status.HTTP_201_CREATED, response_model=ApiResponse[PrecheckDetailResponse]
)
async def create_precheck(
    access: HumanDeveloperAccess,
    request: CreatePrecheckRequest,
    prechecks: Prechecks,
) -> ApiResponse[PrecheckDetailResponse]:
    view = await prechecks.create_precheck(
        access.organization_id, access.user.id, request.service_id, request.target_environment_id
    )
    return ApiResponse(data=PrecheckDetailResponse.from_view(view))


@router.get("/{precheck_id}", response_model=ApiResponse[PrecheckDetailResponse])
async def get_precheck(
    access: ViewerAccess,
    precheck_id: uuid.UUID,
    prechecks: Prechecks,
) -> ApiResponse[PrecheckDetailResponse]:
    view = await prechecks.get_precheck(access.organization_id, precheck_id)
    return ApiResponse(data=PrecheckDetailResponse.from_view(view))


@router.get("/{precheck_id}/logs", response_model=ApiResponse[PrecheckLogPageResponse])
async def search_precheck_logs(
    access: ViewerAccess,
    precheck_id: uuid.UUID,
    prechecks: Prechecks,
    after: Annotated[int, Query(ge=0)] = 0,
    size: Annotated[int, Query(ge=1, le=MAX_LOG_PAGE_SIZE)] = DEFAULT_LOG_PAGE_SIZE,
) -> ApiResponse[PrecheckLogPageResponse]:
    page = await prechecks.search_logs(access.organization_id, precheck_id, after, size)
    return ApiResponse(data=PrecheckLogPageResponse.from_page(page))


@router.post("/{precheck_id}/cancel", response_model=ApiResponse[PrecheckDetailResponse])
async def cancel_precheck(
    access: HumanDeveloperAccess,
    precheck_id: uuid.UUID,
    prechecks: Prechecks,
) -> ApiResponse[PrecheckDetailResponse]:
    view = await prechecks.cancel_precheck(access.organization_id, precheck_id, access.user.id)
    return ApiResponse(data=PrecheckDetailResponse.from_view(view))


@router.post(
    "/{precheck_id}/patches/{patch_id}/approve", response_model=ApiResponse[PrecheckPatchResponse]
)
async def approve_precheck_patch(
    access: HumanDeveloperAccess,
    precheck_id: uuid.UUID,
    patch_id: str,
    request: DecidePrecheckPatchRequest,
    patches: PrecheckPatches,
) -> ApiResponse[PrecheckPatchResponse]:
    view = await patches.approve_patch(
        access.organization_id,
        precheck_id,
        patch_id,
        access.user.id,
        request.patch_hash,
        request.reason,
    )
    return ApiResponse(data=PrecheckPatchResponse.from_view(view))


@router.post(
    "/{precheck_id}/patches/{patch_id}/reject", response_model=ApiResponse[PrecheckPatchResponse]
)
async def reject_precheck_patch(
    access: HumanDeveloperAccess,
    precheck_id: uuid.UUID,
    patch_id: str,
    request: DecidePrecheckPatchRequest,
    patches: PrecheckPatches,
) -> ApiResponse[PrecheckPatchResponse]:
    view = await patches.reject_patch(
        access.organization_id,
        precheck_id,
        patch_id,
        access.user.id,
        request.patch_hash,
        request.reason,
    )
    return ApiResponse(data=PrecheckPatchResponse.from_view(view))
