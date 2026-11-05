"""청구 실측 갱신 명령(운영 1). 운영자가 받은 Cost Explorer 응답을 cloud_billing.json 에 병합한다.

    python -m app.scripts.import_billing --provider aws --input <CE JSON> \
        [--output <경로>] [--fetched-at 2026-11-05T05:50:00Z]

입력은 `aws ce get-cost-and-usage --granularity DAILY --metrics UnblendedCost
--group-by Type=DIMENSION,Key=RECORD_TYPE` 의 응답 그대로다. 네트워크를 부르지 않고 DB·환경 변수도
읽지 않는다. 같은 날짜는 덮어쓰고 다른 날짜는 그대로 두며 날짜순으로 저장한다. 결과는 다음 배포로
서버에 실린다.
"""

import argparse
import datetime
import json
import sys
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.core.clock import SystemClock
from app.services.cost_catalog import (
    BILLING_PATH,
    BillingDay,
    BillingFile,
    BillingProvider,
    CostCatalogError,
    load_billing_file,
)

_AWS_PROVIDER = "aws"
_AWS_SOURCE = "AWS Cost Explorer GetCostAndUsage DAILY UnblendedCost, RECORD_TYPE 묶음"
_COST_METRIC = "UnblendedCost"
_CURRENCY = "USD"
_USAGE_RECORD = "Usage"
_CREDIT_RECORD = "Credit"
_AMOUNT_QUANTUM = Decimal("0.0001")

_INITIAL_BILLING = BillingFile(
    providers=(
        BillingProvider(
            provider=_AWS_PROVIDER,
            scope="ACCOUNT",
            label="AWS 계정 (도쿄 aws-tokyo + 서울 온프레 시연 EC2 등 계정 전체)",
            source=_AWS_SOURCE,
            fetched_at=None,
            note=(
                "크레딧이 사용 금액을 상계해 실제 청구는 0. "
                "환경별로 나누지 못함(비용 배분 태그 없음)."
            ),
            days=(),
        ),
        BillingProvider(
            provider="gcp",
            scope="ACCOUNT",
            label="GCP 프로젝트 team-echo-final",
            source=None,
            fetched_at=None,
            note="BigQuery 청구 내보내기 없음 — 추정만 보여 준다.",
            days=(),
        ),
    )
)


class BillingImportError(Exception):
    """입력 파일을 청구 실측으로 쓸 수 없다. 메시지는 그대로 사용자에게 보여 준다."""


def parse_cost_explorer_days(document: object) -> list[BillingDay]:
    """RECORD_TYPE 묶음 DAILY 응답 → 날짜별 사용·크레딧·순 금액(소수 4자리).

    순 금액은 모든 레코드 종류(세금 등 포함)의 정밀 합을 반올림한 값이다. 크레딧이 사용 금액을
    상계한 날의 작은 차이(-0.000000004)는 0 이 되고, 사용·크레딧의 반올림 차이로 남지 않는다.
    """
    if not isinstance(document, Mapping):
        raise BillingImportError("input must be a JSON object")
    try:
        group_keys = [group["Key"] for group in document["GroupDefinitions"]]
        if group_keys != ["RECORD_TYPE"]:
            raise BillingImportError("input must be grouped by RECORD_TYPE only")
        results = document["ResultsByTime"]
        if not results:
            raise BillingImportError("input has no ResultsByTime")
        return [_parse_day(result) for result in results]
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise BillingImportError(f"input has an unexpected shape: {type(exc).__name__}") from exc


