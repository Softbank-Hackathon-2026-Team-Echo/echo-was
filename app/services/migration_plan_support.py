"""이전 계획 API 와 일꾼이 함께 쓰는 규칙: 고정 단계·예상 시간, 단계 결과 갱신, 지문, 대상 값.

steps·step_results·rollback_conditions·simulate·fingerprint 는 JSONB 이고 키는 응답과 같은
camelCase 다. 시각은 ISO 8601(UTC) 문자열로 넣는다.
"""

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import MigrationStepKind, MigrationStepStatus
from app.executors.base import DeployTarget, MainRoute
from app.models import Deployment, Environment, Service, ServiceEnvironment
from app.repositories.deploy_request_repository import DeployRequestRepository
from app.repositories.deployment_repository import DeploymentRepository
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.services.deploy_support import deploy_target

# 고정 단계: (종류, 도착 비율). 비율 목록은 바꾸지 않는다.
STEP_DEFINITIONS: tuple[tuple[MigrationStepKind, int | None], ...] = (
    (MigrationStepKind.PREFLIGHT, None),
    (MigrationStepKind.SHIFT, 10),
    (MigrationStepKind.SHIFT, 50),
    (MigrationStepKind.DATA_SYNC_CHECK, None),
    (MigrationStepKind.SHIFT, 100),
)
LAST_STEP = len(STEP_DEFINITIONS) - 1
DATA_SYNC_STEP = 3

# 롤백 조건 기본값.
DEFAULT_MAX_ERROR_RATE_PERCENT = 1.0
DEFAULT_MAX_P95_MS = 800
DEFAULT_MAX_REPLICATION_LAG_SECONDS = 1.0

# JSON 키.
STEP_INDEX = "index"
STEP_KIND = "kind"
STEP_TARGET_WEIGHT = "targetWeight"
STEP_SOURCE_WEIGHT = "sourceWeight"
STEP_OBSERVE_SECONDS = "observeSeconds"
STEP_EXPECTED_SECONDS = "expectedSeconds"
RESULT_STATUS = "status"
RESULT_STARTED_AT = "startedAt"
RESULT_FINISHED_AT = "finishedAt"
RESULT_METRICS = "metrics"
RESULT_FAILURE_CODE = "failureCode"
CONDITION_MAX_ERROR_RATE = "maxErrorRatePercent"
CONDITION_MAX_P95_MS = "maxP95Ms"
CONDITION_MAX_REPLICATION_LAG = "maxReplicationLagSeconds"
SIMULATE_FAIL_AT_STEP = "failAtStep"
SIMULATE_REASON = "reason"

# 지문 항목 이름(달라지면 응답 details.field 와 로그 data.changed 에 그대로 쓴다).
FINGERPRINT_SERVICE_STATUS = "serviceStatus"
FINGERPRINT_CONNECTIONS = "connections"
FINGERPRINT_SOURCE_ENVIRONMENT = "sourceEnvironment"
FINGERPRINT_TARGET_ENVIRONMENT = "targetEnvironment"
FINGERPRINT_OPEN_DEPLOY_REQUESTS = "openDeployRequests"
FINGERPRINT_KEYS = (
    FINGERPRINT_SERVICE_STATUS,
    FINGERPRINT_CONNECTIONS,
    FINGERPRINT_SOURCE_ENVIRONMENT,
    FINGERPRINT_TARGET_ENVIRONMENT,
    FINGERPRINT_OPEN_DEPLOY_REQUESTS,
)


def build_steps(observe_seconds: int, overhead_seconds: int) -> list[dict[str, Any]]:
    """고정 5단계. 단계 예상 = 여유 + (비율 단계면 지켜보기 시간)."""
    steps: list[dict[str, Any]] = []
    for index, (kind, target_weight) in enumerate(STEP_DEFINITIONS):
        is_shift = kind is MigrationStepKind.SHIFT
        steps.append(
            {
                STEP_INDEX: index,
                STEP_KIND: kind.value,
                STEP_TARGET_WEIGHT: target_weight,
                STEP_SOURCE_WEIGHT: 100 - target_weight if target_weight is not None else None,
                STEP_OBSERVE_SECONDS: observe_seconds if is_shift else None,
                STEP_EXPECTED_SECONDS: overhead_seconds + (observe_seconds if is_shift else 0),
            }
        )
    return steps


def expected_seconds(steps: Sequence[dict[str, Any]]) -> int:
    return sum(int(step[STEP_EXPECTED_SECONDS]) for step in steps)


