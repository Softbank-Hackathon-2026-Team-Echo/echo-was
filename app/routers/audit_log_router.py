import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime

from app.auth.guards import ViewerAccess
from app.core.exceptions import RequestFieldInvalidError
from app.dependencies import get_audit_log_service
from app.enums import ActorType, AuditResult
from app.models.audit_log import AUDIT_ACTION_MAX_LENGTH, AUDIT_TARGET_TYPE_MAX_LENGTH
from app.repositories.audit_log_repository import AuditLogFilter
from app.schemas.audit_log import AuditLogResponse
from app.schemas.response import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, ApiResponse, Page
from app.services.audit_log_service import AuditLogService

# 뷰어 이상 사람과 그 조직의 에이전트 토큰이 읽는다. 쓰는 API 는 없다.
router = APIRouter(prefix="/api/v1/orgs/{org_id}/audit-logs", tags=["audit-log"])

AuditLogs = Annotated[AuditLogService, Depends(get_audit_log_service)]

QUERY_MAX_LENGTH = 100
FROM_NOT_BEFORE_TO_REASON = "FROM_NOT_BEFORE_TO"


@router.get("", response_model=ApiResponse[Page[AuditLogResponse]])
async def search_audit_logs(
    access: ViewerAccess,
    audit_logs: AuditLogs,
    page: Annotated[int, Query(ge=0)] = 0,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    actor_type: Annotated[ActorType | None, Query(alias="actorType")] = None,
    result: Annotated[AuditResult | None, Query()] = None,
    action: Annotated[str | None, Query(min_length=1, max_length=AUDIT_ACTION_MAX_LENGTH)] = None,
    target_type: Annotated[
        str | None, Query(alias="targetType", min_length=1, max_length=AUDIT_TARGET_TYPE_MAX_LENGTH)
    ] = None,
    target_id: Annotated[uuid.UUID | None, Query(alias="targetId")] = None,
    from_: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    to: Annotated[AwareDatetime | None, Query()] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=QUERY_MAX_LENGTH)] = None,
    include_reads: Annotated[bool, Query(alias="includeReads")] = True,
) -> ApiResponse[Page[AuditLogResponse]]:
    """최신순. `from` 이상 `to` 미만. `includeReads=false` 면 에이전트 읽기 줄을 뺀다."""
    if from_ is not None and to is not None and from_ >= to:
        raise RequestFieldInvalidError("from", FROM_NOT_BEFORE_TO_REASON)
    found = await audit_logs.search_audit_logs(
        access.organization_id,
        AuditLogFilter(
            actor_type=actor_type,
            result=result,
            action=action,
            target_type=target_type,
            target_id=target_id,
            from_at=from_,
            to_at=to,
            query=q,
            include_reads=include_reads,
        ),
        page,
        size,
    )
    return ApiResponse(
        data=Page(
            items=[AuditLogResponse.from_view(view) for view in found.items],
            total=found.total,
            page=page,
            size=size,
        )
    )
