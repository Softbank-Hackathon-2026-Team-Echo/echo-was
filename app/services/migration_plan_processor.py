"""일꾼이 이전 계획 하나를 단계대로 진행한다.

단계(고정): 0 사전 확인 → 1 비율 10% → 2 비율 50% → 3 데이터 확인 → 4 비율 100%.
비율 단계는 APPLY(트래픽 실행기로 비율 적용) → OBSERVE(건수 세기: 출발·도착 누적 카운터를 주기마다
읽어 도착이 요청 N건을 5xx 허용 안에서 처리했는지 센다) → JUDGE(헬스체크)이고, 사전 확인·데이터
확인은 JUDGE 만 있다. 세기 규칙은 migration_counting 에 있다. 판정을 통과하면 사람 개입 없이 다음
단계로 간다. 판정 실패·판단 불가·적용 실패·사람의 중단·일꾼 상실은 출발 100% 로 되돌린다
(ROLLING_BACK). p95 는 판정에 쓰지 않고 단계를 끝낼 때 한 번 읽어 참고 값으로만 남긴다.

규칙
- 한 번의 advance 는 "기다려야 하는 곳"(다음 카운터 읽기 시각, 롤백 재시도 시각)이나 끝 상태까지
  이어서 진행한다. 동작마다 계획 행을 다시 읽고(populate_existing) 그 위치에서 한 가지만 한다.
- 건수 세기는 한 번 실행에 한 번 읽는다. 센 값(step_results[i].counting)은 조건부 UPDATE 로 적고,
  진행 숫자 갱신은 로그 줄을 남기지 않는다(카운터 재시작으로 본 읽기만 COUNTERS_RESET). 필요 건수를
  채우거나 마감 판정을 통과하면 세기를 끝낸 이유(judgeReason)를 같은 트랜잭션에 적고 JUDGE 로 간다.
  JUDGE 는 카운터를 다시 읽지 않고 저장된 값으로 판정하므로, 다시 시작한 일꾼도 같은 경로를 밟는다.
- 8-1 전에 만든 계획(SHIFT 의 OBSERVE·JUDGE 인데 센 값이 없음)은 그 자리에서 세기를 다시 시작한다.
- 쓰기 트랜잭션은 잠금 순서(서비스 → 연결 → 계획 → 할 일)를 지키고 맨 끝에 할 일 임대를 확인한다.
- 계획 상태와 위치는 조건부 UPDATE 로만 바꾼다(지금 상태·단계·위치가 기대와 같을 때만). 0행이면 다시
  읽는다. 실행 로그 줄은 반영됐을 때만 같은 트랜잭션에서 쓴다. 그래서 다시 시작해도 줄이 겹치지
  않는다.
- 외부 호출(트래픽·헬스·카운터·지표·데이터 확인)은 트랜잭션 밖에서 한다. 트래픽은 "요청함
  시각 저장·커밋 → 호출(멱등 키 `{계획}:{단계}`·`{계획}:rollback:{시도}`) → 결과 저장" 순서다.
  다시 시작한 일꾼은 요청함 시각만 있고 결과가 없으면 같은 키로 다시 부른다.
- 비율은 트래픽 실행기가 적용했다고 답했을 때만 두 연결에 함께 적는다(합계 100).
- 매 동작 맨 앞(잠금 안)에서 중단 요청·임대 상실을 본다. 임대를 3번 잃었으면(한 번 참이면 계속 참)
  APPROVED 는 FAILED(WORKER_LOST), RUNNING 은 롤백을 시작하고, 롤백은 남은 시도 횟수만큼 계속한다.
  끝 상태가 되면 할 일을 FAILED(WORKER_LOST) 로 닫는다.
- 보안 게이트(설정으로 켤 때만 주입)는 실행 시작(APPROVED → RUNNING) 잠금 전에 다시 판정하고,
  잠금 안에서 지문이 같을 때 판정 줄을 남긴다. 막혔으면 트래픽을 바꾸지 않고
  FAILED(SECURITY_GATE_BLOCKED)로 끝낸다.
- DB 쓰기 전환 단계(API 서버 17, includeCutover 계획의 5번)는 APPLY(요청 시각 저장 → 컷오버 Job
  만들기 → Job 이름 저장) → OBSERVE(Job 이 끝날 때까지 poll, 상한 = 그 단계 maxWaitSeconds) →
  JUDGE(이 계획 id 의 결과 행으로 판정, migration_db_cutover.judge_db_cutover).
  종료 1·Job 을 못 만듦은 기존 트래픽 롤백, 부분 상태·상한 초과·누락·중복은 아무것도
  자동으로 되돌리지 않고 FAILED(rollbackStatus SKIPPED)로 끝낸다. 롤백 Job 은 자동으로
  부르지 않는다. Job 을 요청한 뒤에는 중단 요청·임대 상실이 있어도 Job 이 끝나 판정할 때까지
  기다린다(그 사이 트래픽을 되돌리면 쓰기 담당과 어긋난 채 끝난다).
  단계 시작·판정 때 감사 줄을 남긴다.
- simulate(가짜 모드)는 그 단계에서 실행기를 부르지 않고 처리기가 결과를 만든다. 비율 단계는
  "설정 rps × 비율 × 지난 시간"으로 카운터를 만든다. ERROR_RATE 는 기준값 뒤 5xx 를 허용 + 1 로,
  TARGET_NOT_RECEIVING 은 도착을 고정하고 출발을 100% 로, LOW_TRAFFIC 은 둘 다 고정한다. HEALTH
  는 헬스 실패, TRAFFIC 은 그 단계 APPLY 실패, DATA_LAG 는 지연 30초다. DB 쓰기 전환 단계는
  DB_CUTOVER_FAIL(종료 1)·DB_CUTOVER_PARTIAL(종료 2)·DB_CUTOVER_TIMEOUT(끝나지 않음)·
  DATA_INTEGRITY(종료 0, 누락 1건)이다. 저장된 옛 LATENCY 는 아무
  실패도 만들지 않는다. 로그·단계 결과에는 만든 값이 그대로 남는다.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.actions import AuditAction, AuditComponent
from app.audit.lifecycle import record_migration_db_cutover, record_migration_plan_finished
from app.core.clock import Clock
from app.core.config import Settings
from app.enums import (
    AuditResult,
    CutoverKind,
    HealthCheckResult,
    MigrationErrorCode,
    MigrationJudgeReason,
    MigrationLogCode,
    MigrationPlanMode,
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
    CountsResult,
    CutoverRun,
    CutoverRunNotFound,
    CutoverRunResult,
    CutoverRunUnavailable,
    DataSyncLag,
    DbCutoverFinished,
    DbCutoverJobAbsent,
    DbCutoverJobFound,
    DbCutoverJobLookup,
    DbCutoverJobUnknown,
    DbCutoverLost,
    DbCutoverPollResult,
    DbCutoverRequest,
    DbCutoverRunning,
    DbCutoverStarted,
    DbCutoverStartFailed,
    HealthHttpError,
    HealthOk,
    HealthProbeResult,
    Metrics,
    RequestCounts,
    TrafficApplied,
    TrafficResult,
    WeightedTarget,
)
from app.models import Environment, MigrationPlan, Service, ServiceEnvironment
from app.models.service import DEFAULT_HEALTH_CHECK_PATH
from app.repositories.migration_plan_repository import MigrationPlanRepository
from app.repositories.service_environment_repository import ServiceEnvironmentRepository
from app.repositories.service_repository import ServiceRepository
from app.services.deploy_support import SNAPSHOT_HEALTH_CHECK_PATH, public_url
from app.services.job_lease import JobLease, JobOutcome
from app.services.migration_counting import (
    CounterValue,
    StepCounting,
    error_rate_percent,
    judge_counting,
)
from app.services.migration_db_cutover import (
    CUTOVER_ALLOW_PROMOTE,
    CUTOVER_EXIT_CODE,
    CUTOVER_FINISHED,
    CUTOVER_JOB_KIND,
    CUTOVER_JOB_NAME,
    CUTOVER_LOST,
    CUTOVER_POLL_ERRORS,
    CUTOVER_REASON,
    CUTOVER_RUN,
    CUTOVER_START_CONFIRMED,
    CUTOVER_START_FAILURE,
    CUTOVER_START_UNCERTAIN,
    CUTOVER_WRITER,
    EXIT_DONE,
    EXIT_NO_CHANGE,
    EXIT_PARTIAL,
    REASON_START_UNCONFIRMED,
    REASON_TARGET_IS_WRITER,
    WRITER_SOURCE,
    WRITER_TARGET,
    WRITER_UNKNOWN,
    CutoverRecovery,
    CutoverVerdict,
    cutover_log_data,
    db_cutover_request,
    judge_db_cutover,
    run_to_json,
)
from app.services.migration_plan_log import MigrationPlanLogWriter
from app.services.migration_plan_support import (
    CONDITION_MAX_ERROR_RATE,
    CONDITION_MAX_REPLICATION_LAG,
    RESULT_COUNTING,
    RESULT_CUTOVER,
    RESULT_FAILURE_CODE,
    RESULT_FINISHED_AT,
    RESULT_JUDGE_REASON,
    RESULT_METRICS,
    RESULT_STARTED_AT,
    RESULT_STATUS,
    SIMULATE_FAIL_AT_STEP,
    SIMULATE_REASON,
    STEP_MAX_WAIT_SECONDS,
    STEP_TARGET_WEIGHT,
    MigrationTargets,
    fingerprint_changes,
    iso,
    last_step,
    load_migration_targets,
    needs_promote_ack,
    read_fingerprint,
    skip_pending_steps,
    step_kind,
    step_required_requests,
    with_step_result,
)
from app.services.security_gate import SecurityGate, gate_log_data

logger = logging.getLogger(__name__)

# 한 번의 advance 에서 이어 갈 동작 수 상한(정상 경로는 10개 안쪽이다).
MAX_ACTIONS_PER_RUN = 32
# simulate 가 만드는 판정 값(실행기를 부르지 않는다).
SIMULATED_REPLICATION_LAG_SECONDS = 30.0
SIMULATED_HEALTH_STATUS_CODE = 503
# 이 simulate 이유면 그 비율 단계의 카운터를 처리기가 만든다.
_COUNT_SIMULATIONS = frozenset(
    {
        MigrationSimulateReason.ERROR_RATE,
        MigrationSimulateReason.TARGET_NOT_RECEIVING,
        MigrationSimulateReason.LOW_TRAFFIC,
        MigrationSimulateReason.HEALTH,
    }
)
# 저장된 옛 계획을 읽을 수 있게 값만 남긴 simulate 이유(아무 실패도 만들지 않는다).
_IGNORED_SIMULATIONS = frozenset({MigrationSimulateReason.LATENCY})
# DB 쓰기 전환 단계 simulate: Job 없이 처리기가 Job 끝(종료 코드)을 만든다. None = 끝나지 않음.
_CUTOVER_SIMULATED_EXIT: dict[MigrationSimulateReason, int | None] = {
    MigrationSimulateReason.DB_CUTOVER_FAIL: EXIT_NO_CHANGE,
    MigrationSimulateReason.DB_CUTOVER_PARTIAL: EXIT_PARTIAL,
    MigrationSimulateReason.DB_CUTOVER_TIMEOUT: None,
    MigrationSimulateReason.DATA_INTEGRITY: EXIT_DONE,
}
SIMULATED_CUTOVER_JOB_PREFIX = "simulated-cutover"
# 컷오버 Job 상태 조회가 이만큼 연속으로 예외를 내면(토큰 파일 사라짐 등) 상한을 기다리지 않고
# DB_CUTOVER_UNKNOWN(되돌리지 않음)으로 끝낸다.
MAX_CUTOVER_POLL_ERRORS = 5
# 실행기 호출이 예외를 냈을 때 단계 결과에 적는 고정 코드(원문은 남기지 않는다).
START_RAISED = "CUTOVER_CALL_RAISED"
# 원본 DB 가 죽은 재난 긴급 이전(EMERGENCY + DB 쓰기 전환)에서 데이터 확인을 건너뛴 이유.
DATA_SYNC_SKIPPED_EMERGENCY = "EMERGENCY_SOURCE_UNREACHABLE"
SIMULATED_MISSING_ROWS = 1

type _LogLine = tuple[MigrationLogCode, dict[str, Any]]

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
        security_gate: SecurityGate | None = None,
    ) -> None:
        self._session = session
        self._lease = lease
        self._settings = settings
        self._clock = clock
        self._executors = executors
        self._security_gate = security_gate
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
        decision = (
            await self._security_gate.evaluate_plan(self._plan_id)
            if self._security_gate is not None
            else None
        )
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
                    await self._record_finished(plan, MigrationPlanStatus.CANCELLED)
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
                    await self._record_finished(plan, MigrationPlanStatus.STALE)
                self._report("migration plan stale at start", plan)
                return self._closed()
            if decision is not None and decision.is_blocked:
                await self._fail(
                    plan,
                    MigrationErrorCode.SECURITY_GATE_BLOCKED,
                    now,
                    preceding_lines=[
                        (MigrationLogCode.SECURITY_GATE_BLOCKED, gate_log_data(decision))
                    ],
                )
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
                if decision is not None:
                    await self._log.append(
                        plan,
                        MigrationLogCode.SECURITY_GATE_PASSED,
                        now,
                        data=gate_log_data(decision),
                    )
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
            # 컷오버 Job 을 요청한 뒤에는 판정할 때까지 중단·임대 상실로 되돌리지 않는다.
            if not _cutover_in_flight(plan):
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
        if kind is MigrationStepKind.DB_CUTOVER:
            match phase:
                case MigrationStepPhase.APPLY:
                    return await self._cutover_apply(plan)
                case MigrationStepPhase.OBSERVE:
                    return await self._cutover_observe(plan)
                case _:
                    return await self._cutover_judge(plan)
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
                await self._record_finished(
                    plan, MigrationPlanStatus.FAILED, MigrationErrorCode.PREFLIGHT_FAILED
                )
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
            if await self._plans.transition(
                plan.id,
                MigrationPlanStatus.RUNNING,
                self._counting_start(plan, now),
                _at_conditions(index, MigrationStepPhase.APPLY),
            ):
                self._record_weights(locked, 100 - target_weight, target_weight, result)
                await self._session.flush()
                await self._log.append(
                    plan, MigrationLogCode.WEIGHT_APPLIED, now, step=index, data=weights_data
                )
        self._report("migration weights applied", plan, step=index)
        # 같은 실행에서 OBSERVE 첫 읽기로 기준값을 잡는다.
        return None

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

    def _counting_start(self, plan: MigrationPlan, now: datetime) -> dict[str, Any]:
        """지금 비율 단계의 건수 세기를 now 부터 시작하는 계획 행 변경값(OBSERVE, 새 센 값).

        최대 대기 끝(observe_until) = now + 계획 행의 observe_seconds(= maxWaitSeconds).
        """
        index = plan.current_step
        counting = StepCounting.start(
            step_required_requests(
                plan.steps, index, self._settings.migration_required_requests_default
            ),
            float(plan.rollback_conditions[CONDITION_MAX_ERROR_RATE]),
            now,
        )
        return {
            "step_phase": MigrationStepPhase.OBSERVE,
            "phase_started_at": now,
            "observe_until": now + timedelta(seconds=plan.observe_seconds),
            "step_results": with_step_result(
                plan.step_results,
                index,
                **{RESULT_COUNTING: counting.to_json(), RESULT_JUDGE_REASON: None},
            ),
            "updated_at": now,
        }

    async def _restart_counting(
        self, plan: MigrationPlan, phase: MigrationStepPhase
    ) -> JobOutcome | None:
        """센 값이 없는 비율 단계(8-1 전에 만든 계획)는 그 자리에서 세기를 다시 시작한다."""
        index = plan.current_step
        now = self._clock.now()
        async with self._session.begin():
            locked = await self._lock()
            plan = locked.plan
            if not _is_at(plan, MigrationPlanStatus.RUNNING, index, phase):
                return None
            await self._plans.transition(
                plan.id,
                MigrationPlanStatus.RUNNING,
                self._counting_start(plan, now),
                _at_conditions(index, phase),
            )
        self._report("migration counting restarted", plan, step=index)
        return None

    async def _observe(self, plan: MigrationPlan) -> JobOutcome | None:
        """건수 세기 한 번: 출발·도착 카운터를 읽어(트랜잭션 밖) 센 값을 쌓고 결론을 낸다.

        5xx 가 허용을 넘으면 바로 롤백, 필요 건수를 채우면 JUDGE(COUNT_REACHED), 최대 대기가 끝나면
        마감 판정(실패면 롤백, 통과면 JUDGE(LOW_TRAFFIC)), 아니면 다음 읽기 시각으로 돌려놓는다.
        """
        index = plan.current_step
        stored = plan.step_results[index].get(RESULT_COUNTING)
        if stored is None or plan.observe_until is None:
            return await self._restart_counting(plan, MigrationStepPhase.OBSERVE)
        counting = StepCounting.from_json(stored)
        deadline = plan.observe_until
        weight = int(plan.steps[index][STEP_TARGET_WEIGHT])
        simulated = _simulated_failure(plan, index)
        targets: MigrationTargets | None = None
        source: CounterValue | None
        target: CounterValue | None
        if simulated is not None and simulated in _COUNT_SIMULATIONS:
            source, target = _simulated_counts(
                simulated,
                weight,
                counting,
                self._clock.now(),
                self._settings.migration_expected_rps,
            )
        else:
            targets = await self._targets(plan)
            await self._lease.extend()
            source = _counter_value(await self._executors.metrics.read_counts(targets.source))
            target = _counter_value(await self._executors.metrics.read_counts(targets.target))
        now = self._clock.now()
        counting, resets = counting.observe(source, target, now)
        verdict = judge_counting(
            counting,
            now=now,
            deadline=deadline,
            target_weight=weight,
            stale=timedelta(seconds=self._settings.migration_counts_stale_seconds),
        )
        p95_ms = (
            await self._read_p95(targets)
            if verdict.failure is not None and targets is not None
            else None
        )
        reset_lines: list[_LogLine] = [
            (MigrationLogCode.COUNTERS_RESET, reset.log_data()) for reset in resets
        ]
        async with self._session.begin():
            locked = await self._lock()
            plan = locked.plan
            if not _is_at(plan, MigrationPlanStatus.RUNNING, index, MigrationStepPhase.OBSERVE):
                return None
            results = with_step_result(
                plan.step_results, index, **{RESULT_COUNTING: counting.to_json()}
            )
            if verdict.failure is not None:
                metrics_json = _shift_metrics_json(counting, p95_ms=p95_ms)
                await self._fail_step(
                    plan,
                    index,
                    metrics_json,
                    verdict.failure,
                    MigrationLogCode.JUDGE_FAILED,
                    now,
                    base_results=results,
                    log_data=_shift_log_data(metrics_json, counting),
                    preceding_lines=reset_lines,
                )
            else:
                changes: dict[str, Any] = {"step_results": results, "updated_at": now}
                if verdict.judge_reason is not None:
                    changes |= {
                        "step_phase": MigrationStepPhase.JUDGE,
                        "phase_started_at": now,
                        "step_results": with_step_result(
                            results, index, **{RESULT_JUDGE_REASON: verdict.judge_reason.value}
                        ),
                    }
                if await self._plans.transition(
                    plan.id,
                    MigrationPlanStatus.RUNNING,
                    changes,
                    _at_conditions(index, MigrationStepPhase.OBSERVE),
                ):
                    await self._append_lines(plan, reset_lines, now, index)
        if verdict.failure is not None:
            self._report(
                "migration step judged unhealthy", plan, step=index, error_code=verdict.failure
            )
            return None
        if verdict.judge_reason is not None:
            return None
        poll = timedelta(seconds=self._settings.migration_count_poll_seconds)
        return JobOutcome.again(min(now + poll, deadline))

    async def _judge_shift(self, plan: MigrationPlan) -> JobOutcome | None:
        """세기를 끝낸 비율 단계를 헬스체크로 판정한다. 카운터는 다시 읽지 않고 저장된 값을 쓴다."""
        index = plan.current_step
        result = plan.step_results[index]
        stored = result.get(RESULT_COUNTING)
        reason_value = result.get(RESULT_JUDGE_REASON)
        if stored is None or reason_value is None:
            return await self._restart_counting(plan, MigrationStepPhase.JUDGE)
        counting = StepCounting.from_json(stored)
        is_low_traffic = MigrationJudgeReason(reason_value) is MigrationJudgeReason.LOW_TRAFFIC
        simulated = _simulated_failure(plan, index)
        health: HealthProbeResult
        p95_ms: float | None = None
        if simulated is MigrationSimulateReason.HEALTH:
            health = HealthHttpError(status_code=SIMULATED_HEALTH_STATUS_CODE)
        elif simulated is not None and simulated in _COUNT_SIMULATIONS:
            health = HealthOk(status_code=200)
        else:
            targets = await self._targets(plan)
            await self._lease.extend()
            health = await self._probe(targets)
            p95_ms = await self._read_p95(targets)
        metrics_json = _shift_metrics_json(
            counting, health=health, p95_ms=p95_ms, is_low_traffic=is_low_traffic
        )
        log_data = _shift_log_data(metrics_json, counting)
        now = self._clock.now()
        async with self._session.begin():
            locked = await self._lock()
            plan = locked.plan
            if not _is_at(plan, MigrationPlanStatus.RUNNING, index, MigrationStepPhase.JUDGE):
                return None
            if isinstance(health, HealthOk):
                outcome = await self._pass_step(
                    plan, metrics_json, MigrationLogCode.JUDGE_PASSED, now, log_data=log_data
                )
                if outcome is not None:
                    self._report("migration succeeded", plan)
                return outcome
            await self._fail_step(
                plan,
                index,
                metrics_json,
                MigrationErrorCode.HEALTH_CHECK_FAILED,
                MigrationLogCode.JUDGE_FAILED,
                now,
                log_data=log_data,
            )
        self._report(
            "migration step judged unhealthy",
            plan,
            step=index,
            error_code=MigrationErrorCode.HEALTH_CHECK_FAILED,
        )
        return None

    async def _read_p95(self, targets: MigrationTargets) -> float | None:
        """단계를 끝낼 때 도착의 최근 구간 p95 를 한 번 읽는다(참고 값, 판정에 쓰지 않는다)."""
        until = self._clock.now()
        since = until - timedelta(seconds=self._settings.migration_p95_reference_window_seconds)
        result = await self._executors.metrics.read(targets.target, since, until)
        return result.p95_ms if isinstance(result, Metrics) else None

    async def _check_data_sync(self, plan: MigrationPlan) -> JobOutcome | None:
        index = plan.current_step
        max_lag = float(plan.rollback_conditions[CONDITION_MAX_REPLICATION_LAG])
        reason: str | None = None
        if _simulated_failure(plan, index) is MigrationSimulateReason.DATA_LAG:
            lag: float | None = SIMULATED_REPLICATION_LAG_SECONDS
        else:
            targets = await self._targets(plan)
            await self._lease.extend()
            result = await self._executors.data_sync.replication_lag(targets.source, targets.target)
            if isinstance(result, DataSyncLag):
                lag, reason = result.seconds, result.reason
            else:
                lag = None
        failure: MigrationErrorCode | None = None
        skipped = False
        if lag is None:
            # 재난 긴급 이전 + DB 쓰기 전환(승격 허용)은 원본 DB 가 죽은 경우가 본래
            # 목적이라 지연을 못 재도 건너뛰고 경고만 남긴다. 승격은 승인 때 동의했다
            # (acknowledgePromote). DB 쓰기 전환이 없는 긴급 이전과 다른 계획은 그대로 실패한다.
            if _emergency_with_cutover(plan):
                skipped = True
            else:
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
            if skipped:
                outcome = await self._pass_step(
                    plan,
                    metrics_json,
                    MigrationLogCode.DATA_SYNC_SKIPPED,
                    now,
                    log_data={"reason": DATA_SYNC_SKIPPED_EMERGENCY},
                )
                self._report("migration data sync check skipped", plan, step=index)
                return outcome
            if failure is None:
                return await self._pass_step(
                    plan,
                    metrics_json,
                    MigrationLogCode.DATA_SYNC_OK,
                    now,
                    log_data=(
                        None if reason is None else {**_numbers(metrics_json), "reason": reason}
                    ),
                )
            await self._fail_step(
                plan, index, metrics_json, failure, MigrationLogCode.DATA_SYNC_FAILED, now
            )
        self._report("migration data sync check failed", plan, step=index, error_code=failure)
        return None

    # --- DB 쓰기 전환 (API 서버 17) ---

    async def _cutover_request(self, plan: MigrationPlan) -> DbCutoverRequest:
        async with self._session.begin():
            target = await self._connections.find_by_id(plan.target_service_environment_id)
            assert target is not None
            environment = await self._session.get_one(Environment, target.environment_id)
            return db_cutover_request(
                plan.id,
                plan.mode,
                environment.id,
                environment.kind,
                promote_consented=needs_promote_ack(plan.steps),
            )

    async def _cutover_apply(self, plan: MigrationPlan) -> JobOutcome | None:
        """요청 시각 저장 → Job 만들기(같은 이름이면 그 Job) → Job 이름·상한 저장(OBSERVE).

        생성 응답을 잃은 적이 있으면(만들었을 수 있음) 그 사실을 단계 결과에 적어 두고, 다시
        시도하다 끝내 만들었다는 답을 못 받으면 원복 전에 같은 이름 Job·이 계획 결과 행을 확인한다
        (_settle_uncertain_start). 분명한 거절만 곧바로 트래픽 원복이다.
        """
        index = plan.current_step
        request = await self._cutover_request(plan)
        timeout = timedelta(seconds=int(plan.steps[index][STEP_MAX_WAIT_SECONDS]))
        simulated = _simulated_failure(plan, index)
        stored = dict(plan.step_results[index].get(RESULT_CUTOVER) or {})
        uncertain = bool(stored.get(CUTOVER_START_UNCERTAIN))
        if simulated in _CUTOVER_SIMULATED_EXIT:
            started: DbCutoverStarted | DbCutoverStartFailed = DbCutoverStarted(
                f"{SIMULATED_CUTOVER_JOB_PREFIX}-{plan.id.hex[:8]}"
            )
        else:
            requested_at = plan.traffic_requested_at
            if requested_at is None:
                requested_at = self._clock.now()
                async with self._session.begin():
                    await self._lock()
                    await self._plans.transition(
                        plan.id,
                        MigrationPlanStatus.RUNNING,
                        {"traffic_requested_at": requested_at, "updated_at": requested_at},
                        _at_conditions(index, MigrationStepPhase.APPLY)
                        + [MigrationPlan.traffic_requested_at.is_(None)],
                    )
            await self._lease.extend()
            try:
                started = await self._executors.db_cutover.start(request)
            except Exception:
                logger.exception(
                    "migration db cutover start raised",
                    extra={"action": "create_cutover_job", "migration_plan_id": str(plan.id)},
                )
                started = DbCutoverStartFailed(START_RAISED, retryable=True)
            if isinstance(started, DbCutoverStartFailed) and started.maybe_created:
                uncertain = True
            if uncertain and not stored.get(CUTOVER_START_UNCERTAIN):
                stored[CUTOVER_START_UNCERTAIN] = True
                await self._store_cutover(plan, index, MigrationStepPhase.APPLY, stored)
            if (
                isinstance(started, DbCutoverStartFailed)
                and started.retryable
                and self._clock.now() < requested_at + timeout
            ):
                poll = timedelta(seconds=self._settings.migration_db_cutover_poll_seconds)
                return JobOutcome.again(self._clock.now() + poll)
        cutover: dict[str, Any] = {
            CUTOVER_JOB_NAME: started.job_name if isinstance(started, DbCutoverStarted) else None,
            CUTOVER_JOB_KIND: request.job_kind.value,
            CUTOVER_ALLOW_PROMOTE: request.allow_promote,
        }
        if uncertain:
            cutover[CUTOVER_START_UNCERTAIN] = True
        if isinstance(started, DbCutoverStartFailed):
            cutover[CUTOVER_START_FAILURE] = started.code
            if not uncertain:
                verdict = CutoverVerdict(
                    MigrationErrorCode.DB_CUTOVER_FAILED,
                    CutoverRecovery.ROLLBACK_TRAFFIC,
                    None,
                    None,
                )
                return await self._finish_cutover(
                    plan, index, MigrationStepPhase.APPLY, cutover, verdict
                )
            settled = await self._settle_uncertain_start(plan, index, request, cutover)
            if settled is not None:
                return settled
        return await self._enter_observe(plan, index, request, cutover, timeout)

    async def _settle_uncertain_start(
        self,
        plan: MigrationPlan,
        index: int,
        request: DbCutoverRequest,
        cutover: dict[str, Any],
    ) -> JobOutcome | None:
        """만들었는지 모르는 Job 을 정리한다. None 이면 Job 을 다시 찾았다(cutover 에 이름을 적음).

        같은 이름 Job 있음 → 기다리기(재부착). 없음(404) → 이 계획 결과 행도 없을 때만 FAILED·트래픽
        원복, 행이 있으면 판정(JUDGE)으로. 어느 쪽이든 확인하지 못하면
        DB_CUTOVER_UNKNOWN(되돌리지 않음).
        """
        await self._lease.extend()
        lookup: DbCutoverJobLookup
        try:
            lookup = await self._executors.db_cutover.find_job(request)
        except Exception:
            lookup = DbCutoverJobUnknown(START_RAISED)
        if isinstance(lookup, DbCutoverJobFound):
            cutover[CUTOVER_JOB_NAME] = lookup.job_name
            cutover[CUTOVER_START_CONFIRMED] = True
            cutover.pop(CUTOVER_START_FAILURE, None)
            return None
        run_result: CutoverRunResult = CutoverRunUnavailable(START_RAISED)
        if isinstance(lookup, DbCutoverJobAbsent):
            run_result = await self._find_cutover_run(request)
            if isinstance(run_result, CutoverRunNotFound):
                verdict = CutoverVerdict(
                    MigrationErrorCode.DB_CUTOVER_FAILED,
                    CutoverRecovery.ROLLBACK_TRAFFIC,
                    None,
                    None,
                )
                return await self._finish_cutover(
                    plan, index, MigrationStepPhase.APPLY, cutover, verdict
                )
            if isinstance(run_result, CutoverRun):
                # Job 은 없지만 이 계획 행이 있다: 전환이 돌았다. 사라진 Job 으로 판정한다.
                cutover[CUTOVER_FINISHED] = False
                cutover[CUTOVER_LOST] = True
                await self._move_cutover(
                    plan, index, MigrationStepPhase.APPLY, MigrationStepPhase.JUDGE, cutover
                )
                return JobOutcome.again(self._clock.now())
        verdict = CutoverVerdict(
            MigrationErrorCode.DB_CUTOVER_UNKNOWN,
            CutoverRecovery.MANUAL,
            REASON_START_UNCONFIRMED,
            None,
        )
        cutover[CUTOVER_REASON] = REASON_START_UNCONFIRMED
        return await self._finish_cutover(plan, index, MigrationStepPhase.APPLY, cutover, verdict)

    async def _enter_observe(
        self,
        plan: MigrationPlan,
        index: int,
        request: DbCutoverRequest,
        cutover: dict[str, Any],
        wait: timedelta,
    ) -> JobOutcome | None:
        now = self._clock.now()
        async with self._session.begin():
            locked = await self._lock()
            plan = locked.plan
            if not _is_at(plan, MigrationPlanStatus.RUNNING, index, MigrationStepPhase.APPLY):
                return None
            if await self._plans.transition(
                plan.id,
                MigrationPlanStatus.RUNNING,
                {
                    "step_phase": MigrationStepPhase.OBSERVE,
                    "phase_started_at": now,
                    "observe_until": now + wait,
                    "step_results": with_step_result(
                        plan.step_results, index, **{RESULT_CUTOVER: cutover}
                    ),
                    "updated_at": now,
                },
                _at_conditions(index, MigrationStepPhase.APPLY),
            ):
                data = cutover_log_data(cutover) | {CUTOVER_ALLOW_PROMOTE: request.allow_promote}
                await self._log.append(
                    plan, MigrationLogCode.DB_CUTOVER_JOB_CREATED, now, step=index, data=data
                )
                await record_migration_db_cutover(
                    self._session,
                    self._clock,
                    plan,
                    action=AuditAction.MIGRATION_DB_CUTOVER_STARTED,
                    result=AuditResult.SUCCEEDED,
                    details=data,
                )
        self._report("migration db cutover job created", plan, step=index)
        return None

    async def _store_cutover(
        self,
        plan: MigrationPlan,
        index: int,
        phase: MigrationStepPhase,
        cutover: dict[str, Any],
    ) -> None:
        """위치는 그대로 두고 단계 결과의 cutover 칸만 적는다(그 위치일 때만)."""
        await self._move_cutover(plan, index, phase, phase, cutover)

    async def _move_cutover(
        self,
        plan: MigrationPlan,
        index: int,
        phase: MigrationStepPhase,
        next_phase: MigrationStepPhase,
        cutover: dict[str, Any],
    ) -> None:
        now = self._clock.now()
        async with self._session.begin():
            locked = await self._lock()
            plan = locked.plan
            if not _is_at(plan, MigrationPlanStatus.RUNNING, index, phase):
                return
            values: dict[str, Any] = {
                "step_results": with_step_result(
                    plan.step_results, index, **{RESULT_CUTOVER: cutover}
                ),
                "updated_at": now,
            }
            if next_phase is not phase:
                values |= {"step_phase": next_phase, "phase_started_at": now}
            await self._plans.transition(
                plan.id, MigrationPlanStatus.RUNNING, values, _at_conditions(index, phase)
            )

    async def _find_cutover_run(self, request: DbCutoverRequest) -> CutoverRunResult:
        await self._lease.extend()
        try:
            return await self._executors.db_cutover.find_run(request)
        except Exception:
            return CutoverRunUnavailable(START_RAISED)

    async def _current_writer(self, plan: MigrationPlan) -> str:
        """지금 쓰기 담당을 데이터 확인 실행기로 본다(SOURCE·TARGET·UNKNOWN).

        출발 pg_stat_replication 에 도착 구독의 walsender 가 있으면 출발이 발행자(쓰기 담당)이고,
        도착이 쓰기 담당이라는 근거가 있으면 TARGET_IS_WRITER 다.
        그 밖(읽기 실패·근거 없음)은 UNKNOWN.
        """
        try:
            targets = await self._targets(plan)
            await self._lease.extend()
            result = await self._executors.data_sync.replication_lag(targets.source, targets.target)
        except Exception:
            return WRITER_UNKNOWN
        if not isinstance(result, DataSyncLag):
            return WRITER_UNKNOWN
        if result.reason == REASON_TARGET_IS_WRITER:  # 데이터 확인 실행기의 TARGET_IS_WRITER
            return WRITER_TARGET
        return WRITER_SOURCE if result.reason is None else WRITER_UNKNOWN

    async def _cutover_observe(self, plan: MigrationPlan) -> JobOutcome | None:
        """Job 상태 한 번. 끝났거나 사라졌거나 상한을 넘으면 JUDGE, 아니면 다음 확인 시각.

        상태 조회의 예외(토큰 파일 사라짐 등)는 흡수해 연속 횟수를 적는다. 그래서 시계 기준
        상한은 언제나 돌고, 연속 MAX_CUTOVER_POLL_ERRORS 번이면 상한을 기다리지 않고
        판정(UNKNOWN)으로 간다.
        """
        index = plan.current_step
        cutover = dict(plan.step_results[index].get(RESULT_CUTOVER) or {})
        deadline = plan.observe_until
        assert deadline is not None
        simulated = _simulated_failure(plan, index)
        result: DbCutoverPollResult | None
        if simulated in _CUTOVER_SIMULATED_EXIT:
            exit_code = _CUTOVER_SIMULATED_EXIT[simulated]
            result = DbCutoverRunning() if exit_code is None else DbCutoverFinished(exit_code)
        else:
            await self._lease.extend()
            try:
                result = await self._executors.db_cutover.poll(str(cutover[CUTOVER_JOB_NAME]))
            except Exception:
                logger.warning(
                    "migration db cutover poll raised",
                    extra={"action": "poll_cutover_job", "migration_plan_id": str(plan.id)},
                    exc_info=True,
                )
                result = None
        now = self._clock.now()
        poll = timedelta(seconds=self._settings.migration_db_cutover_poll_seconds)
        errors = int(cutover.get(CUTOVER_POLL_ERRORS) or 0)
        if result is None:
            errors += 1
            cutover[CUTOVER_POLL_ERRORS] = errors
            if errors < MAX_CUTOVER_POLL_ERRORS and now < deadline:
                await self._store_cutover(plan, index, MigrationStepPhase.OBSERVE, cutover)
                return JobOutcome.again(min(now + poll, deadline))
            # 상한을 넘었거나 연속 실패가 상한에 닿았다: 판정으로(행이 없으면 되돌리지 않음).
            result = DbCutoverRunning()
        elif isinstance(result, DbCutoverRunning) and now < deadline:
            if errors:
                cutover[CUTOVER_POLL_ERRORS] = 0
                await self._store_cutover(plan, index, MigrationStepPhase.OBSERVE, cutover)
            return JobOutcome.again(min(now + poll, deadline))
        elif errors:
            cutover[CUTOVER_POLL_ERRORS] = 0
        cutover[CUTOVER_FINISHED] = isinstance(result, DbCutoverFinished)
        cutover[CUTOVER_LOST] = isinstance(result, DbCutoverLost)
        if isinstance(result, DbCutoverFinished):
            cutover[CUTOVER_EXIT_CODE] = result.exit_code
            cutover[CUTOVER_REASON] = result.reason
        async with self._session.begin():
            locked = await self._lock()
            plan = locked.plan
            if not _is_at(plan, MigrationPlanStatus.RUNNING, index, MigrationStepPhase.OBSERVE):
                return None
            if await self._plans.transition(
                plan.id,
                MigrationPlanStatus.RUNNING,
                {
                    "step_phase": MigrationStepPhase.JUDGE,
                    "phase_started_at": now,
                    "step_results": with_step_result(
                        plan.step_results, index, **{RESULT_CUTOVER: cutover}
                    ),
                    "updated_at": now,
                },
                _at_conditions(index, MigrationStepPhase.OBSERVE),
            ) and not isinstance(result, DbCutoverRunning):
                await self._log.append(
                    plan,
                    MigrationLogCode.DB_CUTOVER_JOB_FINISHED,
                    now,
                    step=index,
                    data=cutover_log_data(cutover) | {CUTOVER_LOST: cutover[CUTOVER_LOST]},
                )
        return None

    async def _cutover_judge(self, plan: MigrationPlan) -> JobOutcome | None:
        """이 계획 id 의 결과 행을 읽어 판정한다. 종료 1 이면 지금 쓰기 담당도 확인한다."""
        index = plan.current_step
        cutover = dict(plan.step_results[index].get(RESULT_CUTOVER) or {})
        finished = bool(cutover.get(CUTOVER_FINISHED))
        exit_code = cutover.get(CUTOVER_EXIT_CODE)
        simulated = _simulated_failure(plan, index)
        run_result: CutoverRunResult | None = None
        writer: str | None = None
        if simulated in _CUTOVER_SIMULATED_EXIT:
            run_result = (
                _simulated_integrity_run(self._clock.now(), plan)
                if simulated is MigrationSimulateReason.DATA_INTEGRITY
                else CutoverRunNotFound()
            )
            if finished and exit_code == EXIT_NO_CHANGE:
                writer = WRITER_SOURCE
        else:
            request = await self._cutover_request(plan)
            run_result = await self._find_cutover_run(request)
            if finished and exit_code == EXIT_NO_CHANGE and not isinstance(run_result, CutoverRun):
                writer = await self._current_writer(plan)
                cutover[CUTOVER_WRITER] = writer
        poll_errors = int(cutover.get(CUTOVER_POLL_ERRORS) or 0)
        verdict = judge_db_cutover(
            exit_code=exit_code,
            reason=cutover.get(CUTOVER_REASON),
            finished=finished,
            lost=bool(cutover.get(CUTOVER_LOST)),
            timed_out=not finished and not cutover.get(CUTOVER_LOST),
            run_result=run_result,
            writer=writer,
            poll_failed=poll_errors >= MAX_CUTOVER_POLL_ERRORS,
        )
        if verdict.reason is not None:
            cutover[CUTOVER_REASON] = verdict.reason
        if verdict.run is not None:
            cutover[CUTOVER_RUN] = run_to_json(verdict.run)
        return await self._finish_cutover(plan, index, MigrationStepPhase.JUDGE, cutover, verdict)

    async def _finish_cutover(
        self,
        plan: MigrationPlan,
        index: int,
        phase: MigrationStepPhase,
        cutover: dict[str, Any],
        verdict: CutoverVerdict,
    ) -> JobOutcome | None:
        """판정을 적는다: 통과면 다음 단계(마지막이면 SUCCEEDED), 실패면 트래픽 롤백·사람 확인."""
        now = self._clock.now()
        log_data = cutover_log_data(cutover)
        metrics_json = _metrics_json()
        outcome: JobOutcome | None = None
        async with self._session.begin():
            locked = await self._lock()
            plan = locked.plan
            if not _is_at(plan, MigrationPlanStatus.RUNNING, index, phase):
                return None
            results = with_step_result(plan.step_results, index, **{RESULT_CUTOVER: cutover})
            failure = verdict.failure
            if failure is None:
                if phase is not MigrationStepPhase.JUDGE:
                    return None
                outcome = await self._pass_step(
                    plan,
                    metrics_json,
                    MigrationLogCode.DB_CUTOVER_OK,
                    now,
                    log_data=log_data,
                    base_results=results,
                )
            elif verdict.recovery is CutoverRecovery.ROLLBACK_TRAFFIC:
                await self._fail_step(
                    plan,
                    index,
                    metrics_json,
                    failure,
                    MigrationLogCode.DB_CUTOVER_FAILED,
                    now,
                    base_results=results,
                    log_data=log_data,
                    conditions=[MigrationPlan.step_phase == phase],
                )
            else:
                await self._finish_manual(plan, index, phase, results, failure, log_data, now)
                outcome = self._closed()
            await record_migration_db_cutover(
                self._session,
                self._clock,
                plan,
                action=AuditAction.MIGRATION_DB_CUTOVER_FINISHED,
                result=AuditResult.SUCCEEDED if failure is None else AuditResult.FAILED,
                details=log_data | ({"errorCode": failure.value} if failure is not None else {}),
            )
        if failure is None:
            self._report("migration db cutover passed", plan, step=index)
            if outcome is not None:
                self._report("migration succeeded", plan)
        else:
            self._report("migration db cutover failed", plan, step=index, error_code=failure)
        return outcome

    async def _finish_manual(
        self,
        plan: MigrationPlan,
        index: int,
        phase: MigrationStepPhase,
        results: list[dict[str, Any]],
        failure: MigrationErrorCode,
        log_data: dict[str, Any],
        now: datetime,
    ) -> None:
        """트래픽도 DB 도 자동으로 되돌리지 않고 FAILED(rollbackStatus SKIPPED)로 끝낸다."""
        results = with_step_result(
            results,
            index,
            **{
                RESULT_STATUS: MigrationStepStatus.FAILED.value,
                RESULT_FINISHED_AT: iso(now),
                RESULT_METRICS: _metrics_json(),
                RESULT_FAILURE_CODE: failure.value,
            },
        )
        if await self._plans.transition(
            plan.id,
            MigrationPlanStatus.RUNNING,
            {
                "status": MigrationPlanStatus.FAILED,
                "error_code": failure,
                "rollback_status": MigrationRollbackStatus.SKIPPED,
                "step_phase": None,
                "observe_until": None,
                "step_results": skip_pending_steps(results),
                "finished_at": now,
                "updated_at": now,
            },
            _at_conditions(index, phase),
        ):
            await self._log.append(
                plan,
                MigrationLogCode.DB_CUTOVER_FAILED,
                now,
                step=index,
                data=log_data,
                error_code=failure,
            )
            await self._log.append(
                plan, MigrationLogCode.DB_CUTOVER_MANUAL_REQUIRED, now, step=index, data=log_data
            )
            await self._log.append(plan, MigrationLogCode.FAILED, now, error_code=failure)
            await self._record_finished(plan, MigrationPlanStatus.FAILED, failure)

    async def _pass_step(
        self,
        plan: MigrationPlan,
        metrics_json: dict[str, Any],
        code: MigrationLogCode,
        now: datetime,
        *,
        log_data: dict[str, Any] | None = None,
        base_results: list[dict[str, Any]] | None = None,
    ) -> JobOutcome | None:
        """잠근 트랜잭션 안에서 지금 단계를 통과로 적고 다음 단계로(마지막이면 SUCCEEDED) 간다."""
        index = plan.current_step
        line_data = log_data if log_data is not None else _numbers(metrics_json)
        results = with_step_result(
            base_results if base_results is not None else plan.step_results,
            index,
            **{
                RESULT_STATUS: MigrationStepStatus.PASSED.value,
                RESULT_FINISHED_AT: iso(now),
                RESULT_METRICS: metrics_json,
            },
        )
        conditions = _at_conditions(index, MigrationStepPhase.JUDGE)
        if index == last_step(plan.steps):
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
                await self._log.append(plan, code, now, step=index, data=line_data)
                await self._log.append(plan, MigrationLogCode.SUCCEEDED, now)
                await self._record_finished(plan, MigrationPlanStatus.SUCCEEDED)
            return self._closed()
        following = index + 1
        next_phase = (
            MigrationStepPhase.APPLY
            if step_kind(plan.steps, following)
            in (MigrationStepKind.SHIFT, MigrationStepKind.DB_CUTOVER)
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
            await self._log.append(plan, code, now, step=index, data=line_data)
        return None

    async def _fail_step(
        self,
        plan: MigrationPlan,
        index: int,
        metrics_json: dict[str, Any],
        failure: MigrationErrorCode,
        code: MigrationLogCode,
        now: datetime,
        *,
        base_results: list[dict[str, Any]] | None = None,
        log_data: dict[str, Any] | None = None,
        preceding_lines: Sequence[_LogLine] = (),
        conditions: Sequence[Any] = (),
    ) -> None:
        results = with_step_result(
            base_results if base_results is not None else plan.step_results,
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
            failure_line=(code, log_data if log_data is not None else _numbers(metrics_json)),
            preceding_lines=preceding_lines,
            conditions=conditions,
        )

    async def _append_lines(
        self, plan: MigrationPlan, lines: Sequence[_LogLine], now: datetime, step: int
    ) -> None:
        for code, data in lines:
            await self._log.append(plan, code, now, step=step, data=data)

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
        preceding_lines: Sequence[_LogLine] = (),
        conditions: Sequence[Any] = (),
    ) -> bool:
        """잠근 트랜잭션 안에서 RUNNING → ROLLING_BACK. 원인 줄(있으면)과 ROLLBACK_STARTED 를 쓴다.

        중단·일꾼 상실처럼 단계 결과가 정해지지 않은 채 멈추면 지금 단계를 그 원인으로 FAILED 로
        적는다. 남은 단계는 SKIPPED 다. preceding_lines 는 원인 줄 앞에 쓸 줄(카운터 재시작)이다.
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
            ([MigrationPlan.current_step == index] if index is not None else []) + list(conditions),
        ):
            return False
        if index is not None:
            await self._append_lines(plan, preceding_lines, now, index)
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
                    await self._record_finished(
                        plan, MigrationPlanStatus.ROLLED_BACK, plan.error_code
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
            await self._record_finished(plan, MigrationPlanStatus.FAILED, plan.error_code)
        self._report("migration rollback failed", plan, error_code=plan.error_code)

    # --- 공통 ---

    async def _fail(
        self,
        plan: MigrationPlan,
        code: MigrationErrorCode,
        now: datetime,
        *,
        preceding_lines: Sequence[_LogLine] = (),
    ) -> None:
        """트래픽을 바꾸기 전(APPROVED) 또는 방어용으로 FAILED 로 끝낸다(잠근 트랜잭션 안).

        preceding_lines 는 전환이 반영됐을 때 FAILED 줄 앞에 쓴다(예: 보안 게이트 판정 줄).
        """
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
            for line_code, data in preceding_lines:
                await self._log.append(plan, line_code, now, data=data)
            await self._log.append(plan, MigrationLogCode.FAILED, now, error_code=code)
            await self._record_finished(plan, MigrationPlanStatus.FAILED, code)
        self._report("migration failed", plan, error_code=code)

    async def _record_finished(
        self,
        plan: MigrationPlan,
        status: MigrationPlanStatus,
        error_code: MigrationErrorCode | str | None = None,
    ) -> None:
        """끝 상태 전환이 반영됐을 때만, 그 트랜잭션 안에서 부른다."""
        await record_migration_plan_finished(
            self._session,
            self._clock,
            plan,
            status=status,
            component=AuditComponent.MIGRATION_WORKER,
            error_code=error_code,
        )

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


def _emergency_with_cutover(plan: MigrationPlan) -> bool:
    """재난 긴급 이전이고 DB 쓰기 전환 단계(승격 허용)가 들어 있다."""
    return plan.mode == MigrationPlanMode.EMERGENCY and any(
        step_kind(plan.steps, index) is MigrationStepKind.DB_CUTOVER
        for index in range(len(plan.steps))
    )


def _cutover_in_flight(plan: MigrationPlan) -> bool:
    """DB 쓰기 전환 단계에서 Job 을 요청했다(만들었을 수 있다)."""
    if step_kind(plan.steps, plan.current_step) is not MigrationStepKind.DB_CUTOVER:
        return False
    return plan.step_phase != MigrationStepPhase.APPLY or plan.traffic_requested_at is not None


def _simulated_integrity_run(now: datetime, plan: MigrationPlan) -> CutoverRun:
    """simulate DATA_INTEGRITY: 전환은 끝났지만 장부 대조에서 1건이 빠진 결과 행."""
    kind = CutoverKind.ROLLBACK if plan.mode == MigrationPlanMode.FAILBACK else CutoverKind.CUTOVER
    return CutoverRun(
        kind=kind,
        paused_at=now,
        resumed_at=now,
        write_pause_ms=0,
        drain_ms=None,
        catchup_ms=None,
        switch_ms=None,
        rows_total=0,
        missing_rows=SIMULATED_MISSING_ROWS,
        duplicate_rows=0,
        mode="NORMAL",
    )


def _simulated_failure(plan: MigrationPlan, index: int) -> MigrationSimulateReason | None:
    simulate = plan.simulate
    if not simulate or int(simulate.get(SIMULATE_FAIL_AT_STEP, -1)) != index:
        return None
    reason = MigrationSimulateReason(simulate[SIMULATE_REASON])
    return None if reason in _IGNORED_SIMULATIONS else reason


def _simulated_counts(
    reason: MigrationSimulateReason,
    target_weight: int,
    counting: StepCounting,
    now: datetime,
    requests_per_second: int,
) -> tuple[CounterValue, CounterValue]:
    """simulate 단계의 (출발, 도착) 누적 카운터. 세기 시작 순간 0 에서 rps × 비율 × 지난 초다.

    ERROR_RATE 의 5xx 는 도착 기준값을 잡은 뒤의 읽기부터 허용 + 1 이다. 첫 읽기는 기준값이 되므로
    거기서 5xx 를 주면 센 5xx 가 늘 0 이다(첫 읽기가 세기 시작보다 조금이라도 늦은 실제 시계에서).
    """
    elapsed = max((now - counting.started_at).total_seconds(), 0.0)
    target_requests = int(requests_per_second * target_weight * elapsed / 100)
    source_requests = int(requests_per_second * (100 - target_weight) * elapsed / 100)
    target_errors = 0
    if reason is MigrationSimulateReason.ERROR_RATE and counting.target.baseline is not None:
        target_errors = counting.allowed_errors_5xx + 1
    elif reason is MigrationSimulateReason.TARGET_NOT_RECEIVING:
        # 진입점이 비율을 적용하지 않아 모든 요청이 출발로 가는 상황.
        target_requests = 0
        source_requests = int(requests_per_second * elapsed)
    elif reason is MigrationSimulateReason.LOW_TRAFFIC:
        target_requests = source_requests = 0
    return CounterValue(source_requests, 0), CounterValue(target_requests, target_errors)


def _counter_value(result: CountsResult) -> CounterValue | None:
    if not isinstance(result, RequestCounts):
        return None
    return CounterValue(max(result.requests, 0), max(result.errors_5xx, 0))


def _health_status(health: HealthProbeResult | None) -> str | None:
    if health is None:
        return None
    return str(_HEALTH_RESULTS.get(type(health), HealthCheckResult.UNREACHABLE))


def _metrics_json(
    *,
    health: HealthProbeResult | None = None,
    replication_lag_seconds: float | None = None,
) -> dict[str, Any]:
    """사전 확인·데이터 확인 단계 결과 metrics 칸. 재지 않은 값은 null."""
    return {
        "healthStatus": _health_status(health),
        "errorRatePercent": None,
        "p95Ms": None,
        "requestCount": None,
        "errors5xx": None,
        "allowedErrors5xx": None,
        "sourceRequestCount": None,
        "lowTraffic": None,
        "replicationLagSeconds": replication_lag_seconds,
    }


def _shift_metrics_json(
    counting: StepCounting,
    *,
    health: HealthProbeResult | None = None,
    p95_ms: float | None = None,
    is_low_traffic: bool = False,
) -> dict[str, Any]:
    """비율 단계 결과 metrics 칸: 도착이 센 요청·5xx, 허용 5xx, 출발이 센 요청, p95(참고)."""
    target = counting.target.counted
    return {
        "healthStatus": _health_status(health),
        "errorRatePercent": error_rate_percent(target.errors_5xx, target.requests),
        "p95Ms": p95_ms,
        "requestCount": target.requests,
        "errors5xx": target.errors_5xx,
        "allowedErrors5xx": counting.allowed_errors_5xx,
        "sourceRequestCount": counting.source.counted.requests,
        "lowTraffic": is_low_traffic,
        "replicationLagSeconds": None,
    }


def _shift_log_data(metrics_json: dict[str, Any], counting: StepCounting) -> dict[str, Any]:
    """비율 단계 JUDGE_PASSED·JUDGE_FAILED 줄의 data(센 건수와 트래픽 적음 여부)."""
    return {
        "requestCount": metrics_json["requestCount"],
        "requiredRequests": counting.required_requests,
        "errors5xx": metrics_json["errors5xx"],
        "allowedErrors5xx": metrics_json["allowedErrors5xx"],
        "sourceRequestCount": metrics_json["sourceRequestCount"],
        "lowTraffic": metrics_json["lowTraffic"],
    }


def _numbers(metrics_json: dict[str, Any]) -> dict[str, Any]:
    """로그 data 에는 숫자 지표만 싣는다(헬스 결과 문자열은 단계 결과에만)."""
    return {
        key: value
        for key, value in metrics_json.items()
        if isinstance(value, int | float) and not isinstance(value, bool)
    }
