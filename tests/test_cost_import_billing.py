"""청구 실측 갱신 명령 시험(운영 1 AC-6). 고정한 Cost Explorer 응답만 읽는다(네트워크 없음)."""

import datetime
import json
import socket
from pathlib import Path
from typing import Any

import pytest

from app.scripts.import_billing import (
    BillingImportError,
    import_billing,
    main,
    parse_cost_explorer_days,
)
from app.services.cost_catalog import BILLING_PATH, CostCatalogError, load_billing_file

FIXTURE = Path(__file__).parent / "data" / "ce-2026-11-04-record-type.json"
FETCHED_AT = datetime.datetime(2026, 11, 5, 5, 50, tzinfo=datetime.UTC)
LATER = datetime.datetime(2026, 11, 7, 1, 0, tzinfo=datetime.UTC)


def _document() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return document


def _group(record_type: str, amount: str, unit: str = "USD") -> dict[str, Any]:
    return {"Keys": [record_type], "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": unit}}}


def _single_day(day: str, next_day: str, usage: str, credit: str) -> dict[str, Any]:
    return {
        "GroupDefinitions": [{"Type": "DIMENSION", "Key": "RECORD_TYPE"}],
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": day, "End": next_day},
                "Total": {},
                "Groups": [_group("Usage", usage), _group("Credit", credit)],
                "Estimated": False,
            }
        ],
    }


def _write(tmp_path: Path, name: str, document: object) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _not_daily() -> dict[str, Any]:
    return _single_day("2026-11-01", "2026-12-01", "1", "0")


def _other_currency() -> dict[str, Any]:
    document = _single_day("2026-11-06", "2026-11-07", "1", "0")
    document["ResultsByTime"][0]["Groups"] = [_group("Usage", "1", unit="JPY")]
    return document


def _grouped_by_service() -> dict[str, Any]:
    return {**_document(), "GroupDefinitions": [{"Type": "DIMENSION", "Key": "SERVICE"}]}


def _without_estimated() -> dict[str, Any]:
    document = _document()
    del document["ResultsByTime"][0]["Estimated"]
    return document


