import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.auth.guards import HumanDeveloperAccess, ViewerAccess
from app.dependencies import (
    get_security_compare_service,
    get_security_manual_review_service,
    get_security_matrix_service,
)
from app.enums import SecurityControlId
from app.schemas.response import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, ApiResponse, Page
from app.schemas.security import (
    SecurityCompareResponse,
    SecurityFindingResponse,
    SecurityMatrixResponse,
)
from app.schemas.security_manual_review import RecordManualReviewRequest
from app.services.security_compare_service import SecurityCompareService
from app.services.security_manual_review_service import SecurityManualReviewService
from app.services.security_matrix_service import FINDING_IDS_LIMIT, SecurityMatrixService

# 보안 매트릭스·증거 조회(보안·거버넌스 4, 계약 4-7). 읽기는 뷰어(에이전트 가능).
# 사람 점검 기록은 개발자 이상 사람만(에이전트는 증거를 만들 수 없다).
router = APIRouter(prefix="/api/v1/orgs/{org_id}/security", tags=["security"])

Service = Annotated[SecurityMatrixService, Depends(get_security_matrix_service)]
CompareService = Annotated[SecurityCompareService, Depends(get_security_compare_service)]
ManualReviewService = Annotated[
    SecurityManualReviewService, Depends(get_security_manual_review_service)
]


@router.get("/matrix", response_model=ApiResponse[SecurityMatrixResponse])
async def get_security_matrix(
    access: ViewerAccess, service: Service
) -> ApiResponse[SecurityMatrixResponse]:
    view = await service.get_matrix(access.organization_id)
    return ApiResponse(data=SecurityMatrixResponse.from_view(view))


@router.get("/compare", response_model=ApiResponse[SecurityCompareResponse])
async def compare_security(
    access: ViewerAccess,
    service: CompareService,
    source: Annotated[uuid.UUID, Query(description="출발 환경 id")],
    target: Annotated[uuid.UUID, Query(description="도착 환경 id")],
    service_id: Annotated[uuid.UUID | None, Query(alias="serviceId")] = None,
) -> ApiResponse[SecurityCompareResponse]:
    """이전 전후 보안 비교(보안 5-2). 칸 값은 매트릭스와 같고 blocksMigration 은 게이트 규칙이다."""
    view = await service.compare(access.organization_id, source, target, service_id=service_id)
    return ApiResponse(data=SecurityCompareResponse.from_view(view))


@router.get("/findings", response_model=ApiResponse[Page[SecurityFindingResponse]])
async def search_security_findings(
    access: ViewerAccess,
    service: Service,
    page: Annotated[int, Query(ge=0)] = 0,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    environment_id: Annotated[uuid.UUID | None, Query(alias="environmentId")] = None,
    service_id: Annotated[uuid.UUID | None, Query(alias="serviceId")] = None,
    control_id: Annotated[SecurityControlId | None, Query(alias="controlId")] = None,
    finding_ids: Annotated[
        list[uuid.UUID] | None, Query(alias="findingId", max_length=FINDING_IDS_LIMIT)
    ] = None,
    include_expired: Annotated[bool, Query(alias="includeExpired")] = False,
) -> ApiResponse[Page[SecurityFindingResponse]]:
    result = await service.search_findings(
        access.organization_id,
        environment_id=environment_id,
        service_id=service_id,
        control_id=control_id,
        finding_ids=finding_ids or [],
        include_expired=include_expired,
        page=page,
        size=size,
    )
    return ApiResponse(
        data=Page(
            items=[SecurityFindingResponse.from_model(model) for model in result.items],
            total=result.total,
            page=page,
            size=size,
        )
    )


@router.post(
    "/manual-reviews",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[SecurityFindingResponse],
)
async def record_security_manual_review(
    access: HumanDeveloperAccess,
    request: RecordManualReviewRequest,
    service: ManualReviewService,
) -> ApiResponse[SecurityFindingResponse]:
    """사람 점검 기록(규칙표 출처에 사람 점검이 있는 통제만). 응답은 만든 증거 한 건이다."""
    finding = await service.record_review(
        access.organization_id, access.user.id, request.to_command()
    )
    return ApiResponse(data=SecurityFindingResponse.from_model(finding))
