import json
import logging
import sys

from app.core.logging import ContextFilter, JsonFormatter, log_context


def _record(**extra: object) -> logging.LogRecord:
    return logging.makeLogRecord(
        {"name": "app.test", "levelname": "INFO", "levelno": logging.INFO, "msg": "deploy started"}
        | extra
    )


def _format(record: logging.LogRecord) -> dict[str, object]:
    ContextFilter("echo-api").filter(record)
    return json.loads(JsonFormatter().format(record))  # type: ignore[no-any-return]


def test_json_formatter_merges_component_context_extra_and_masks_secrets() -> None:
    with log_context(request_id="req-1", deployment_id="dep-1"):
        payload = _format(_record(environment_id="env-1", api_token="s3cr3t"))

    assert payload["message"] == "deploy started"
    assert payload["component"] == "echo-api"
    assert payload["request_id"] == "req-1"
    assert payload["deployment_id"] == "dep-1"
    assert payload["environment_id"] == "env-1"
    assert payload["api_token"] == "***"


def test_log_context_extra_wins_and_resets_after_block() -> None:
    with log_context(deployment_id="dep-1"):
        inside = _format(_record(deployment_id="dep-2"))
    outside = _format(_record())

    assert inside["deployment_id"] == "dep-2"
    assert "deployment_id" not in outside


def test_json_formatter_exception_adds_type_and_stack() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.getLogger("app.test").makeRecord(
            "app.test", logging.ERROR, __file__, 0, "failed", None, sys.exc_info()
        )

    payload = _format(record)

    assert payload["exc_type"] == "ValueError"
    assert "boom" in str(payload["stack"])
