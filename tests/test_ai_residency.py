"""AI 데이터 레지던시 요약의 순수 계산 단위 시험(AI 3 AC-6). DB 없음."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.enums import (
    AgentRunDataClass,
    AgentRunKind,
    AgentRunStatus,
    AiResidencyPolicySource,
    AiResidencyVerdict,
    AiResidencyViolationReason,
)
from app.services.ai_residency import (
    DEFAULT_RESIDENCY_POLICY,
    RECENT_RUNS_LIMIT,
    ResidencyBucket,
    ResidencyPolicy,
    ResidencyRun,
    ResidencySummary,
    decide_verdict,
    find_violations,
    summarize_residency,
)

NOW = datetime(2026, 11, 5, 5, 0, tzinfo=UTC)
FROM = NOW - timedelta(days=7)
REGION = AiResidencyViolationReason.MODEL_REGION_NOT_ALLOWED
FORBIDDEN = AiResidencyViolationReason.FORBIDDEN_CLASS_SENT_EXTERNALLY


def _no_model(**changes: Any) -> dict[str, Any]:
    return {
        "location": "gcp/asia-northeast3",
        "sandbox": True,
        "modelProvider": "NONE",
        "model": None,
        "modelRegion": None,
        "dataClasses": [],
        "externalTransfer": False,
        "externalTransferBytes": None,
    } | changes


def _anthropic(**changes: Any) -> dict[str, Any]:
    return {
        "location": "gcp/asia-northeast3",
        "sandbox": True,
        "modelProvider": "ANTHROPIC",
        "model": "claude-opus-5-5",
        "modelRegion": None,
        "dataClasses": ["CONFIG", "FINDINGS"],
        "externalTransfer": True,
        "externalTransferBytes": 18432,
    } | changes


def _run(
    processing: dict[str, Any] | None, *, hours_ago: float = 1, **changes: Any
) -> ResidencyRun:
    values: dict[str, Any] = {
        "run_id": uuid.uuid4(),
        "service_id": uuid.uuid4(),
        "kind": AgentRunKind.PRECHECK,
        "status": AgentRunStatus.SUCCEEDED,
        "finished_at": NOW - timedelta(hours=hours_ago),
        "simulated": False,
        "is_ai_reviewed": False,
        "processing": processing,
    }
    return ResidencyRun(**(values | changes))


def _summarize(*runs: ResidencyRun, is_truncated: bool = False) -> ResidencySummary:
    return summarize_residency(
        runs,
        policy=DEFAULT_RESIDENCY_POLICY,
        from_at=FROM,
        to_at=NOW,
        is_truncated=is_truncated,
    )


# --- 정책 ---


def test_default_policy_is_the_decided_constant() -> None:
    assert DEFAULT_RESIDENCY_POLICY == ResidencyPolicy(
        source=AiResidencyPolicySource.DEFAULT,
        allowed_model_regions=("asia-northeast3", "ap-northeast-1", "ap-northeast-2"),
        forbid_external_for_classes=(AgentRunDataClass.SECRETS,),
    )


# --- 위반(AD-10) ---


@pytest.mark.parametrize(
    ("processing", "expected"),
    [
        (None, ()),
        (_no_model(), ()),
        (_anthropic(modelProvider="LOCAL", externalTransfer=False), ()),
        (_anthropic(modelProvider="LOCAL", modelRegion="us-east-1"), ()),
        (_anthropic(modelRegion="asia-northeast3"), ()),
        (_anthropic(modelRegion="ap-northeast-1"), ()),
        (_anthropic(modelRegion="ap-northeast-2"), ()),
        (_anthropic(), (REGION,)),
        (_anthropic(modelRegion="us-east-1"), (REGION,)),
        (_anthropic(modelProvider="GOOGLE_VERTEX", modelRegion=None), (REGION,)),
        (_anthropic(modelRegion="ap-northeast-1", dataClasses=["SECRETS"]), (FORBIDDEN,)),
        (_anthropic(dataClasses=["CONFIG", "SECRETS"]), (REGION, FORBIDDEN)),
        (
            _anthropic(
                modelProvider="LOCAL",
                modelRegion=None,
                dataClasses=["SECRETS"],
                externalTransfer=False,
            ),
            (),
        ),
    ],
    ids=[
        "no-record",
        "no-model",
        "local-inside",
        "local-any-region",
        "allowed-asia-northeast3",
        "allowed-ap-northeast-1",
        "allowed-ap-northeast-2",
        "unknown-region",
        "region-outside",
        "vertex-unknown-region",
        "secrets-sent",
        "both-reasons",
        "secrets-kept-inside",
    ],
)
def test_find_violations_returns_reasons(
    processing: dict[str, Any] | None, expected: tuple[AiResidencyViolationReason, ...]
) -> None:
    assert find_violations(processing, DEFAULT_RESIDENCY_POLICY) == expected


# --- 판정(AD-9) ---


@pytest.mark.parametrize(
    ("sensitive", "unrecorded", "is_truncated", "expected"),
    [
        (0, 0, False, AiResidencyVerdict.ZERO),
        (0, 2, False, AiResidencyVerdict.ZERO_WITH_UNRECORDED),
        (0, 0, True, AiResidencyVerdict.ZERO_WITH_UNRECORDED),
        (1, 0, False, AiResidencyVerdict.DETECTED),
        (1, 2, True, AiResidencyVerdict.DETECTED),
    ],
)
def test_decide_verdict(
    sensitive: int, unrecorded: int, is_truncated: bool, expected: AiResidencyVerdict
) -> None:
    assert decide_verdict(sensitive, unrecorded, is_truncated=is_truncated) is expected


def test_summary_of_no_runs_is_zero_and_empty() -> None:
    summary = _summarize()

    assert summary.runs.total == 0
    assert summary.runs.recorded == 0
    assert summary.runs.unrecorded == 0
    assert summary.by_provider == ()
    assert summary.by_model_region == ()
    assert summary.external_transfer_bytes == 0
    assert summary.sensitive_external_transfer.verdict is AiResidencyVerdict.ZERO
    assert summary.sensitive_external_transfer.classes == (AgentRunDataClass.SECRETS,)
    assert summary.violations == ()
    assert summary.recent_runs == ()


def test_summary_only_recorded_clean_runs_is_zero() -> None:
    summary = _summarize(_run(_no_model()), _run(_anthropic(modelRegion="asia-northeast3")))

    assert summary.sensitive_external_transfer.runs == 0
    assert summary.sensitive_external_transfer.verdict is AiResidencyVerdict.ZERO
    assert summary.violations == ()


def test_summary_unrecorded_run_prevents_zero() -> None:
    summary = _summarize(_run(_no_model()), _run(None))

    assert summary.sensitive_external_transfer.verdict is AiResidencyVerdict.ZERO_WITH_UNRECORDED


def test_summary_secrets_sent_externally_is_detected() -> None:
    summary = _summarize(_run(_anthropic(dataClasses=["SECRETS"])), _run(None))

    assert summary.sensitive_external_transfer.runs == 1
    assert summary.sensitive_external_transfer.verdict is AiResidencyVerdict.DETECTED


def test_summary_secrets_not_sent_externally_is_not_counted() -> None:
    kept = _anthropic(modelProvider="LOCAL", dataClasses=["SECRETS"], externalTransfer=False)

    summary = _summarize(_run(kept))

    assert summary.sensitive_external_transfer.runs == 0
    assert summary.sensitive_external_transfer.verdict is AiResidencyVerdict.ZERO


def test_summary_truncated_prevents_zero_and_is_reported() -> None:
    summary = _summarize(_run(_no_model()), is_truncated=True)

    assert summary.runs.is_truncated is True
    assert summary.sensitive_external_transfer.verdict is AiResidencyVerdict.ZERO_WITH_UNRECORDED


# --- 숫자 ---


def test_summary_counts_recorded_unrecorded_sandboxed_and_transfer() -> None:
    summary = _summarize(
        _run(None),
        _run(None),
        _run(_no_model(sandbox=False)),
        _run(_no_model()),
        _run(_anthropic(externalTransfer=False, externalTransferBytes=0)),
        _run(_anthropic()),
    )

    assert summary.runs.total == 6
    assert summary.runs.recorded == 4
    assert summary.runs.unrecorded == 2
    assert summary.runs.sandboxed == 3
    assert summary.runs.external_transfer == 1
    assert summary.runs.is_truncated is False


def test_summary_sums_bytes_of_transfers_only_and_skips_null() -> None:
    summary = _summarize(
        _run(_anthropic(externalTransferBytes=100)),
        _run(_anthropic(externalTransferBytes=None)),
        _run(_anthropic(externalTransferBytes=0)),
        _run(_anthropic(externalTransferBytes=23)),
        _run(_no_model(externalTransferBytes=0)),
    )

    assert summary.external_transfer_bytes == 123


def test_summary_buckets_sort_by_runs_then_key_with_null_last() -> None:
    summary = _summarize(
        _run(_anthropic(modelRegion="us-east-1", location="aws/ap-northeast-1")),
        _run(_anthropic(modelRegion="ap-northeast-1", model="b-model")),
        _run(_anthropic(modelRegion="ap-northeast-1", model="a-model")),
        _run(_anthropic(modelRegion=None, modelProvider="OPENAI", model="a-model")),
        _run(_no_model()),
        _run(_no_model()),
        _run(None),
    )

    assert summary.by_provider == (
        ResidencyBucket("ANTHROPIC", 3),
        ResidencyBucket("NONE", 2),
        ResidencyBucket("OPENAI", 1),
    )
    assert summary.by_model == (
        ResidencyBucket("a-model", 2),
        ResidencyBucket("b-model", 1),
        ResidencyBucket("claude-opus-5-5", 1),
    )
    assert summary.by_location == (
        ResidencyBucket("gcp/asia-northeast3", 5),
        ResidencyBucket("aws/ap-northeast-1", 1),
    )
    assert summary.by_model_region == (
        ResidencyBucket("ap-northeast-1", 2),
        ResidencyBucket("us-east-1", 1),
        ResidencyBucket(None, 1),
    )
    assert summary.by_data_class == (
        ResidencyBucket("CONFIG", 4),
        ResidencyBucket("FINDINGS", 4),
    )


def test_summary_model_region_bucket_null_sorts_after_same_count() -> None:
    summary = _summarize(_run(_anthropic(modelRegion=None)), _run(_anthropic(modelRegion="zz")))

    assert summary.by_model_region == (ResidencyBucket("zz", 1), ResidencyBucket(None, 1))


def test_summary_model_region_excludes_no_model_but_includes_local() -> None:
    summary = _summarize(
        _run(_no_model()),
        _run(_anthropic(modelProvider="LOCAL", modelRegion=None, externalTransfer=False)),
    )

    assert summary.by_model_region == (ResidencyBucket(None, 1),)


# --- 목록 ---


def test_summary_lists_violations_and_recent_runs_latest_first() -> None:
    old_violation = _run(_anthropic(), hours_ago=3)
    clean = _run(_no_model(), hours_ago=2)
    new_violation = _run(_anthropic(dataClasses=["SECRETS"]), hours_ago=1)

    summary = _summarize(old_violation, clean, new_violation)

    assert [item.run for item in summary.violations] == [new_violation, old_violation]
    assert summary.violations[0].reasons == (REGION, FORBIDDEN)
    assert [item.run for item in summary.recent_runs] == [new_violation, clean, old_violation]
    assert [item.reasons for item in summary.recent_runs] == [(REGION, FORBIDDEN), (), (REGION,)]


def test_summary_limits_recent_runs_but_not_violations_or_counts() -> None:
    runs = [_run(_anthropic(), hours_ago=index + 1) for index in range(RECENT_RUNS_LIMIT + 5)]

    summary = _summarize(*runs)

    assert len(summary.recent_runs) == RECENT_RUNS_LIMIT
    assert summary.recent_runs[0].run == runs[0]
    assert len(summary.violations) == RECENT_RUNS_LIMIT + 5
    assert summary.runs.total == RECENT_RUNS_LIMIT + 5
