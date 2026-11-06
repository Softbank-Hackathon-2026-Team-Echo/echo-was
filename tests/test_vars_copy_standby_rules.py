"""변수 복사·예비 환경 함께 띄우기의 순수 규칙(이슈 #99)."""

import uuid

import pytest

from app.core.exceptions import (
    EnvVarKeyInvalidError,
    EnvVarKeyReservedError,
    RequestFieldInvalidError,
)
from app.enums import DeployRequestTrigger, RefType
from app.models import DeployRequest
from app.services.deploy_log_messages import standby_skipped_message
from app.services.deploy_request_processor import _requested_standby_ids
from app.services.deploy_request_service import (
    CreateDeployCommand,
    _check_standby_command,
    _with_standby_ids,
)
from app.services.service_variable_service import CopyServiceVariablesCommand, _check_copy_command

A = uuid.uuid4()
B = uuid.uuid4()
C = uuid.uuid4()


def _copy(source: uuid.UUID, targets: list[uuid.UUID], keys: list[str]) -> None:
    _check_copy_command(CopyServiceVariablesCommand(source, targets, keys))


def _reason(error: pytest.ExceptionInfo[RequestFieldInvalidError]) -> tuple[str, str]:
    assert error.value.details is not None
    (detail,) = error.value.details
    return detail.field, detail.reason


def test_copy_command_accepts_distinct_targets_and_keys() -> None:
    _copy(A, [B, C], ["DATABASE_URL", "API_KEY"])


def test_copy_command_rejects_source_in_targets_first() -> None:
    with pytest.raises(RequestFieldInvalidError) as error:
        _copy(A, [B, A, B], ["K"])
    assert _reason(error) == ("targetEnvironmentIds", "SOURCE_IN_TARGETS")


def test_copy_command_rejects_duplicates() -> None:
    with pytest.raises(RequestFieldInvalidError) as targets:
        _copy(A, [B, B], ["K"])
    with pytest.raises(RequestFieldInvalidError) as keys:
        _copy(A, [B], ["K", "K"])
    assert _reason(targets) == ("targetEnvironmentIds", "DUPLICATE")
    assert _reason(keys) == ("keys", "DUPLICATE")


@pytest.mark.parametrize(
    ("key", "error"),
    [
        ("lower", EnvVarKeyInvalidError),
        ("PORT", EnvVarKeyReservedError),
        ("ECHO_X", EnvVarKeyReservedError),
    ],
)
def test_copy_command_uses_put_key_rules(key: str, error: type[Exception]) -> None:
    with pytest.raises(error):
        _copy(A, [B], [key])


def _deploy(environment: uuid.UUID, standby: tuple[uuid.UUID, ...]) -> CreateDeployCommand:
    return CreateDeployCommand(environment, RefType.BRANCH, "main", None, standby)


def test_standby_command_rules() -> None:
    _check_standby_command(_deploy(A, ()))
    _check_standby_command(_deploy(A, (B, C)))
    with pytest.raises(RequestFieldInvalidError) as same:
        _check_standby_command(_deploy(A, (B, A)))
    with pytest.raises(RequestFieldInvalidError) as duplicate:
        _check_standby_command(_deploy(A, (B, B)))
    assert _reason(same) == ("standbyEnvironmentIds", "SAME_AS_ENVIRONMENT")
    assert _reason(duplicate) == ("standbyEnvironmentIds", "DUPLICATE")


def test_snapshot_keeps_standby_ids_only_when_given() -> None:
    assert _with_standby_ids({"mainUrl": "x"}, ()) == {"mainUrl": "x"}
    assert _with_standby_ids({"mainUrl": "x"}, (B, C)) == {
        "mainUrl": "x",
        "standbyEnvironmentIds": [str(B), str(C)],
    }


@pytest.mark.parametrize(
    ("trigger", "expected"),
    [
        (DeployRequestTrigger.MANUAL, [B]),
        (DeployRequestTrigger.ROLLBACK, []),
        (DeployRequestTrigger.STANDBY_SYNC, []),
    ],
)
def test_requested_standby_ids_only_for_manual(
    trigger: DeployRequestTrigger, expected: list[uuid.UUID]
) -> None:
    request = DeployRequest(trigger=trigger, config_snapshot={"standbyEnvironmentIds": [str(B)]})
    assert _requested_standby_ids(request) == expected
    assert (
        _requested_standby_ids(
            DeployRequest(trigger=DeployRequestTrigger.MANUAL, config_snapshot={})
        )
        == []
    )


def test_standby_skipped_message_has_environment_and_fixed_reason() -> None:
    assert standby_skipped_message(str(A), "the environment was deleted.") == (
        f"Requested standby environment {A} was not started: the environment was deleted."
    )
