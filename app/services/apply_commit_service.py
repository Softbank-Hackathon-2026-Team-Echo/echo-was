"""승인된 진단 패치를 서비스 저장소에 커밋하고 PR 을 연다(SPEC api-19).

러너가 진단 패치 반영(APPLY_PATCH)을 샌드박스에서 SUCCEEDED 로 검증한 뒤, 재배포를 만들기 직전에
`DiagnosisService.redeploy_after_apply` 가 한 번 부른다. 스위치 `AI_APPLY_COMMIT_ENABLED`(기본
꺼짐)가 꺼져 있으면 아무 것도 하지 않고 원래 실패 커밋을 돌려준다(#101 동작 그대로).

켜져 있으면:
- 가짜 반영(simulated)은 GitHub 를 부르지 않는다(가짜 sha·`pullRequestUrl` null 그대로, 재배포는
  원래 커밋 — 지어낸 sha 는 저장소에 없어 빌드할 수 없다).
- 러너 반영: 실패 커밋의 파일을 읽어 승인된 diff 를 엄격히 적용 → 브랜치 `echoport/fix-<run8>` +
  커밋(작성자 EchoPort bot, 메시지에 진단 요약·승인자·실행 id) + PR → 반영 실행 결과의
  `appliedCommitSha`·`branch`·`pullRequestUrl` 을 저장소 값으로 바꾸고(러너의 로컬 값은
  `sandboxCommitSha`·`sandboxBranch` 로 남김) 그 커밋을 재배포 ref 로 돌려준다.
- 실패면 반영 실행을 FAILED·failureCode(REPO_…)로 바꾸고 재배포를 만들지 않는다.
- 같은 반영을 다시 받으면(재전송) 이미 기록된 커밋을 그대로 쓴다(GitHub 를 다시 부르지 않음).
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.repo_commit_writer import (
    RepoCommitError,
    RepoCommitErrorCode,
    RepoCommitRequest,
    RepoCommitWriter,
)
from app.core.clock import Clock
from app.core.log_masking import mask_secrets
from app.core.repo_url import InvalidRepoUrlError, parse_repo_url
from app.enums import RefType
from app.models import AgentRun, DeployRequest
from app.repositories.agent_run_repository import AgentRunRepository
from app.repositories.user_repository import UserRepository
from app.services.patch_text import PatchApplyError, apply_patch, diff_paths
from app.services.run_result import compute_patch_hash

logger = logging.getLogger(__name__)

BRANCH_PREFIX = "echoport/fix-"
# 반영 실행 result 에 WAS 가 덧붙이는 칸(재배포 칸과 겹치지 않는다).
PULL_REQUEST_URL_KEY = "pullRequestUrl"
REPO_COMMIT_KEY = "repoCommitted"
SANDBOX_COMMIT_SHA_KEY = "sandboxCommitSha"
SANDBOX_BRANCH_KEY = "sandboxBranch"
CAUSE_TITLE_LIMIT = 72
TEXT_LIMIT = 500


def fix_branch_name(apply_run_id: uuid.UUID) -> str:
    """`echoport/fix-<반영 실행 id 앞 8자>`."""
    return f"{BRANCH_PREFIX}{apply_run_id.hex[:8]}"


@dataclass(frozen=True)
class CommitContext:
    """커밋 메시지·PR 본문 재료. 문자열은 비밀값 모양을 가린 값이다."""

    service_name: str | None
    cause: str | None
    category: str | None
    approver: str
    diagnosis_run_id: uuid.UUID
    apply_run_id: uuid.UUID
    deploy_request_id: uuid.UUID
    source_commit_sha: str
    patch_id: str
    patch_hash: str


def _short(text: str | None, limit: int) -> str:
    value = " ".join((text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def commit_message(context: CommitContext) -> str:
    """제목 한 줄 + 진단 요약·승인자·실행 id."""
    title = _short(context.cause, CAUSE_TITLE_LIMIT) or "AI 진단 수정"
    lines = [
        f"fix: EchoPort AI 진단 수정 반영 — {title}",
        "",
        f"진단({context.category or '분류 없음'}): {_short(context.cause, TEXT_LIMIT) or '-'}",
        f"승인: {context.approver}",
        f"진단 실행: {context.diagnosis_run_id}",
        f"반영 실행: {context.apply_run_id}",
        f"실패 배포: {context.deploy_request_id} (커밋 {context.source_commit_sha[:12]})",
        f"패치: {context.patch_id} {context.patch_hash}",
        "",
        "EchoPort 샌드박스에서 이미지 빌드·Trivy·healthz 를 통과한 패치입니다.",
    ]
    return mask_secrets("\n".join(lines))


def pull_request_title(context: CommitContext) -> str:
    title = _short(context.cause, CAUSE_TITLE_LIMIT) or "AI 진단 수정"
    service = f"[{context.service_name}] " if context.service_name else ""
    return mask_secrets(f"{service}EchoPort AI 진단 수정: {title}")


def pull_request_body(context: CommitContext) -> str:
    rows = [
        "EchoPort 가 배포 실패를 진단하고, 사람이 승인한 수정 패치를 샌드박스에서 검증한 뒤 "
        "만든 PR 입니다. EchoPort 는 이 브랜치의 커밋으로 같은 환경에 다시 배포합니다"
        "(이 PR 의 병합은 저장소 주인이 정합니다).",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        f"| 원인 | {_short(context.cause, TEXT_LIMIT) or '-'} |",
        f"| 분류 | {context.category or '-'} |",
        f"| 승인 | {context.approver} |",
        f"| 진단 실행 | `{context.diagnosis_run_id}` |",
        f"| 반영 실행 | `{context.apply_run_id}` |",
        f"| 실패 배포 | `{context.deploy_request_id}` (커밋 `{context.source_commit_sha[:12]}`) |",
        f"| 패치 | `{context.patch_id}` `{context.patch_hash}` |",
    ]
    return mask_secrets("\n".join(rows))


@dataclass(frozen=True)
class CommitOutcome:
    """재배포에 쓸 커밋(ref). None 이면 재배포를 만들지 않는다(run = 바뀐 반영 실행)."""

    ref: str | None
    run: AgentRun | None = None


class ApplyCommitService:
    def __init__(
        self,
        session: AsyncSession,
        clock: Clock,
        writer: RepoCommitWriter | None,
        *,
        enabled: bool,
    ) -> None:
        self._session = session
        self._clock = clock
        self._writer = writer
        self._enabled = enabled
        self._runs = AgentRunRepository(session)
        self._users = UserRepository(session)

    async def commit_for_redeploy(
        self, apply_run: AgentRun, parent: AgentRun, failed: DeployRequest
    ) -> CommitOutcome:
        """재배포 ref 를 정한다. 스위치 꺼짐·가짜 반영이면 원래 실패 커밋 그대로."""
        if not self._enabled or apply_run.simulated:
            return CommitOutcome(failed.commit_sha)
        result: dict[str, Any] = dict(apply_run.result or {})
        if result.get(REPO_COMMIT_KEY) and isinstance(result.get("appliedCommitSha"), str):
            return CommitOutcome(result["appliedCommitSha"])
        try:
            request = await self._build_request(apply_run, parent, failed)
            if self._writer is None:
                raise RepoCommitError(RepoCommitErrorCode.WRITE_UNAVAILABLE)
            originals = await self._writer.read_files(
                request.repo, request.base_commit_sha, list(request.files)
            )
            try:
                patched = apply_patch(originals, _diff(apply_run))
            except PatchApplyError as exc:
                raise RepoCommitError(RepoCommitErrorCode.PATCH_CONFLICT, exc.reason) from None
            committed = await self._writer.commit_and_open_pull_request(
                RepoCommitRequest(
                    repo=request.repo,
                    base_commit_sha=request.base_commit_sha,
                    branch=request.branch,
                    files=patched,
                    message=request.message,
                    pr_title=request.pr_title,
                    pr_body=request.pr_body,
                    pr_base=request.pr_base,
                )
            )
        except RepoCommitError as exc:
            logger.warning(
                "repo commit failed",
                extra={
                    "action": "apply_commit",
                    "apply_run_id": str(apply_run.id),
                    "code": exc.code,
                    "detail": exc.detail,
                },
            )
            return CommitOutcome(None, await self._fail(apply_run.id, exc.code))
        now = self._clock.now()
        async with self._session.begin():
            fresh = await self._runs.find_by_id(apply_run.id)
            current = dict((fresh.result if fresh else None) or result)
            await self._runs.merge_result(
                apply_run.id,
                now,
                {
                    **current,
                    SANDBOX_COMMIT_SHA_KEY: result.get("appliedCommitSha"),
                    SANDBOX_BRANCH_KEY: result.get("branch"),
                    "appliedCommitSha": committed.commit_sha,
                    "branch": committed.branch,
                    PULL_REQUEST_URL_KEY: committed.pull_request_url,
                    REPO_COMMIT_KEY: True,
                },
            )
        logger.info(
            "apply committed to service repository",
            extra={
                "action": "apply_commit",
                "apply_run_id": str(apply_run.id),
                "commit_sha": committed.commit_sha,
                "branch": committed.branch,
            },
        )
        return CommitOutcome(committed.commit_sha)

    async def _build_request(
        self, apply_run: AgentRun, parent: AgentRun, failed: DeployRequest
    ) -> RepoCommitRequest:
        """파일 내용(files)은 경로 → 빈 값이다. 실제 내용은 읽은 뒤 채운다."""
        snapshot = apply_run.input_snapshot or {}
        patch = snapshot.get("patch") or {}
        diff = _diff(apply_run)
        if compute_patch_hash(diff) != patch.get("patchHash"):
            raise RepoCommitError(RepoCommitErrorCode.PATCH_CONFLICT, "PATCH_HASH")
        try:
            repo = parse_repo_url(failed.repo_url)
        except InvalidRepoUrlError:
            raise RepoCommitError(RepoCommitErrorCode.WRITE_FAILED, "REPO_URL") from None
        paths = diff_paths(diff)
        if not paths:
            raise RepoCommitError(RepoCommitErrorCode.PATCH_CONFLICT, "NO_FILE")
        async with self._session.begin():
            user = await self._users.find_by_id(apply_run.requested_by_user_id)
        approver = (
            f"@{user.github_login}" if user is not None else str(apply_run.requested_by_user_id)
        )
        diagnosis = (parent.result or {}).get("diagnosis") or {}
        service = (parent.input_snapshot or {}).get("service") or {}
        context = CommitContext(
            service_name=service.get("name") if isinstance(service.get("name"), str) else None,
            cause=diagnosis.get("cause") if isinstance(diagnosis.get("cause"), str) else None,
            category=(
                diagnosis.get("category") if isinstance(diagnosis.get("category"), str) else None
            ),
            approver=approver,
            diagnosis_run_id=parent.id,
            apply_run_id=apply_run.id,
            deploy_request_id=failed.id,
            source_commit_sha=failed.commit_sha,
            patch_id=str(patch.get("id")),
            patch_hash=str(patch.get("patchHash")),
        )
        return RepoCommitRequest(
            repo=repo,
            base_commit_sha=failed.commit_sha,
            branch=fix_branch_name(apply_run.id),
            files=dict.fromkeys(paths, ""),
            message=commit_message(context),
            pr_title=pull_request_title(context),
            pr_body=pull_request_body(context),
            pr_base=failed.ref_value if failed.ref_type == RefType.BRANCH else None,
        )

    async def _fail(self, apply_run_id: uuid.UUID, code: str) -> AgentRun | None:
        """반영 실행을 FAILED·code 로 바꾼다. appliedCommitSha 는 null(계약: 실패면 null)."""
        now = self._clock.now()
        async with self._session.begin():
            fresh = await self._runs.find_by_id(apply_run_id)
            current = dict((fresh.result if fresh else None) or {})
            await self._runs.fail_after_success(
                apply_run_id,
                now,
                code,
                {
                    **current,
                    SANDBOX_COMMIT_SHA_KEY: current.get("appliedCommitSha"),
                    "appliedCommitSha": None,
                    PULL_REQUEST_URL_KEY: None,
                },
            )
            return await self._runs.find_by_id(apply_run_id)


def _diff(apply_run: AgentRun) -> str:
    patch = (apply_run.input_snapshot or {}).get("patch") or {}
    diff = patch.get("diff")
    if not isinstance(diff, str) or not diff:
        raise RepoCommitError(RepoCommitErrorCode.PATCH_CONFLICT, "NO_DIFF")
    return diff