def merge_billing(
    existing: BillingFile,
    provider: str,
    days: Sequence[BillingDay],
    fetched_at: datetime.datetime,
) -> BillingFile:
    """그 공급자의 같은 날짜는 덮어쓰고 다른 날짜는 유지한다. 표시 문구(label·note)는 유지한다."""
    current = next((entry for entry in existing.providers if entry.provider == provider), None)
    template = current or next(e for e in _INITIAL_BILLING.providers if e.provider == provider)
    days_by_date = {day.date: day for day in template.days} | {day.date: day for day in days}
    merged = template.model_copy(
        update={
            "source": _AWS_SOURCE,
            "fetched_at": fetched_at,
            "days": tuple(days_by_date[date] for date in sorted(days_by_date)),
        }
    )
    if current is None:
        return BillingFile(providers=(*existing.providers, merged))
    return BillingFile(
        providers=tuple(merged if entry is current else entry for entry in existing.providers)
    )


def import_billing(
    provider: str, input_path: Path, output_path: Path, fetched_at: datetime.datetime
) -> BillingFile:
    try:
        document = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BillingImportError(f"cannot read input: {type(exc).__name__}") from exc
    days = parse_cost_explorer_days(document)
    existing = load_billing_file(output_path) if output_path.exists() else _INITIAL_BILLING
    merged = merge_billing(existing, provider, days, fetched_at)
    body = json.dumps(merged.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2)
    output_path.write_text(f"{body}\n", encoding="utf-8")
    return merged


def _parse_day(result: Mapping[str, Any]) -> BillingDay:
    start = datetime.date.fromisoformat(result["TimePeriod"]["Start"])
    end = datetime.date.fromisoformat(result["TimePeriod"]["End"])
    if end - start != datetime.timedelta(days=1):
        raise BillingImportError(f"input must be DAILY: {start} to {end}")
    estimated = result["Estimated"]
    if not isinstance(estimated, bool):
        raise BillingImportError(f"Estimated must be true or false: {start}")
    amounts = [_parse_group(group) for group in result["Groups"]]
    return BillingDay(
        date=start,
        usage_usd=_round_amount(sum((a for r, a in amounts if r == _USAGE_RECORD), Decimal(0))),
        credits_usd=_round_amount(sum((a for r, a in amounts if r == _CREDIT_RECORD), Decimal(0))),
        net_usd=_round_amount(sum((a for _, a in amounts), Decimal(0))),
        estimated=estimated,
    )


def _parse_group(group: Mapping[str, Any]) -> tuple[str, Decimal]:
    metric = group["Metrics"][_COST_METRIC]
    if metric["Unit"] != _CURRENCY:
        raise BillingImportError(f"amount must be in {_CURRENCY}")
    return group["Keys"][0], Decimal(metric["Amount"])


def _round_amount(amount: Decimal) -> float:
    rounded = amount.quantize(_AMOUNT_QUANTUM, rounding=ROUND_HALF_UP)
    # 작은 음수는 -0.0000 이 된다. 파일·응답에 -0.0 을 쓰지 않는다.
    return float(rounded.copy_abs() if rounded.is_zero() else rounded)


def _parse_fetched_at(value: str) -> datetime.datetime:
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO 8601 time") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("must include a time zone")
    return parsed.astimezone(datetime.UTC)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cost Explorer 응답을 청구 실측 파일에 병합한다.")
    parser.add_argument("--provider", required=True, choices=[_AWS_PROVIDER], help="청구 공급자")
    parser.add_argument("--input", required=True, type=Path, help="Cost Explorer 응답 JSON 파일")
    parser.add_argument("--output", type=Path, default=BILLING_PATH, help="청구 실측 파일")
    parser.add_argument(
        "--fetched-at", type=_parse_fetched_at, help="받은 시각(ISO 8601). 비우면 지금"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    fetched_at = args.fetched_at or SystemClock().now()
    try:
        merged = import_billing(args.provider, args.input, args.output, fetched_at)
    except (BillingImportError, CostCatalogError) as exc:
        sys.stderr.write(f"import_billing: {exc}\n")
        return 1
    entry = next(item for item in merged.providers if item.provider == args.provider)
    sys.stdout.write(f"{args.provider}: {len(entry.days)} days in {args.output}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
