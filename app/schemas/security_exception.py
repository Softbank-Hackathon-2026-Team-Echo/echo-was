"""기한부 예외 API 요청·응답 스키마.

JSON 이름은 camelCase, 모든 응답 칸은 항상 있고 값이 없으면 null.
"""

import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import AwareDatetime, Field, field_validator

from app.enums import SecurityControlId, SecurityExceptionStatus
from app.models.deployment import IMAGE_DIGEST_PATTERN
from app.models.security_exception import SECURITY_EXCEPTION_REASON_MAX_LENGTH
from app.schemas.migration_plan import UserRefResponse
from app.schemas.response import ApiModel, ApiRequestModel
from app.services.security_exception_service import (
    CreateSecurityExceptionCommand,
    SecurityExceptionView,
)

CONFIG_COMMIT_PATTERN = "^[0-9a-f]{40}$"
WAS_VERSION_MAX_LENGTH = 64


class CreateSecurityExceptionRequest(ApiRequestModel):
    """범위 칸은 그 통제가 정한 것만 채운다(나머지는 null 또는 생략). 만료는 지금부터 7일 이하."""

    control_id: SecurityControlId
    service_id: uuid.UUID | None = None
    environment_id: uuid.UUID | None = None
    image_digest: Annotated[str, Field(pattern=IMAGE_DIGEST_PATTERN)] | None = None
    config_commit: Annotated[str, Field(pattern=CONFIG_COMMIT_PATTERN)] | None = None
    was_version: Annotated[str, Field(min_length=1, max_length=WAS_VERSION_MAX_LENGTH)] | None = (
        None
    )
    reason: Annotated[str, Field(max_length=SECURITY_EXCEPTION_REASON_MAX_LENGTH)]
    expires_at: AwareDatetime

    @field_validator("reason")
    @classmethod
    def _strip_reason(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reason must not be blank")
        return stripped

    def to_command(self) -> CreateSecurityExceptionCommand:
        return CreateSecurityExceptionCommand(
            control_id=self.control_id,
            reason=self.reason,
            expires_at=self.expires_at,
            service_id=self.service_id,
            environment_id=self.environment_id,
            image_digest=self.image_digest,
            config_commit=self.config_commit,
            was_version=self.was_version,
        )


class SecurityExceptionResponse(ApiModel):
    id: uuid.UUID
    control_id: SecurityControlId
    status: SecurityExceptionStatus
    service_id: uuid.UUID | None
    environment_id: uuid.UUID | None
    image_digest: str | None
    config_commit: str | None
    was_version: str | None
    reason: str
    requested_by: UserRefResponse | None
    approved_by: UserRefResponse | None
    approved_at: datetime | None
    revoked_by: UserRefResponse | None
    revoked_at: datetime | None
    created_at: datetime
    expires_at: datetime

    @classmethod
    def from_view(cls, view: SecurityExceptionView) -> Self:
        exception = view.exception
        return cls(
            id=exception.id,
            control_id=SecurityControlId(exception.control_id),
            status=view.status,
            service_id=exception.service_id,
            environment_id=exception.environment_id,
            image_digest=exception.image_digest,
            config_commit=exception.config_commit,
            was_version=exception.was_version,
            reason=exception.reason,
            requested_by=UserRefResponse.from_model(view.user(exception.requested_by_user_id)),
            approved_by=UserRefResponse.from_model(view.user(exception.approved_by_user_id)),
            approved_at=exception.approved_at,
            revoked_by=UserRefResponse.from_model(view.user(exception.revoked_by_user_id)),
            revoked_at=exception.revoked_at,
            created_at=exception.created_at,
            expires_at=exception.expires_at,
        )
