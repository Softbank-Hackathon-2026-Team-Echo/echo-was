"""앱에 등록된 쓰기 경로 목록과 감사 행동 이름표 대조(보안 5-3, C-IAM-2·C-AUD-1 자기 시험).

`tests/integration/test_audit_logs.py::test_every_write_route_has_an_action_name` 과 같은 계산을
운영 일꾼도 쓸 수 있게 앱 안에 둔다: 쓰기 경로(POST·PUT·PATCH·DELETE) − 로그아웃 =
WRITE_ROUTE_ACTIONS ∪ RUNNER_ROUTE_ACTIONS 여야 한다.
"""

from collections.abc import Iterable, Iterator
from typing import Any

from fastapi.routing import APIRoute

from app.audit.actions import RUNNER_ROUTE_ACTIONS, WRITE_ROUTE_ACTIONS

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# 조직 밖 행동이라 이름표에 넣지 않는 유일한 쓰기 경로.
LOGOUT_ROUTE = ("POST", "/api/v1/auth/logout")

Route = tuple[str, str]


def iter_api_routes(routes: Iterable[Any]) -> Iterator[APIRoute]:
    """등록된 모든 API 라우트(포함된 라우터 안쪽까지)."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            yield from iter_api_routes(route.original_router.routes)


def write_routes(routes: Iterable[Any]) -> frozenset[Route]:
    """(메서드, 경로 틀) 쓰기 경로 전부(로그아웃 포함)."""
    return frozenset(
        (method, route.path)
        for route in iter_api_routes(routes)
        for method in route.methods or ()
        if method in WRITE_METHODS
    )


def unnamed_write_routes(routes: Iterable[Any]) -> tuple[Route, ...]:
    """감사 행동 이름표(사람·러너 표)에 없는 쓰기 경로. 비어 있어야 한다."""
    named = set(WRITE_ROUTE_ACTIONS) | set(RUNNER_ROUTE_ACTIONS)
    return tuple(sorted(write_routes(routes) - named - {LOGOUT_ROUTE}))


def agent_probe_routes(routes: Iterable[Any]) -> tuple[Route, ...]:
    """에이전트 토큰으로 두드려 볼 쓰기 경로: 쓰기 경로 − 로그아웃 − 러너 경로(러너 토큰 전용)."""
    return tuple(sorted(write_routes(routes) - set(RUNNER_ROUTE_ACTIONS) - {LOGOUT_ROUTE}))
