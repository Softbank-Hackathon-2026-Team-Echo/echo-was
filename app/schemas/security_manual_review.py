"""사람 점검 기록 API 요청 스키마. 응답은 증거 조회와 같은 `SecurityFindingResponse` 다."""

import uuid
from typing import Annotated

from pydantic import Field, field_validator

from app.enums import SecurityControlId, SecurityResult
from app.schemas.response import ApiRequestModel
from app.services.security_manual_review_service import RecordManualReviewCommand

MANUAL_REVIEW_SUMMARY_MAX_LENGTH = 1000
MANUAL_REVIEW_EVIDENCE_URL_MAX_LENGTH = 2000


class RecordManualReviewRequest(ApiRequestModel):
    """범위 칸은 그 통제가 정한 것만 채운다(나머지는 null 또는 생략). 요약·근거 링크는 필수."""

    control_id: SecurityControlId
    service_id: uuid.UUID | None = None
    environment_id: uuid.UUID | None = None
    result: SecurityResult
    summary: Annotated[str, Field(max_length=MANUAL_REVIEW_SUMMARY_MAX_LENGTH)]
    evidence_url: Annotated[str, Field(max_length=MANUAL_REVIEW_EVIDENCE_URL_MAX_LENGTH)]

    @field_validator("summary", "evidence_url")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    def to_command(self) -> RecordManualReviewCommand:
        return RecordManualReviewCommand(
            control_id=self.control_id,
            result=self.result,
            summary=self.summary,
            evidence_url=self.evidence_url,
            service_id=self.service_id,
            environment_id=self.environment_id,
        )
