"""배포 접수(API)와 일꾼이 함께 쓰는 규칙.

퍼블릭 주소, 설정 사본, 대기 요청 밀어내기, 실행기에 넘길 대상 값.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import (
    DeployRequestCancelReason,
    DeployRequestStatus,
    JobKind,
    JobStatus,
)
from app.executors.base import DeploymentSpec, DeployTarget
from app.models import Deployment, DeployRequest, Environment, Job, Service, ServiceEnvironment
from app.repositories.deploy_request_repository import DeployRequestRepository
from app.repositories.job_repository import JobRepository

# config_snapshot 키(요청·배포 기록 공통). 비밀값·토큰은 넣지 않는다.
SNAPSHOT_HEALTH_CHECK_PATH = "healthCheckPath"
SNAPSHOT_HEALTH_CHECK_TIMEOUT_SECONDS = "healthCheckTimeoutSeconds"
SNAPSHOT_CONTAINER_PORT = "containerPort"
SNAPSHOT_MAIN_URL = "mainUrl"
SNAPSHOT_ENVIRONMENT_URL = "environmentUrl"
SIMULATE_FAIL_AT = "failAt"


def service_public_host(service_name: str, public_base_domain: str) -> str:
    return f"{service_name}.{public_base_domain}"


def connection_public_host(
    service_name: str, environment_name: str, public_base_domain: str
) -> str:
    return f"{service_name}.{environment_name}.{public_base_domain}"


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
    request: DeployRequest,
    now: datetime,
) -> uuid.UUID | None:
    """새 요청과 할 일을 저장한다. 같은 연결의 대기 요청이 있으면 SUPERSEDED 로 취소한다.

    호출하는 쪽이 서비스 행 → 연결 행을 잠근 트랜잭션 안에서 부른다. 밀려난 요청의 id 를 돌려준다.
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
    deploy_requests.add(request)
    await session.flush()
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
    return DeployTarget(
        service_id=service.id,
        service_name=service.name,
        service_environment_id=service_environment.id,
        environment_id=environment.id,
        environment_kind=environment.kind,
        public_host=service_environment.public_host,
        container_port=service.container_port,
    )


def deployment_spec(deployment: Deployment) -> DeploymentSpec:
    return DeploymentSpec(
        deployment_id=deployment.id,
        image_repository=deployment.image_repository,
        image_digest=deployment.image_digest,
        config_snapshot=dict(deployment.config_snapshot),
    )
