"""배포 요청 API 요청·응답 스키마.

JSON 이름은 camelCase, 모든 응답 칸은 항상 있고 값이 없으면 null.
"""

import re
import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import StringConstraints, field_validator, model_validator
from pydantic_core import PydanticCustomError

from app.enums import (
    BuildStatus,
    DeployRequestCancelReason,
    DeployRequestErrorCode,
    DeployRequestStatus,
    DeployRequestTrigger,
    RefType,
    RevertStatus,
    SimulatedFailureStage,
)
from app.schemas.response import ApiModel, ApiRequestModel
from app.schemas.service import EnvironmentRefResponse
from app.services.deploy_request_service import DeployRequestView
from app.services.deploy_support import SNAPSHOT_ENVIRONMENT_URL, SNAPSHOT_MAIN_URL

BRANCH_MAX_LENGTH = 255
INVALID_BRANCH_REASON = "INVALID_BRANCH"
REF_REQUIRED_REASON = "EXACTLY_ONE_OF_BRANCH_OR_COMMIT_SHA"
# git ref 이름 기본 금지: 공백·제어 문자, ~ ^ : ? * [ \, "..", "@{".
_BRANCH_FORBIDDEN = re.compile(r"[\s~^:?*\[\\\x00-\x1f\x7f]|\.\.|@\{")

CommitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,40}$")]

# 오류 코드별 고정 문장. 외부 응답 원문(비밀값이 섞일 수 있음)은 넣지 않는다.
ERROR_MESSAGES: dict[DeployRequestErrorCode, str] = {
    DeployRequestErrorCode.REPO_NOT_ACCESSIBLE: (
        "The repository was not found or the GitHub App is not installed on it."
    ),
    DeployRequestErrorCode.REF_NOT_FOUND: "The branch or commit was not found in the repository.",
    DeployRequestErrorCode.SOURCE_PROVIDER_UNAVAILABLE: (
        "The source provider is unavailable (rate limit, server error or timeout)."
    ),
    DeployRequestErrorCode.BUILD_FAILED: "The image build failed.",
    DeployRequestErrorCode.BUILD_TIMEOUT: "The image build did not finish in time.",
    DeployRequestErrorCode.DEPLOY_FAILED: "The deployment to the environment failed.",
    DeployRequestErrorCode.DEPLOY_TIMEOUT: "The deployment did not finish in time.",
    DeployRequestErrorCode.HEALTH_CHECK_FAILED: "The health check did not return a 2xx response.",
    DeployRequestErrorCode.HEALTH_CHECK_TIMEOUT: "The health check could not be completed in time.",
    DeployRequestErrorCode.PUBLIC_URL_UNREACHABLE: "The environment public URL was unreachable.",
    DeployRequestErrorCode.WORKER_LOST: "The deploy worker stopped repeatedly during this request.",
    DeployRequestErrorCode.INTERNAL_ERROR: "An internal error occurred.",
}


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

    @classmethod
    def from_view(cls, view: DeployRequestView) -> Self:
        return cls(**_summary_fields(view))


class DeployRequestDetailResponse(DeployRequestSummaryResponse):
    repo_url: str
    superseded_by_id: uuid.UUID | None
    error_message: str | None
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
    }
