"""에이전트 토큰 발급·목록·폐기. 원문은 발급 응답에 한 번만 싣고 저장하지 않는다."""

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import AGENT_TOKEN_LAST_CHARS, hash_token, new_agent_token
from app.core.clock import Clock
from app.core.exceptions import AgentTokenNotFoundError, RequestFieldInvalidError
from app.core.secrets import SecretValue
from app.models import AgentToken
from app.repositories.agent_token_repository import AgentTokenRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IssuedAgentToken:
    token: AgentToken
    raw: SecretValue


@dataclass(frozen=True)
class AgentTokenSearchResult:
    items: list[AgentToken]
    total: int


async def issue_agent_token(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    created_by_user_id: uuid.UUID,
    name: str,
    expires_in_days: int,
    clock: Clock,
) -> IssuedAgentToken:
    """토큰 행을 넣는다. 호출하는 쪽이 트랜잭션을 연다(데모 준비 명령도 쓴다)."""
    raw = new_agent_token()
    now = clock.now()
    token = AgentToken(
        organization_id=organization_id,
        name=name,
        token_hash=hash_token(raw.get_secret_value()),
        last4=raw.get_secret_value()[-AGENT_TOKEN_LAST_CHARS:],
        created_by_user_id=created_by_user_id,
        expires_at=now + timedelta(days=expires_in_days),
        created_at=now,
    )
    AgentTokenRepository(session).add(token)
    await session.flush()
    logger.info(
        "agent token issued",
        extra={
            "action": "issue_agent_token",
            "agent_token_id": str(token.id),
            "organization_id": str(organization_id),
        },
    )
    return IssuedAgentToken(token=token, raw=raw)


class AgentTokenService:
    def __init__(
        self, session: AsyncSession, clock: Clock, default_days: int, max_days: int
    ) -> None:
        self._session = session
        self._clock = clock
        self._default_days = default_days
        self._max_days = max_days
        self._tokens = AgentTokenRepository(session)

    async def issue(
        self,
        organization_id: uuid.UUID,
        created_by_user_id: uuid.UUID,
        name: str,
        expires_in_days: int | None,
    ) -> IssuedAgentToken:
        days = self._default_days if expires_in_days is None else expires_in_days
        if days > self._max_days:
            raise RequestFieldInvalidError("expiresInDays", "EXPIRES_IN_DAYS_TOO_LONG")
        async with self._session.begin():
            return await issue_agent_token(
                self._session,
                organization_id=organization_id,
                created_by_user_id=created_by_user_id,
                name=name,
                expires_in_days=days,
                clock=self._clock,
            )

    async def search(
        self, organization_id: uuid.UUID, page: int, size: int
    ) -> AgentTokenSearchResult:
        async with self._session.begin():
            items = await self._tokens.search_by_organization(
                organization_id, offset=page * size, limit=size
            )
            total = await self._tokens.count_by_organization(organization_id)
        return AgentTokenSearchResult(items=items, total=total)

    async def revoke(self, organization_id: uuid.UUID, token_id: uuid.UUID) -> None:
        """폐기한다. 이미 폐기됐어도 성공이다."""
        now = self._clock.now()
        async with self._session.begin():
            token = await self._tokens.find_in_organization(organization_id, token_id)
            if token is None:
                raise AgentTokenNotFoundError("agent token not found", agent_token_id=str(token_id))
            if token.revoked_at is None:
                token.revoked_at = now
        logger.info(
            "agent token revoked",
            extra={
                "action": "revoke_agent_token",
                "agent_token_id": str(token_id),
                "organization_id": str(organization_id),
            },
        )
