"""GitHub 로그인(시작·콜백)·로그아웃·내 정보.

로그인 시작은 무작위 state 를 httpOnly 상태 쿠키(10분)로 내려 주고 GitHub 인증 화면으로 보낸다.
콜백은 state 를 쿠키 값과 상수 시간으로 비교하고, GitHub 호출(code 교환·내 정보·허용 조직 멤버십)을
트랜잭션 밖에서 한 뒤 한 트랜잭션에서 사용자·개인 조직·초대 적용·세션을 처리한다.

콜백의 실패는 예외가 아니라 프론트 `/login?error=<코드>` 로 보내는 결과다.
"""

import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import (
    OauthState,
    hash_token,
    is_same_secret,
    new_oauth_state,
    new_session_token,
    safe_next_path,
)
from app.clients.github_user import GithubUser, GithubUserClient
from app.core.clock import Clock
from app.core.config import Settings
from app.core.exceptions import (
    GithubCodeRejectedError,
    GithubUnavailableError,
    LoginNotConfiguredError,
)
from app.core.secrets import SecretValue
from app.models import User, UserSession
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.user_repository import UserRepository
from app.repositories.user_session_repository import UserSessionRepository
from app.services.invitation_service import apply_pending_invitations
from app.services.organization_service import OrganizationView, create_personal_organization

logger = logging.getLogger(__name__)

OAUTH_DENIED = "OAUTH_DENIED"
OAUTH_STATE_MISMATCH = "OAUTH_STATE_MISMATCH"
GITHUB_UNAVAILABLE = "GITHUB_UNAVAILABLE"
LOGIN_NOT_ALLOWED = "LOGIN_NOT_ALLOWED"
LOGIN_NOT_CONFIGURED = "LOGIN_NOT_CONFIGURED"
_AUTHORIZE_PATH = "/login/oauth/authorize"


@dataclass(frozen=True)
class LoginStart:
    authorize_url: str
    state_cookie: str


@dataclass(frozen=True)
class LoginResult:
    """redirect_url 로 302. session_token 이 있으면 세션 쿠키를 내려 준다."""

    redirect_url: str
    session_token: SecretValue | None = None


@dataclass(frozen=True)
class MeView:
    user: User
    organizations: list[OrganizationView]


