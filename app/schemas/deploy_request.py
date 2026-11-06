"""배포 요청 API 요청·응답 스키마.

JSON 이름은 camelCase, 모든 응답 칸은 항상 있고 값이 없으면 null.
"""

import re
import uuid
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import StringConstraints, field_validator, model_validator
from pydantic_core import PydanticCustomError

from app.enums import (
    BuildStatus,
    DeployRequestCancelReason,
    DeployRequestErrorCode,
    DeployRequestStatus,
    DeployRequestTrigger,
    LogLevel,
    LogSource,
    RefType,
    RevertStatus,
    SimulatedFailureStage,
)
from app.schemas.response import ApiModel, ApiRequestModel
from app.schemas.service import EnvironmentRefResponse
from app.services.container_diagnostics import FailureDetail
from app.services.deploy_log_messages import ERROR_MESSAGES
from app.services.deploy_request_service import DeployRequestView, LogPageView
from app.services.deploy_support import SNAPSHOT_ENVIRONMENT_URL, SNAPSHOT_MAIN_URL

BRANCH_MAX_LENGTH = 255
INVALID_BRANCH_REASON = "INVALID_BRANCH"
REF_REQUIRED_REASON = "EXACTLY_ONE_OF_BRANCH_OR_COMMIT_SHA"
# git ref 이름 기본 금지: 공백·제어 문자, ~ ^ : ? * [ \, "..", "@{".
_BRANCH_FORBIDDEN = re.compile(r"[\s~^:?*\[\\\x00-\x1f\x7f]|\.\.|@\{")

CommitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,40}$")]


def _validate_branch(value: str) -> str:
    if (
        not 1 <= len(value) <= BRANCH_MAX_LENGTH
        or _BRANCH_FORBIDDEN.search(value)
        or value.endswith(("/", ".lock"))
    ):
        raise PydanticCustomError("invalid_branch", INVALID_BRANCH_REASON)
    return value


class SimulateRequest(ApiRequestModel):
    """가짜 실행기 모드에서만 받는다. 이 단계에서 요청을 일부러 실패시킨다."""

    fail_at: SimulatedFailureStage


class CreateDeployRequest(ApiRequestModel):
    """원터치 배포 요청. branch 와 commitSha 중 정확히 하나를 준다. commitSha 는 7~40자."""

    environment_id: uuid.UUID
    branch: str | None = None
    commit_sha: CommitSha | None = None
    simulate: SimulateRequest | None = None

    @field_validator("branch")
    @classmethod
    def _check_branch(cls, value: str | None) -> str | None:
        return None if value is None else _validate_branch(value)

    @model_validator(mode="after")
    def _check_exactly_one_ref(self) -> Self:
        if (self.branch is None) == (self.commit_sha is None):
            raise PydanticCustomError("ref_required", REF_REQUIRED_REASON)
        return self

    @property
    def ref(self) -> tuple[RefType, str]:
        if self.branch is not None:
            return RefType.BRANCH, self.branch
        assert self.commit_sha is not None
        return RefType.COMMIT, self.commit_sha


class RollbackSimulateRequest(ApiRequestModel):
    """되돌리기에는 빌드 단계가 없어 DEPLOYING·VERIFYING 만 받는다(BUILDING 은 422)."""

    fail_at: Literal[SimulatedFailureStage.DEPLOYING, SimulatedFailureStage.VERIFYING]


class RollbackRequest(ApiRequestModel):
    """되돌리기 요청 본문(비워도 된다). simulate 는 가짜 실행기 모드에서만 받는다."""

    simulate: RollbackSimulateRequest | None = None


class RefResponse(ApiModel):
    type: RefType
    value: str


class BuildRefResponse(ApiModel):
    """reused=true 면 이 요청이 새로 만든 빌드가 아니다(같은 커밋 빌드 재사용·예비 따라가기)."""

    id: uuid.UUID
    status: BuildStatus
    image_repository: str | None
    image_digest: str | None
    reused: bool


class UrlsResponse(ApiModel):
    main: str
    environment: str


class StageTimesResponse(ApiModel):
    queued_at: datetime
    building_at: datetime | None
    deploying_at: datetime | None
    verifying_at: datetime | None
    finished_at: datetime | None


class DeployRequestSummaryResponse(ApiModel):
    """배포 요청 목록 한 항목. simulated=true 면 가짜 실행기로 처리한 요청이다."""

    id: uuid.UUID
    service_id: uuid.UUID
    environment: EnvironmentRefResponse
    service_environment_id: uuid.UUID
    trigger: DeployRequestTrigger
    source_request_id: uuid.UUID | None
    ref: RefResponse
    commit_sha: str
    status: DeployRequestStatus
    cancel_reason: DeployRequestCancelReason | None
    error_code: DeployRequestErrorCode | None
    revert_status: RevertStatus | None
    stage_times: StageTimesResponse
    simulated: bool
    # 이 요청이 만든 배포 기록이 지금 그 연결의 현재 배포인가.
    is_current: bool
    # 이 요청을 골라 되돌릴 수 있는가(성공·배포 기록 있음·현재 아님·연결 살아 있음·서비스 ACTIVE).
    rollbackable: bool
    # 되돌리기 요청이면 대상 요청 id.
    rollback_of_request_id: uuid.UUID | None

    @classmethod
    def from_view(cls, view: DeployRequestView) -> Self:
        return cls(**_summary_fields(view))


