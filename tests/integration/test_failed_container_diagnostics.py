"""배포 실패 진단 통합 시험(API 서버 4-1).

컨테이너 로그·종료 코드·사유가 배포 작업 로그와 실패 상세에 남고, 비밀값이 가려지고,
이전 버전이 없으면 '실행 중인 버전 없음'으로 나온다.
"""

import uuid
from typing import Any

from app.enums import DeployLogCode, DeployRequestErrorCode
from app.executors.fake import FakeDeployExecutor
from app.services.deploy_log_messages import DEPLOY_LOG_ENTRIES, ERROR_MESSAGES
from tests.integration.deploy_harness import DeployHarness
from tests.integration.rollback_support import (
    SUFFIX,
    deploy_and_finish,
    log_codes,
    read_all_logs,
)

DB_PASSWORD = "s3cr3t-pa55word"
DB_URL = f"postgresql+asyncpg://shop:{DB_PASSWORD}@db.internal:5432/shop"
NO_PREVIOUS = DEPLOY_LOG_ENTRIES[DeployLogCode.REVERT_SUCCEEDED_NO_PREVIOUS].message
KEPT = DEPLOY_LOG_ENTRIES[DeployLogCode.REVERT_SUCCEEDED].message
DEPLOY_FAILED = ERROR_MESSAGES[DeployRequestErrorCode.DEPLOY_FAILED]
FAILURE_KEYS = {
    "code",
    "message",
    "container",
    "containerReason",
    "terminationReason",
    "exitCode",
    "restartCount",
    "logTail",
    "logTruncated",
    "previousVersionRunning",
}


async def _service(harness: DeployHarness) -> tuple[str, uuid.UUID]:
    harness.host_suffixes.queue(SUFFIX)
    service = await harness.create_service("shap-api")
    aws = await harness.add_environment("aws-tokyo")
    return service["id"], aws.id


async def _connection_id(harness: DeployHarness, service_id: str) -> uuid.UUID:
    service = await harness.get_service(service_id)
    return uuid.UUID(service["environments"][0]["serviceEnvironmentId"])


def _failing_deployer(harness: DeployHarness, log_tail: list[str]) -> FakeDeployExecutor:
    deployer = FakeDeployExecutor(harness.clock, 0, crash_log_tail=log_tail)
    harness.replace_executors(deploy=deployer)
    return deployer


async def _put_variable(harness: DeployHarness, service_id: str, environment_id: object) -> None:
    response = await harness.client.put(
        f"{harness.services}/{service_id}/environments/{environment_id}/variables/DATABASE_URL",
        json={"value": DB_URL},
    )
    assert response.status_code in (200, 201), response.text


async def test_first_deploy_crash_records_tail_exit_code_and_no_running_version(
    harness: DeployHarness,
) -> None:
    service_id, aws = await _service(harness)

    failed = await deploy_and_finish(harness, service_id, aws, simulate={"failAt": "DEPLOYING"})
    items = await read_all_logs(harness, failed["id"], "DEPLOY_JOB")
    failure: dict[str, Any] = failed["failure"]

    assert (failed["status"], failed["errorCode"], failed["revertStatus"]) == (
        "FAILED",
        "DEPLOY_FAILED",
        "SUCCEEDED",
    )
    # 기존 칸은 그대로(고정 문장).
    assert failed["errorMessage"] == DEPLOY_FAILED
    assert set(failure) == FAILURE_KEYS
    assert failure["code"] == "DEPLOY_FAILED"
    assert failure["message"] == f"{DEPLOY_FAILED} Last error: ValidationError: database_url"
    assert (failure["containerReason"], failure["terminationReason"]) == (
        "CrashLoopBackOff",
        "Error",
    )
    assert (failure["exitCode"], failure["restartCount"]) == (1, 3)
    assert "database_url" in failure["logTail"]
    assert failure["logTruncated"] is False
    assert failure["previousVersionRunning"] is False

    codes = [item["code"] for item in items]
    assert codes[-3 - len(failure["logTail"]) :] == [
        "CONTAINER_DIAGNOSTICS",
        *["CONTAINER_LOG"] * len(failure["logTail"]),
        "FAILED",
        "REVERT_SUCCEEDED_NO_PREVIOUS",
    ]
    assert [item["message"] for item in items if item["code"] == "CONTAINER_LOG"] == failure[
        "logTail"
    ]
    assert items[-1]["message"] == NO_PREVIOUS
    assert items[-2]["message"] == failure["message"]
    assert items[-2]["level"] == "ERROR"
    assert all(set(item) == {"at", "level", "message", "code"} for item in items)


