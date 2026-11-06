"""AI 실행 데이터 레지던시 요약 계산(순수 계산, DB·네트워크 없음).

러너가 낸 기록을 다시 계산하지 않고 그대로 센다. 기록이 없는 실행은 지어내지 않고 "기록 없음"으로
센다. processing 은 저장한 camelCase 값이다.
"""

import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, assert_never

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

RECENT_RUNS_LIMIT = 20
# 요약에서 빼는 실행 종류. 배포 실패 진단(CHANGE_PROPOSAL, SPEC api-98)은 사전 점검 증거가 아니고
# 레지던시 숫자·판정·위반에도 넣지 않는다.
RESIDENCY_EXCLUDED_KINDS = frozenset({AgentRunKind.CHANGE_PROPOSAL})
_PROVIDERS_WITHOUT_REGION_RULE = (AgentRunModelProvider.NONE, AgentRunModelProvider.LOCAL)


@dataclass(frozen=True)
class RegionlessProvider:
    """리전을 못 고르는 제공자의 조건부 허용 항목(AD-12). modelRegion null 일 때만 본다."""

    provider: AgentRunModelProvider
    condition: AiResidencyRegionlessCondition


@dataclass(frozen=True)
class ResidencyPolicy:
    source: AiResidencyPolicySource
    allowed_model_regions: tuple[str, ...]
    forbid_external_for_classes: tuple[AgentRunDataClass, ...]
    allowed_regionless_providers: tuple[RegionlessProvider, ...]


DEFAULT_RESIDENCY_POLICY = ResidencyPolicy(
    source=AiResidencyPolicySource.DEFAULT,
    allowed_model_regions=("asia-northeast3", "ap-northeast-1", "ap-northeast-2"),
    forbid_external_for_classes=(AgentRunDataClass.SECRETS,),
    # Anthropic 1P(api.anthropic.com)는 리전을 고를 수 없다. 금지 등급을 안 넘겼을 때만 허용한다.
    allowed_regionless_providers=(
        RegionlessProvider(
            provider=AgentRunModelProvider.ANTHROPIC,
            condition=AiResidencyRegionlessCondition.NO_FORBIDDEN_CLASSES,
        ),
    ),
)


@dataclass(frozen=True)
class ResidencyRun:
    run_id: uuid.UUID
    service_id: uuid.UUID
    kind: AgentRunKind
    status: AgentRunStatus
    finished_at: datetime
    simulated: bool
    is_ai_reviewed: bool
    processing: Mapping[str, Any] | None


@dataclass(frozen=True)
class ResidencyRunCounts:
    total: int
    recorded: int
    unrecorded: int
    sandboxed: int
    external_transfer: int
    is_truncated: bool


@dataclass(frozen=True)
class ResidencyBucket:
    key: str | None
    runs: int


@dataclass(frozen=True)
class SensitiveExternalTransfer:
    classes: tuple[AgentRunDataClass, ...]
    runs: int
    verdict: AiResidencyVerdict


@dataclass(frozen=True)
class ResidencyViolation:
    run: ResidencyRun
    reasons: tuple[AiResidencyViolationReason, ...]
    is_regionless_allowed: bool = False


@dataclass(frozen=True)
class ResidencySummary:
    from_at: datetime
    to_at: datetime
    policy: ResidencyPolicy
    runs: ResidencyRunCounts
    by_provider: tuple[ResidencyBucket, ...]
    by_model: tuple[ResidencyBucket, ...]
    by_location: tuple[ResidencyBucket, ...]
    by_model_region: tuple[ResidencyBucket, ...]
    by_data_class: tuple[ResidencyBucket, ...]
    external_transfer_bytes: int
    sensitive_external_transfer: SensitiveExternalTransfer
    violations: tuple[ResidencyViolation, ...]
    recent_runs: tuple[ResidencyViolation, ...]


def sends_forbidden_class(processing: Mapping[str, Any], policy: ResidencyPolicy) -> bool:
    """금지 등급을 밖으로 보냈는가. 모델에 넘겼어도 밖으로 나가지 않았으면 아니다."""
    if not processing["externalTransfer"]:
        return False
    return not set(policy.forbid_external_for_classes).isdisjoint(processing["dataClasses"])


def _meets_condition(
    condition: AiResidencyRegionlessCondition,
    processing: Mapping[str, Any],
    policy: ResidencyPolicy,
) -> bool:
    match condition:
        case AiResidencyRegionlessCondition.NO_FORBIDDEN_CLASSES:
            # 밖으로 나갔는지와 상관없이 모델에 넘긴 등급만 본다(넘겼으면 조건 불충족).
            return set(policy.forbid_external_for_classes).isdisjoint(processing["dataClasses"])
        case _:
            assert_never(condition)


