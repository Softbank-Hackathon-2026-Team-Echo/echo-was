import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, Response, status

from app.auth.guards import DeveloperAccess, HumanDeveloperAccess, ViewerAccess
from app.dependencies import get_deploy_request_service, get_diagnosis_service
from app.enums import DiagnosisResult, LogSource
from app.schemas.deploy_request import (
    CreateDiagnosisRequest,
    DeployRequestDetailResponse,
    LogPageResponse,
    RollbackRequest,
)
from app.schemas.precheck import PrecheckDetailResponse
from app.schemas.response import ApiResponse
from app.services.deploy_request_service import DeployRequestService
from app.services.diagnosis_service import DiagnosisService, DiagnosisSimulate

# 조회·로그는 뷰어(에이전트 가능), 되돌리기는 개발자(배포 요청과 같은 검사기).
# AI 진단 만들기는 개발자 이상 사람(사전 점검 만들기와 같은 검사기, SPEC api-98).
router = APIRouter(prefix="/api/v1/orgs/{org_id}/deploy-requests", tags=["deploy-request"])

DeployRequests = Annotated[DeployRequestService, Depends(get_deploy_request_service)]
Diagnoses = Annotated[DiagnosisService, Depends(get_diagnosis_service)]

DEFAULT_LOG_PAGE_SIZE = 200
MAX_LOG_PAGE_SIZE = 500


@router.get("/{deploy_request_id}", response_model=ApiResponse[DeployRequestDetailResponse])
async def get_deploy_request(
    access: ViewerAccess, deploy_request_id: uuid.UUID, deploy_requests: DeployRequests
) -> ApiResponse[DeployRequestDetailResponse]:
    view = await deploy_requests.get_deploy_request(access.organization_id, deploy_request_id)
    return ApiResponse(data=DeployRequestDetailResponse.from_view(view))


@router.post(
    "/{deploy_request_id}/rollback",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ApiResponse[DeployRequestDetailResponse],
)
async def create_rollback(
    access: DeveloperAccess,
    deploy_request_id: uuid.UUID,
    deploy_requests: DeployRequests,
    request: Annotated[RollbackRequest | None, Body()] = None,
) -> ApiResponse[DeployRequestDetailResponse]:
    simulate = request.simulate if request is not None else None
    view = await deploy_requests.create_rollback(
        access.organization_id,
        deploy_request_id,
        access.user.id,
        simulate.fail_at if simulate is not None else None,
    )
    return ApiResponse(data=DeployRequestDetailResponse.from_view(view))


@router.post(
    "/{deploy_request_id}/diagnosis",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[PrecheckDetailResponse],
    responses={200: {"description": "이미 있는 진단(같은 실행)"}},
)
async def create_diagnosis(
    access: HumanDeveloperAccess,
    deploy_request_id: uuid.UUID,
    diagnoses: Diagnoses,
    response: Response,
    request: Annotated[CreateDiagnosisRequest | None, Body()] = None,
) -> ApiResponse[PrecheckDetailResponse]:
    """실패한 배포의 AI 진단(SPEC api-98). 본문은 비워도 된다. 이미 있으면 200 으로 같은 실행."""
    simulate = request.simulate if request is not None else None
    created = await diagnoses.create_diagnosis(
        access.organization_id,
        access.user.id,
        deploy_request_id,
        DiagnosisSimulate(DiagnosisResult(simulate.result)) if simulate is not None else None,
    )
    if not created.is_created:
        response.status_code = status.HTTP_200_OK
    return ApiResponse(data=PrecheckDetailResponse.from_view(created.view))


@router.get("/{deploy_request_id}/logs", response_model=ApiResponse[LogPageResponse])
async def get_deploy_request_logs(
    access: ViewerAccess,
    deploy_request_id: uuid.UUID,
    deploy_requests: DeployRequests,
    source: Annotated[LogSource, Query()],
    cursor: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LOG_PAGE_SIZE)] = DEFAULT_LOG_PAGE_SIZE,
) -> ApiResponse[LogPageResponse]:
    view = await deploy_requests.get_deploy_request_logs(
        access.organization_id, deploy_request_id, source, cursor, limit
    )
    return ApiResponse(data=LogPageResponse.from_view(view))
