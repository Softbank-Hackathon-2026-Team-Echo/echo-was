"""감사 줄에 실을 요청 값(경로 틀·실제 경로·요청 ID).

서비스 메서드마다 인수를 더하지 않으려고 미들웨어가 ASGI scope 를 contextvar 에 묶어 두고, 기록할 때
읽는다. 경로 틀(scope["route"])은 미들웨어보다 안쪽에서 정해지므로 묶는 시점이 아니라 읽는 시점에
본다. 요청 밖(일꾼·시험의 서비스 직접 호출)에서는 None 이고 그 칸들은 NULL 로 남는다.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from starlette.types import Scope

_current_scope: ContextVar[Scope | None] = ContextVar("audit_request_scope", default=None)


@dataclass(frozen=True)
class RequestContext:
    route: str | None
    path: str | None
    request_id: str | None


@contextmanager
def bind_request_scope(scope: Scope) -> Iterator[None]:
    token = _current_scope.set(scope)
    try:
        yield
    finally:
        _current_scope.reset(token)


def request_context_of(scope: Scope) -> RequestContext:
    """`route` 는 `{메서드} {경로 틀}`. 라우트가 정해지기 전이면 None 이다."""
    template = getattr(scope.get("route"), "path", None)
    state = scope.get("state") or {}
    return RequestContext(
        route=f"{scope['method']} {template}" if template is not None else None,
        path=scope.get("path"),
        request_id=state.get("request_id"),
    )


def current_request_context() -> RequestContext | None:
    scope = _current_scope.get()
    return request_context_of(scope) if scope is not None else None
