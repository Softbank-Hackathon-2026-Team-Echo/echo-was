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

from app.core.exceptions import SourceNotSingleError
from app.enums import MigrationStepKind, MigrationStepStatus
from app.executors.base import DeployTarget, MainRoute
from app.models import Deployment, Environment, Service, ServiceEnvironment
from app.repositories.deploy_request_repository import DeployRequestRepository
from app.repositories.deployment_repository import DeploymentRepository
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.services.deploy_support import deploy_target
from app.services.migration_counting import expected_shift_seconds

# 새 계획의 단계: 저장된 계획을 읽을 때는 그 계획의 steps 를 쓴다.
STEP_DEFINITIONS: tuple[tuple[MigrationStepKind, int | None], ...] = (
    (MigrationStepKind.PREFLIGHT, None),
    (MigrationStepKind.SHIFT, 5),
    (MigrationStepKind.SHIFT, 10),
    (MigrationStepKind.SHIFT, 25),
    (MigrationStepKind.SHIFT, 50),
    (MigrationStepKind.DATA_SYNC_CHECK, None),
    (MigrationStepKind.SHIFT, 75),
    (MigrationStepKind.SHIFT, 100),
)
DATA_SYNC_STEP = 5
# includeCutover 계획만 비율 100% 뒤에 DB 쓰기 전환을 더한다.
DB_CUTOVER_STEP = len(STEP_DEFINITIONS)
# Job 시작·기다림의 예상 초(쓰기 정지 실측 17초 안팎 + Job 파드 시작). 표시용이다.
DB_CUTOVER_EXPECTED_SECONDS = 30
# 새 계획의 비율 단계 번호. 필요 건수 목록(requiredRequests)의 순서와 같다.
SHIFT_STEPS = tuple(
    index for index, (kind, _) in enumerate(STEP_DEFINITIONS) if kind is MigrationStepKind.SHIFT
)

# 롤백 조건 기본값.
DEFAULT_MAX_ERROR_RATE_PERCENT = 1.0
DEFAULT_MAX_P95_MS = 800
DEFAULT_MAX_REPLICATION_LAG_SECONDS = 1.0

# JSON 키.
STEP_INDEX = "index"
STEP_KIND = "kind"
STEP_TARGET_WEIGHT = "targetWeight"
STEP_SOURCE_WEIGHT = "sourceWeight"
# observeSeconds 는 롤링 배포 중 옛 파드가 읽을 수 있게 maxWaitSeconds 와 같은 값으로 계속 적는다
# (응답에는 내보내지 않는다).
STEP_OBSERVE_SECONDS = "observeSeconds"
STEP_REQUIRED_REQUESTS = "requiredRequests"
STEP_MIN_OBSERVE_SECONDS = "minObserveSeconds"
STEP_MAX_WAIT_SECONDS = "maxWaitSeconds"
STEP_EXPECTED_SECONDS = "expectedSeconds"
RESULT_STATUS = "status"
RESULT_STARTED_AT = "startedAt"
RESULT_FINISHED_AT = "finishedAt"
RESULT_METRICS = "metrics"
RESULT_FAILURE_CODE = "failureCode"
# 비율 단계의 건수 세기 상태(migration_counting.StepCounting)와 세기를 끝낸 이유.
RESULT_COUNTING = "counting"
RESULT_JUDGE_REASON = "judgeReason"
# DB 쓰기 전환 단계 결과(API 서버 17): Job·종료 코드·결과 행 숫자(아래 CUTOVER_* 키).
RESULT_CUTOVER = "cutover"
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