async def test_failure_with_previous_version_says_previous_kept_and_masks_env_values(
    harness: DeployHarness,
) -> None:
    service_id, aws = await _service(harness)
    first = await deploy_and_finish(harness, service_id, aws)
    await _put_variable(harness, service_id, aws)
    deployer = _failing_deployer(
        harness,
        [
            "Starting shap-api",
            f"connecting to {DB_URL}",
            f"retrying with password={DB_PASSWORD}",
            "sqlalchemy.exc.OperationalError: connection refused",
        ],
    )
    deployer.fail_service_environment(await _connection_id(harness, service_id))

    failed = await deploy_and_finish(harness, service_id, aws)
    items = await read_all_logs(harness, failed["id"], "DEPLOY_JOB")
    failure = failed["failure"]

    assert first["status"] == "SUCCEEDED"
    assert failed["status"] == "FAILED"
    assert failure["previousVersionRunning"] is True
    assert items[-1]["code"] == "REVERT_SUCCEEDED"
    assert items[-1]["message"] == KEPT
    assert failure["message"].endswith("Last error: OperationalError: connection refused")
    rendered = repr(failed) + repr(items)
    assert DB_PASSWORD not in rendered
    assert DB_URL not in rendered
    assert failure["logTail"][1] == "connecting to ***"
    assert failure["logTail"][2] == "retrying with password=***"
    assert harness.responses and all(DB_PASSWORD not in text for text in harness.responses)


async def test_tail_longer_than_50_lines_is_cut_and_marked_truncated(
    harness: DeployHarness,
) -> None:
    service_id, aws = await _service(harness)
    await deploy_and_finish(harness, service_id, aws)
    deployer = _failing_deployer(harness, [f"line {index}" for index in range(120)])
    deployer.fail_service_environment(await _connection_id(harness, service_id))

    failed = await deploy_and_finish(harness, service_id, aws)
    failure = failed["failure"]

    assert len(failure["logTail"]) == 50
    assert failure["logTail"][0] == "line 70"
    assert failure["logTruncated"] is True
    assert (await log_codes(harness, failed["id"])).count("CONTAINER_LOG") == 50


async def test_failure_without_container_diagnostics_keeps_old_shape(
    harness: DeployHarness,
) -> None:
    service_id, aws = await _service(harness)

    failed = await deploy_and_finish(harness, service_id, aws, simulate={"failAt": "VERIFYING"})
    succeeded = await deploy_and_finish(harness, service_id, aws)
    failure = failed["failure"]

    assert failure["code"] == "HEALTH_CHECK_FAILED"
    assert failure["message"] == failed["errorMessage"]
    assert (failure["container"], failure["exitCode"], failure["logTail"]) == (None, None, [])
    assert failure["previousVersionRunning"] is False
    assert succeeded["failure"] is None
    assert "CONTAINER_DIAGNOSTICS" not in await log_codes(harness, failed["id"])


async def test_build_failure_has_failure_without_revert(harness: DeployHarness) -> None:
    service_id, aws = await _service(harness)

    failed = await deploy_and_finish(harness, service_id, aws, simulate={"failAt": "BUILDING"})

    assert failed["failure"]["code"] == "BUILD_FAILED"
    assert failed["failure"]["previousVersionRunning"] is None
    assert failed["revertStatus"] is None