def initial_step_results(steps: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            STEP_INDEX: step[STEP_INDEX],
            RESULT_STATUS: MigrationStepStatus.PENDING.value,
            RESULT_STARTED_AT: None,
            RESULT_FINISHED_AT: None,
            RESULT_METRICS: None,
            RESULT_FAILURE_CODE: None,
        }
        for step in steps
    ]


def step_kind(steps: Sequence[dict[str, Any]], index: int) -> MigrationStepKind:
    return MigrationStepKind(steps[index][STEP_KIND])


def iso(moment: datetime) -> str:
    return moment.isoformat()


def with_step_result(
    results: Sequence[dict[str, Any]], index: int, **changes: Any
) -> list[dict[str, Any]]:
    """index 번 단계 결과만 바꾼 새 리스트(JSONB 칸은 통째로 다시 넣어야 저장된다)."""
    updated = [dict(item) for item in results]
    updated[index].update(changes)
    return updated


def skip_pending_steps(results: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """아직 끝나지 않은(PENDING·RUNNING) 단계를 모두 SKIPPED 로 바꾼 새 리스트."""
    open_statuses = {MigrationStepStatus.PENDING.value, MigrationStepStatus.RUNNING.value}
    return [
        {**item, RESULT_STATUS: MigrationStepStatus.SKIPPED.value}
        if item[RESULT_STATUS] in open_statuses
        else dict(item)
        for item in results
    ]


def _sha256(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode()).hexdigest()


def _environment_fingerprint(environment: Environment) -> dict[str, Any]:
    """설정·비밀 참조는 값 대신 해시만 넣는다. 접속 확인 시각·updated_at 은 넣지 않는다."""
    return {
        "configSha256": _sha256(json.dumps(environment.config, sort_keys=True)),
        "secretRefSha256": _sha256(environment.secret_ref),
        "connectionStatus": str(environment.connection_status),
        "live": not environment.is_deleted,
    }


async def read_fingerprint(
    session: AsyncSession,
    service: Service,
    connections: Sequence[ServiceEnvironment],
    source_connection_id: uuid.UUID,
    target_connection_id: uuid.UUID,
) -> dict[str, Any]:
    """지금 상태의 지문. connections 는 서비스의 살아 있는 연결(잠근 것)이다."""
    repository = ServiceEnvironmentRepository(session)
    source = await repository.find_by_id(source_connection_id)
    target = await repository.find_by_id(target_connection_id)
    assert source is not None and target is not None
    open_requests = await DeployRequestRepository(session).search_open_by_service(service.id)
    return {
        FINGERPRINT_SERVICE_STATUS: str(service.status),
        FINGERPRINT_CONNECTIONS: [
            [
                str(item.id),
                str(item.current_deployment_id) if item.current_deployment_id else None,
                item.traffic_weight,
            ]
            for item in sorted(connections, key=lambda item: str(item.id))
        ],
        FINGERPRINT_SOURCE_ENVIRONMENT: _environment_fingerprint(source.environment),
        FINGERPRINT_TARGET_ENVIRONMENT: _environment_fingerprint(target.environment),
        FINGERPRINT_OPEN_DEPLOY_REQUESTS: sorted(str(request.id) for request in open_requests),
    }


def fingerprint_changes(saved: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """항목별로 비교해 달라진 항목 이름을 정해진 순서로 돌려준다."""
    return [key for key in FINGERPRINT_KEYS if saved.get(key) != current.get(key)]


@dataclass(frozen=True)
class MigrationTargets:
    """실행기에 넘길 값: 대표 주소, 출발·도착 대상, 두 연결의 현재 배포."""

    route: MainRoute
    source: DeployTarget
    target: DeployTarget
    target_connection: ServiceEnvironment
    source_deployment: Deployment | None
    target_deployment: Deployment | None


async def load_migration_targets(
    session: AsyncSession,
    service: Service,
    source_connection_id: uuid.UUID,
    target_connection_id: uuid.UUID,
) -> MigrationTargets:
    repository = ServiceEnvironmentRepository(session)
    source = await repository.find_by_id(source_connection_id)
    target = await repository.find_by_id(target_connection_id)
    assert source is not None and target is not None
    deployments = DeploymentRepository(session)
    source_deployment = (
        await deployments.find_by_id(source.current_deployment_id)
        if source.current_deployment_id
        else None
    )
    target_deployment = (
        await deployments.find_by_id(target.current_deployment_id)
        if target.current_deployment_id
        else None
    )
    return MigrationTargets(
        route=MainRoute(
            service_id=service.id, service_name=service.name, public_host=service.public_host
        ),
        source=deploy_target(service, source, source.environment),
        target=deploy_target(service, target, target.environment),
        target_connection=target,
        source_deployment=source_deployment,
        target_deployment=target_deployment,
    )
