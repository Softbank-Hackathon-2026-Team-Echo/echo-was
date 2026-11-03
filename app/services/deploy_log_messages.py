"""배포 작업 로그와 실패 응답의 고정 문장.

외부 응답 원문(비밀값이 섞일 수 있음)은 넣지 않는다. 줄마다 코드·수준·단계·문장이 정해져 있고,
FAILED 줄만 문장이 그 실패 코드의 문장이다.
"""

from dataclasses import dataclass

from app.enums import DeployLogCode, DeployLogStage, DeployRequestErrorCode, LogLevel

# 오류 코드별 고정 문장(배포 요청 응답 errorMessage·FAILED 로그 줄).
ERROR_MESSAGES: dict[DeployRequestErrorCode, str] = {
    DeployRequestErrorCode.REPO_NOT_ACCESSIBLE: (
        "The repository was not found or the GitHub App is not installed on it."
    ),
    DeployRequestErrorCode.REF_NOT_FOUND: "The branch or commit was not found in the repository.",
    DeployRequestErrorCode.SOURCE_PROVIDER_UNAVAILABLE: (
        "The source provider is unavailable (rate limit, server error or timeout)."
    ),
    DeployRequestErrorCode.BUILD_FAILED: "The image build failed.",
    DeployRequestErrorCode.BUILD_TIMEOUT: "The image build did not finish in time.",
    DeployRequestErrorCode.DEPLOY_FAILED: "The deployment to the environment failed.",
    DeployRequestErrorCode.DEPLOY_TIMEOUT: "The deployment did not finish in time.",
    DeployRequestErrorCode.HEALTH_CHECK_FAILED: "The health check did not return a 2xx response.",
    DeployRequestErrorCode.HEALTH_CHECK_TIMEOUT: "The health check could not be completed in time.",
    DeployRequestErrorCode.PUBLIC_URL_UNREACHABLE: "The environment public URL was unreachable.",
    DeployRequestErrorCode.WORKER_LOST: "The deploy worker stopped repeatedly during this request.",
    DeployRequestErrorCode.INTERNAL_ERROR: "An internal error occurred.",
    DeployRequestErrorCode.BUILD_DISPATCH_FAILED: (
        "The build workflow could not be started (missing permission or workflow)."
    ),
    DeployRequestErrorCode.CLUSTER_UNAUTHORIZED: (
        "The deploy executor was not authorized by the environment cluster."
    ),
    DeployRequestErrorCode.IMAGE_PULL_FAILED: "The environment could not pull the built image.",
    DeployRequestErrorCode.CONTAINER_START_FAILED: (
        "The container kept crashing after it started."
    ),
}


@dataclass(frozen=True)
class DeployLogEntry:
    level: LogLevel
    stage: DeployLogStage
    message: str


_INFO, _WARN, _ERROR = LogLevel.INFO, LogLevel.WARN, LogLevel.ERROR
_S = DeployLogStage

DEPLOY_LOG_ENTRIES: dict[DeployLogCode, DeployLogEntry] = {
    DeployLogCode.REQUEST_ACCEPTED: DeployLogEntry(_INFO, _S.QUEUED, "Deploy request accepted."),
    DeployLogCode.STARTED: DeployLogEntry(_INFO, _S.BUILDING, "Deploy request started."),
    DeployLogCode.BUILD_ATTACHED: DeployLogEntry(
        _INFO, _S.BUILDING, "A new image build was created for this commit."
    ),
    DeployLogCode.BUILD_REUSED: DeployLogEntry(
        _INFO, _S.BUILDING, "An existing image build for this commit is reused."
    ),
    DeployLogCode.DEPLOYMENT_CREATED: DeployLogEntry(
        _INFO, _S.DEPLOYING, "A deployment record was created from the built image."
    ),
    DeployLogCode.DEPLOY_REQUESTED: DeployLogEntry(
        _INFO, _S.DEPLOYING, "Deployment to the environment was requested."
    ),
    DeployLogCode.DEPLOY_APPLIED: DeployLogEntry(
        _INFO, _S.DEPLOYING, "The deployment was applied to the environment."
    ),
    DeployLogCode.HEALTH_PROBE_OK: DeployLogEntry(
        _INFO, _S.VERIFYING, "The health check returned a 2xx response."
    ),
    DeployLogCode.HEALTH_PROBE_HTTP_ERROR: DeployLogEntry(
        _WARN, _S.VERIFYING, "The health check did not return a 2xx response. Retrying."
    ),
    DeployLogCode.HEALTH_PROBE_UNREACHABLE: DeployLogEntry(
        _WARN, _S.VERIFYING, "The environment public URL was unreachable. Retrying."
    ),
    DeployLogCode.SUCCEEDED: DeployLogEntry(
        _INFO, _S.SUCCEEDED, "Deploy request succeeded. The environment now runs this version."
    ),
    DeployLogCode.STANDBY_SYNC_REQUESTED: DeployLogEntry(
        _INFO, _S.SUCCEEDED, "A standby environment was asked to follow this version."
    ),
    # FAILED 의 문장은 실패 코드의 문장(ERROR_MESSAGES)으로 바꿔 쓴다.
    DeployLogCode.FAILED: DeployLogEntry(_ERROR, _S.FAILED, "Deploy request failed."),
    DeployLogCode.REVERT_SUCCEEDED: DeployLogEntry(
        _INFO, _S.REVERT, "The failed version was withdrawn and the previous version was kept."
    ),
    DeployLogCode.REVERT_FAILED: DeployLogEntry(
        _ERROR, _S.REVERT, "Withdrawing the failed version did not succeed."
    ),
    DeployLogCode.CANCELLED_SUPERSEDED: DeployLogEntry(
        _INFO,
        _S.CANCELLED,
        "Cancelled: a newer request for the same environment replaced this one.",
    ),
    DeployLogCode.CANCELLED_SERVICE_DELETED: DeployLogEntry(
        _INFO, _S.CANCELLED, "Cancelled: the service is being deleted."
    ),
    DeployLogCode.CANCELLED_BASE_CHANGED: DeployLogEntry(
        _INFO,
        _S.CANCELLED,
        "Cancelled: the current deployment changed after this rollback was accepted.",
    ),
    # 단계는 이 줄이 대신한 원래 줄의 단계를 쓴다.
    DeployLogCode.LOG_TRUNCATED: DeployLogEntry(
        _INFO,
        _S.QUEUED,
        "Log line limit reached. Further lines for this request are not recorded.",
    ),
}


def deploy_log_message(
    code: DeployLogCode, error_code: DeployRequestErrorCode | None = None
) -> str:
    if code is DeployLogCode.FAILED and error_code is not None:
        return ERROR_MESSAGES[error_code]
    return DEPLOY_LOG_ENTRIES[code].message
