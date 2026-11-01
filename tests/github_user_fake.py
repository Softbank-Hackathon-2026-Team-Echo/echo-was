"""가짜 GitHub 사용자 클라이언트. 시험이 app.dependency_overrides 로만 넣는다(앱 설정으로 못 켬).

사용자 토큰·client secret 은 시험 코드 안 상수다. 이 값들이 로그·응답·DB 에 나오면 안 된다(AC-15).
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs

import httpx

from app.clients.github_user import GithubUser
from app.core.exceptions import GithubCodeRejectedError, GithubUnavailableError
from app.core.secrets import SecretValue

USER_TOKEN_PREFIX = "gho_test_user_token_do_not_log_"
CLIENT_SECRET_VALUE = "test-client-secret-do-not-log-5d1c"
CLIENT_SECRET_ENV = "ECHO_SECRET_GITHUB_APP_CLIENT_SECRET"


def user_token_for(code: str) -> str:
    return f"{USER_TOKEN_PREFIX}{code}"


@dataclass
class FakeGithubUserClient:
    """code → 사용자. 허용 조직 멤버(github_id), 찾을 수 있는 아이디를 정해 둔다.

    - users_by_code: 로그인 code 마다 GitHub 사용자.
    - org_members: 허용 조직의 active 멤버 github_id.
    - directory: `GET /users/{login}` 으로 찾을 수 있는 사람(소문자 아이디 → 사용자).
    - unavailable: True 면 모든 호출이 GITHUB_UNAVAILABLE.
    - rejected_codes: GitHub 가 받지 않는 code.
    """

    users_by_code: dict[str, GithubUser] = field(default_factory=dict)
    org_members: set[int] = field(default_factory=set)
    directory: dict[str, GithubUser] = field(default_factory=dict)
    unavailable: bool = False
    rejected_codes: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)

    def add_user(self, code: str, user: GithubUser) -> None:
        self.users_by_code[code] = user
        self.directory[user.login.lower()] = user

    async def exchange_code(self, code: str, redirect_uri: str) -> SecretValue:
        self.calls.append(f"exchange_code:{redirect_uri}")
        self._check_available()
        if code in self.rejected_codes or code not in self.users_by_code:
            raise GithubCodeRejectedError
        return SecretValue(user_token_for(code))

    async def get_user(self, token: SecretValue) -> GithubUser:
        self.calls.append("get_user")
        self._check_available()
        code = token.get_secret_value().removeprefix(USER_TOKEN_PREFIX)
        return self.users_by_code[code]

    async def is_active_org_member(self, token: SecretValue, org: str) -> bool:
        self.calls.append(f"is_active_org_member:{org}")
        self._check_available()
        user = await self.get_user(token)
        return user.id in self.org_members

    async def find_user(self, login: str) -> GithubUser | None:
        self.calls.append("find_user")
        self._check_available()
        return self.directory.get(login.lower())

    def _check_available(self) -> None:
        if self.unavailable:
            raise GithubUnavailableError


OAUTH_BASE = "https://github.oauth.test"
API_BASE = "https://api.github.test"


@dataclass
class FakeGithubOauth:
    """GitHub 로그인·사용자 API 를 흉내 내는 httpx.MockTransport. 실제 GitHub 에 닿지 않는다.

    - users_by_code: 로그인 code → 사용자(코드 교환 때 client secret 이 맞아야 토큰을 준다).
    - memberships: github_id → 허용 조직 멤버십 state(active·pending). 없으면 404.
    - override: 요청 → 고정 응답(장애·한도 초과 시험용). None 을 돌려주면 기본 처리.
    """

    users_by_code: dict[str, GithubUser] = field(default_factory=dict)
    memberships: dict[int, str] = field(default_factory=dict)
    client_secret: str = CLIENT_SECRET_VALUE
    override: Callable[[httpx.Request], httpx.Response | None] | None = None
    requests: list[httpx.Request] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.override is not None and (response := self.override(request)) is not None:
            return response
        path = request.url.path
        if request.method == "POST" and path == "/login/oauth/access_token":
            form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
            code = form.get("code", "")
            if form.get("client_secret") != self.client_secret or code not in self.users_by_code:
                return httpx.Response(200, json={"error": "bad_verification_code"})
            return httpx.Response(200, json={"access_token": user_token_for(code)})
        user = self._token_user(request)
        if request.method == "GET" and path == "/user":
            return _user_response(user) if user else httpx.Response(401)
        if request.method == "GET" and path.startswith("/user/memberships/orgs/"):
            if user is None:
                return httpx.Response(401)
            state = self.memberships.get(user.id)
            if state is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"state": state, "role": "member"})
        if request.method == "GET" and path.startswith("/users/"):
            login = path.removeprefix("/users/").lower()
            for candidate in self.users_by_code.values():
                if candidate.login.lower() == login:
                    return _user_response(candidate)
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(404)

    def _token_user(self, request: httpx.Request) -> GithubUser | None:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not token.startswith(USER_TOKEN_PREFIX):
            return None
        return self.users_by_code.get(token.removeprefix(USER_TOKEN_PREFIX))


def _user_response(user: GithubUser) -> httpx.Response:
    return httpx.Response(
        200,
        content=json.dumps(
            {"id": user.id, "login": user.login, "name": user.name, "avatar_url": user.avatar_url}
        ),
        headers={"content-type": "application/json"},
    )
