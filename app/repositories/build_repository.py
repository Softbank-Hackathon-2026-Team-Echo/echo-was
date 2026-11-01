import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import BuildStatus
from app.models import Build
from app.repositories.conditional_update import update_where


class BuildRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, build: Build) -> None:
        self._session.add(build)

    async def find_by_id(self, build_id: uuid.UUID, *, lock: bool = False) -> Build | None:
        statement = (
            select(Build).where(Build.id == build_id).execution_options(populate_existing=True)
        )
        if lock:
            statement = statement.with_for_update(key_share=True)
        return await self._session.scalar(statement)

    async def find_not_failed(
        self, service_id: uuid.UUID, repo_url: str, commit_sha: str
    ) -> Build | None:
        """같은 서비스·저장소·커밋의 QUEUED·RUNNING·SUCCEEDED 빌드(부분 unique 라 많아야 하나)."""
        statement = (
            select(Build)
            .where(
                Build.service_id == service_id,
                Build.repo_url == repo_url,
                Build.commit_sha == commit_sha,
                Build.status != BuildStatus.FAILED,
            )
            .execution_options(populate_existing=True)
        )
        return await self._session.scalar(statement)

    async def transition(
        self,
        build_id: uuid.UUID,
        expected: BuildStatus | Iterable[BuildStatus],
        values: dict[str, Any],
    ) -> bool:
        statuses = [expected] if isinstance(expected, BuildStatus) else list(expected)
        return await update_where(
            self._session, Build, build_id, [Build.status.in_(statuses)], values
        )

    async def mark_start_requested(self, build_id: uuid.UUID, values: dict[str, Any]) -> bool:
        return await update_where(
            self._session,
            Build,
            build_id,
            [Build.status == BuildStatus.QUEUED, Build.start_requested_at.is_(None)],
            values,
        )
