"""AI 데이터 레지던시 응답 스키마. 실행 기록 모양은 사전 점검 상세와 같이 쓴다."""

import uuid
from datetime import datetime
from typing import Self

from pydantic import Field

from app.enums import (
    AgentRunDataClass,
    AgentRunKind,
    AgentRunModelProvider,
    AgentRunStatus,
    AiResidencyPolicySource,
    AiResidencyRegionlessCondition,
    AiResidencyVerdict,
    AiResidencyViolationReason,
)
from app.schemas.response import ApiModel
from app.services.ai_residency import ResidencyBucket, ResidencySummary, ResidencyViolation


class AgentRunProcessingResponse(ApiModel):
    location: str
    sandbox: bool
    model_provider: AgentRunModelProvider
    model: str | None
    model_region: str | None
    data_classes: list[AgentRunDataClass]
    external_transfer: bool
    external_transfer_bytes: int | None


class AiResidencyRegionlessProviderResponse(ApiModel):
    """리전 없이 조건부로 허용하는 제공자(AD-12)."""

    provider: AgentRunModelProvider
    condition: AiResidencyRegionlessCondition


class AiResidencyPolicyResponse(ApiModel):
    source: AiResidencyPolicySource
    allowed_model_regions: list[str]
    forbid_external_for_classes: list[AgentRunDataClass]
    allowed_regionless_providers: list[AiResidencyRegionlessProviderResponse]


class AiResidencyRunCountsResponse(ApiModel):
    total: int
    recorded: int
    unrecorded: int
    sandboxed: int
    external_transfer: int
    truncated: bool


class AiResidencyBucketResponse(ApiModel):
    key: str | None
    runs: int

    @classmethod
    def from_bucket(cls, bucket: ResidencyBucket) -> Self:
        return cls(key=bucket.key, runs=bucket.runs)


class SensitiveExternalTransferResponse(ApiModel):
    classes: list[AgentRunDataClass]
    runs: int
    verdict: AiResidencyVerdict


class AiResidencyViolationResponse(ApiModel):
    run_id: uuid.UUID
    service_id: uuid.UUID
    finished_at: datetime
    reasons: list[AiResidencyViolationReason]

    @classmethod
    def from_violation(cls, violation: ResidencyViolation) -> Self:
        return cls(
            run_id=violation.run.run_id,
            service_id=violation.run.service_id,
            finished_at=violation.run.finished_at,
            reasons=list(violation.reasons),
        )


class AiResidencyRecentRunResponse(ApiModel):
    run_id: uuid.UUID
    service_id: uuid.UUID
    kind: AgentRunKind
    status: AgentRunStatus
    finished_at: datetime
    simulated: bool
    ai_reviewed: bool
    processing: AgentRunProcessingResponse | None
    violations: list[AiResidencyViolationReason]
    regionless_allowed: bool = Field(
        description="리전 없이(modelRegion null) 조건부 허용 항목으로 리전 규칙을 통과했는가(AD-12)"
    )

    @classmethod
    def from_violation(cls, violation: ResidencyViolation) -> Self:
        run = violation.run
        return cls(
            run_id=run.run_id,
            service_id=run.service_id,
            kind=run.kind,
            status=run.status,
            finished_at=run.finished_at,
            simulated=run.simulated,
            ai_reviewed=run.is_ai_reviewed,
            processing=(
                AgentRunProcessingResponse.model_validate(run.processing)
                if run.processing is not None
                else None
            ),
            violations=list(violation.reasons),
            regionless_allowed=violation.is_regionless_allowed,
        )


class AiResidencySummaryResponse(ApiModel):
    from_: datetime = Field(alias="from")
    to: datetime
    policy: AiResidencyPolicyResponse
    runs: AiResidencyRunCountsResponse
    by_provider: list[AiResidencyBucketResponse]
    by_model: list[AiResidencyBucketResponse]
    by_location: list[AiResidencyBucketResponse]
    by_model_region: list[AiResidencyBucketResponse]
    by_data_class: list[AiResidencyBucketResponse]
    external_transfer_bytes: int
    sensitive_external_transfer: SensitiveExternalTransferResponse
    violations: list[AiResidencyViolationResponse]
    recent_runs: list[AiResidencyRecentRunResponse]

    @classmethod
    def from_summary(cls, summary: ResidencySummary) -> Self:
        policy = summary.policy
        runs = summary.runs
        sensitive = summary.sensitive_external_transfer
        return cls(
            from_=summary.from_at,
            to=summary.to_at,
            policy=AiResidencyPolicyResponse(
                source=policy.source,
                allowed_model_regions=list(policy.allowed_model_regions),
                forbid_external_for_classes=list(policy.forbid_external_for_classes),
                allowed_regionless_providers=[
                    AiResidencyRegionlessProviderResponse(
                        provider=item.provider, condition=item.condition
                    )
                    for item in policy.allowed_regionless_providers
                ],
            ),
            runs=AiResidencyRunCountsResponse(
                total=runs.total,
                recorded=runs.recorded,
                unrecorded=runs.unrecorded,
                sandboxed=runs.sandboxed,
                external_transfer=runs.external_transfer,
                truncated=runs.is_truncated,
            ),
            by_provider=[AiResidencyBucketResponse.from_bucket(b) for b in summary.by_provider],
            by_model=[AiResidencyBucketResponse.from_bucket(b) for b in summary.by_model],
            by_location=[AiResidencyBucketResponse.from_bucket(b) for b in summary.by_location],
            by_model_region=[
                AiResidencyBucketResponse.from_bucket(b) for b in summary.by_model_region
            ],
            by_data_class=[AiResidencyBucketResponse.from_bucket(b) for b in summary.by_data_class],
            external_transfer_bytes=summary.external_transfer_bytes,
            sensitive_external_transfer=SensitiveExternalTransferResponse(
                classes=list(sensitive.classes), runs=sensitive.runs, verdict=sensitive.verdict
            ),
            violations=[
                AiResidencyViolationResponse.from_violation(item) for item in summary.violations
            ],
            recent_runs=[
                AiResidencyRecentRunResponse.from_violation(item) for item in summary.recent_runs
            ],
        )
