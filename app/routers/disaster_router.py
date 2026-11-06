import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.auth.guards import HumanDeveloperAccess, ViewerAccess
from app.core.places import Coordinates
from app.dependencies import get_disaster_service, get_live_disaster_feed_service
from app.enums import DisasterStatus, DisasterType
from app.models.disaster import MAX_MAGNITUDE, MIN_MAGNITUDE
from app.schemas.disaster import (
    MAGNITUDE_DECIMAL_PLACES,
    MAGNITUDE_MAX_DIGITS,
    MAX_LAT,
    MAX_LNG,
    MIN_LAT,
    MIN_LNG,
    CreateDisasterRequest,
    DisasterPreviewResponse,
    DisasterResponse,
    InjectFeedEventRequest,
    InjectFeedEventResponse,
    LiveFeedStatusResponse,
)
from app.schemas.response import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, ApiResponse, Page
from app.services.disaster_service import DisasterInput, DisasterService
from app.services.live_disaster_feed import LiveDisasterFeedService

# 조회·미리보기는 뷰어(에이전트 가능). 만들기·종료는 개발자 이상 사람(에이전트는 재난을 낼 수 없다).
router = APIRouter(prefix="/api/v1/orgs/{org_id}/disasters", tags=["disaster"])

Disasters = Annotated[DisasterService, Depends(get_disaster_service)]
LiveFeed = Annotated[LiveDisasterFeedService, Depends(get_live_disaster_feed_service)]


# `/{disaster_id}` 보다 먼저 등록한다(feed 가 id 로 읽히지 않게).
@router.get("/feed", response_model=ApiResponse[LiveFeedStatusResponse])
async def get_live_feed_status(
    access: ViewerAccess, feed: LiveFeed
) -> ApiResponse[LiveFeedStatusResponse]:
    """실제 재난 피드(USGS) 상태: 스위치·연결·마지막 조회·피드 지진 수·이 조직 기준."""
    view = await feed.get_status(access.organization_id)
    return ApiResponse(data=LiveFeedStatusResponse.from_view(view))


@router.post(
    "/feed:inject",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[InjectFeedEventResponse],
)
async def inject_feed_event(
    access: HumanDeveloperAccess,
    request: InjectFeedEventRequest,
    feed: LiveFeed,
    disasters: Disasters,
) -> ApiResponse[InjectFeedEventResponse]:
    """시연용 가짜 지진을 피드 경로로 이 조직에 흘린다. 기준에 맞으면 재난(FEED_INJECTED)이 생긴다.

    피드 스위치와 상관없이 동작하고 피드 상태는 바꾸지 않는다. 대피 계획은 피드와 같이 제안만 한다.
    """
    result = await feed.inject(
        access.organization_id,
        access.user.id,
        request.magnitude,
        Coordinates(request.epicenter.lat, request.epicenter.lng),
        request.place,
    )
    disaster = (
        await disasters.get_disaster(access.organization_id, result.outcome.disaster_id)
        if result.outcome.disaster_id is not None
        else None
    )
    return ApiResponse(data=InjectFeedEventResponse.build(result, disaster))


# `/{disaster_id}` 보다 먼저 등록한다(preview 가 id 로 읽히지 않게).
@router.get("/preview", response_model=ApiResponse[DisasterPreviewResponse])
async def preview_disaster(
    access: ViewerAccess,
    disasters: Disasters,
    type_: Annotated[DisasterType, Query(alias="type")],
    lat: Annotated[float, Query(ge=MIN_LAT, le=MAX_LAT)],
    lng: Annotated[float, Query(ge=MIN_LNG, le=MAX_LNG)],
    magnitude: Annotated[
        Decimal,
        Query(
            ge=MIN_MAGNITUDE,
            le=MAX_MAGNITUDE,
            max_digits=MAGNITUDE_MAX_DIGITS,
            decimal_places=MAGNITUDE_DECIMAL_PLACES,
        ),
    ],
) -> ApiResponse[DisasterPreviewResponse]:
    """반경과 피해·안전·모름 환경만 계산한다. 저장하지 않고 계획도 만들지 않는다."""
    preview = await disasters.preview_disaster(
        access.organization_id, DisasterInput(type_, magnitude, Coordinates(lat, lng))
    )
    return ApiResponse(data=DisasterPreviewResponse.from_preview(preview))


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[DisasterResponse],
)
async def create_disaster(
    access: HumanDeveloperAccess,
    request: CreateDisasterRequest,
    disasters: Disasters,
) -> ApiResponse[DisasterResponse]:
    """재난을 저장하고 피해 서비스마다 긴급 이전 계획을 만든다. 서비스별 결과는 plans[] 에 있다."""
    view = await disasters.declare_disaster(
        access.organization_id, access.user.id, request.to_input()
    )
    return ApiResponse(data=DisasterResponse.from_view(view))


@router.get("", response_model=ApiResponse[Page[DisasterResponse]])
async def search_disasters(
    access: ViewerAccess,
    disasters: Disasters,
    status_: Annotated[DisasterStatus | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=0)] = 0,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[DisasterResponse]]:
    result = await disasters.search_disasters(access.organization_id, status_, page, size)
    return ApiResponse(
        data=Page(
            items=[DisasterResponse.from_view(view) for view in result.items],
            total=result.total,
            page=page,
            size=size,
        )
    )


@router.get("/{disaster_id}", response_model=ApiResponse[DisasterResponse])
async def get_disaster(
    access: ViewerAccess, disaster_id: uuid.UUID, disasters: Disasters
) -> ApiResponse[DisasterResponse]:
    view = await disasters.get_disaster(access.organization_id, disaster_id)
    return ApiResponse(data=DisasterResponse.from_view(view))


@router.post("/{disaster_id}/evacuate", response_model=ApiResponse[DisasterResponse])
async def evacuate_disaster(
    access: HumanDeveloperAccess, disaster_id: uuid.UUID, disasters: Disasters
) -> ApiResponse[DisasterResponse]:
    """제안된 긴급 이전을 이 사람으로 시작한다(버튼 재난과 같은 트리거). 결과는 plans[] 에 있다."""
    view = await disasters.evacuate_disaster(access.organization_id, disaster_id, access.user.id)
    return ApiResponse(data=DisasterResponse.from_view(view))


@router.post("/{disaster_id}/end", response_model=ApiResponse[DisasterResponse])
async def end_disaster(
    access: HumanDeveloperAccess, disaster_id: uuid.UUID, disasters: Disasters
) -> ApiResponse[DisasterResponse]:
    """재난을 끝낸다. 복귀 잠금이 풀린다."""
    view = await disasters.end_disaster(access.organization_id, disaster_id, access.user.id)
    return ApiResponse(data=DisasterResponse.from_view(view))
