"""승인 패치를 서비스 저장소에 커밋·PR 하고 그 커밋으로 재배포(SPEC api-19).

GitHub 쓰기는 가짜 클라이언트(FakeRepoCommitWriter)로 바꿔 끼운다.

러너 진단 → 승인 → 러너 반영(APPLY_PATCH SUCCEEDED) 결과 요청 안에서 WAS 가
- 성공: 저장소 커밋·PR → apply.pullRequestUrl·appliedCommitSha·branch → 재배포 ref = 그 커밋
- 실패: apply FAILED·failureCode(REPO_…), 재배포를 만들지 않는다
- 스위치 꺼짐: #98 동작 그대로(원래 실패 커밋 재배포, pullRequestUrl null, 클라이언트 안 부름)
- 가짜 진단(simulate): 스위치가 켜져 있어도 GitHub 를 부르지 않는다(가짜 sha·null URL)
"""

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import func, select

from app.clients.repo_commit_writer import (
    FakeRepoCommitWriter,
    RepoCommitRequest,
    RepoCommitResult,
)
from app.dependencies import get_repo_commit_writer
from app.main import app
from app.models import DeployRequest
from app.services.run_result import compute_patch_hash
from tests.integration.migration_support import Migration
from tests.integration.runner_support import RunnerFixture, add_runner, event, run_patch
from tests.integration.test_failed_deploy_diagnosis import (
    DIAGNOSIS_CAPABILITIES,
    _approve,
    _claim,
    _data,
    _detail,
    _failed_deploy,
    _request_diagnosis,
)

SANDBOX_SHA = "0123456789abcdef0123456789abcdef01234567"
REPO_SHA = "f1" * 20
PR_URL = "https://github.com/team-echo/shop-api/pull/1"
# Dockerfile: FROM a → FROM fix-1. 승인 화면 files 와 diff 경로가 같아야 저장소에 쓴다(SPEC D-13).
PATCH = run_patch("fix-1", origin="AI", files=["Dockerfile"])
BRANCH = re.compile(r"^echoport/fix-[0-9a-f]{8}$")


def _use_writer(writer: FakeRepoCommitWriter | None) -> None:
    app.dependency_overrides[get_repo_commit_writer] = lambda: writer


def _diagnosis_result(patch: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": "SUCCEEDED",
        "failureCode": None,
        "simulated": False,
        "sandbox": None,
        "model": None,
        "configDiff": [],
        "featureGaps": [],
        "findings": [],
        "patches": [patch or PATCH],
        "blockedAttempts": [],
        "diagnosis": {
            "result": "DIAGNOSED",
            "category": "CODE",
            "cause": "Dockerfile 1번째 줄의 베이스 이미지가 잘못됐습니다.",
            "fixable": True,
            "unfixableReason": None,
            "evidence": [{"source": "LOG", "line": 1, "text": "pull access denied for a"}],
        },
    }


async def _deploy_count(migration: Migration) -> int:
    async with migration.harness.session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(DeployRequest))
    return int(count or 0)


async def _runner_apply(
    migration: Migration, *, commit_enabled: bool = True, patch: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], int]:
    """실패 배포 → 러너 진단 → 승인 → 러너 반영 성공. (실패 배포, 진단 상세 patch, apply, 반영
    결과 직전 배포 요청 수)."""
    harness = migration.harness
    harness.use_settings(
        ai_diagnosis_runner_enabled=True,
        patch_apply_enabled=True,
        ai_apply_commit_enabled=commit_enabled,
    )
    runner: RunnerFixture = await add_runner(harness.session_factory)
    failed = await _failed_deploy(migration)
    run_id = _data(await _request_diagnosis(migration, failed["id"]))["id"]
    claimed = await _claim(runner, DIAGNOSIS_CAPABILITIES)
    assert claimed.run_id == run_id
    await claimed.post("events", {"events": [event(1, "INPUT_LOAD")]})
    submitted = await claimed.post("result", _diagnosis_result(patch))
    assert submitted.status_code == 200, submitted.text
    approved = await _approve(migration, run_id, patch or PATCH)
    assert approved.status_code == 200, approved.text
    apply_claim = await _claim(runner, DIAGNOSIS_CAPABILITIES)
    before = await _deploy_count(migration)
    done = await apply_claim.post(
        "result",
        {
            "status": "SUCCEEDED",
            "failureCode": None,
            "simulated": False,
            "appliedCommitSha": SANDBOX_SHA,
            "branch": f"applied/{apply_claim.run_id}",
        },
    )
    assert done.status_code == 200, done.text
    patch = (await _detail(migration, run_id))["patches"][0]
    return failed, patch, patch["apply"], before


