"""배포 접수(API)와 일꾼이 함께 쓰는 규칙.

퍼블릭 주소, 설정 사본, 대기 요청 밀어내기, 실행기에 넘길 대상 값.
"""

import secrets
import string
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import (
    EnvironmentConfig,
    InvalidEnvironmentConfigError,
    UnknownEnvironmentKindError,
)
from app.audit.actions import AuditComponent
from app.audit.lifecycle import record_deploy_request_finished
from app.core.clock import Clock
from app.enums import (
    DeployLogCode,
    DeployRequestCancelReason,
    DeployRequestStatus,
    JobKind,
    JobStatus,
)
from app.executors.base import DeploymentSpec, DeployTarget
from app.models import Deployment, DeployRequest, Environment, Job, Service, ServiceEnvironment
from app.models.service import HOST_SUFFIX_LENGTH
from app.repositories.deploy_request_repository import DeployRequestRepository
from app.repositories.job_repository import JobRepository
from app.services.deploy_job_log import DeployJobLog

# config_snapshot 키(요청·배포 기록 공통). 비밀값·토큰은 넣지 않는다.
SNAPSHOT_HEALTH_CHECK_PATH = "healthCheckPath"
SNAPSHOT_HEALTH_CHECK_TIMEOUT_SECONDS = "healthCheckTimeoutSeconds"
SNAPSHOT_CONTAINER_PORT = "containerPort"
SNAPSHOT_MAIN_URL = "mainUrl"
SNAPSHOT_ENVIRONMENT_URL = "environmentUrl"
SIMULATE_FAIL_AT = "failAt"


HOST_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits
# DNS 레이블 한 칸의 최대 길이. 넘으면 suffix 앞부분을 잘라 `-{suffix}` 자리를 남긴다.
PUBLIC_HOST_LABEL_MAX_LENGTH = 63
PUBLIC_HOST_LABEL_PREFIX_MAX_LENGTH = PUBLIC_HOST_LABEL_MAX_LENGTH - HOST_SUFFIX_LENGTH - 1

HostSuffixGenerator = Callable[[], str]


def generate_host_suffix() -> str:
    """소문자·숫자 8자 무작위 값. 지운 서비스의 주소를 같은 이름의 새 서비스가 잇지 못하게 한다."""
    return "".join(secrets.choice(HOST_SUFFIX_ALPHABET) for _ in range(HOST_SUFFIX_LENGTH))


def public_host_label(prefix: str, host_suffix: str) -> str:
    """`{prefix}-{suffix}`. 63자를 넘으면 prefix 를 54자로 자르고 끝의 `-` 를 뗀다."""
    label = f"{prefix}-{host_suffix}"
    if len(label) <= PUBLIC_HOST_LABEL_MAX_LENGTH:
        return label
    truncated = prefix[:PUBLIC_HOST_LABEL_PREFIX_MAX_LENGTH].rstrip("-")
    return f"{truncated}-{host_suffix}"


def service_public_host(service_name: str, host_suffix: str, public_base_domain: str) -> str:
    """대표 주소 `{서비스}-{suffix}.{도메인}`. 도메인 바로 아래 한 단계라 `*.도메인` 이 덮는다."""
    return f"{public_host_label(service_name, host_suffix)}.{public_base_domain}"


def connection_public_host(
    service_name: str,
    environment_name: str,
    host_suffix: str,
    public_base_domain: str,
    environment_host_domain: str | None = None,
) -> str:
    """환경별 주소. 서비스의 suffix 를 그대로 쓴다.

    - 환경에 하위 도메인(config ingressDomain)이 있으면 `{서비스}-{suffix}.{하위 도메인}`
      (클라우드: 그 환경의 와일드카드 DNS·인증서가 덮는다).
    - 없으면 `{서비스}-{환경}-{suffix}.{도메인}`(온프레·예전 환경).
    """
    if environment_host_domain is not None:
        return f"{public_host_label(service_name, host_suffix)}.{environment_host_domain}"
    label = public_host_label(f"{service_name}-{environment_name}", host_suffix)
    return f"{label}.{public_base_domain}"


def public_url(public_host: str) -> str:
    return f"https://{public_host}"