class AuthService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        clock: Clock,
        github_client: GithubUserClient,
    ) -> None:
        self._session = session
        self._settings = settings
        self._clock = clock
        self._github_client = github_client
        self._reserved_slugs: Collection[str] = frozenset(settings.reserved_org_slugs)

    def start_login(self, next_path: str | None) -> LoginStart:
        settings = self._require_login_settings()
        state = OauthState(
            state=new_oauth_state(),
            issued_at=int(self._clock.now().timestamp()),
            next_path=safe_next_path(next_path),
        )
        query = urlencode(
            {
                "client_id": settings.github_app_client_id,
                "redirect_uri": settings.github_oauth_redirect_uri,
                "state": state.state,
            }
        )
        base_url = settings.github_oauth_base_url.rstrip("/")
        return LoginStart(
            authorize_url=f"{base_url}{_AUTHORIZE_PATH}?{query}", state_cookie=state.encode()
        )

    async def complete_login(
        self,
        *,
        code: str | None,
        state: str | None,
        error: str | None,
        state_cookie: str | None,
    ) -> LoginResult:
        if not self._settings.is_login_configured:
            # 프론트 주소가 있으면 그쪽으로 알리고, 없으면 503.
            if self.failure_redirect(LOGIN_NOT_CONFIGURED) is None:
                raise LoginNotConfiguredError
            return self._failure(LOGIN_NOT_CONFIGURED, "login_not_configured")
        settings = self._settings
        if error is not None:
            return self._failure(OAUTH_DENIED, "github_error")
        stored = OauthState.decode(state_cookie) if state_cookie else None
        if stored is None or state is None or not is_same_secret(stored.state, state):
            return self._failure(OAUTH_STATE_MISMATCH, "state_mismatch")
        now = self._clock.now()
        if now.timestamp() - stored.issued_at > settings.oauth_state_ttl_seconds:
            return self._failure(OAUTH_STATE_MISMATCH, "state_expired")
        if not code:
            return self._failure(OAUTH_DENIED, "code_missing")

        assert settings.github_oauth_redirect_uri is not None
        try:
            user_token = await self._github_client.exchange_code(
                code, settings.github_oauth_redirect_uri
            )
            github_user = await self._github_client.get_user(user_token)
            if settings.login_allowed_github_org and not (
                await self._github_client.is_active_org_member(
                    user_token, settings.login_allowed_github_org
                )
            ):
                return self._failure(LOGIN_NOT_ALLOWED, "not_org_member")
        except GithubCodeRejectedError:
            return self._failure(OAUTH_DENIED, "code_rejected")
        except GithubUnavailableError:
            return self._failure(GITHUB_UNAVAILABLE, "github_unavailable")

        session_token = new_session_token()
        user = await self._sign_in(github_user, session_token)
        logger.info("user signed in", extra={"action": "complete_login", "user_id": str(user.id)})
        return LoginResult(
            redirect_url=f"{settings.frontend_base_url}{safe_next_path(stored.next_path)}",
            session_token=session_token,
        )

    def failure_redirect(self, code: str) -> str | None:
        """프론트 주소가 있으면 `/login?error=<code>` 주소, 없으면 None."""
        if not self._settings.frontend_base_url:
            return None
        return f"{self._settings.frontend_base_url}/login?{urlencode({'error': code})}"

    async def logout(self, session_cookie: str | None) -> None:
        if not session_cookie:
            return
        async with self._session.begin():
            await UserSessionRepository(self._session).revoke_by_hash(
                hash_token(session_cookie), self._clock.now()
            )

    async def get_me(self, user: User) -> MeView:
        """대기 초대를 적용한 뒤 사용자와 내 조직(역할 포함)을 돌려준다."""
        async with self._session.begin():
            await apply_pending_invitations(self._session, user, self._clock)
            rows = await OrganizationRepository(self._session).search_by_member(
                user.id, offset=0, limit=None
            )
        return MeView(
            user=user,
            organizations=[OrganizationView(organization=org, role=role) for org, role in rows],
        )

    async def _sign_in(self, github_user: GithubUser, session_token: SecretValue) -> User:
        users = UserRepository(self._session)
        now = self._clock.now()
        async with self._session.begin():
            new_user_id = await users.insert_if_absent(
                github_id=github_user.id,
                github_login=github_user.login,
                name=github_user.name,
                avatar_url=github_user.avatar_url,
                now=now,
            )
            if new_user_id is None:
                await users.update_profile(
                    github_user.id,
                    github_login=github_user.login,
                    name=github_user.name,
                    avatar_url=github_user.avatar_url,
                    now=now,
                )
            user = await users.find_by_github_id(github_user.id)
            assert user is not None
            if new_user_id is not None:
                await create_personal_organization(
                    self._session, user=user, reserved_slugs=self._reserved_slugs, clock=self._clock
                )
            await apply_pending_invitations(self._session, user, self._clock)
            UserSessionRepository(self._session).add(
                UserSession(
                    user_id=user.id,
                    token_hash=hash_token(session_token.get_secret_value()),
                    expires_at=now + timedelta(seconds=self._settings.session_ttl_seconds),
                    created_at=now,
                )
            )
        return user

    def _failure(self, code: str, reason: str) -> LoginResult:
        logger.info("login failed", extra={"action": "complete_login", "reason": reason})
        redirect_url = self.failure_redirect(code)
        assert redirect_url is not None
        return LoginResult(redirect_url=redirect_url)

    def _require_login_settings(self) -> Settings:
        if not self._settings.is_login_configured:
            raise LoginNotConfiguredError
        return self._settings
