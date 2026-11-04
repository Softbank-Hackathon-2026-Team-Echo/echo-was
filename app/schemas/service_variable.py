"""고객 앱 환경 변수 API 스키마(API 서버 10). 응답에 값·값 일부·길이는 없다."""

import uuid
from datetime import datetime
from typing import Self

from pydantic import SecretStr, model_validator
from pydantic_core import PydanticCustomError

from app.schemas.response import ApiModel, ApiRequestModel
from app.services.service_variable_service import SavedServiceVariable, ServiceVariableView


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


class SavedServiceVariableResponse(ServiceVariableResponse):
    simulated: bool

    @classmethod
    def from_saved(cls, saved: SavedServiceVariable) -> Self:
        return cls.model_validate(
            {**cls._fields_from_view(saved.variable), "simulated": saved.simulated}
        )
