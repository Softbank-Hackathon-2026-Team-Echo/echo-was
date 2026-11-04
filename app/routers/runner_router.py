"""AI 러너 경로(WAS ↔ 러너 계약 v1). 가져가기는 러너 토큰, 나머지는 그 실행의 실행 토큰으로 부른다.

이벤트·결과 본문은 FastAPI 본문 인자가 아니라 parse_runner_payload 로 읽는다. 크기를 먼저 보고,
검증 실패를 422 RESULT_INVALID 로 돌려주기 위해서다. 검사기는 본문보다 먼저 돈다.
"""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request

from app.auth.runner_guards import RunnerIdentity, RunTokenRunId
from app.dependencies import get_agent_run_service
from app.schemas.response import ApiResponse
from app.schemas.runner import (
    ClaimRunRequest,
    ClaimRunResponse,
    HeartbeatResponse,
    RunResultRequest,
    SubmitRunEventsRequest,
    SubmitRunEventsResponse,
    SubmitRunResultResponse,
    openapi_request_body,
    parse_runner_payload,
)
from app.services.agent_run_service import AgentRunService

router = APIRouter(prefix="/api/v1/runner/runs", tags=["runner"])

AgentRuns = Annotated[AgentRunService, Depends(get_agent_run_service)]


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
    openapi_extra=openapi_request_body(RunResultRequest),
)
async def submit_run_result(
    run_id: RunTokenRunId,
    http_request: Request,
    agent_runs: AgentRuns,
) -> ApiResponse[SubmitRunResultResponse]:
    request = parse_runner_payload(await http_request.body(), RunResultRequest)
    status = await agent_runs.submit_result(run_id, request.to_normalized())
    return ApiResponse(data=SubmitRunResultResponse(status=status))
