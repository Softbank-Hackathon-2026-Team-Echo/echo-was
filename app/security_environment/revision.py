"""현재 설정과 자동 증거의 범위를 비교한다."""

import uuid
from typing import Any

from app.core.config import get_settings
from app.security_environment.types import EnvironmentSecurityProfile


def find_profile(
    organization_id: uuid.UUID, environment_id: uuid.UUID
) -> EnvironmentSecurityProfile | None:
    return next(
        (
            profile
            for profile in get_settings().security_environment_profiles
            if profile.organization_id == organization_id
            and profile.environment_id == environment_id
        ),
        None,
    )


def current_revision(
    organization_id: uuid.UUID,
    environment_id: uuid.UUID,
    config: dict[str, Any],
    secret_ref: str | None,
) -> str | None:
    profile = find_profile(organization_id, environment_id)
    return profile.revision(config, secret_ref) if profile is not None else None