class FailureResponse(ApiModel):
    """실패한 요청의 원인(API 서버 4-1). 에러 수정 에이전트(echo-ai-agent #22)가 그대로 읽는다.

    message 는 errorMessage 고정 문장 뒤에 원인 한 줄(`Last error: …`)을 붙인 것이다. 컨테이너 칸은
    실행기가 컨테이너 실패를 판정해 진단을 남겼을 때만 값이 있다(없으면 null·빈 목록·false).
    logTail 은 비밀값(그 배포의 환경 변수 값·접속 문자열 비밀번호·토큰 모양)을 가린
    마지막 50줄 이하다.
    """

    code: DeployRequestErrorCode
    message: str
    container: str | None
    container_reason: str | None
    termination_reason: str | None
    exit_code: int | None
    restart_count: int | None
    log_tail: list[str]
    log_truncated: bool
    # 되돌리기 뒤 이전 버전이 실행 중인가. 이전 배포가 없었으면 false, 되돌리기 전·실패면 null.
    previous_version_running: bool | None

    @classmethod
    def from_detail(cls, detail: FailureDetail) -> Self:
        return cls(
            code=detail.code,
            message=detail.message,
            container=detail.container,
            container_reason=detail.container_reason,
            termination_reason=detail.termination_reason,
            exit_code=detail.exit_code,
            restart_count=detail.restart_count,
            log_tail=detail.log_tail,
            log_truncated=detail.log_truncated,
            previous_version_running=detail.previous_version_running,
        )


class DeployRequestDetailResponse(DeployRequestSummaryResponse):
    repo_url: str
    superseded_by_id: uuid.UUID | None
    error_message: str | None
    # 실패한 요청의 원인·컨테이너 진단(API 서버 4-1). 실패가 아니면 null.
    failure: FailureResponse | None
    build: BuildRefResponse | None
    deployment_id: uuid.UUID | None
    urls: UrlsResponse

    @classmethod
    def from_view(cls, view: DeployRequestView) -> Self:
        request = view.deploy_request
        build = view.build
        error_code = (
            DeployRequestErrorCode(request.error_code) if request.error_code is not None else None
        )
        return cls(
            **_summary_fields(view),
            repo_url=request.repo_url,
            superseded_by_id=request.superseded_by_id,
            error_message=ERROR_MESSAGES[error_code] if error_code is not None else None,
            failure=(
                FailureResponse.from_detail(view.failure) if view.failure is not None else None
            ),
            build=(
                BuildRefResponse(
                    id=build.id,
                    status=BuildStatus(build.status),
                    image_repository=build.image_repository,
                    image_digest=build.image_digest,
                    reused=bool(request.build_reused),
                )
                if build is not None
                else None
            ),
            deployment_id=request.deployment_id,
            urls=UrlsResponse(
                main=str(request.config_snapshot[SNAPSHOT_MAIN_URL]),
                environment=str(request.config_snapshot[SNAPSHOT_ENVIRONMENT_URL]),
            ),
        )


def _summary_fields(view: DeployRequestView) -> dict[str, object]:
    request = view.deploy_request
    return {
        "id": request.id,
        "service_id": request.service_id,
        "environment": EnvironmentRefResponse.from_model(view.environment),
        "service_environment_id": request.service_environment_id,
        "trigger": request.trigger,
        "source_request_id": request.source_request_id,
        "ref": RefResponse(type=request.ref_type, value=request.ref_value),
        "commit_sha": request.commit_sha,
        "status": request.status,
        "cancel_reason": request.cancel_reason,
        "error_code": request.error_code,
        "revert_status": request.revert_status,
        "stage_times": StageTimesResponse(
            queued_at=request.created_at,
            building_at=request.building_at,
            deploying_at=request.deploying_at,
            verifying_at=request.verifying_at,
            finished_at=request.finished_at,
        ),
        "simulated": request.simulated,
        "is_current": view.is_current,
        "rollbackable": view.rollbackable,
        "rollback_of_request_id": request.rollback_of_request_id,
    }


class LogLineResponse(ApiModel):
    at: datetime
    level: LogLevel
    message: str
    # 배포 작업 로그 줄의 코드(CONTAINER_LOG 등, API 서버 4-1). 빌드·실행 로그는 null.
    code: str | None


class LogPageResponse(ApiModel):
    """로그 한 페이지. simulated=true 면 가짜 실행기로 만든 기록이다. nextCursor 로 이어 읽는다."""

    source: LogSource
    simulated: bool
    items: list[LogLineResponse]
    next_cursor: str | None
    has_more: bool

    @classmethod
    def from_view(cls, view: LogPageView) -> Self:
        return cls(
            source=view.source,
            simulated=view.simulated,
            items=[
                LogLineResponse(at=line.at, level=line.level, message=line.message, code=line.code)
                for line in view.lines
            ],
            next_cursor=view.next_cursor,
            has_more=view.has_more,
        )