def build_steps(
    required_requests: Sequence[int],
    max_wait_seconds: int,
    requests_per_second: int,
    overhead_seconds: int,
    *,
    min_observe_seconds: int = 4,
    db_cutover_timeout_seconds: int | None = None,
) -> list[dict[str, Any]]:
    """새 계획의 8단계와 선택 DB 전환. 설정값을 각 비율 단계에 고정한다."""
    required_by_step = dict(zip(SHIFT_STEPS, required_requests, strict=True))
    steps: list[dict[str, Any]] = []
    for index, (kind, target_weight) in enumerate(STEP_DEFINITIONS):
        required = required_by_step.get(index)
        shift_seconds = (
            max(
                min_observe_seconds,
                expected_shift_seconds(
                    required, max_wait_seconds, requests_per_second, target_weight
                ),
            )
            if required is not None and target_weight is not None
            else 0
        )
        is_shift = required is not None
        steps.append(
            {
                STEP_INDEX: index,
                STEP_KIND: kind.value,
                STEP_TARGET_WEIGHT: target_weight,
                STEP_SOURCE_WEIGHT: 100 - target_weight if target_weight is not None else None,
                STEP_REQUIRED_REQUESTS: required,
                STEP_MIN_OBSERVE_SECONDS: min_observe_seconds if is_shift else None,
                STEP_MAX_WAIT_SECONDS: max_wait_seconds if is_shift else None,
                STEP_OBSERVE_SECONDS: max_wait_seconds if is_shift else None,
                STEP_EXPECTED_SECONDS: overhead_seconds + shift_seconds,
            }
        )
    if db_cutover_timeout_seconds is not None:
        steps.append(
            {
                STEP_INDEX: DB_CUTOVER_STEP,
                STEP_KIND: MigrationStepKind.DB_CUTOVER.value,
                STEP_TARGET_WEIGHT: None,
                STEP_SOURCE_WEIGHT: None,
                STEP_REQUIRED_REQUESTS: None,
                STEP_MIN_OBSERVE_SECONDS: None,
                STEP_MAX_WAIT_SECONDS: db_cutover_timeout_seconds,
                STEP_OBSERVE_SECONDS: None,
                STEP_EXPECTED_SECONDS: overhead_seconds + DB_CUTOVER_EXPECTED_SECONDS,
            }
        )
    return steps


def last_step(steps: Sequence[dict[str, Any]]) -> int:
    """저장된 단계 목록의 마지막 번호."""
    return len(steps) - 1


def has_db_cutover(steps: Sequence[dict[str, Any]]) -> bool:
    return any(step[STEP_KIND] == MigrationStepKind.DB_CUTOVER.value for step in steps)


PROMOTE_WARNING = (
    "Emergency migration with a database cutover: if the source database cannot be reached, the "
    "cutover job promotes the target database to writer. Promotion cannot be undone "
    "automatically: writes not yet replicated may be lost (see the cutover rpoEstimateMs), the "
    "source may still be running if it is only unreachable, and moving back needs a manual "
    "resync. Approve with acknowledgePromote: true."
)


# DB 쓰기 전환 단계 정의에 만들 때 적는 표시: 재난 긴급 이전으로 만들어 승격(ALLOW_PROMOTE)을
# 허용하는 단계다. 승인 때 동의(acknowledgePromote)를 받고, 실행 때 이 표시가 있어야만
# 승격을 허용한다.
STEP_ALLOW_PROMOTE = "allowPromote"