def build_config_snapshot(
    service: Service, service_environment: ServiceEnvironment
) -> dict[str, Any]:
    """접수 순간의 서비스 설정 사본. 뒤에 서비스가 바뀌어도 이 요청은 이 값을 쓴다."""
    return {
        SNAPSHOT_HEALTH_CHECK_PATH: service.health_check_path,
        SNAPSHOT_HEALTH_CHECK_TIMEOUT_SECONDS: service.health_check_timeout_seconds,
        SNAPSHOT_CONTAINER_PORT: service.container_port,
        SNAPSHOT_MAIN_URL: public_url(service.public_host),
        SNAPSHOT_ENVIRONMENT_URL: public_url(service_environment.public_host),
    }


async def enqueue_deploy_request(
    session: AsyncSession,
    deploy_requests: DeployRequestRepository,
    jobs: JobRepository,
    job_log: DeployJobLog,
    request: DeployRequest,
    now: datetime,
    clock: Clock,
    component: AuditComponent,
) -> uuid.UUID | None:
    """새 요청과 할 일을 저장한다. 같은 연결의 대기 요청이 있으면 SUPERSEDED 로 취소한다.

    호출하는 쪽이 서비스 행 → 연결 행을 잠근 트랜잭션 안에서 부른다. 밀려난 요청의 id 를 돌려준다.
    배포 작업 로그: 밀려난 요청에 CANCELLED_SUPERSEDED, 새 요청에 REQUEST_ACCEPTED. 밀려난 요청의
    끝 감사 줄은 component(접수 API 또는 예비 따라가기를 만든 일꾼) 이름으로 쓴다.
    대기 요청은 연결마다 하나뿐이므로(부분 unique), 먼저 취소하고 새 행을 넣은 뒤
    superseded_by_id 를 적는다. 취소한 요청의 할 일이 QUEUED 면 닫고, RUNNING 이면 그 일꾼이
    끝 상태를 보고 닫는다.
    """
    superseded_id: uuid.UUID | None = None
    queued = await deploy_requests.find_queued_for_connection(request.service_environment_id)
    if queued is not None and await deploy_requests.transition(
        queued.id,
        DeployRequestStatus.QUEUED,
        {
            "status": DeployRequestStatus.CANCELLED,
            "cancel_reason": DeployRequestCancelReason.SUPERSEDED,
            "finished_at": now,
            "updated_at": now,
        },
    ):
        superseded_id = queued.id
        await job_log.append(queued, DeployLogCode.CANCELLED_SUPERSEDED, now)
        await record_deploy_request_finished(
            session,
            clock,
            queued,
            status=DeployRequestStatus.CANCELLED,
            component=component,
            cancel_reason=DeployRequestCancelReason.SUPERSEDED,
        )
    deploy_requests.add(request)
    await session.flush()
    await job_log.append(request, DeployLogCode.REQUEST_ACCEPTED, now)
    if superseded_id is not None:
        await deploy_requests.update_if(
            superseded_id, [], {"superseded_by_id": request.id, "updated_at": now}
        )
        await jobs.cancel_queued_for_subject(superseded_id, now)
    jobs.add(
        Job(
            kind=JobKind.DEPLOY_REQUEST,
            subject_id=request.id,
            status=JobStatus.QUEUED,
            run_after=now,
            created_at=now,
            updated_at=now,
        )
    )
    await session.flush()
    return superseded_id


def deploy_target(
    service: Service, service_environment: ServiceEnvironment, environment: Environment
) -> DeployTarget:
    """실행기에 넘길 대상.

    환경 config 를 지금 양식으로 읽지 못하면 None 이다(실제 실행기가 설정 오류로 본다).
    """
    return DeployTarget(
        service_id=service.id,
        service_name=service.name,
        service_environment_id=service_environment.id,
        environment_id=environment.id,
        environment_kind=environment.kind,
        public_host=service_environment.public_host,
        container_port=service.container_port,
        environment_name=environment.name,
        environment_config=read_environment_config(environment),
        environment_secret_ref=environment.secret_ref,
        main_public_host=service.public_host,
    )


def read_environment_config(environment: Environment) -> EnvironmentConfig | None:
    try:
        return environment.parsed_config()
    except (InvalidEnvironmentConfigError, UnknownEnvironmentKindError):
        return None


def deployment_spec(deployment: Deployment) -> DeploymentSpec:
    return DeploymentSpec(
        deployment_id=deployment.id,
        image_repository=deployment.image_repository,
        image_digest=deployment.image_digest,
        config_snapshot=dict(deployment.config_snapshot),
    )
