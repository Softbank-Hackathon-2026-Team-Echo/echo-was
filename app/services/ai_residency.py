"""AI 실행 데이터 레지던시 요약 계산(순수 계산, DB·네트워크 없음).

러너가 낸 기록을 다시 계산하지 않고 그대로 센다. 기록이 없는 실행은 지어내지 않고 "기록 없음"으로
센다. processing 은 저장한 camelCase 값이다.
"""

import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.enums import (
    AgentRunDataClass,
    AgentRunKind,
    AgentRunModelProvider,
    AgentRunStatus,
    AiResidencyPolicySource,
    AiResidencyVerdict,
    AiResidencyViolationReason,
)

RECENT_RUNS_LIMIT = 20
_PROVIDERS_WITHOUT_REGION_RULE = (AgentRunModelProvider.NONE, AgentRunModelProvider.LOCAL)


@dataclass(frozen=True)
class ResidencyPolicy:
    source: AiResidencyPolicySource
    allowed_model_regions: tuple[str, ...]
    forbid_external_for_classes: tuple[AgentRunDataClass, ...]


DEFAULT_RESIDENCY_POLICY = ResidencyPolicy(
    source=AiResidencyPolicySource.DEFAULT,
    allowed_model_regions=("asia-northeast3", "ap-northeast-1", "ap-northeast-2"),
    forbid_external_for_classes=(AgentRunDataClass.SECRETS,),
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
    """기간 안에 끝난 실행을 센다. 응답의 숫자·판정·위반은 모두 여기서 정한다."""
    ordered = sorted(runs, key=lambda run: (run.finished_at, run.run_id), reverse=True)
    records = [run.processing for run in ordered if run.processing is not None]
    unrecorded = len(ordered) - len(records)
    transferred = [record for record in records if record["externalTransfer"]]
    sensitive_runs = sum(1 for record in records if sends_forbidden_class(record, policy))
    judged = [ResidencyViolation(run, find_violations(run.processing, policy)) for run in ordered]
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
