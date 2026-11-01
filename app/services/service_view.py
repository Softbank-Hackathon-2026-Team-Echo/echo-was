"""서비스 상세 화면 값 계산: 연결마다 역할·버전 상태·마지막 따라가기 실패·대기/실행 중 요청.

역할은 저장하지 않고 traffic_weight 와 현재 배포로 계산한다.
- SERVING: traffic_weight > 0. STANDBY: 0 이고 현재 배포 있음. NONE: 현재 배포 없음.
- versionStatus(STANDBY 만): 기준 연결(비율이 가장 큰 연결, 같으면 traffic_applied_at 이 늦은 쪽,
  그다음 id)의 현재 배포 digest 와 같으면 IN_SYNC, 다르면 OUT_OF_SYNC. 서비스 중 연결이 없으면 null.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import (
    DeployRequestErrorCode,
    DeployRequestStatus,
    ServiceEnvironmentRole,
    VersionStatus,
)
from app.models import Deployment, DeployRequest, Environment, Service, ServiceEnvironment
from app.repositories.deploy_request_repository import DeployRequestRepository
from app.repositories.deployment_repository import DeploymentRepository
from app.repositories.service_environment_repository import ServiceEnvironmentRepository

_MIN_DATETIME = datetime.min.replace(tzinfo=None)


@dataclass(frozen=True)
class CurrentDeploymentView:
    deployment: Deployment
    commit_sha: str | None
    simulated: bool | None


@dataclass(frozen=True)
class ServiceEnvironmentView:
    service_environment: ServiceEnvironment
    environment: Environment
    role: ServiceEnvironmentRole
    current_deployment: CurrentDeploymentView | None
    version_status: VersionStatus | None
    last_sync_error: DeployRequestErrorCode | None
    active_request_id: uuid.UUID | None
    queued_request_id: uuid.UUID | None


@dataclass(frozen=True)
class ServiceDetailView:
    service: Service
    environments: list[ServiceEnvironmentView]


def role_of(service_environment: ServiceEnvironment) -> ServiceEnvironmentRole:
    if service_environment.traffic_weight > 0:
        return ServiceEnvironmentRole.SERVING
    if service_environment.current_deployment_id is not None:
        return ServiceEnvironmentRole.STANDBY
    return ServiceEnvironmentRole.NONE


def reference_connection(
    service_environments: list[ServiceEnvironment],
) -> ServiceEnvironment | None:
    """예비 연결이 맞춰야 할 서비스 중 연결. 서비스 중 연결이 없으면 None."""
    serving = [item for item in service_environments if item.traffic_weight > 0]
    if not serving:
        return None
    return max(
        serving,
        key=lambda item: (
            item.traffic_weight,
            item.traffic_applied_at.replace(tzinfo=None)
            if item.traffic_applied_at
            else _MIN_DATETIME,
            str(item.id),
        ),
    )


async def load_service_detail(session: AsyncSession, service: Service) -> ServiceDetailView:
    """서비스의 살아 있는 연결로 상세 화면 값을 만든다. 호출하는 쪽이 트랜잭션을 연다."""
    if service.is_deleted:
        return ServiceDetailView(service=service, environments=[])
    connections = await ServiceEnvironmentRepository(session).search_live_by_service(service.id)
    connection_ids = [item.id for item in connections]
    deploy_requests = DeployRequestRepository(session)
    current_ids = [item.current_deployment_id for item in connections if item.current_deployment_id]
    deployments_by_id = {
        deployment.id: deployment
        for deployment in await DeploymentRepository(session).search_by_ids(current_ids)
    }
    requests_by_deployment_id: dict[uuid.UUID, DeployRequest] = {
        request.deployment_id: request
        for request in await deploy_requests.search_by_deployment_ids(current_ids)
        if request.deployment_id is not None
    }
    open_requests = await deploy_requests.search_open_by_connections(connection_ids)
    latest_sync_by_connection = {
        request.service_environment_id: request
        for request in await deploy_requests.search_latest_standby_sync_by_connections(
            connection_ids
        )
    }

    reference = reference_connection(connections)
    reference_digest = (
        deployments_by_id[reference.current_deployment_id].image_digest
        if reference is not None and reference.current_deployment_id in deployments_by_id
        else None
    )

    views: list[ServiceEnvironmentView] = []
    for connection in connections:
        role = role_of(connection)
        deployment = (
            deployments_by_id.get(connection.current_deployment_id)
            if connection.current_deployment_id
            else None
        )
        current = None
        if deployment is not None:
            source = requests_by_deployment_id.get(deployment.id)
            current = CurrentDeploymentView(
                deployment=deployment,
                commit_sha=source.commit_sha if source else None,
                simulated=source.simulated if source else None,
            )
        version_status = None
        if role is ServiceEnvironmentRole.STANDBY and reference_digest is not None:
            version_status = (
                VersionStatus.IN_SYNC
                if deployment is not None and deployment.image_digest == reference_digest
                else VersionStatus.OUT_OF_SYNC
            )
        latest_sync = latest_sync_by_connection.get(connection.id)
        last_sync_error = (
            DeployRequestErrorCode(latest_sync.error_code)
            if latest_sync is not None
            and latest_sync.status == DeployRequestStatus.FAILED
            and latest_sync.error_code is not None
            else None
        )
        connection_requests = [
            request for request in open_requests if request.service_environment_id == connection.id
        ]
        views.append(
            ServiceEnvironmentView(
                service_environment=connection,
                environment=connection.environment,
                role=role,
                current_deployment=current,
                version_status=version_status,
                last_sync_error=last_sync_error,
                active_request_id=next(
                    (
                        request.id
                        for request in connection_requests
                        if DeployRequestStatus(request.status).is_active
                    ),
                    None,
                ),
                queued_request_id=next(
                    (
                        request.id
                        for request in connection_requests
                        if request.status == DeployRequestStatus.QUEUED
                    ),
                    None,
                ),
            )
        )
    return ServiceDetailView(service=service, environments=views)
