"""일꾼이 이전 계획 하나를 단계대로 진행한다.

단계(고정): 0 사전 확인 → 1 비율 10% → 2 비율 50% → 3 데이터 확인 → 4 비율 100%.
비율 단계는 APPLY(트래픽 실행기로 비율 적용) → OBSERVE(observe_seconds 지켜보기) → JUDGE(헬스체크 +
지표 판정)이고, 사전 확인·데이터 확인은 JUDGE 만 있다. 판정을 통과하면 사람 개입 없이 다음 단계로
간다. 판정 실패·판단 불가·적용 실패·사람의 중단·일꾼 상실은 출발 100% 로 되돌린다(ROLLING_BACK).

규칙
- 한 번의 advance 는 "기다려야 하는 곳"(지켜보기 끝 시각, 롤백 재시도 시각)이나 끝 상태까지 이어서
  진행한다. 동작마다 계획 행을 다시 읽고(populate_existing) 그 위치에서 한 가지만 한다.
- 쓰기 트랜잭션은 잠금 순서(서비스 → 연결 → 계획 → 할 일)를 지키고 맨 끝에 할 일 임대를 확인한다.
- 계획 상태와 위치는 조건부 UPDATE 로만 바꾼다(지금 상태·단계·위치가 기대와 같을 때만). 0행이면 다시
  읽는다. 실행 로그 줄은 반영됐을 때만 같은 트랜잭션에서 쓴다. 그래서 다시 시작해도 줄이 겹치지
  않는다.
- 외부 호출(트래픽·헬스·지표·데이터 확인)은 트랜잭션 밖에서 한다. 트래픽은 "요청함 시각 저장·커밋
  → 호출(멱등 키 `{계획}:{단계}`·`{계획}:rollback:{시도}`) → 결과 저장" 순서다. 다시 시작한 일꾼은
  요청함 시각만 있고 결과가 없으면 같은 키로 다시 부른다.
- 비율은 트래픽 실행기가 적용했다고 답했을 때만 두 연결에 함께 적는다(합계 100).
- 매 동작 맨 앞(잠금 안)에서 중단 요청·임대 상실을 본다. 임대를 3번 잃었으면(한 번 참이면 계속 참)
  APPROVED 는 FAILED(WORKER_LOST), RUNNING 은 롤백을 시작하고, 롤백은 남은 시도 횟수만큼 계속한다.
  끝 상태가 되면 할 일을 FAILED(WORKER_LOST) 로 닫는다.
- simulate(가짜 모드)는 그 단계 판정(또는 TRAFFIC 이면 그 단계 APPLY)에서 실행기를 부르지 않고
  처리기가 실패 결과를 만든다. 로그·단계 결과에는 만든 값이 그대로 남는다.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.enums import (
    HealthCheckResult,
    MigrationErrorCode,
    MigrationLogCode,
    MigrationPlanStatus,
    MigrationRollbackStatus,
    MigrationSimulateReason,
    MigrationStepKind,
    MigrationStepPhase,
    MigrationStepStatus,
    ServiceStatus,
)
from app.executors import ExecutorSet
from app.executors.base import (
    DataSyncLag,
    HealthHttpError,
    HealthOk,
    HealthProbeResult,
    Metrics,
    MetricsResult,
    TrafficApplied,
    TrafficResult,
    WeightedTarget,
)
from app.models import MigrationPlan, Service, ServiceEnvironment
from app.models.service import DEFAULT_HEALTH_CHECK_PATH
from app.repositories.migration_plan_repository import MigrationPlanRepository
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.repositories.service_repository import ServiceRepository
from app.services.deploy_support import SNAPSHOT_HEALTH_CHECK_PATH, public_url
from app.services.job_lease import JobLease, JobOutcome
from app.services.migration_plan_log import MigrationPlanLogWriter
from app.services.migration_plan_support import (
    CONDITION_MAX_ERROR_RATE,
    CONDITION_MAX_P95_MS,
    CONDITION_MAX_REPLICATION_LAG,
    LAST_STEP,
    RESULT_FAILURE_CODE,
    RESULT_FINISHED_AT,
    RESULT_METRICS,
    RESULT_STARTED_AT,
    RESULT_STATUS,
    SIMULATE_FAIL_AT_STEP,
    SIMULATE_REASON,
    STEP_TARGET_WEIGHT,
    MigrationTargets,
    fingerprint_changes,
    iso,
    load_migration_targets,
    read_fingerprint,
    skip_pending_steps,
    step_kind,
    with_step_result,
)

logger = logging.getLogger(__name__)

# 한 번의 advance 에서 이어 갈 동작 수 상한(정상 경로는 10개 안쪽이다).
MAX_ACTIONS_PER_RUN = 32
# simulate 가 만드는 판정 값(실행기를 부르지 않는다).
SIMULATED_ERROR_RATE_PERCENT = 5.0
SIMULATED_P95_MS = 5000.0
SIMULATED_REPLICATION_LAG_SECONDS = 30.0
SIMULATED_HEALTH_STATUS_CODE = 503
_BASELINE_ERROR_RATE_PERCENT = 0.1
_BASELINE_P95_MS = 120.0
_BASELINE_REQUESTS_PER_SECOND = 10

_HEALTH_RESULTS = {
    HealthOk: HealthCheckResult.OK,
    HealthHttpError: HealthCheckResult.HTTP_ERROR,
}


@dataclass(frozen=True)
class _Locked:
    service: Service
    connections: list[ServiceEnvironment]
    plan: MigrationPlan
    source: ServiceEnvironment | None
    target: ServiceEnvironment | None


class MigrationPlanProcessor:
    def __init__(
        self,
        session: AsyncSession,
        lease: JobLease,
        settings: Settings,
        clock: Clock,
        executors: ExecutorSet,
    ) -> None:
        self._session = session
        self._lease = lease
        self._settings = settings
        self._clock = clock
        self._executors = executors
        self._plans = MigrationPlanRepository(session)
        self._services = ServiceRepository(session)
        self._connections = ServiceEnvironmentRepository(session)
        self._log = MigrationPlanLogWriter(session)
        self._plan_id = lease.job.subject_id

    async def advance(self) -> JobOutcome:
        for _ in range(MAX_ACTIONS_PER_RUN):
            outcome = await self._act()
            if outcome is not None:
                return outcome
        return JobOutcome.again(self._clock.now())

    async def _act(self) -> JobOutcome | None:
        """지금 위치에서 한 가지를 한다. None 이면 같은 실행에서 이어 간다."""
        async with self._session.begin():
            plan = await self._plans.find_by_id(self._plan_id)
        if plan is None:
            return JobOutcome.done()
        status = MigrationPlanStatus(plan.status)
        if status.is_terminal:
            return self._closed()
        match status:
            case MigrationPlanStatus.APPROVED:
                return await self._start()
            case MigrationPlanStatus.RUNNING:
                return await self._run(plan)
            case MigrationPlanStatus.ROLLING_BACK:
                return await self._roll_back(plan)
            case _:
                # 승인 전 계획에는 할 일이 없다.
                return JobOutcome.done()

    def _closed(self) -> JobOutcome:
        if self._lease.is_exhausted:
            return JobOutcome.failed(MigrationErrorCode.WORKER_LOST)
        return JobOutcome.done()

    # --- APPROVED: 지문 확인 → RUNNING ---

    async def _start(self) -> JobOutcome | None:
        now = self._clock.now()
        async with self._session.begin():
            locked = await self._lock()
            plan = locked.plan
            if plan.status != MigrationPlanStatus.APPROVED:
                return None
            if self._lease.is_exhausted:
                await self._fail(plan, MigrationErrorCode.WORKER_LOST, now)
                return self._closed()
            service = locked.service
            if (
                service.is_deleted
                or service.status != ServiceStatus.ACTIVE
                or locked.source is None
                or locked.target is None
            ):
                # 실행 중에는 서비스 삭제 접수가 막혀 생기지 않는다. 방어용.
                await self._fail(plan, MigrationErrorCode.INTERNAL_ERROR, now)
                return self._closed()
            if plan.abort_requested_at is not None:
                if await self._plans.transition(
                    plan.id,
                    MigrationPlanStatus.APPROVED,
                    {
                        "status": MigrationPlanStatus.CANCELLED,
                        "cancelled_by_user_id": plan.aborted_by_user_id,
                        "step_results": skip_pending_steps(plan.step_results),
                        "finished_at": now,
                        "updated_at": now,
                    },
                ):
                    await self._log.append(plan, MigrationLogCode.CANCELLED, now)
                self._report("migration cancelled before start", plan)
                return self._closed()
            current = await read_fingerprint(
                self._session,
                service,
                locked.connections,
                plan.source_service_environment_id,
                plan.target_service_environment_id,
            )
            changed = fingerprint_changes(plan.fingerprint, current)
            if changed:
                if await self._plans.transition(
                    plan.id,
                    MigrationPlanStatus.APPROVED,
                    {
                        "status": MigrationPlanStatus.STALE,
                        "step_results": skip_pending_steps(plan.step_results),
                        "finished_at": now,
                        "updated_at": now,
                    },
                ):
                    await self._log.append(
                        plan, MigrationLogCode.STALE, now, data={"changed": changed}
                    )
                self._report("migration plan stale at start", plan)
                return self._closed()
            if await self._plans.transition(
                plan.id,
                MigrationPlanStatus.APPROVED,
                {
                    "status": MigrationPlanStatus.RUNNING,
                    "started_at": now,
                    "current_step": 0,
                    "step_phase": MigrationStepPhase.JUDGE,
                    "phase_started_at": now,
                    "step_results": with_step_result(
                        plan.step_results,
                        0,
                        **{
                            RESULT_STATUS: MigrationStepStatus.RUNNING.value,
                            RESULT_STARTED_AT: iso(now),
                        },
                    ),
                    "updated_at": now,
                },
            ):
                await self._log.append(plan, MigrationLogCode.STARTED, now)
        self._report("migration started", plan)
        return None

    # --- RUNNING ---

    async def _run(self, plan: MigrationPlan) -> JobOutcome | None:
        now = self._clock.now()
        async with self._session.begin():
            locked = await self._lock()
            plan = locked.plan
            if plan.status != MigrationPlanStatus.RUNNING:
                return None
            if locked.source is None or locked.target is None:
                await self._fail(plan, MigrationErrorCode.INTERNAL_ERROR, now)
                return self._closed()
            if self._lease.is_exhausted:
                await self._start_rollback(plan, MigrationErrorCode.WORKER_LOST, now)
                return None
            if plan.abort_requested_at is not None:
                await self._start_rollback(plan, MigrationErrorCode.ABORTED, now)
                return None
        index = plan.current_step
        kind = step_kind(plan.steps, index)
        phase = MigrationStepPhase(plan.step_phase) if plan.step_phase else None
        if kind is MigrationStepKind.PREFLIGHT:
            return await self._preflight(plan)
        if kind is MigrationStepKind.DATA_SYNC_CHECK:
            return await self._check_data_sync(plan)
        match phase:
            case MigrationStepPhase.APPLY:
                return await self._apply(plan)
            case MigrationStepPhase.OBSERVE:
                return await self._observe(plan)
            case _:
                return await self._judge_shift(plan)

    async def _preflight(self, plan: MigrationPlan) -> JobOutcome | None:
        """도착 환경별 주소 헬스체크 한 번 + 도착 현재 배포가 출발과 같은 이미지인지."""
        targets = await self._targets(plan)
        await self._lease.extend()
        health = await self._probe(targets)
        is_same_image = (
            targets.source_deployment is not None
            and targets.target_deployment is not None
            and targets.source_deployment.image_digest == targets.target_deployment.image_digest
        )
        metrics = _metrics_json(health=health)
        now = self._clock.now()
        async with self._session.begin():
            locked = await self._lock()
            plan = locked.plan
            if not _is_at(plan, MigrationPlanStatus.RUNNING, 0, MigrationStepPhase.JUDGE):
                return None
            if isinstance(health, HealthOk) and is_same_image:
                await self._pass_step(plan, metrics, MigrationLogCode.PREFLIGHT_OK, now)
                return None
            results = with_step_result(
                plan.step_results,
                0,
                **{
                    RESULT_STATUS: MigrationStepStatus.FAILED.value,
                    RESULT_FINISHED_AT: iso(now),
                    RESULT_METRICS: metrics,
                    RESULT_FAILURE_CODE: MigrationErrorCode.PREFLIGHT_FAILED.value,
                },
            )
            if await self._plans.transition(
                plan.id,
                MigrationPlanStatus.RUNNING,
                {
                    "status": MigrationPlanStatus.FAILED,
                    "error_code": MigrationErrorCode.PREFLIGHT_FAILED,
                    # 트래픽을 바꾼 적이 없어 되돌릴 것이 없다.
                    "rollback_status": MigrationRollbackStatus.NOT_NEEDED,
                    "step_phase": None,
                    "step_results": skip_pending_steps(results),
                    "finished_at": now,
                    "updated_at": now,
                },
                [MigrationPlan.current_step == 0],
            ):
                await self._log.append(plan, MigrationLogCode.PREFLIGHT_FAILED, now, step=0)
        self._report("migration preflight failed", plan)
        return self._closed()

    async def _apply(self, plan: MigrationPlan) -> JobOutcome | None:
        index = plan.current_step
        target_weight = int(plan.steps[index][STEP_TARGET_WEIGHT])
        weights_data = {"sourceWeight": 100 - target_weight, "targetWeight": target_weight}
        if _simulated_failure(plan, index) is MigrationSimulateReason.TRAFFIC:
            return await self._traffic_failed(plan, index, weights_data, self._clock.now())
        if plan.traffic_requested_at is None:
            async with self._session.begin():
                await self._lock()
                await self._plans.transition(
                    plan.id,
                    MigrationPlanStatus.RUNNING,
                    {"traffic_requested_at": self._clock.now(), "updated_at": self._clock.now()},
                    _at_conditions(index, MigrationStepPhase.APPLY)
                    + [MigrationPlan.traffic_requested_at.is_(None)],
                )
        targets = await self._targets(plan)
        await self._lease.extend()
        result = await self._executors.traffic.set_weights(
            targets.route,
            (
                WeightedTarget(target=targets.source, weight=100 - target_weight),
                WeightedTarget(target=targets.target, weight=target_weight),
            ),
            idempotency_key=f"{plan.id}:{index}",
        )
        now = self._clock.now()
        if not isinstance(result, TrafficApplied):
            return await self._traffic_failed(plan, index, weights_data, now)
        async with self._session.begin():
            locked = await self._lock()
            plan = locked.plan
            if not _is_at(plan, MigrationPlanStatus.RUNNING, index, MigrationStepPhase.APPLY):
                return None
            observe_until = now + timedelta(seconds=plan.observe_seconds)
            if await self._plans.transition(
                plan.id,
                MigrationPlanStatus.RUNNING,
                {
                    "step_phase": MigrationStepPhase.OBSERVE,
                    "phase_started_at": now,
                    "observe_until": observe_until,
                    "updated_at": now,
                },
                _at_conditions(index, MigrationStepPhase.APPLY),
            ):
                self._record_weights(locked, 100 - target_weight, target_weight, result)
                await self._session.flush()
                await self._log.append(
                    plan, MigrationLogCode.WEIGHT_APPLIED, now, step=index, data=weights_data
                )
        self._report("migration weights applied", plan, step=index)
        return JobOutcome.again(observe_until)

    async def _traffic_failed(
        self, plan: MigrationPlan, index: int, weights_data: dict[str, Any], now: datetime
    ) -> JobOutcome | None:
        async with self._session.begin():
            locked = await self._lock()
            plan = locked.plan
            if not _is_at(plan, MigrationPlanStatus.RUNNING, index, MigrationStepPhase.APPLY):
                return None
            results = with_step_result(
                plan.step_results,
                index,
                **{
                    RESULT_STATUS: MigrationStepStatus.FAILED.value,
                    RESULT_FINISHED_AT: iso(now),
                    RESULT_METRICS: None,
                    RESULT_FAILURE_CODE: MigrationErrorCode.TRAFFIC_APPLY_FAILED.value,
                },
            )
            await self._start_rollback(
                plan, MigrationErrorCode.TRAFFIC_APPLY_FAILED, now, results=results, index=index
            )
        self._report(
            "migration weights not applied",
            plan,
            step=index,
            error_code=MigrationErrorCode.TRAFFIC_APPLY_FAILED,
        )
        return None

    async def _observe(self, plan: MigrationPlan) -> JobOutcome | None:
        now = self._clock.now()
        if plan.observe_until is not None and now < plan.observe_until:
            return JobOutcome.again(plan.observe_until)
        async with self._session.begin():
            await self._lock()
            await self._plans.transition(
                plan.id,
                MigrationPlanStatus.RUNNING,
                {
                    "step_phase": MigrationStepPhase.JUDGE,
                    "phase_started_at": now,
                    "updated_at": now,
                },
                _at_conditions(plan.current_step, MigrationStepPhase.OBSERVE),
            )
        return None

    async def _judge_shift(self, plan: MigrationPlan) -> JobOutcome | None:
        """지켜본 구간을 헬스체크 + 지표(오류율·p95)로 판정한다."""
        index = plan.current_step
        conditions = plan.rollback_conditions
        targets = await self._targets(plan)
        until = self._clock.now()
        since = (
            plan.observe_until - timedelta(seconds=plan.observe_seconds)
            if plan.observe_until is not None
            else until
        )
        simulated = _simulated_failure(plan, index)
        health: HealthProbeResult
        metrics: MetricsResult
        if simulated is None:
            await self._lease.extend()
            health = await self._probe(targets)
            metrics = await self._executors.metrics.read(targets.target, since, until)
        else:
            weight = int(plan.steps[index][STEP_TARGET_WEIGHT])
            health, metrics = _simulated_judgement(simulated, weight, since, until)
        failure: MigrationErrorCode | None = None
        if not isinstance(health, HealthOk):
            failure = MigrationErrorCode.HEALTH_CHECK_FAILED
        elif not isinstance(metrics, Metrics):
            failure = MigrationErrorCode.METRICS_UNAVAILABLE
        elif metrics.error_rate_percent > float(
            conditions[CONDITION_MAX_ERROR_RATE]
        ) or metrics.p95_ms > float(conditions[CONDITION_MAX_P95_MS]):
            failure = MigrationErrorCode.THRESHOLD_EXCEEDED
        metrics_json = _metrics_json(health=health, metrics=metrics)
        now = self._clock.now()
        async with self._session.begin():
            locked = await self._lock()
            plan = locked.plan
            if not _is_at(plan, MigrationPlanStatus.RUNNING, index, MigrationStepPhase.JUDGE):
                return None
            if failure is None:
                outcome = await self._pass_step(
                    plan, metrics_json, MigrationLogCode.JUDGE_PASSED, now
                )
                if outcome is not None:
                    self._report("migration succeeded", plan)
                return outcome
            await self._fail_step(
                plan, index, metrics_json, failure, MigrationLogCode.JUDGE_FAILED, now
            )
        self._report("migration step judged unhealthy", plan, step=index, error_code=failure)
        return None

    async def _check_data_sync(self, plan: MigrationPlan) -> JobOutcome | None:
        index = plan.current_step
        max_lag = float(plan.rollback_conditions[CONDITION_MAX_REPLICATION_LAG])
        if _simulated_failure(plan, index) is MigrationSimulateReason.DATA_LAG:
            lag: float | None = SIMULATED_REPLICATION_LAG_SECONDS
        else:
            targets = await self._targets(plan)
            await self._lease.extend()
            result = await self._executors.data_sync.replication_lag(targets.source, targets.target)
            lag = result.seconds if isinstance(result, DataSyncLag) else None
        failure: MigrationErrorCode | None = None
        if lag is None:
            failure = MigrationErrorCode.METRICS_UNAVAILABLE
        elif lag > max_lag:
            failure = MigrationErrorCode.DATA_LAG_EXCEEDED
        metrics_json = _metrics_json(replication_lag_seconds=lag)
        now = self._clock.now()
        async with self._session.begin():
            locked = await self._lock()
            plan = locked.plan
            if not _is_at(plan, MigrationPlanStatus.RUNNING, index, MigrationStepPhase.JUDGE):
                return None
            if failure is None:
                return await self._pass_step(plan, metrics_json, MigrationLogCode.DATA_SYNC_OK, now)
            await self._fail_step(
                plan, index, metrics_json, failure, MigrationLogCode.DATA_SYNC_FAILED, now
            )
        self._report("migration data sync check failed", plan, step=index, error_code=failure)
        return None

    async def _pass_step(
        self,
        plan: MigrationPlan,
        metrics_json: dict[str, Any],
        code: MigrationLogCode,
        now: datetime,
    ) -> JobOutcome | None:
        """잠근 트랜잭션 안에서 지금 단계를 통과로 적고 다음 단계로(마지막이면 SUCCEEDED) 간다."""
        index = plan.current_step
        results = with_step_result(
            plan.step_results,
            index,
            **{
                RESULT_STATUS: MigrationStepStatus.PASSED.value,
                RESULT_FINISHED_AT: iso(now),
                RESULT_METRICS: metrics_json,
            },
        )
        conditions = _at_conditions(index, MigrationStepPhase.JUDGE)
        if index == LAST_STEP:
            if await self._plans.transition(
                plan.id,
                MigrationPlanStatus.RUNNING,
                {
                    "status": MigrationPlanStatus.SUCCEEDED,
                    "step_phase": None,
                    "observe_until": None,
                    "step_results": results,
                    "finished_at": now,
                    "updated_at": now,
                },
                conditions,
            ):
                await self._log.append(plan, code, now, step=index, data=_numbers(metrics_json))
                await self._log.append(plan, MigrationLogCode.SUCCEEDED, now)
            return self._closed()
        following = index + 1
        next_phase = (
            MigrationStepPhase.APPLY
            if step_kind(plan.steps, following) is MigrationStepKind.SHIFT
            else MigrationStepPhase.JUDGE
        )
        results = with_step_result(
            results,
            following,
            **{RESULT_STATUS: MigrationStepStatus.RUNNING.value, RESULT_STARTED_AT: iso(now)},
        )
        if await self._plans.transition(
            plan.id,
            MigrationPlanStatus.RUNNING,
            {
                "current_step": following,
                "step_phase": next_phase,
                "phase_started_at": now,
                "observe_until": None,
                "traffic_requested_at": None,
                "step_results": results,
                "updated_at": now,
            },
            conditions,
        ):
            await self._log.append(plan, code, now, step=index, data=_numbers(metrics_json))
        return None

    async def _fail_step(
        self,
        plan: MigrationPlan,
        index: int,
        metrics_json: dict[str, Any],
        failure: MigrationErrorCode,
        code: MigrationLogCode,
        now: datetime,
    ) -> None:
        results = with_step_result(
            plan.step_results,
            index,
            **{
                RESULT_STATUS: MigrationStepStatus.FAILED.value,
                RESULT_FINISHED_AT: iso(now),
                RESULT_METRICS: metrics_json,
                RESULT_FAILURE_CODE: failure.value,
            },
        )
        await self._start_rollback(
            plan,
            failure,
            now,
            results=results,
            index=index,
            failure_line=(code, _numbers(metrics_json)),
        )

    # --- 롤백 ---

    async def _start_rollback(
        self,
        plan: MigrationPlan,
        cause: MigrationErrorCode,
        now: datetime,
        *,
        results: list[dict[str, Any]] | None = None,
        index: int | None = None,
        failure_line: tuple[MigrationLogCode, dict[str, Any]] | None = None,
    ) -> bool:
        """잠근 트랜잭션 안에서 RUNNING → ROLLING_BACK. 원인 줄(있으면)과 ROLLBACK_STARTED 를 쓴다.

        중단·일꾼 상실처럼 단계 결과가 정해지지 않은 채 멈추면 지금 단계를 그 원인으로 FAILED 로
        적는다. 남은 단계는 SKIPPED 다.
        """
        if results is None:
            current = plan.current_step
            results = list(plan.step_results)
            if results[current][RESULT_STATUS] == MigrationStepStatus.RUNNING.value:
                results = with_step_result(
                    results,
                    current,
                    **{
                        RESULT_STATUS: MigrationStepStatus.FAILED.value,
                        RESULT_FINISHED_AT: iso(now),
                        RESULT_FAILURE_CODE: cause.value,
                    },
                )
        if not await self._plans.transition(
            plan.id,
            MigrationPlanStatus.RUNNING,
            {
                "status": MigrationPlanStatus.ROLLING_BACK,
                "error_code": cause,
                "step_phase": None,
                "observe_until": None,
                "rollback_requested_at": None,
                "step_results": skip_pending_steps(results),
                "updated_at": now,
            },
            [MigrationPlan.current_step == index] if index is not None else [],
        ):
            return False
        if failure_line is not None:
            failure_code, failure_data = failure_line
            await self._log.append(
                plan, failure_code, now, step=index, data=failure_data, error_code=cause
            )
        elif cause is MigrationErrorCode.TRAFFIC_APPLY_FAILED and index is not None:
            target_weight = int(plan.steps[index][STEP_TARGET_WEIGHT])
            await self._log.append(
                plan,
                MigrationLogCode.WEIGHT_APPLY_FAILED,
                now,
                step=index,
                data={"sourceWeight": 100 - target_weight, "targetWeight": target_weight},
            )
        await self._log.append(
            plan, MigrationLogCode.ROLLBACK_STARTED, now, step=plan.current_step, error_code=cause
        )
        self._report("migration rollback started", plan, error_code=cause)
        return True

    async def _roll_back(self, plan: MigrationPlan) -> JobOutcome | None:
        """출발 100%·도착 0% 를 한 번 시도한다. 실패하면 poll 간격 뒤 다시, 최대 횟수면 FAILED."""
        max_attempts = self._settings.migration_rollback_max_attempts
        attempt = plan.rollback_attempts
        if attempt >= max_attempts:
            return await self._give_up_rollback(plan, attempt)
        if plan.rollback_requested_at is None:
            async with self._session.begin():
                await self._lock()
                await self._plans.transition(
                    plan.id,
                    MigrationPlanStatus.ROLLING_BACK,
                    {"rollback_requested_at": self._clock.now(), "updated_at": self._clock.now()},
                    [
                        MigrationPlan.rollback_attempts == attempt,
                        MigrationPlan.rollback_requested_at.is_(None),
                    ],
                )
        targets = await self._targets(plan)
        await self._lease.extend()
        result: TrafficResult = await self._executors.traffic.set_weights(
            targets.route,
            (
                WeightedTarget(target=targets.source, weight=100),
                WeightedTarget(target=targets.target, weight=0),
            ),
            idempotency_key=f"{plan.id}:rollback:{attempt}",
        )
        now = self._clock.now()
        async with self._session.begin():
            locked = await self._lock()
            plan = locked.plan
            if plan.status != MigrationPlanStatus.ROLLING_BACK or plan.rollback_attempts != attempt:
                return None
            at_attempt = [MigrationPlan.rollback_attempts == attempt]
            if isinstance(result, TrafficApplied):
                if await self._plans.transition(
                    plan.id,
                    MigrationPlanStatus.ROLLING_BACK,
                    {
                        "status": MigrationPlanStatus.ROLLED_BACK,
                        "rollback_status": MigrationRollbackStatus.SUCCEEDED,
                        "finished_at": now,
                        "updated_at": now,
                    },
                    at_attempt,
                ):
                    self._record_weights(locked, 100, 0, result)
                    await self._session.flush()
                    await self._log.append(
                        plan,
                        MigrationLogCode.ROLLBACK_APPLIED,
                        now,
                        data={"sourceWeight": 100, "targetWeight": 0},
                    )
                self._report("migration rolled back", plan, error_code=plan.error_code)
                return self._closed()
            if attempt + 1 >= max_attempts:
                await self._finish_rollback_failed(plan, attempt + 1, now)
                return self._closed()
            if await self._plans.transition(
                plan.id,
                MigrationPlanStatus.ROLLING_BACK,
                {
                    "rollback_attempts": attempt + 1,
                    "rollback_requested_at": None,
                    "updated_at": now,
                },
                at_attempt,
            ):
                await self._log.append(
                    plan,
                    MigrationLogCode.ROLLBACK_APPLY_FAILED,
                    now,
                    data={"attempt": attempt + 1, "maxAttempts": max_attempts},
                )
        return JobOutcome.again(
            now + timedelta(seconds=self._settings.worker_poll_interval_seconds)
        )

    async def _give_up_rollback(self, plan: MigrationPlan, attempts: int) -> JobOutcome:
        now = self._clock.now()
        async with self._session.begin():
            locked = await self._lock()
            if locked.plan.status == MigrationPlanStatus.ROLLING_BACK:
                await self._finish_rollback_failed(locked.plan, attempts, now)
        return self._closed()

    async def _finish_rollback_failed(
        self, plan: MigrationPlan, attempts: int, now: datetime
    ) -> None:
        """롤백을 다 시도했다. 원인 error_code 는 그대로, 비율은 마지막으로 적용된 값 그대로다."""
        if await self._plans.transition(
            plan.id,
            MigrationPlanStatus.ROLLING_BACK,
            {
                "status": MigrationPlanStatus.FAILED,
                "rollback_status": MigrationRollbackStatus.FAILED,
                "rollback_attempts": attempts,
                "finished_at": now,
                "updated_at": now,
            },
        ):
            await self._log.append(
                plan, MigrationLogCode.ROLLBACK_FAILED, now, data={"attempt": attempts}
            )
        self._report("migration rollback failed", plan, error_code=plan.error_code)

    # --- 공통 ---

    async def _fail(self, plan: MigrationPlan, code: MigrationErrorCode, now: datetime) -> None:
        """트래픽을 바꾸기 전(APPROVED) 또는 방어용으로 FAILED 로 끝낸다(잠근 트랜잭션 안)."""
        status = MigrationPlanStatus(plan.status)
        rollback_status = (
            MigrationRollbackStatus.NOT_NEEDED
            if status is MigrationPlanStatus.APPROVED
            else MigrationRollbackStatus.FAILED
        )
        if await self._plans.transition(
            plan.id,
            status,
            {
                "status": MigrationPlanStatus.FAILED,
                "error_code": code,
                "rollback_status": rollback_status,
                "step_phase": None,
                "step_results": skip_pending_steps(plan.step_results),
                "finished_at": now,
                "updated_at": now,
            },
        ):
            await self._log.append(plan, MigrationLogCode.FAILED, now, error_code=code)
        self._report("migration failed", plan, error_code=code)

    def _record_weights(
        self, locked: _Locked, source_weight: int, target_weight: int, result: TrafficApplied
    ) -> None:
        """트래픽 실행기가 적용했다고 답한 비율을 두 연결에 함께 적는다(합계 100)."""
        assert locked.source is not None and locked.target is not None
        locked.source.traffic_weight = source_weight
        locked.source.traffic_applied_at = result.applied_at
        locked.target.traffic_weight = target_weight
        locked.target.traffic_applied_at = result.applied_at

    async def _lock(self) -> _Locked:
        """서비스 → 연결(id 오름차순) → 계획 순서로 잠그고 마지막에 할 일 임대를 확인한다."""
        snapshot = await self._plans.find_by_id(self._plan_id)
        assert snapshot is not None
        service = await self._services.find_by_id(snapshot.service_id, lock=True)
        assert service is not None
        connections = await self._connections.lock_live_by_service(service.id)
        plan = await self._plans.find_by_id(self._plan_id, lock=True)
        assert plan is not None
        await self._lease.hold()
        by_id = {item.id: item for item in connections}
        return _Locked(
            service=service,
            connections=connections,
            plan=plan,
            source=by_id.get(plan.source_service_environment_id),
            target=by_id.get(plan.target_service_environment_id),
        )

    async def _targets(self, plan: MigrationPlan) -> MigrationTargets:
        async with self._session.begin():
            service = await self._services.find_by_id(plan.service_id)
            assert service is not None
            return await load_migration_targets(
                self._session,
                service,
                plan.source_service_environment_id,
                plan.target_service_environment_id,
            )

    async def _probe(self, targets: MigrationTargets) -> HealthProbeResult:
        snapshot = targets.target_deployment.config_snapshot if targets.target_deployment else {}
        path = str(snapshot.get(SNAPSHOT_HEALTH_CHECK_PATH, DEFAULT_HEALTH_CHECK_PATH))
        return await self._executors.health.probe(
            public_url(targets.target_connection.public_host),
            path,
            self._settings.health_check_request_timeout_seconds,
        )

    def _report(
        self,
        message: str,
        plan: MigrationPlan,
        *,
        step: int | None = None,
        error_code: str | None = None,
    ) -> None:
        logger.info(
            message,
            extra={
                "action": "advance_migration_plan",
                "migration_plan_id": str(plan.id),
                "service_id": str(plan.service_id),
                "step": step,
                "error_code": error_code,
            },
        )


def _is_at(
    plan: MigrationPlan, status: MigrationPlanStatus, index: int, phase: MigrationStepPhase
) -> bool:
    return plan.status == status and plan.current_step == index and plan.step_phase == phase


def _at_conditions(index: int, phase: MigrationStepPhase) -> list[Any]:
    return [MigrationPlan.current_step == index, MigrationPlan.step_phase == phase]


def _simulated_failure(plan: MigrationPlan, index: int) -> MigrationSimulateReason | None:
    simulate = plan.simulate
    if not simulate or int(simulate.get(SIMULATE_FAIL_AT_STEP, -1)) != index:
        return None
    return MigrationSimulateReason(simulate[SIMULATE_REASON])


def _simulated_judgement(
    reason: MigrationSimulateReason, weight: int, since: datetime, until: datetime
) -> tuple[HealthProbeResult, MetricsResult]:
    window_seconds = max((until - since).total_seconds(), 0.0)
    request_count = max(1, round(_BASELINE_REQUESTS_PER_SECOND * weight / 100 * window_seconds))
    health: HealthProbeResult = HealthOk(status_code=200)
    error_rate, p95 = _BASELINE_ERROR_RATE_PERCENT, _BASELINE_P95_MS
    if reason is MigrationSimulateReason.ERROR_RATE:
        error_rate = SIMULATED_ERROR_RATE_PERCENT
    elif reason is MigrationSimulateReason.LATENCY:
        p95 = SIMULATED_P95_MS
    elif reason is MigrationSimulateReason.HEALTH:
        health = HealthHttpError(status_code=SIMULATED_HEALTH_STATUS_CODE)
    return health, Metrics(error_rate_percent=error_rate, p95_ms=p95, request_count=request_count)


def _health_status(health: HealthProbeResult | None) -> str | None:
    if health is None:
        return None
    return str(_HEALTH_RESULTS.get(type(health), HealthCheckResult.UNREACHABLE))


def _metrics_json(
    *,
    health: HealthProbeResult | None = None,
    metrics: MetricsResult | None = None,
    replication_lag_seconds: float | None = None,
) -> dict[str, Any]:
    """단계 결과 metrics 칸. 없는 값은 null."""
    measured = metrics if isinstance(metrics, Metrics) else None
    return {
        "healthStatus": _health_status(health),
        "errorRatePercent": measured.error_rate_percent if measured else None,
        "p95Ms": measured.p95_ms if measured else None,
        "requestCount": measured.request_count if measured else None,
        "replicationLagSeconds": replication_lag_seconds,
    }


def _numbers(metrics_json: dict[str, Any]) -> dict[str, Any]:
    """로그 data 에는 숫자 지표만 싣는다(헬스 결과 문자열은 단계 결과에만)."""
    return {
        key: value
        for key, value in metrics_json.items()
        if isinstance(value, int | float) and not isinstance(value, bool)
    }
