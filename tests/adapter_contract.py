"""모든 환경 어댑터가 지켜야 하는 공통 계약 검사(오프라인).

등록된 모든 종류를 도는 시험과, 새 종류 파일 하나를 더하는 시험(AC-2)이 이 함수를 함께 쓴다.
실제 어댑터로 바꿀 때도 네트워크 대신 스텁 전송을 넣어 이 검사를 통과해야 한다.
"""

import inspect
import logging
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from app.adapters import base
from app.adapters.base import ClusterInfo, ConnectionCheckError, EnvironmentAdapter
from app.adapters.registry import get_environment_adapter, validate_environment_config
from app.core.logging import JsonFormatter
from app.core.secrets import SecretValue
from app.enums import ConnectionErrorCode

ALLOWED_REASONS = frozenset(ConnectionErrorCode)


@dataclass(frozen=True)
class FailureCase:
    """어댑터가 실패해야 하는 설정·비밀값 묶음. config 는 DB 표기(snake_case)다."""

    config: Mapping[str, object]
    credentials: SecretValue


class _CollectingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []
        self._formatter = JsonFormatter()

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self._formatter.format(record))
        self.lines.append(repr(record.__dict__))


@contextmanager
def _captured_logs() -> Iterator[list[str]]:
    root = logging.getLogger()
    handler = _CollectingHandler()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler.lines
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def make_fake_credentials() -> SecretValue:
    return SecretValue(f"contract-fake-secret-{uuid.uuid4().hex}")


async def assert_adapter_contract(
    kind: str,
    config: Mapping[str, object],
    *,
    credentials: SecretValue | None = None,
    failure_cases: Sequence[FailureCase] = (),
) -> None:
    """kind 의 동작 클래스가 공통 계약을 지키는지 확인한다. 어기면 AssertionError.

    - 동작 클래스가 등록돼 있고 EnvironmentAdapter 를 상속한다.
    - check_connection·read_cluster_info 가 async 다.
    - 가짜 비밀값으로 부른 성공 결과가 ClusterInfo 모양이다.
    - 실패 묶음을 주면 ConnectionCheckError 를 내고, 이유가 정해진 코드(§7.2)다.
    - repr(credentials)·잡은 로그·실패 예외 메시지에 비밀값이 없다.
    """
    adapter_class = base.registered_adapters().get(kind)
    assert adapter_class is not None, f"no adapter registered for kind {kind!r}"
    assert issubclass(adapter_class, EnvironmentAdapter), adapter_class
    adapter = get_environment_adapter(kind)
    for method_name in ("check_connection", "read_cluster_info"):
        method = getattr(adapter, method_name)
        assert inspect.iscoroutinefunction(method), f"{kind}.{method_name} must be async"

    credentials = credentials or make_fake_credentials()
    secret = credentials.get_secret_value()
    parsed_config = validate_environment_config(kind, config)

    with _captured_logs() as log_lines:
        assert await adapter.check_connection(parsed_config, credentials) is None
        cluster_info = await adapter.read_cluster_info(parsed_config, credentials)
        failure_messages = [
            await _assert_fails_with_known_reason(adapter, kind, case) for case in failure_cases
        ]

    assert isinstance(cluster_info, ClusterInfo), type(cluster_info)
    # 불변 모델이라도 값이 계약 모양인지 다시 검사한다(model_construct 등으로 만든 경우 대비).
    ClusterInfo.model_validate(cluster_info.model_dump())

    assert secret not in repr(credentials)
    assert secret not in str(credentials)
    for case in failure_cases:
        case_secret = case.credentials.get_secret_value()
        assert case_secret not in repr(case.credentials)
        assert not any(case_secret in line for line in log_lines), "secret leaked to logs"
        assert not any(case_secret in message for message in failure_messages)
    assert not any(secret in line for line in log_lines), "secret leaked to logs"


async def _assert_fails_with_known_reason(
    adapter: EnvironmentAdapter, kind: str, case: FailureCase
) -> str:
    parsed_config = validate_environment_config(kind, case.config)
    try:
        await adapter.check_connection(parsed_config, case.credentials)
        await adapter.read_cluster_info(parsed_config, case.credentials)
    except ConnectionCheckError as exc:
        assert exc.reason in ALLOWED_REASONS, f"reason outside the fixed codes: {exc.reason!r}"
        return f"{exc!s} {exc!r} {exc.fields!r}"
    raise AssertionError(f"{kind} adapter did not fail for the failure case")