def _without_key(key: str) -> dict[str, Any]:
    document = _document()
    del document[key]
    return document


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("network call attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def test_parse_fixture_computes_usage_credits_net_and_estimated() -> None:
    days = parse_cost_explorer_days(_document())

    assert len(days) == 1
    assert days[0].date == datetime.date(2026, 11, 4)
    assert (days[0].usage_usd, days[0].credits_usd) == (6.7374, -6.7374)
    assert days[0].estimated is True


def test_parse_small_negative_net_rounds_to_positive_zero() -> None:
    # Usage 6.737445717 + Credit -6.737445721 = -0.000000004. 파일·응답에 -0.0 을 쓰지 않는다.
    net = parse_cost_explorer_days(_document())[0].net_usd

    assert json.dumps(net) == "0.0"


def test_parse_net_includes_other_record_types() -> None:
    document = _single_day("2026-11-06", "2026-11-07", "10", "-4")
    document["ResultsByTime"][0]["Groups"].append(_group("Tax", "1.5"))

    day = parse_cost_explorer_days(document)[0]

    assert (day.usage_usd, day.credits_usd, day.net_usd) == (10.0, -4.0, 7.5)


def test_parse_day_without_groups_is_zero() -> None:
    document = _single_day("2026-11-06", "2026-11-07", "0", "0")
    document["ResultsByTime"][0]["Groups"] = []

    day = parse_cost_explorer_days(document)[0]

    assert (day.usage_usd, day.credits_usd, day.net_usd) == (0.0, 0.0, 0.0)


@pytest.mark.parametrize(
    "document",
    [
        [],
        _without_key("GroupDefinitions"),
        _without_key("ResultsByTime"),
        {**_document(), "ResultsByTime": []},
        _not_daily(),
        _other_currency(),
        _grouped_by_service(),
        _single_day("2026-11-06", "2026-11-07", "NaN", "0"),
        _without_estimated(),
    ],
    ids=[
        "not-an-object",
        "no-group-definitions",
        "no-results",
        "empty-results",
        "not-daily",
        "other-currency",
        "grouped-by-service",
        "nan-amount",
        "no-estimated-flag",
    ],
)
def test_parse_rejects_files_that_are_not_daily_record_type_usd(document: object) -> None:
    with pytest.raises(BillingImportError):
        parse_cost_explorer_days(document)


def test_import_creates_file_with_both_providers_when_missing(tmp_path: Path) -> None:
    # given
    output = tmp_path / "billing.json"

    # when
    import_billing("aws", FIXTURE, output, FETCHED_AT)

    # then
    aws, gcp = load_billing_file(output).providers
    assert (aws.provider, aws.scope, aws.fetched_at) == ("aws", "ACCOUNT", FETCHED_AT)
    assert [day.date.isoformat() for day in aws.days] == ["2026-11-04"]
    assert (gcp.provider, gcp.source, gcp.fetched_at, gcp.days) == ("gcp", None, None, ())
    assert gcp.note == "BigQuery 청구 내보내기 없음 — 추정만 보여 준다."


def test_import_overwrites_same_date_keeps_other_dates_and_sorts(tmp_path: Path) -> None:
    # given: 11/04 가 있는 파일에 11/06 → 11/02 → 11/04(새 값) 순으로 넣는다.
    output = tmp_path / "billing.json"
    import_billing("aws", FIXTURE, output, FETCHED_AT)
    later = _write(tmp_path, "later.json", _single_day("2026-11-06", "2026-11-07", "3", "-3"))
    earlier = _write(tmp_path, "earlier.json", _single_day("2026-11-02", "2026-11-03", "2", "-2"))
    again = _write(tmp_path, "again.json", _single_day("2026-11-04", "2026-11-05", "9", "-1"))

    # when
    import_billing("aws", later, output, LATER)
    import_billing("aws", earlier, output, LATER)
    import_billing("aws", again, output, LATER)

    # then
    aws, gcp = load_billing_file(output).providers
    assert [(d.date.isoformat(), d.usage_usd, d.net_usd, d.estimated) for d in aws.days] == [
        ("2026-11-02", 2.0, 0.0, False),
        ("2026-11-04", 9.0, 8.0, False),
        ("2026-11-06", 3.0, 0.0, False),
    ]
    assert aws.fetched_at == LATER
    assert gcp.days == ()


def test_import_keeps_hand_edited_label_and_note(tmp_path: Path) -> None:
    output = tmp_path / "billing.json"
    import_billing("aws", FIXTURE, output, FETCHED_AT)
    stored = json.loads(output.read_text(encoding="utf-8"))
    stored["providers"][0]["label"] = "손으로 고친 이름"
    stored["providers"][0]["note"] = "손으로 고친 메모"
    output.write_text(json.dumps(stored), encoding="utf-8")

    import_billing("aws", FIXTURE, output, LATER)

    aws = load_billing_file(output).providers[0]
    assert (aws.label, aws.note, aws.fetched_at) == ("손으로 고친 이름", "손으로 고친 메모", LATER)


def test_import_rejected_input_leaves_output_unchanged(tmp_path: Path) -> None:
    output = tmp_path / "billing.json"
    import_billing("aws", FIXTURE, output, FETCHED_AT)
    before = output.read_bytes()
    bad = _write(tmp_path, "bad.json", _not_daily())

    with pytest.raises(BillingImportError):
        import_billing("aws", bad, output, LATER)

    assert output.read_bytes() == before


def test_import_refuses_to_overwrite_invalid_output(tmp_path: Path) -> None:
    output = tmp_path / "billing.json"
    output.write_text("{broken", encoding="utf-8")

    with pytest.raises(CostCatalogError):
        import_billing("aws", FIXTURE, output, FETCHED_AT)

    assert output.read_text(encoding="utf-8") == "{broken"


def test_real_billing_file_is_what_the_november_4_fixture_produces(tmp_path: Path) -> None:
    output = tmp_path / "billing.json"

    import_billing("aws", FIXTURE, output, FETCHED_AT)

    assert output.read_text(encoding="utf-8") == BILLING_PATH.read_text(encoding="utf-8")


def test_main_writes_output_and_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output = tmp_path / "billing.json"

    code = main(
        [
            "--provider",
            "aws",
            "--input",
            str(FIXTURE),
            "--output",
            str(output),
            "--fetched-at",
            "2026-11-05T14:50:00+09:00",
        ]
    )

    assert code == 0
    assert "aws: 1 days" in capsys.readouterr().out
    stored = json.loads(output.read_text(encoding="utf-8"))
    assert stored["providers"][0]["fetchedAt"] == "2026-11-05T05:50:00Z"


def test_main_returns_one_on_bad_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        [
            "--provider",
            "aws",
            "--input",
            str(tmp_path / "none.json"),
            "--output",
            str(tmp_path / "billing.json"),
        ]
    )

    assert code == 1
    assert "import_billing:" in capsys.readouterr().err
    assert not (tmp_path / "billing.json").exists()


@pytest.mark.parametrize(
    "extra",
    [["--provider", "gcp"], ["--provider", "aws", "--fetched-at", "2026-11-05T05:50:00"]],
    ids=["gcp-not-accepted", "naive-fetched-at"],
)
def test_main_rejects_bad_arguments(tmp_path: Path, extra: list[str]) -> None:
    with pytest.raises(SystemExit):
        main([*extra, "--input", str(FIXTURE), "--output", str(tmp_path / "billing.json")])
