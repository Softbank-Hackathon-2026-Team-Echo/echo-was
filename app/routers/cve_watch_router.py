import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Response

from app.auth.guards import HumanDeveloperAccess
from app.clients.osv import AdvisoryFeed
from app.core.config import Settings, get_settings
from app.core.exceptions import CveWatchDisabledError, CveWatchSimulateNotAllowedError
from app.dependencies import get_advisory_feed, get_cve_diagnosis_service, get_cve_watch_service
from app.schemas.cve_watch import CveWatchInjectRequest, CveWatchRunResponse
from app.schemas.precheck import PrecheckDetailResponse
from app.schemas.response import ApiResponse
from app.services.cve_diagnosis_service import CveDiagnosisService
from app.services.cve_watch_service import CveWatchService

# CVE 감시(SPEC sec-6-cve-watch). 조회·주입·진단 요청은 개발자 이상 사람만(에이전트 토큰 불가).
router = APIRouter(prefix="/api/v1/orgs/{org_id}/security", tags=["cve-watch"])

CveWatch = Annotated[CveWatchService, Depends(get_cve_watch_service)]
Feed = Annotated[AdvisoryFeed, Depends(get_advisory_feed)]
CurrentSettings = Annotated[Settings, Depends(get_settings)]


@router.post("/cve-watch:check", response_model=ApiResponse[CveWatchRunResponse])
async def check_cve_watch(
    access: HumanDeveloperAccess, service: CveWatch, feed: Feed, settings: CurrentSettings
) -> ApiResponse[CveWatchRunResponse]:
    """지금 바로 OSV 로 조회한다(주기 감시와 같은 길). 스위치가 꺼져 있으면 409."""
    if not settings.cve_watch_enabled:
        raise CveWatchDisabledError("cve watch is disabled")
    view = await service.check(access.organization_id, feed, actor_user_id=access.user.id)
    return ApiResponse(data=CveWatchRunResponse.from_view(view))


@router.post("/cve-watch:inject", response_model=ApiResponse[CveWatchRunResponse])
async def inject_cve_advisory(
    access: HumanDeveloperAccess,
    service: CveWatch,
    settings: CurrentSettings,
    request: Annotated[CveWatchInjectRequest | None, Body()] = None,
) -> ApiResponse[CveWatchRunResponse]:
    """가짜 권고 한 건을 주입해 맞춰 본다(시연용, simulated 증거 — 판정에 쓰지 않음)."""
    if not settings.cve_watch_simulate_enabled:
        raise CveWatchSimulateNotAllowedError("cve watch simulate is disabled")
    body = request or CveWatchInjectRequest()
    view = await service.inject(
        access.organization_id,
        body.advisory.to_advisory(),
        actor_user_id=access.user.id,
        service_id=body.service_id,
    )
    return ApiResponse(data=CveWatchRunResponse.from_view(view))


@router.post(
    "/findings/{finding_id}/diagnosis",
    status_code=201,
    response_model=ApiResponse[PrecheckDetailResponse],
)
async def request_finding_diagnosis(
    access: HumanDeveloperAccess,
    finding_id: uuid.UUID,
    response: Response,
    settings: CurrentSettings,
    service: Annotated[CveDiagnosisService, Depends(get_cve_diagnosis_service)],
) -> ApiResponse[PrecheckDetailResponse]:
    if not settings.cve_remediation_enabled:
        raise CveWatchDisabledError("CVE remediation is disabled")
    view, created = await service.create_diagnosis(
        access.organization_id, finding_id, access.user.id
    )
    response.status_code = 201 if created else 200
    return ApiResponse(data=PrecheckDetailResponse.from_view(view))
