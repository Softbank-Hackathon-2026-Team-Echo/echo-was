"""요청한 쪽(행위자)과 조직 경로 검사 결과."""

import uuid
from dataclasses import dataclass

from app.enums import ActorType, OrganizationRole
from app.models import AgentToken, Organization, User


@dataclass(frozen=True)
class HumanActor:
    """세션 쿠키로 들어온 사람."""

    user: User
    session_id: uuid.UUID

    @property
    def actor_type(self) -> ActorType:
        return ActorType.USER


@dataclass(frozen=True)
class AgentActor:
    """에이전트 토큰으로 들어온 AI 에이전트. 토큰의 조직 하나만 읽는다."""

    token: AgentToken

    @property
    def actor_type(self) -> ActorType:
        return ActorType.AGENT

    @property
    def organization_id(self) -> uuid.UUID:
        return self.token.organization_id


Actor = HumanActor | AgentActor


@dataclass(frozen=True)
class OrganizationAccess:
    """조직 경로 검사를 통과한 결과. role 은 사람의 역할이고 에이전트면 None."""

    organization: Organization
    actor: Actor
    role: OrganizationRole | None

    @property
    def organization_id(self) -> uuid.UUID:
        return self.organization.id

    @property
    def user(self) -> User:
        """사람만 쓰는 경로에서 행위자 사용자. 에이전트면 AssertionError(검사기가 먼저 막는다)."""
        assert isinstance(self.actor, HumanActor)
        return self.actor.user
