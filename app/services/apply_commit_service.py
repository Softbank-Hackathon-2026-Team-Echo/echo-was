"""승인된 진단 패치를 서비스 저장소에 커밋하고 PR 을 연다(SPEC api-19).

러너가 진단 패치 반영(APPLY_PATCH)을 샌드박스에서 SUCCEEDED 로 검증한 뒤, 재배포를 만들기 직전에
`DiagnosisService.redeploy_after_apply` 가 행 잠금 트랜잭션 전에 한 번 부른다.
스위치 `AI_APPLY_COMMIT_ENABLED`(기본 꺼짐)가 꺼져 있으면 아무 것도 하지 않고 원래 실패 커밋을
쓰게 한다(#101 동작 그대로).

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
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.repo_commit_writer import (
    EDITABLE_MODES,
    RepoCommitError,
    RepoCommitErrorCode,
    RepoCommitRequest,
    RepoCommitWriter,
)
from app.core.clock import Clock
from app.core.log_masking import mask_secrets
from app.core.repo_url import InvalidRepoUrlError, parse_repo_url
from app.enums import AgentRunKind, AgentRunStatus, RefType
from app.models import AgentRun, DeployRequest
from app.repositories.agent_run_repository import AgentRunRepository
from app.repositories.deploy_request_repository import DeployRequestRepository
from app.repositories.user_repository import UserRepository
from app.services.job_lease import JobLease, LeaseLostError
from app.services.patch_text import (
    PatchApplyError,
    UnsafePathError,
    apply_patch,
    check_diff_paths,
)
from app.services.run_result import compute_patch_hash

logger = logging.getLogger(__name__)

BRANCH_PREFIX = "echoport/fix-"
# diagnosis_service.REDEPLOY_REQUEST_ID_KEY 와 같은 값(순환 import 를 피해 둔다).
REDEPLOY_REQUEST_ID_KEY = "redeployRequestId"
# 반영 실행 result 에 WAS 가 덧붙이는 칸(재배포 칸과 겹치지 않는다).
PULL_REQUEST_URL_KEY = "pullRequestUrl"
REPO_COMMIT_KEY = "repoCommitted"
SANDBOX_COMMIT_SHA_KEY = "sandboxCommitSha"
SANDBOX_BRANCH_KEY = "sandboxBranch"
# 저장소 쓰기 표시(SPEC api-19 D-11). 행 잠금 안에서 IN_PROGRESS 를 잡은 요청 하나만 GitHub 에
# 쓴다. 잡은 요청이 죽으면 TTL 뒤 다른 요청이 다시 잡는다(브랜치 검증 D-14 로 이어 쓰기 안전).
COMMIT_STATE_KEY = "commitState"
COMMIT_CLAIMED_AT_KEY = "commitClaimedAt"
COMMIT_IN_PROGRESS = "IN_PROGRESS"
COMMIT_DONE = "DONE"
COMMIT_FAILED = "FAILED"
COMMIT_CLAIM_TTL = timedelta(minutes=10)
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
    """저장소 반영 결과.

    - `stopped=False, ref=None`: 반영 안 함(스위치 꺼짐·가짜 반영·진단 반영 아님) → 원래 커밋 재배포
    - `stopped=False, ref=<sha>`: 저장소 커밋 → 그 커밋 재배포. `result_fields` 가 있으면(이번
      요청이 GitHub 에 썼다) 재배포 잠금 트랜잭션 안에서 반영 실행 result 에 그 칸만 부분 갱신한다
    - `stopped=True`: 저장소 반영 실패 → 재배포 없음(run = FAILED 로 바뀐 반영 실행), 또는
      `pending=True`: 다른 요청이 지금 GitHub 에 쓰는 중 → 이 요청은 재배포를 만들지 않는다(그
      요청이 잇는다). run = 지금의 반영 실행
    """

    ref: str | None = None
    run: AgentRun | None = None
    stopped: bool = False
    pending: bool = False
    result_fields: dict[str, Any] | None = None
    skip_reason: str | None = None
    owner: str | None = None


def _claim_is_live(result: dict[str, Any], now: datetime) -> bool:
    """다른 요청이 잡은 저장소 쓰기 표시가 아직 살아 있나(TTL 안)."""
    if result.get(COMMIT_STATE_KEY) != COMMIT_IN_PROGRESS:
        return False
    claimed_at = result.get(COMMIT_CLAIMED_AT_KEY)
    if not isinstance(claimed_at, str):
        return False
    try:
        started = datetime.fromisoformat(claimed_at)
    except ValueError:
        return False
    return now - started < COMMIT_CLAIM_TTL


class ApplyCommitService:
    def __init__(
        self,
        session: AsyncSession,
        clock: Clock,
        writer: RepoCommitWriter | None,
        *,
        enabled: bool,
        lease: JobLease | None = None,
    ) -> None:
        self._session = session
        self._lease = lease
        self._clock = clock
        self._writer = writer
        self._enabled = enabled
        self._runs = AgentRunRepository(session)
        self._deploy_requests = DeployRequestRepository(session)
        self._users = UserRepository(session)

    async def prepare_redeploy_commit(self, apply_run_id: uuid.UUID) -> CommitOutcome:
        """재배포 전에 부른다(GitHub 호출은 행 잠금 밖에서 한다).

        두 단계(SPEC api-19 D-11):
        1. 짧은 트랜잭션: 반영 실행 행 `FOR UPDATE` → 이미 저장소에 썼으면(DONE) 그 커밋을
           돌려주고, 다른 요청이 쓰는 중이면(IN_PROGRESS, TTL 안) `pending` 을 돌려준다. 아니면
           `commitState=IN_PROGRESS` 를 부분 갱신으로 표시하고 커밋한다(잠금 해제).
        2. 잠금 없이 GitHub 쓰기. 결과 칸은 여기서 저장하지 않고 `result_fields` 로 넘긴다 —
           `DiagnosisService.redeploy_after_apply` 의 재배포 잠금 트랜잭션 안에서 부분 갱신한다.

        진단 반영이 아니거나·성공이 아니거나·이미 재배포가 연결됐거나·스위치 꺼짐·가짜 반영이면
        아무 것도 하지 않는다(ref None). 그래서 같은 반영을 두 요청이 함께 이어도(러너 결과 +
        재승인) GitHub 커밋·PR 은 한 번, 재배포는 하나다.
        """
        if not self._enabled:
            return CommitOutcome(skip_reason="COMMIT_DISABLED")
        now = self._clock.now()
        async with self._session.begin():
            apply_run = await self._runs.find_by_id(apply_run_id, lock=True)
            if (
                apply_run is None
                or apply_run.kind != AgentRunKind.APPLY_PATCH
                or apply_run.status != AgentRunStatus.SUCCEEDED
                or apply_run.parent_run_id is None
                or apply_run.simulated
                or REDEPLOY_REQUEST_ID_KEY in (apply_run.result or {})
            ):
                return CommitOutcome()
            result: dict[str, Any] = dict(apply_run.result or {})
            if result.get(REPO_COMMIT_KEY) and isinstance(result.get("appliedCommitSha"), str):
                return CommitOutcome(result["appliedCommitSha"])
            if _claim_is_live(result, now):
                return CommitOutcome(run=apply_run, pending=True)
            parent = await self._runs.find_by_id(apply_run.parent_run_id)
            if parent is None or parent.deploy_request_id is None:
                return CommitOutcome()
            failed = await self._deploy_requests.find_by_id(parent.deploy_request_id)
            if failed is None:
                return CommitOutcome()
            owner = str(uuid.uuid4())
            expected_owner = result.get("commitOwner")
            if expected_owner is not None and not isinstance(expected_owner, str):
                return CommitOutcome(run=apply_run, stopped=True)
            if not await self._runs.claim_commit(apply_run_id, now, owner, expected_owner):
                return CommitOutcome(run=apply_run, pending=True)
            if self._lease is not None:
                await self._lease.hold()
        try:
            await self._hold_owner(apply_run_id, owner)
            outcome = await self._commit(
                apply_run, {**result, "commitOwner": owner}, parent, failed
            )
            if not outcome.stopped:
                await self._hold_owner(apply_run_id, owner)
            return replace(outcome, owner=owner)
        except BaseException:
            # 늦은 취소가 새 소유자의 claim을 지우지 않도록 정확 세대만 해제한다.
            await self._release(apply_run_id, owner)
            raise

    async def _hold_owner(self, apply_run_id: uuid.UUID, owner: str) -> None:
        async with self._session.begin():
            fresh = await self._runs.find_by_id(apply_run_id, lock=True)
            if (
                fresh is None
                or fresh.status != AgentRunStatus.SUCCEEDED
                or (fresh.result or {}).get("commitOwner") != owner
                or (fresh.result or {}).get(COMMIT_STATE_KEY) != COMMIT_IN_PROGRESS
                or REDEPLOY_REQUEST_ID_KEY in (fresh.result or {})
            ):
                raise LeaseLostError
            if self._lease is not None:
                await self._lease.hold()

    async def _release(self, apply_run_id: uuid.UUID, owner: str) -> None:
        try:
            async with self._session.begin():
                await self._runs.patch_owned_commit(
                    apply_run_id, self._clock.now(), owner, {COMMIT_STATE_KEY: None}
                )
        except Exception:
            logger.exception(
                "repo commit claim release failed",
                extra={"action": "apply_commit", "apply_run_id": str(apply_run_id)},
            )

    async def _commit(
        self,
        apply_run: AgentRun,
        result: dict[str, Any],
        parent: AgentRun,
        failed: DeployRequest,
    ) -> CommitOutcome:
        try:
            request = await self._build_request(apply_run, parent, failed)
            if self._writer is None:
                raise RepoCommitError(RepoCommitErrorCode.WRITE_UNAVAILABLE)
            modes = await self._writer.file_modes(
                request.repo, request.base_commit_sha, list(request.files)
            )
            if any(mode not in EDITABLE_MODES for mode in modes.values()):
                # 심볼릭 링크(120000)·서브모듈(160000)·디렉터리: 내용 덮어쓰기로 종류가 바뀐다.
                raise RepoCommitError(RepoCommitErrorCode.PATH_REJECTED, "MODE")
            originals = await self._writer.read_files(
                request.repo, request.base_commit_sha, list(request.files)
            )
            try:
                patched = apply_patch(originals, _diff(apply_run))
            except PatchApplyError as exc:
                raise RepoCommitError(RepoCommitErrorCode.PATCH_CONFLICT, exc.reason) from None
            prepared = RepoCommitRequest(
                repo=request.repo,
                base_commit_sha=request.base_commit_sha,
                branch=request.branch,
                files=patched,
                message=request.message,
                pr_title=request.pr_title,
                pr_body=request.pr_body,
                pr_base=request.pr_base,
                modes=modes,
            )
            if COMMIT_CLAIMED_AT_KEY in result:
                # 이전 요청의 효과는 모른다. ref 부재 한 번으로 재송신하지 않는다.
                committed = await self._writer.reconcile(prepared)
            else:
                committed = await self._writer.commit_and_open_pull_request(prepared)
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
            return CommitOutcome(
                run=await self._fail(apply_run.id, exc.code, result["commitOwner"]), stopped=True
            )
        fields: dict[str, Any] = {
            SANDBOX_COMMIT_SHA_KEY: result.get("appliedCommitSha"),
            SANDBOX_BRANCH_KEY: result.get("branch"),
            "appliedCommitSha": committed.commit_sha,
            "branch": committed.branch,
            PULL_REQUEST_URL_KEY: committed.pull_request_url,
            REPO_COMMIT_KEY: True,
            COMMIT_STATE_KEY: COMMIT_DONE,
            "repositoryFinishedAt": self._clock.now().isoformat(),
        }
        logger.info(
            "apply committed to service repository",
            extra={
                "action": "apply_commit",
                "apply_run_id": str(apply_run.id),
                "commit_sha": committed.commit_sha,
                "branch": committed.branch,
            },
        )
        return CommitOutcome(committed.commit_sha, result_fields=fields)

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
        declared = patch.get("files")
        if not isinstance(declared, list) or not all(isinstance(name, str) for name in declared):
            raise RepoCommitError(RepoCommitErrorCode.PATH_REJECTED, "FILES")
        try:
            # 경로 검사 + 승인 화면 files 대조(SPEC D-13). GitHub 를 부르기 전에 막는다.
            paths = check_diff_paths(diff, declared)
        except UnsafePathError as exc:
            raise RepoCommitError(RepoCommitErrorCode.PATH_REJECTED, exc.reason) from None
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

    async def _fail(self, apply_run_id: uuid.UUID, code: str, owner: str) -> AgentRun | None:
        """반영 실행을 FAILED·code 로 바꾼다. appliedCommitSha 는 null(계약: 실패면 null)."""
        now = self._clock.now()
        async with self._session.begin():
            # 행 잠금 아래에서 읽고 쓴다(다른 요청이 그 사이 쓴 칸을 덮지 않는다).
            fresh = await self._runs.find_by_id(apply_run_id, lock=True)
            current = dict((fresh.result if fresh else None) or {})
            await self._runs.patch_owned_commit(
                apply_run_id,
                now,
                owner,
                {
                    SANDBOX_COMMIT_SHA_KEY: current.get("appliedCommitSha"),
                    "appliedCommitSha": None,
                    PULL_REQUEST_URL_KEY: None,
                    COMMIT_STATE_KEY: COMMIT_FAILED,
                    "repositoryFinishedAt": now.isoformat(),
                },
                fail_code=code,
            )
            return await self._runs.find_by_id(apply_run_id)


def _diff(apply_run: AgentRun) -> str:
    patch = (apply_run.input_snapshot or {}).get("patch") or {}
    diff = patch.get("diff")
    if not isinstance(diff, str) or not diff:
        raise RepoCommitError(RepoCommitErrorCode.PATCH_CONFLICT, "NO_DIFF")
    return diff
