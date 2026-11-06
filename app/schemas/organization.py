"""로그인·조직·멤버·초대·에이전트 토큰 API 요청·응답 스키마. JSON 이름은 camelCase."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import Field, StringConstraints

from app.clients.github_user import GITHUB_LOGIN_PATTERN
from app.enums import InvitationStatus, OrganizationRole
from app.models import AgentToken, Invitation, User
from app.models.agent_token import AGENT_TOKEN_NAME_MAX_LENGTH
from app.models.organization import (
    LIVE_FEED_MIN_MAGNITUDE_RANGE,
    LIVE_FEED_RADIUS_KM_RANGE,
    ORGANIZATION_NAME_MAX_LENGTH,
    ORGANIZATION_SLUG_MAX_LENGTH,
    ORGANIZATION_SLUG_PATTERN,
)
from app.models.user import GITHUB_LOGIN_MAX_LENGTH
from app.schemas.response import ApiModel, ApiRequestModel
from app.services.agent_token_service import IssuedAgentToken
from app.services.auth_service import MeView
from app.services.organization_service import MemberView, OrganizationView

# 스키마 단계의 상한. 설정(agent_token_max_days)이 더 낮으면 서비스가 다시 막는다.
AGENT_TOKEN_MAX_DAYS_LIMIT = 90

OrganizationSlug = Annotated[
    str,
    StringConstraints(pattern=ORGANIZATION_SLUG_PATTERN, max_length=ORGANIZATION_SLUG_MAX_LENGTH),
]
OrganizationName = Annotated[
    str, StringConstraints(min_length=1, max_length=ORGANIZATION_NAME_MAX_LENGTH)
]
GithubLogin = Annotated[
    str,
    StringConstraints(pattern=GITHUB_LOGIN_PATTERN.pattern, max_length=GITHUB_LOGIN_MAX_LENGTH),
]
AgentTokenName = Annotated[
    str, StringConstraints(min_length=1, max_length=AGENT_TOKEN_NAME_MAX_LENGTH)
]


class UserResponse(ApiModel):
    id: uuid.UUID
    github_login: str
    name: str | None
    avatar_url: str | None

    @classmethod
    def from_model(cls, user: User) -> "UserResponse":
        return cls(
            id=user.id, github_login=user.github_login, name=user.name, avatar_url=user.avatar_url
        )


class OrganizationResponse(ApiModel):
    """role 은 요청한 사람의 역할이다. 에이전트 토큰으로 읽으면 null.

    allowSelfApproval 은 이전 계획을 만든 사람이 스스로 승인·거절할 수 있는지다(기본 false).
    """

    id: uuid.UUID
    slug: str
    name: str
    personal: bool
    role: OrganizationRole | None
    allow_self_approval: bool
    created_at: datetime

    @classmethod
    def from_view(cls, view: OrganizationView) -> "OrganizationResponse":
        organization = view.organization
        return cls(
            id=organization.id,
            slug=organization.slug,
            name=organization.name,
            personal=organization.personal,
            role=view.role,
            allow_self_approval=organization.allow_self_approval,
            created_at=organization.created_at,
        )


class MeResponse(ApiModel):
    user: UserResponse
    organizations: list[OrganizationResponse]

    @classmethod
    def from_view(cls, view: MeView) -> "MeResponse":
        return cls(
            user=UserResponse.from_model(view.user),
            organizations=[OrganizationResponse.from_view(item) for item in view.organizations],
        )


class CreateOrganizationRequest(ApiRequestModel):
    """slug 는 1~39자 소문자·숫자·하이픈(처음·끝·연속 하이픈 금지)이고 만든 뒤 바꿀 수 없다."""

    slug: OrganizationSlug
    name: OrganizationName


class UpdateOrganizationSettingsRequest(ApiRequestModel):
    """조직 설정 변경(어드민). allowSelfApproval 은 필수다.

    liveFeedMinMagnitude·liveFeedRadiusKm 은 실제 재난 피드 기준이다(API 서버 18, 생략하면 그대로).
    지금 값은 GET /disasters/feed 의 threshold 로 본다.
    """

    allow_self_approval: bool
    live_feed_min_magnitude: Annotated[
        Decimal | None,
        Field(
            default=None,
            ge=LIVE_FEED_MIN_MAGNITUDE_RANGE[0],
            le=LIVE_FEED_MIN_MAGNITUDE_RANGE[1],
            max_digits=3,
            decimal_places=1,
        ),
    ] = None
    live_feed_radius_km: Annotated[
        int | None,
        Field(default=None, ge=LIVE_FEED_RADIUS_KM_RANGE[0], le=LIVE_FEED_RADIUS_KM_RANGE[1]),
    ] = None


class MemberResponse(ApiModel):
    """id 는 사용자 id 다(멤버 변경 경로의 {user_id})."""

    id: uuid.UUID
    github_login: str
    name: str | None
    avatar_url: str | None
    role: OrganizationRole
    joined_at: datetime

    @classmethod
    def from_view(cls, view: MemberView) -> "MemberResponse":
        return cls(
            id=view.user.id,
            github_login=view.user.github_login,
            name=view.user.name,
            avatar_url=view.user.avatar_url,
            role=OrganizationRole(view.member.role),
            joined_at=view.member.created_at,
        )


class UpdateMemberRoleRequest(ApiRequestModel):
    role: OrganizationRole


class CreateInvitationRequest(ApiRequestModel):
    github_login: GithubLogin
    role: OrganizationRole


class InvitationResponse(ApiModel):
    id: uuid.UUID
    github_login: str
    github_id: int
    role: OrganizationRole
    status: InvitationStatus
    invited_by_user_id: uuid.UUID
    expires_at: datetime
    accepted_at: datetime | None
    created_at: datetime

    @classmethod
    def from_model(cls, invitation: Invitation) -> "InvitationResponse":
        return cls(
            id=invitation.id,
            github_login=invitation.github_login,
            github_id=invitation.github_id,
            role=OrganizationRole(invitation.role),
            status=InvitationStatus(invitation.status),
            invited_by_user_id=invitation.invited_by_user_id,
            expires_at=invitation.expires_at,
            accepted_at=invitation.accepted_at,
            created_at=invitation.created_at,
        )


class CreateAgentTokenRequest(ApiRequestModel):
    """expiresInDays 를 빼면 설정 기본값(30일)이다."""

    name: AgentTokenName
    expires_in_days: int | None = Field(default=None, ge=1, le=AGENT_TOKEN_MAX_DAYS_LIMIT)


class AgentTokenResponse(ApiModel):
    """원문은 싣지 않는다(last4 만)."""

    id: uuid.UUID
    name: str
    last4: str
    created_by: uuid.UUID
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None

    @classmethod
    def from_model(cls, token: AgentToken) -> "AgentTokenResponse":
        return cls(
            id=token.id,
            name=token.name,
            last4=token.last4,
            created_by=token.created_by_user_id,
            created_at=token.created_at,
            expires_at=token.expires_at,
            revoked_at=token.revoked_at,
            last_used_at=token.last_used_at,
        )


class IssuedAgentTokenResponse(AgentTokenResponse):
    """발급 응답. token(원문)은 이 응답에만 한 번 싣는다. 다시 볼 수 없다."""

    token: str

    @classmethod
    def from_issued(cls, issued: IssuedAgentToken) -> "IssuedAgentTokenResponse":
        base = AgentTokenResponse.from_model(issued.token)
        return cls(**base.model_dump(), token=issued.raw.get_secret_value())