async def test_commit_success_fills_pr_url_and_redeploys_that_commit(
    migration: Migration,
) -> None:
    writer = FakeRepoCommitWriter(files={"Dockerfile": "FROM a\n"}, commit_sha=REPO_SHA)
    _use_writer(writer)

    failed, patch, apply, _ = await _runner_apply(migration)

    assert patch["status"] == "APPLIED"
    assert (apply["status"], apply["failureCode"], apply["simulated"]) == (
        "SUCCEEDED",
        None,
        False,
    )
    assert apply["appliedCommitSha"] == REPO_SHA
    assert BRANCH.match(apply["branch"]) and apply["branch"].endswith(apply["runId"][:8])
    assert apply["pullRequestUrl"] == PR_URL
    redeploy = await migration.harness.get_request(apply["redeployRequestId"])
    assert (redeploy["ref"]["type"], redeploy["commitSha"]) == ("COMMIT", REPO_SHA)
    assert redeploy["environment"]["id"] == failed["environment"]["id"]

    assert writer.reads == [("team-echo/shop-api", failed["commitSha"], ("Dockerfile",))]
    (request,) = writer.requests
    assert request.base_commit_sha == failed["commitSha"]
    assert request.files == {"Dockerfile": "FROM fix-1\n"}
    assert request.branch == apply["branch"]
    login = migration.approver.user.github_login
    assert f"승인: @{login}" in request.message
    assert f"반영 실행: {apply['runId']}" in request.message
    assert "Dockerfile 1번째 줄의 베이스 이미지가 잘못됐습니다." in request.message
    expected_base = failed["ref"]["value"] if failed["ref"]["type"] == "BRANCH" else None
    assert request.pr_base == expected_base


async def test_commit_failure_marks_apply_failed_without_redeploy(migration: Migration) -> None:
    writer = FakeRepoCommitWriter(
        files={"Dockerfile": "FROM a\n"}, fail_code="REPO_WRITE_FORBIDDEN"
    )
    _use_writer(writer)

    _, patch, apply, before = await _runner_apply(migration)

    assert patch["status"] == "APPLY_FAILED"
    assert (apply["status"], apply["failureCode"]) == ("FAILED", "REPO_WRITE_FORBIDDEN")
    assert apply["appliedCommitSha"] is None and apply["pullRequestUrl"] is None
    assert (apply["redeployRequestId"], apply["redeployRejectedCode"]) == (None, None)
    assert await _deploy_count(migration) == before
    assert writer.requests == []


async def test_patch_conflict_on_repository_file_fails_before_commit(
    migration: Migration,
) -> None:
    writer = FakeRepoCommitWriter(files={"Dockerfile": "FROM changed\n"})
    _use_writer(writer)

    _, _, apply, before = await _runner_apply(migration)

    assert (apply["status"], apply["failureCode"]) == ("FAILED", "REPO_PATCH_CONFLICT")
    assert writer.requests == [] and await _deploy_count(migration) == before


async def test_switch_on_without_github_app_is_unavailable(migration: Migration) -> None:
    _use_writer(None)

    _, _, apply, before = await _runner_apply(migration)

    assert (apply["status"], apply["failureCode"]) == ("FAILED", "REPO_WRITE_UNAVAILABLE")
    assert await _deploy_count(migration) == before


