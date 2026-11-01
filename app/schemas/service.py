"""서비스 API 요청·응답 스키마. JSON 이름은 camelCase, 모든 응답 칸은 항상 있고 값이 없으면 null."""

import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import Field, StringConstraints, field_validator
from pydantic_core import PydanticCustomError

from app.core.repo_url import InvalidRepoUrlError, normalize_repo_url
from app.enums import (
    DeployRequestErrorCode,
    ServiceEnvironmentRole,
    ServiceStatus,
    TeardownErrorCode,
    VersionStatus,
)
from app.models import Environment, Service
from app.models.service import (
    DEFAULT_CONTAINER_PORT,
    DEFAULT_HEALTH_CHECK_PATH,
    DEFAULT_HEALTH_CHECK_TIMEOUT_SECONDS,
    HEALTH_CHECK_PATH_MAX_LENGTH,
    MAX_HEALTH_CHECK_TIMEOUT_SECONDS,
    MIN_HEALTH_CHECK_TIMEOUT_SECONDS,
)
from app.schemas.environment import NULL_NOT_ALLOWED_REASON, ResourceName
from app.schemas.response import ApiModel, ApiRequestModel
from app.services.deploy_support import public_url
from app.services.service_view import ServiceDetailView, ServiceEnvironmentView

INVALID_REPO_URL_REASON = "INVALID_REPO_URL"

HealthCheckPath = Annotated[
    str, StringConstraints(pattern=r"^/\S*$", max_length=HEALTH_CHECK_PATH_MAX_LENGTH)
]
HealthCheckTimeoutSeconds = Annotated[
    int, Field(ge=MIN_HEALTH_CHECK_TIMEOUT_SECONDS, le=MAX_HEALTH_CHECK_TIMEOUT_SECONDS)
]
ContainerPort = Annotated[int, Field(ge=1, le=65535)]


def _validate_repo_url(value: str) -> str:
    try:
        return normalize_repo_url(value)
    except InvalidRepoUrlError:
        raise PydanticCustomError("invalid_repo_url", INVALID_REPO_URL_REASON) from None


class CreateServiceRequest(ApiRequestModel):
    """서비스 등록. repoUrl 은 https://github.com/<owner>/<repo> 만 받는다."""

    name: ResourceName
    description: str | None = None
    repo_url: str
    health_check_path: HealthCheckPath = DEFAULT_HEALTH_CHECK_PATH
    health_check_timeout_seconds: HealthCheckTimeoutSeconds = DEFAULT_HEALTH_CHECK_TIMEOUT_SECONDS
    container_port: ContainerPort = DEFAULT_CONTAINER_PORT

    @field_validator("repo_url")
    @classmethod
    def _check_repo_url(cls, value: str) -> str:
        return _validate_repo_url(value)


class UpdateServiceRequest(ApiRequestModel):
    """서비스 수정. 들어온 칸만 바꾼다. null 은 description 만 받는다. name 은 바꿀 수 없다."""

    name: ResourceName | None = None
    description: str | None = None
    repo_url: str | None = None
    health_check_path: HealthCheckPath | None = None
    health_check_timeout_seconds: HealthCheckTimeoutSeconds | None = None
    container_port: ContainerPort | None = None

    @field_validator(
        "name",
        "repo_url",
        "health_check_path",
        "health_check_timeout_seconds",
        "container_port",
        mode="before",
    )
    @classmethod
    def _reject_null(cls, value: object) -> object:
        if value is None:
            raise PydanticCustomError("null_not_allowed", NULL_NOT_ALLOWED_REASON)
        return value

    @field_validator("repo_url")
    @classmethod
    def _check_repo_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_repo_url(value)

    @property
    def is_description_set(self) -> bool:
        return "description" in self.model_fields_set


class EnvironmentRefResponse(ApiModel):
    id: uuid.UUID
    name: str
    kind: str

    @classmethod
    def from_model(cls, environment: Environment) -> Self:
        return cls(id=environment.id, name=environment.name, kind=environment.kind)


class CurrentDeploymentResponse(ApiModel):
    id: uuid.UUID
    commit_sha: str | None
    image_digest: str
    finished_at: datetime | None


class ServiceEnvironmentResponse(ApiModel):
    """서비스가 올라간 환경 하나. role 은 traffic_weight 와 현재 배포로 계산한 값이다.

    simulated 는 현재 배포를 만든 요청의 값(가짜 실행기면 true), 현재 배포가 없으면 null 이다.
    """

    service_environment_id: uuid.UUID
    environment: EnvironmentRefResponse
    role: ServiceEnvironmentRole
    traffic_weight: int
    url: str
    current_deployment: CurrentDeploymentResponse | None
    version_status: VersionStatus | None
    last_sync_error: DeployRequestErrorCode | None
    active_request_id: uuid.UUID | None
    queued_request_id: uuid.UUID | None
    simulated: bool | None
    teardown_error: TeardownErrorCode | None

    @classmethod
    def from_view(cls, view: ServiceEnvironmentView) -> Self:
        connection = view.service_environment
        current = view.current_deployment
        return cls(
            service_environment_id=connection.id,
            environment=EnvironmentRefResponse.from_model(view.environment),
            role=view.role,
            traffic_weight=connection.traffic_weight,
            url=public_url(connection.public_host),
            current_deployment=(
                CurrentDeploymentResponse(
                    id=current.deployment.id,
                    commit_sha=current.commit_sha,
                    image_digest=current.deployment.image_digest,
                    finished_at=current.deployment.finished_at,
                )
                if current is not None
                else None
            ),
            version_status=view.version_status,
            last_sync_error=view.last_sync_error,
            active_request_id=view.active_request_id,
            queued_request_id=view.queued_request_id,
            simulated=current.simulated if current is not None else None,
            teardown_error=connection.teardown_error,
        )


class ServiceSummaryResponse(ApiModel):
    """서비스 목록 한 항목. url 은 대표 주소다. simulated=true 면 가짜 소스 저장소로 등록했다."""

    id: uuid.UUID
    name: str
    description: str | None
    repo_url: str
    private: bool
    health_check_path: str
    health_check_timeout_seconds: int
    container_port: int
    url: str
    status: ServiceStatus
    delete_error: TeardownErrorCode | None
    simulated: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, service: Service) -> Self:
        return cls(
            id=service.id,
            name=service.name,
            description=service.description,
            repo_url=service.repo_url,
            private=service.repo_private,
            health_check_path=service.health_check_path,
            health_check_timeout_seconds=service.health_check_timeout_seconds,
            container_port=service.container_port,
            url=public_url(service.public_host),
            status=ServiceStatus(service.status),
            delete_error=service.delete_error,
            simulated=service.simulated,
            created_at=service.created_at,
            updated_at=service.updated_at,
        )


class ServiceDetailResponse(ServiceSummaryResponse):
    """서비스 한 건 + 올라간 환경들(만든 순서)."""

    environments: list[ServiceEnvironmentResponse]

    @classmethod
    def from_view(cls, view: ServiceDetailView) -> Self:
        summary = ServiceSummaryResponse.from_model(view.service)
        return cls(
            **summary.model_dump(),
            environments=[ServiceEnvironmentResponse.from_view(item) for item in view.environments],
        )