def stamp_promote(steps: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """DB 쓰기 전환 단계에 승격 허용 표시를 단다(재난 긴급 이전 계획을 만들 때만)."""
    return [
        {**step, STEP_ALLOW_PROMOTE: True}
        if step[STEP_KIND] == MigrationStepKind.DB_CUTOVER.value
        else dict(step)
        for step in steps
    ]


def needs_promote_ack(steps: Sequence[dict[str, Any]]) -> bool:
    """승격을 허용하는 DB 쓰기 전환 단계가 있다 = 승인 때 동의를 받는다."""
    return any(
        step[STEP_KIND] == MigrationStepKind.DB_CUTOVER.value and step.get(STEP_ALLOW_PROMOTE)
        for step in steps
    )


def promote_warning(steps: Sequence[dict[str, Any]]) -> str | None:
    """승격 동의가 필요한 계획이면 그 위험 고지 문장(응답 promoteWarning), 아니면 None."""
    return PROMOTE_WARNING if needs_promote_ack(steps) else None


def step_required_requests(
    steps: Sequence[dict[str, Any]], index: int, defaults: Sequence[int]
) -> int:
    """저장값 우선. 8-1 이전의 3개 비율 단계는 당시 기본값으로 실행한다."""
    stored = steps[index].get(STEP_REQUIRED_REQUESTS)
    if stored is not None:
        return int(stored)
    shift_indices = [i for i in range(len(steps)) if step_kind(steps, i) is MigrationStepKind.SHIFT]
    fallback = (20, 50, 50) if len(shift_indices) == 3 else defaults
    return int(fallback[shift_indices.index(index)])


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
    """지금 상태의 지문. connections 는 서비스의 살아 있는 연결(잠근 것)이다.

    connections 항목에서 배포 없는 연결(현재 배포 없음 + 비율 0 = 역할 NONE)은 뺀다. 그런 연결은
    서비스 중·예비가 아니라 이전의 출발·도착·진입점 가중치·예비 따라가기 어디에도 관여하지 않는다
    (첫 배포 전 변수 넣기(API 서버 10-4)·첫 배포 실패가 만든다). 넣어 두면 무관한 환경에 변수 하나만
    넣어도 승인된 이전(재난 자동 대피 포함)이 시작 직전 STALE 로 끝난다. 이전에 관여할 수 있는
    변화는 그대로 잡힌다: 배포가 생기면(첫 배포) 항목이 새로 나타나고, 배포·비율이 바뀌거나 연결이
    지워지면 항목이 바뀌거나 사라진다. 첫 배포 접수 자체도 openDeployRequests 로 잡힌다.
    """
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
            if item.current_deployment_id is not None or item.traffic_weight != 0
        ],
        FINGERPRINT_SOURCE_ENVIRONMENT: _environment_fingerprint(source.environment),
        FINGERPRINT_TARGET_ENVIRONMENT: _environment_fingerprint(target.environment),
        FINGERPRINT_OPEN_DEPLOY_REQUESTS: sorted(str(request.id) for request in open_requests),
    }


def fingerprint_changes(saved: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """항목별로 비교해 달라진 항목 이름을 정해진 순서로 돌려준다.

    배포 없는 연결을 빼기 전에 저장한 지문(이 규칙 이전에 만든 계획)도 같은 규칙으로 걸러 비교한다.
    """
    saved = _without_undeployed_connections(saved)
    current = _without_undeployed_connections(current)
    return [key for key in FINGERPRINT_KEYS if saved.get(key) != current.get(key)]


def _without_undeployed_connections(fingerprint: dict[str, Any]) -> dict[str, Any]:
    items = fingerprint.get(FINGERPRINT_CONNECTIONS)
    if not isinstance(items, list):
        return fingerprint
    kept = [
        item
        for item in items
        if not (isinstance(item, list) and len(item) == 3 and item[1] is None and item[2] == 0)
    ]
    return {**fingerprint, FINGERPRINT_CONNECTIONS: kept}


@dataclass(frozen=True)
class MigrationTargets:
    """실행기에 넘길 값: 대표 주소, 출발·도착 대상, 두 연결의 현재 배포."""

    route: MainRoute
    source: DeployTarget
    target: DeployTarget
    target_connection: ServiceEnvironment
    source_deployment: Deployment | None
    target_deployment: Deployment | None


def single_source(connections: Sequence[ServiceEnvironment]) -> ServiceEnvironment:
    """출발 연결: 비율 100 으로 서비스 중인 연결 하나(현재 배포 있음). 아니면 409."""
    serving = [item for item in connections if item.traffic_weight > 0]
    if len(serving) != 1 or serving[0].traffic_weight != 100:
        raise SourceNotSingleError("service must be served by exactly one environment at 100%")
    source = serving[0]
    if source.current_deployment_id is None:
        raise SourceNotSingleError("serving environment has no deployment")
    return source


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