def is_regionless_allowed(processing: Mapping[str, Any], policy: ResidencyPolicy) -> bool:
    """리전 없이(modelRegion null) 조건부 허용 항목으로 리전 규칙을 통과하는가(AD-12).

    허용 리전 목록에 null 을 넣지 않고 제공자·조건으로 따로 본다. 리전을 밝혔으면 목록으로만 본다.
    """
    if processing["modelRegion"] is not None:
        return False
    return any(
        item.provider == processing["modelProvider"]
        and _meets_condition(item.condition, processing, policy)
        for item in policy.allowed_regionless_providers
    )


def find_violations(
    processing: Mapping[str, Any] | None, policy: ResidencyPolicy
) -> tuple[AiResidencyViolationReason, ...]:
    """기록의 정책 위반 이유. 기록이 없으면 판단할 것이 없어 위반도 없다."""
    if processing is None:
        return ()
    reasons = []
    if (
        processing["modelProvider"] not in _PROVIDERS_WITHOUT_REGION_RULE
        and processing["modelRegion"] not in policy.allowed_model_regions
        and not is_regionless_allowed(processing, policy)
    ):
        reasons.append(AiResidencyViolationReason.MODEL_REGION_NOT_ALLOWED)
    if sends_forbidden_class(processing, policy):
        reasons.append(AiResidencyViolationReason.FORBIDDEN_CLASS_SENT_EXTERNALLY)
    return tuple(reasons)


def decide_verdict(
    sensitive_runs: int, unrecorded_runs: int, *, is_truncated: bool = False
) -> AiResidencyVerdict:
    """읽지 못한 실행이 있으면(is_truncated) 기록 없는 실행처럼 0건이라고 단정하지 않는다."""
    if sensitive_runs > 0:
        return AiResidencyVerdict.DETECTED
    if unrecorded_runs > 0 or is_truncated:
        return AiResidencyVerdict.ZERO_WITH_UNRECORDED
    return AiResidencyVerdict.ZERO


def _buckets(keys: Iterable[str | None]) -> tuple[ResidencyBucket, ...]:
    """건수 내림차순 → 키 오름차순(null 은 같은 건수의 맨 뒤)."""
    counts = Counter(keys)
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0] is None, item[0] or ""))
    return tuple(ResidencyBucket(key, runs) for key, runs in ordered)


def summarize_residency(
    runs: Sequence[ResidencyRun],
    *,
    policy: ResidencyPolicy,
    from_at: datetime,
    to_at: datetime,
    is_truncated: bool = False,
) -> ResidencySummary:
    """기간 안에 끝난 실행을 센다. 응답의 숫자·판정·위반은 모두 여기서 정한다.

    RESIDENCY_EXCLUDED_KINDS(배포 실패 진단)는 세지 않는다.
    """
    ordered = sorted(
        (run for run in runs if run.kind not in RESIDENCY_EXCLUDED_KINDS),
        key=lambda run: (run.finished_at, run.run_id),
        reverse=True,
    )
    records = [run.processing for run in ordered if run.processing is not None]
    unrecorded = len(ordered) - len(records)
    transferred = [record for record in records if record["externalTransfer"]]
    sensitive_runs = sum(1 for record in records if sends_forbidden_class(record, policy))
    judged = [
        ResidencyViolation(
            run,
            find_violations(run.processing, policy),
            is_regionless_allowed=(
                run.processing is not None and is_regionless_allowed(run.processing, policy)
            ),
        )
        for run in ordered
    ]
    return ResidencySummary(
        from_at=from_at,
        to_at=to_at,
        policy=policy,
        runs=ResidencyRunCounts(
            total=len(ordered),
            recorded=len(records),
            unrecorded=unrecorded,
            sandboxed=sum(1 for record in records if record["sandbox"]),
            external_transfer=len(transferred),
            is_truncated=is_truncated,
        ),
        by_provider=_buckets(record["modelProvider"] for record in records),
        by_model=_buckets(record["model"] for record in records if record["model"] is not None),
        by_location=_buckets(record["location"] for record in records),
        by_model_region=_buckets(
            record["modelRegion"]
            for record in records
            if record["modelProvider"] != AgentRunModelProvider.NONE
        ),
        by_data_class=_buckets(
            data_class for record in records for data_class in record["dataClasses"]
        ),
        external_transfer_bytes=sum(record["externalTransferBytes"] or 0 for record in transferred),
        sensitive_external_transfer=SensitiveExternalTransfer(
            classes=policy.forbid_external_for_classes,
            runs=sensitive_runs,
            verdict=decide_verdict(sensitive_runs, unrecorded, is_truncated=is_truncated),
        ),
        violations=tuple(item for item in judged if item.reasons),
        recent_runs=tuple(judged[:RECENT_RUNS_LIMIT]),
    )
