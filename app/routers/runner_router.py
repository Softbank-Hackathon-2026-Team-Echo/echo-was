"""AI 러너 경로(WAS ↔ 러너 계약 v1). 가져가기는 러너 토큰, 나머지는 그 실행의 실행 토큰으로 부른다.

이벤트·결과 본문은 FastAPI 본문 인자가 아니라 parse_runner_payload 로 읽는다. 크기를 먼저 보고,
검증 실패를 422 RESULT_INVALID 로 돌려주기 위해서다. 검사기는 본문보다 먼저 돈다.
"""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request

from app.auth.runner_guards import RunnerIdentity, RunTokenRunId
from app.core.exceptions import ErrorField, ResultInvalidError
from app.dependencies import get_agent_run_service, get_diagnosis_service
from app.enums import AgentRunKind
from app.schemas.response import ApiResponse
from app.schemas.runner import (
    DIAGNOSIS_FIELD,
    DIAGNOSIS_NOT_ALLOWED_REASON,
    ApplyPatchResultRequest,
    ClaimRunRequest,
    ClaimRunResponse,
    DiagnosisRunResultRequest,
    HeartbeatResponse,
    RunResultRequest,
    SubmitRunEventsRequest,
    SubmitRunEventsResponse,
    SubmitRunResultResponse,
    openapi_request_body,
    parse_runner_payload,
)
from app.services.agent_run_service import AgentRunService
from app.services.diagnosis_service import DiagnosisService

router = APIRouter(prefix="/api/v1/runner/runs", tags=["runner"])

AgentRuns = Annotated[AgentRunService, Depends(get_agent_run_service)]
Diagnoses = Annotated[DiagnosisService, Depends(get_diagnosis_service)]


@router.post(":claim", response_model=ApiResponse[ClaimRunResponse])
async def claim_run(
    runner: RunnerIdentity,
    request: Annotated[ClaimRunRequest, Body()],
    agent_runs: AgentRuns,
) -> ApiResponse[ClaimRunResponse]:
    claimed = await agent_runs.claim_run(runner, request.runner_id, request.capabilities)
    return ApiResponse(data=ClaimRunResponse.from_claimed(claimed))


@router.post("/{run_id}/heartbeat", response_model=ApiResponse[HeartbeatResponse])
async def send_run_heartbeat(
    run_id: RunTokenRunId,
    agent_runs: AgentRuns,
) -> ApiResponse[HeartbeatResponse]:
    run = await agent_runs.heartbeat(run_id)
    return ApiResponse(data=HeartbeatResponse(status=run.status, lease_until=run.lease_until))


@router.post(
    "/{run_id}/events",
    response_model=ApiResponse[SubmitRunEventsResponse],
    openapi_extra=openapi_request_body(SubmitRunEventsRequest),
)
async def submit_run_events(
    run_id: RunTokenRunId,
    http_request: Request,
    agent_runs: AgentRuns,
) -> ApiResponse[SubmitRunEventsResponse]:
    request = parse_runner_payload(await http_request.body(), SubmitRunEventsRequest)
    accepted_up_to = await agent_runs.add_events(run_id, request.to_commands())
    return ApiResponse(data=SubmitRunEventsResponse(accepted_up_to=accepted_up_to))


@router.post(
    "/{run_id}/result",
    response_model=ApiResponse[SubmitRunResultResponse],
    openapi_extra=openapi_request_body(RunResultRequest, ApplyPatchResultRequest),
)
async def submit_run_result(
    run_id: RunTokenRunId,
    http_request: Request,
    agent_runs: AgentRuns,
    diagnoses: Diagnoses,
) -> ApiResponse[SubmitRunResultResponse]:
    """본문 모양은 실행 종류로 고른다(PD-9): APPLY_PATCH 면 ApplyPatchResult, 그 밖은 RunResult.
    RunResult 의 diagnosis 칸은 CHANGE_PROPOSAL(SPEC api-98)만 받는다 — 사전 점검이 보내면 422.

    진단에서 온 반영이 성공하면 같은 환경에 재배포 요청을 만든다(접수 거절은 결과 응답에 영향 없음).
    """
    raw = await http_request.body()
    kind = await agent_runs.find_run_kind(run_id)
    if kind == AgentRunKind.APPLY_PATCH:
        apply_request = parse_runner_payload(raw, ApplyPatchResultRequest)
        status = await agent_runs.submit_apply_result(run_id, apply_request.to_normalized())
        await diagnoses.redeploy_after_apply(run_id)
    elif kind == AgentRunKind.CHANGE_PROPOSAL:
        diagnosis_request = parse_runner_payload(raw, DiagnosisRunResultRequest)
        status = await agent_runs.submit_result(run_id, diagnosis_request.to_normalized())
    else:
        request = parse_runner_payload(raw, RunResultRequest)
        if DIAGNOSIS_FIELD in request.model_fields_set:
            raise ResultInvalidError([ErrorField(DIAGNOSIS_FIELD, DIAGNOSIS_NOT_ALLOWED_REASON)])
        status = await agent_runs.submit_result(run_id, request.to_normalized())
    return ApiResponse(data=SubmitRunResultResponse(status=status))
