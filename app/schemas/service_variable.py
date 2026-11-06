"""고객 앱 환경 변수 API 스키마(API 서버 10). 응답에 값·값 일부·길이는 없다."""

import uuid
from datetime import datetime
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_core import PydanticCustomError

from app.schemas.response import ApiModel, ApiRequestModel
from app.services.service_variable_service import (
    COPY_KEYS_MAX,
    COPY_TARGETS_MAX,
    CopyServiceVariablesCommand,
    SavedServiceVariable,
    ServiceVariableCopyResult,
    ServiceVariableView,
)


class PutServiceVariableRequest(ApiRequestModel):
    """값을 넣거나 바꾼다. 범위 검사(1~16,384바이트)는 서비스가 고정 코드로 거절한다."""

    value: SecretStr

    @model_validator(mode="before")
    @classmethod
    def _reject_unknown_fields(cls, data: object) -> object:
        # 기본 거부(extra="forbid")는 모르는 칸 이름을 응답 details 에 그대로 싣는다. 이 본문에서는
        # 칸 이름 자리에 값을 잘못 넣은 요청도 값을 되돌려 주면 안 된다.
        if isinstance(data, dict) and set(data) - {"value"}:
            raise PydanticCustomError("unknown_field", "UNKNOWN_FIELD")
        return data


class ServiceVariableUpdatedByResponse(ApiModel):
    id: uuid.UUID
    github_login: str


class ServiceVariableResponse(ApiModel):
    key: str
    version: int
    secret_ref: str
    updated_at: datetime
    updated_by: ServiceVariableUpdatedByResponse
    # 연결의 현재 배포 기록에 고정된 버전. 없으면 null.
    applied_version: int | None
    # appliedVersion ≠ version. 앱은 다시 배포해야 바뀐다.
    pending_redeploy: bool

    @classmethod
    def _fields_from_view(cls, view: ServiceVariableView) -> dict[str, object]:
        return {
            "key": view.key,
            "version": view.version,
            "secret_ref": view.secret_ref,
            "updated_at": view.updated_at,
            "updated_by": ServiceVariableUpdatedByResponse(
                id=view.updated_by_id, github_login=view.updated_by_github_login
            ),
            "applied_version": view.applied_version,
            "pending_redeploy": view.pending_redeploy,
        }

    @classmethod
    def from_view(cls, view: ServiceVariableView) -> Self:
        return cls.model_validate(cls._fields_from_view(view))


class ServiceVariableListResponse(ApiModel):
    items: list[ServiceVariableResponse]
    # 가짜 보관함(APP_SECRET_STORE_MODE=fake)이면 true.
    simulated: bool
    # 그 환경 연결에 현재 배포가 있는가. 첫 배포 전이면 false(넣은 값은 첫 배포 때 적용된다).
    deployed: bool


class SavedServiceVariableResponse(ServiceVariableResponse):
    simulated: bool
    deployed: bool

    @classmethod
    def from_saved(cls, saved: SavedServiceVariable) -> Self:
        return cls.model_validate(
            {
                **cls._fields_from_view(saved.variable),
                "simulated": saved.simulated,
                "deployed": saved.deployed,
            }
        )


class CopyServiceVariablesRequest(ApiRequestModel):
    """원본 환경의 키를 대상 환경으로 복사한다(이슈 #99). 값은 본문·응답 어디에도 없다.

    같은 환경을 원본·대상에 함께 넣거나 중복된 대상·키는 서비스가 VALIDATION_ERROR 로 거절한다.
    """

    source_environment_id: uuid.UUID
    target_environment_ids: list[uuid.UUID] = Field(min_length=1, max_length=COPY_TARGETS_MAX)
    keys: list[str] = Field(min_length=1, max_length=COPY_KEYS_MAX)
    # DATABASE_URL 처럼 환경마다 달라야 하는 값을 덮지 않게 기본은 false.
    overwrite: bool = False

    def to_command(self) -> CopyServiceVariablesCommand:
        return CopyServiceVariablesCommand(
            source_environment_id=self.source_environment_id,
            target_environment_ids=list(self.target_environment_ids),
            keys=list(self.keys),
            overwrite=self.overwrite,
        )


class CopiedServiceVariableResponse(ApiModel):
    environment_id: uuid.UUID
    key: str
    version: int
    # 대상 연결에서 appliedVersion ≠ version(넣기와 같은 규칙). 복사 직후라 늘 true 다.
    pending_redeploy: bool


class FailedServiceVariableCopyResponse(ApiModel):
    environment_id: uuid.UUID
    key: str
    # ENV_VAR_ALREADY_EXISTS · ENV_VAR_NOT_FOUND · ENV_VAR_LIMIT_EXCEEDED · ENV_SECRET_UNAVAILABLE ·
    # MIGRATION_IN_PROGRESS · PUBLIC_HOST_CONFLICT · SERVICE_ENVIRONMENT_NOT_FOUND ·
    # SECRET_STORE_UNAVAILABLE · SECRET_STORE_FORBIDDEN
    code: str


class ServiceVariableCopyResponse(ApiModel):
    copied: list[CopiedServiceVariableResponse]
    failed: list[FailedServiceVariableCopyResponse]
    # 가짜 보관함(APP_SECRET_STORE_MODE=fake)이면 true.
    simulated: bool

    @classmethod
    def from_result(cls, result: ServiceVariableCopyResult) -> Self:
        return cls(
            copied=[
                CopiedServiceVariableResponse(
                    environment_id=item.environment_id,
                    key=item.key,
                    version=item.version,
                    pending_redeploy=item.pending_redeploy,
                )
                for item in result.copied
            ],
            failed=[
                FailedServiceVariableCopyResponse(
                    environment_id=item.environment_id, key=item.key, code=item.code
                )
                for item in result.failed
            ],
            simulated=result.simulated,
        )
