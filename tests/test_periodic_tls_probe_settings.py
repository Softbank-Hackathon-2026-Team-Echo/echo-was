"""TLS 주기 시험 간격이 C-TLS-1 준비 증거 유효기간(1시간) 안에 머무는지."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.enums import SecurityControlId
from app.services.security_rules import get_rule

DATABASE_URL = "postgresql+asyncpg://x:y@127.0.0.1:1/z"


def test_default_interval_is_shorter_than_tls_evidence_lifetime() -> None:
    interval = Settings(database_url=DATABASE_URL).security_tls_probe_interval_seconds

    assert timedelta(seconds=interval) < get_rule(SecurityControlId.TLS_1).max_age


@pytest.mark.parametrize("value", [59, 3001, 3600], ids=["too-short", "over-50m", "one-hour"])
def test_interval_outside_range_is_rejected(value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(database_url=DATABASE_URL, security_tls_probe_interval_seconds=value)