async def test_switch_off_keeps_original_commit_redeploy(migration: Migration) -> None:
    writer = FakeRepoCommitWriter(files={"Dockerfile": "FROM a\n"})
    _use_writer(writer)

    failed, _, apply, _ = await _runner_apply(migration, commit_enabled=False)

    assert (apply["status"], apply["appliedCommitSha"]) == ("SUCCEEDED", SANDBOX_SHA)
    assert apply["branch"] == f"applied/{apply['runId']}"
    assert apply["pullRequestUrl"] is None
    redeploy = await migration.harness.get_request(apply["redeployRequestId"])
    assert redeploy["commitSha"] == failed["commitSha"]
    assert writer.reads == [] and writer.requests == []


async def test_simulated_diagnosis_never_calls_github(migration: Migration) -> None:
    migration.harness.use_settings(ai_apply_commit_enabled=True)
    writer = FakeRepoCommitWriter(files={"Dockerfile": "FROM a\n"})
    _use_writer(writer)
    failed = await _failed_deploy(migration)
    run = _data(await _request_diagnosis(migration, failed["id"]))
    assert run["simulated"] is True

    approved = await _approve(migration, run["id"], run["patches"][0])

    assert approved.status_code == 200, approved.text
    apply = _data(approved)["apply"]
    assert (apply["status"], apply["simulated"], apply["pullRequestUrl"]) == (
        "SUCCEEDED",
        True,
        None,
    )
    assert re.fullmatch(r"[0-9a-f]{40}", apply["appliedCommitSha"])
    redeploy = await migration.harness.get_request(apply["redeployRequestId"])
    assert redeploy["commitSha"] == failed["commitSha"]
    assert writer.reads == [] and writer.requests == []


# --- 검증 반영(SPEC api-19 D-11·D-13): 경로 거절·동시 요청 ---


def _patch_with(diff: str, files: list[str]) -> dict[str, Any]:
    return run_patch(
        "fix-1", origin="AI", files=files, diff=diff, patchHash=compute_patch_hash(diff)
    )


def _diff_for(path: str) -> str:
    return f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-FROM a\n+FROM fix-1\n"


@pytest.mark.parametrize(
    "path",
    [
        "/absolute",
        "../escape",
        "dir/../../escape",
        ".github/workflows/deploy.yml",
        ".git/config",
        "a//Dockerfile",
    ],
)
async def test_unsafe_diff_path_is_rejected_before_github(migration: Migration, path: str) -> None:
    writer = FakeRepoCommitWriter(files={path: "FROM a\n"})
    _use_writer(writer)

    _, _, apply, before = await _runner_apply(migration, patch=_patch_with(_diff_for(path), [path]))

    assert (apply["status"], apply["failureCode"]) == ("FAILED", "REPO_PATH_REJECTED")
    assert apply["appliedCommitSha"] is None and apply["redeployRequestId"] is None
    assert await _deploy_count(migration) == before
    assert writer.reads == [] and writer.requests == []


async def test_declared_files_must_match_diff_paths(migration: Migration) -> None:
    # 승인 화면에는 Dockerfile 만 보였는데 diff 는 다른 파일을 고친다.
    writer = FakeRepoCommitWriter(files={"app.py": "FROM a\n", "Dockerfile": "FROM a\n"})
    _use_writer(writer)

    _, _, apply, before = await _runner_apply(
        migration, patch=_patch_with(_diff_for("app.py"), ["Dockerfile"])
    )

    assert (apply["status"], apply["failureCode"]) == ("FAILED", "REPO_PATH_REJECTED")
    assert await _deploy_count(migration) == before
    assert writer.reads == [] and writer.requests == []


