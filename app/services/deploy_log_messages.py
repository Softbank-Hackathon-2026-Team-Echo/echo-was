"""배포 작업 로그와 실패 응답의 고정 문장.

외부 응답 원문(비밀값이 섞일 수 있음)은 넣지 않는다. 줄마다 코드·수준·단계·문장이 정해져 있고,
FAILED 줄만 문장이 그 실패 코드의 문장이다.

예외(API 서버 4-1): 컨테이너 실패 진단 줄(CONTAINER_DIAGNOSTICS·CONTAINER_LOG)과 FAILED 줄 뒤의
원인 한 줄은 컨테이너 로그에서 온다. 처리기가 비밀값을 가린 뒤에만 쓴다(container_diagnostics.py).
"""

from collections.abc import Sequence
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
    DeployRequestErrorCode.ENV_SECRET_UNAVAILABLE: (
        "An app environment variable could not be read from the secret store."
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
    # 문장은 적용한 키 이름으로 만든다(env_vars_applied_message). 값은 넣지 않는다.
    DeployLogCode.ENV_VARS_APPLIED: DeployLogEntry(
        _INFO, _S.DEPLOYING, "App environment variables were applied."
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
    # 문장은 처리기가 진단 값으로 만든다(container_diagnostics.diagnostics_message·로그 한 줄).
    DeployLogCode.CONTAINER_DIAGNOSTICS: DeployLogEntry(
        _ERROR, _S.FAILED, "Container status was read before withdrawing the failed version."
    ),
    DeployLogCode.CONTAINER_LOG: DeployLogEntry(_INFO, _S.FAILED, "Container log line."),
    # FAILED 의 문장은 실패 코드의 문장(ERROR_MESSAGES)으로 바꿔 쓴다.
    DeployLogCode.FAILED: DeployLogEntry(_ERROR, _S.FAILED, "Deploy request failed."),
    DeployLogCode.REVERT_SUCCEEDED: DeployLogEntry(
        _INFO, _S.REVERT, "The failed version was withdrawn and the previous version was kept."
    ),
    DeployLogCode.REVERT_SUCCEEDED_NO_PREVIOUS: DeployLogEntry(
        _INFO,
        _S.REVERT,
        "The failed version was withdrawn. No version is running in this environment "
        "because there was no previous deployment.",
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


def env_vars_applied_message(keys: Sequence[str], max_length: int) -> str:
    """앱 환경 변수 N개 적용 줄. 키 이름은 max_length 안에 들어가는 만큼만 적는다."""
    head = f"App environment variables applied: {len(keys)} (keys: "
    shown: list[str] = []
    for index, key in enumerate(keys):
        hidden = len(keys) - index - 1
        tail = f", and {hidden} more)." if hidden else ")."
        if len(head) + len(", ".join([*shown, key])) + len(tail) > max_length:
            break
        shown.append(key)
    hidden = len(keys) - len(shown)
    return f"{head}{', '.join(shown)}{f', and {hidden} more' if hidden else ''})."


def deploy_log_message(
    code: DeployLogCode, error_code: DeployRequestErrorCode | None = None
) -> str:
    if code is DeployLogCode.FAILED and error_code is not None:
        return ERROR_MESSAGES[error_code]
    return DEPLOY_LOG_ENTRIES[code].message
