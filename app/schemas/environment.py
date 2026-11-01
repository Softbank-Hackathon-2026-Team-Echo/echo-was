"""환경 API 요청·응답 스키마. JSON 이름은 camelCase, 모든 응답 칸은 항상 있고 값이 없으면 null."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import Field, StringConstraints, field_validator
from pydantic_core import PydanticCustomError

from app.adapters.base import ClusterInfo, EnvironmentConfig
from app.core.secrets import InvalidSecretRefError, parse_secret_ref
from app.enums import ConnectionErrorCode, EnvironmentConnectionStatus
from app.models.base import RESOURCE_NAME_MAX_LENGTH, RESOURCE_NAME_PATTERN
from app.models.environment import ENVIRONMENT_KIND_MAX_LENGTH
from app.schemas.response import ApiModel, ApiRequestModel
from app.services.environment_service import EnvironmentDetailView, EnvironmentView

ResourceName = Annotated[
    str, StringConstraints(pattern=RESOURCE_NAME_PATTERN, max_length=RESOURCE_NAME_MAX_LENGTH)
]
EnvironmentKindName = Annotated[
    str, StringConstraints(min_length=1, max_length=ENVIRONMENT_KIND_MAX_LENGTH)
]

# 응답 details.reason 에 들어가는 고정 문구. 입력 값·정규식을 넣지 않는다.
INVALID_SECRET_REF_REASON = "INVALID_SECRET_REF"
NULL_NOT_ALLOWED_REASON = "NULL_NOT_ALLOWED"


def _validate_secret_ref(value: str) -> str:
    try:
        parse_secret_ref(value)
    except InvalidSecretRefError:
        raise PydanticCustomError("invalid_secret_ref", INVALID_SECRET_REF_REASON) from None
    return value


class CreateEnvironmentRequest(ApiRequestModel):
    """환경 등록. config 키는 camelCase 다. 비밀값은 받지 않고 보관 위치(secretRef)만 받는다."""

    name: ResourceName
    kind: EnvironmentKindName
    config: dict[str, Any]
    secret_ref: str

    @field_validator("secret_ref")
    @classmethod
    def _check_secret_ref(cls, value: str) -> str:
        return _validate_secret_ref(value)


class UpdateEnvironmentRequest(ApiRequestModel):
    """환경 수정. 들어온 칸만 바꾼다. config 는 전체 교체다. null 은 거부한다."""

    name: ResourceName | None = None
    kind: EnvironmentKindName | None = None
    config: dict[str, Any] | None = None
    secret_ref: str | None = None

    @field_validator("name", "kind", "config", "secret_ref", mode="before")
    @classmethod
    def _reject_null(cls, value: object) -> object:
        # 기본값(빠진 칸)에는 불리지 않고, 요청에 null 이 직접 들어왔을 때만 불린다.
        if value is None:
            raise PydanticCustomError("null_not_allowed", NULL_NOT_ALLOWED_REASON)
        return value

    @field_validator("secret_ref")
    @classmethod
    def _check_secret_ref(cls, value: str | None) -> str | None:
        return None if value is None else _validate_secret_ref(value)


class ClusterInfoResponse(ApiModel):
    """클러스터 정보. simulated=true 면 가짜 어댑터가 만든 값이다(실제 클러스터에 붙지 않음)."""

    server_version: str
    node_count: int
    simulated: bool

    @classmethod
    def from_cluster_info(cls, cluster_info: ClusterInfo) -> Self:
        return cls(
            server_version=cluster_info.server_version,
            node_count=cluster_info.node_count,
            simulated=cluster_info.simulated,
        )


class EnvironmentSummaryResponse(ApiModel):
    """환경 목록 한 항목. 저장된 값만 담는다.

    location·config 는 저장된 config 를 지금 양식으로 읽지 못하면 null 이다.
    다른 파트는 환경을 id 로만 가리킨다. name 은 표시용이며 바뀔 수 있다.
    """

    id: uuid.UUID
    name: str
    kind: str
    location: str | None
    config: dict[str, Any] | None
    secret_ref: str | None
    connection_status: EnvironmentConnectionStatus
    connection_checked_at: datetime | None
    connection_error: ConnectionErrorCode | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_view(cls, view: EnvironmentView) -> Self:
        return cls(**_summary_fields(view))


class EnvironmentDetailResponse(EnvironmentSummaryResponse):
    """환경 한 건 + 그 자리에서 읽은 클러스터 정보.

    읽지 못하면 clusterInfo=null 이고 이유는 clusterInfoError 에 있다.
    """

    cluster_info: ClusterInfoResponse | None
    cluster_info_error: ConnectionErrorCode | None

    @classmethod
    def from_detail_view(cls, view: EnvironmentDetailView) -> Self:
        return cls(
            **_summary_fields(view),
            cluster_info=(
                ClusterInfoResponse.from_cluster_info(view.cluster_info)
                if view.cluster_info is not None
                else None
            ),
            cluster_info_error=view.cluster_info_error,
        )


class EnvironmentKindResponse(ApiModel):
    """등록된 환경 종류와 그 config 의 JSON Schema(camelCase 키)."""

    kind: str
    config_schema: dict[str, Any] = Field(description="config 의 JSON Schema(camelCase 키)")

    @classmethod
    def from_config_class(cls, config_class: type[EnvironmentConfig]) -> Self:
        return cls(
            kind=config_class.kind, config_schema=config_class.model_json_schema(by_alias=True)
        )


def _summary_fields(view: EnvironmentView) -> dict[str, Any]:
    environment = view.environment
    return {
        "id": environment.id,
        "name": environment.name,
        "kind": environment.kind,
        "location": view.config.display_location() if view.config is not None else None,
        "config": (
            view.config.model_dump(mode="json", by_alias=True) if view.config is not None else None
        ),
        "secret_ref": environment.secret_ref,
        "connection_status": environment.connection_status,
        "connection_checked_at": environment.connection_checked_at,
        "connection_error": environment.connection_error,
        "created_at": environment.created_at,
        "updated_at": environment.updated_at,
    }