async def test_symlink_target_is_rejected(migration: Migration) -> None:
    link = FakeRepoCommitWriter(files={"Dockerfile": "FROM a\n"}, modes={"Dockerfile": "120000"})
    _use_writer(link)
    _, _, apply, before = await _runner_apply(migration)
    assert (apply["status"], apply["failureCode"]) == ("FAILED", "REPO_PATH_REJECTED")
    assert await _deploy_count(migration) == before
    assert link.reads == [] and link.requests == []


async def test_executable_file_mode_is_preserved(migration: Migration) -> None:
    writer = FakeRepoCommitWriter(
        files={"Dockerfile": "FROM a\n"}, modes={"Dockerfile": "100755"}, commit_sha=REPO_SHA
    )
    _use_writer(writer)
    _, _, apply, _ = await _runner_apply(migration)
    assert (apply["status"], apply["appliedCommitSha"]) == ("SUCCEEDED", REPO_SHA)
    (request,) = writer.requests
    assert dict(request.modes) == {"Dockerfile": "100755"}


@dataclass
class _SlowWriter(FakeRepoCommitWriter):
    """커밋 단계에서 멈춰, 그동안 다른 요청이 같은 반영을 잇게 한다."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def commit_and_open_pull_request(self, request: RepoCommitRequest) -> RepoCommitResult:
        self.entered.set()
        await asyncio.wait_for(self.release.wait(), 10)
        return await super().commit_and_open_pull_request(request)


async def test_concurrent_runner_result_and_reapproval_commit_once(migration: Migration) -> None:
    """러너 결과(GitHub 쓰는 중) + 같은 패치 재승인이 겹쳐도 커밋·PR 1회, 재배포 1개."""
    harness = migration.harness
    harness.use_settings(
        ai_diagnosis_runner_enabled=True, patch_apply_enabled=True, ai_apply_commit_enabled=True
    )
    runner = await add_runner(harness.session_factory)
    failed = await _failed_deploy(migration)
    run_id = _data(await _request_diagnosis(migration, failed["id"]))["id"]
    claimed = await _claim(runner, DIAGNOSIS_CAPABILITIES)
    await claimed.post("events", {"events": [event(1, "INPUT_LOAD")]})
    assert (await claimed.post("result", _diagnosis_result())).status_code == 200
    assert (await _approve(migration, run_id, PATCH)).status_code == 200
    apply_claim = await _claim(runner, DIAGNOSIS_CAPABILITIES)
    writer = _SlowWriter(files={"Dockerfile": "FROM a\n"}, commit_sha=REPO_SHA)
    _use_writer(writer)
    before = await _deploy_count(migration)

    done_task = asyncio.create_task(
        apply_claim.post(
            "result",
            {
                "status": "SUCCEEDED",
                "failureCode": None,
                "simulated": False,
                "appliedCommitSha": SANDBOX_SHA,
                "branch": f"applied/{apply_claim.run_id}",
            },
        )
    )
    await asyncio.wait_for(writer.entered.wait(), 10)
    # 앞 요청이 GitHub 에 쓰는 중(행 잠금은 풀려 있음)에 재승인이 온다 → 기다리지 않고 200.
    repeated = await asyncio.wait_for(_approve(migration, run_id, PATCH), 10)
    assert repeated.status_code == 200, repeated.text
    assert len(writer.requests) == 0
    writer.release.set()
    done = await asyncio.wait_for(done_task, 10)
    assert done.status_code == 200, done.text
    # 끝난 뒤 한 번 더 와도 이미 연결된 값을 쓴다.
    again = await _approve(migration, run_id, PATCH)
    assert again.status_code == 200, again.text

    assert len(writer.requests) == 1
    assert await _deploy_count(migration) == before + 1
    apply = (await _detail(migration, run_id))["patches"][0]["apply"]
    assert (apply["status"], apply["appliedCommitSha"]) == ("SUCCEEDED", REPO_SHA)
    assert apply["pullRequestUrl"] == PR_URL
    redeploy = await migration.harness.get_request(apply["redeployRequestId"])
    assert redeploy["commitSha"] == REPO_SHA
