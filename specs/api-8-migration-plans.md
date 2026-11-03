# API 서버 8: 이전 계획 생성 · 승인 · 실행 API SPEC

> 비율 단계의 판정 방식(지켜보기 시간 → 요청 N건 처리 확인)은 [api-8-1](api-8-1-request-count-judgement.md)로 바뀌었다. 둘이 다르면 api-8-1 이 우선한다.

- 상태: Confirmed (fresh-eyes 검토 10건 반영, 검토 전 초안 `~/archive/softbank-hackathon-2026/api-8/api-8-migration-plans.draft-v1.md`)
- 확인일: 2026-11-02
- Depth: Deep
- Ambiguity: Low (≈ 15% / 목표 ≤ 20%)
- High-impact unresolved decisions: None
- Interview rounds: 3 (R1~R3) + 체크포인트(A1 B1 C0 D1 E1) · 상태 파일 `.deep-interview/api-8-migration-plan.state.md`
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/8 (Project #4, `part:API 서버`, `phase:1차`, In Progress)
- 저장소: `echo-was` · 브랜치 `feat/migration-plans` (`main` = `5b26fb5`에서)
- 그림: `~/archive/softbank-hackathon-2026/api-8/api8-r1-background.html`, `api8-r2-followup.html`, `api8-checkpoint.html`

## 1. 목표

서비스 중인 앱의 트래픽을 지금 환경(출발)에서 같은 이미지가 올라간 예비 환경(도착)으로 옮기는 **이전 계획**을 만들고, **승인**을 받아, 일꾼이 계획의 단계대로 **실행**한다.

- 계획: 단계(사전 확인 → 10% → 50% → 데이터 확인 → 100%) · 단계별 예상 시간 · 롤백 조건을 저장한다.
- 승인: 개발자 이상의 사람이 승인해야 실행된다. 기본은 계획을 만든 사람이 아닌 사람이고, 조직 설정 스위치로 자기 승인을 허용할 수 있다.
- 낡은 계획 거부: 계획을 만든 순간의 상태(지문)를 저장하고, 승인 순간과 일꾼 시작 순간에 다시 계산해 다르면 거부한다.
- 실행: 단계마다 트래픽 실행기로 비율을 적용하고, 정해진 시간(기본 5초) 지켜본 뒤 헬스체크·지표로 판정한다. 통과하면 자동으로 다음 단계, 실패·판단 불가·사람의 중단이면 출발 환경 100%로 되돌린다.
- 실행 로그: 계획 단계 번호와 같은 번호로 DB에 한 줄씩 남겨, 응답에서 "계획 / 실제"를 나란히 보여 준다.

GitHub 이슈 완료 기준(원문, 11/2 수정): **단계·예상 시간·롤백 조건을 담은 계획. 개발자 이상의 사람이 승인해야 실행된다(기본은 계획을 만든 사람이 아닌 사람, 조직 설정으로 자기 승인 허용). 계획 뒤 환경이 바뀌면 실행을 거부한다. 실행 로그가 계획과 일치한다.**

이슈 메모: 트래픽 나눠 보내기·지표·데이터 복제는 실행기 틀 + 가짜(`simulated`)로 만들고 실제 연결은 인프라 6·7·9. 시연용 실제 Envoy 트래픽 실행기는 이 작업 범위 밖(인프라 6, 늦으면 지민)이다. 병합해도 실제 연결 확인 전에는 이슈를 `In Progress`로 둔다.

## 2. 현재 맥락과 확인된 사실 (`main` = `5b26fb5`)

- `app/models/service_environment.py`: `traffic_weight`(0~100 CHECK, "마지막으로 프록시에 적용한 값"), `traffic_applied_at`, `current_deployment_id`(복합 FK), `public_host`. 서비스별 합계 100%는 DB가 검사하지 않고 API 8이 맞춘다(용어 사전, `docs/schema.md` 설계 이유).
- `app/services/service_view.py`: 역할은 저장하지 않고 계산(`SERVING` = `traffic_weight > 0`, `STANDBY` = 0 + 현재 배포 있음, `NONE`). `versionStatus`(STANDBY만, 기준 연결 digest와 비교 `IN_SYNC`·`OUT_OF_SYNC`), `last_sync_error`, `active_request_id`·`queued_request_id`를 이미 보여 준다. `reference_connection()`이 기준 연결을 고른다.
- `app/services/deploy_request_processor.py::_succeed`: 서비스 중 연결의 `MANUAL`·`ROLLBACK` 성공 트랜잭션 안에서, `traffic_weight == 0`이고 현재 배포가 있는 연결마다 `_enqueue_standby_sync`로 `STANDBY_SYNC` 요청을 넣는다(`build_id` 재사용, `run_after = now`). 배포 확인 간격은 `worker_poll_interval_seconds`(1초), 헬스 첫 확인은 `VERIFYING` 진입 즉시. 서비스 중 연결이 하나도 없을 때만 `_route_main`으로 대표 주소를 이 연결로 보내고 `traffic_weight = 100`을 적는다.
- `app/services/deploy_request_service.py`: `create_deploy_request`(L149), `create_rollback`(L205). `app/services/service_service.py::delete_service`(L193). 셋 다 서비스 행을 잠근 트랜잭션에서 접수한다.
- `app/executors/base.py`·`__init__.py`: `ExecutorSet(build, deploy, main_route, health, teardown)`, `build_executor_set`(가짜 모드만, 아니면 `NotImplementedError`). `MainRouteExecutor.route_main`은 대표 주소를 한 연결로 100% 보낸다. 나눠 보내기 메서드는 없다. `HealthChecker.probe`는 실제 구현 `HttpHealthChecker`가 있다(`HEALTH_CHECKER_MODE=http`).
- `app/models/job.py`: `kind`(CHECK, `DEPLOY_REQUEST`·`SERVICE_TEARDOWN`), `subject_id`, 임대(`lease_token`·`lease_expires_at`·`lease_losses`). `app/services/job_lease.py::MAX_LEASE_LOSSES = 3`, `is_exhausted = lease_losses >= 3`(한 번 닿으면 초기화되지 않아 그 뒤 실행마다 참). `app/worker/runner.py::_advance`는 `kind`로 고르는 구조가 아니라 `if DEPLOY_REQUEST … else ServiceTeardownProcessor`이고, 내리기 처리기는 서비스를 못 찾으면 할 일을 조용히 DONE으로 닫는다.
- 응답 오류 `details`는 `[{field, reason, value?}]` 목록이고 `value`는 문자열 하나다(`app/core/exceptions.py::ErrorField`, `exception_handlers.py`).
- `async with session.begin():` 블록 안에서 raise하면 그 트랜잭션은 되돌려진다. 403 거부 기록은 `app/auth/audit.py`가 별도 세션으로 커밋한다.
- `app/auth/guards.py`: `ViewerAccess`·`HumanDeveloperAccess`·`HumanAdminAccess` 등. `app/core/exceptions.py`: `AuditedForbiddenError` 하위(`RoleForbiddenError`·`AgentReadOnlyError`·`OriginNotAllowedError`·`CannotChangeOwnRoleError`) → 처리기가 `audit_logs`에 거부 기록.
- `app/routers/organization_router.py`: `POST /orgs`, `GET /orgs`, `GET /orgs/{org_id}`, 멤버 GET·PATCH·DELETE. 조직 설정 변경 API는 없다. `app/models/organization.py`: `slug`·`name`·`personal`·`created_by_user_id`.
- 배포 작업 로그 패턴(API 4): `deploy_request_logs`(요청, `seq`) unique, `DeployJobLog.append`는 상태 전환과 같은 트랜잭션·서비스 행 잠금 안에서 `max(seq)+1`, 고정 문장(`deploy_log_messages`), 삭제 금지 트리거 `prevent_append_only_delete()` 재사용.
- `tests/integration/test_authorization.py`: `ROLE_TABLE`·`test_role_table_covers_every_api_route`·`test_every_route_has_guards`. 시험 기반: `conftest.py` TRUNCATE 목록, `factories.py`, `test_migrations.py`, `deploy_harness.py`. 최근 revision `20261102_1750_33ad047a493c_add_rollback_and_deploy_request_logs.py`.

### 앞 태스크에서 넘겨받은 결정
- 이전 실행 = 개발자 이상 사람, 에이전트는 승인 불가(API 6 D4, `docs/api/auth.md`). 자기 승인 규칙은 이번에 정함.
- 요청 1번 = 환경 1개, 이미 서비스 중이면 새 환경은 예비(트래픽 0). 트래픽 전환은 API 8(API 3 D5).
- 대표 주소는 서비스 중 환경을 가리키고 이전해도 그대로(API 3 D8).
- 접수 때 상태를 기억해 두고 실행 시작 때 다르면 취소하는 장치(API 4 `BASE_CHANGED`)와 같은 방식으로 낡은 계획을 거부한다.
- 요청자·감사 로그 조회 → API 7.

## 3. 범위

### 포함
- 이전 계획 API 8개 + 조직 설정 API 1개(§8.1)
- 새 표 `migration_plans`·`migration_plan_logs`, `organizations.allow_self_approval` 칸, `jobs.kind` 값 `MIGRATION_PLAN`, Alembic revision 1개, `db-schema.sql`
- 일꾼 처리기 `MigrationPlanProcessor`(단계 진행·판정·롤백·재시작)
- 새 실행기 3종(트래픽·지표·데이터 확인) 인터페이스 + 가짜 + 계약 시험
- 기존 접수 경로(배포 요청·되돌리기·서비스 삭제)의 "이전 진행 중" 거절
- 서비스 상세 응답에 예비 따라가기 걸린 시간·진행 중 계획 id(B1·P3)
- 문서: `docs/api/migration-plans.md`(새), `docs/api/organizations.md`, `docs/api/services.md`, `docs/api/deploy-requests.md`, `docs/schema.md`, `docs/executors.md`, `docs/api/auth.md` 역할 표, 용어 사전(이전 계획·승인·지문·단계 등), `README.md`(이전 데모 순서, Argo CD 웹훅 메모)

### 제외
- 실제 Envoy 트래픽 실행기(인프라 6 또는 지민 후속), 실제 지표(인프라 9 OpenTelemetry), 실제 DB 복제·컷오버(인프라 7), 역방향 복제·절차서(인프라 8)
- 비율 목록(10·50·100) 변경, 단계별 사람 승인(멈춤 무기한), 판정 최소 요청 수(P4 미채택)
- 재난 이벤트 자동 트리거(API 9), 사전 점검 체크리스트(AI 에이전트 4), 감사 로그 조회(API 7), 화면(프론트 5·6)
- 예비와 주력 동시 배포(B2 미채택)
- push, `main` 병합(지민 확인 뒤 M1이 한다)

## 4. 구성 요소

| 구성 요소 | 상태 | 비고 |
|---|---|---|
| 계획 API(만들기·목록·단건·로그) | active | `MigrationPlanService`, `migration_plan_router` |
| 승인·거절·취소·중단 API | active | 같은 서비스 |
| 조직 설정 API(자기 승인 스위치) | active | `OrganizationService` |
| 일꾼 처리기 | active | `MigrationPlanProcessor`, `JobKind.MIGRATION_PLAN` |
| 실행기(트래픽·지표·데이터 확인) | active(가짜) | 실제 구현 deferred(인프라 6·9·7) |
| 기존 접수 경로 거절 | active | 배포 요청·되돌리기·서비스 삭제 |
| 서비스 상세 확장 | active | `lastSyncSeconds`, `activeMigrationPlanId` |

## 5. 흐름

### 5.0 잠금·트랜잭션 규칙
- 잠금 순서는 기존 하나를 이어 쓴다: 조직(설정 변경만, `FOR NO KEY UPDATE`) → 환경(접수만, `FOR SHARE`) → 서비스 → 그 서비스의 연결(`id` 오름차순) → 이전 계획 → 배포 요청 → 빌드 → 할 일. 상태만 바꾸는 잠금은 `FOR NO KEY UPDATE`.
- 계획 상태 변경은 조건부 UPDATE(`WHERE id = :id AND status = :expected`), 0행이면 결과를 버리고 다시 읽는다.
- 일꾼 쓰기 트랜잭션은 끝에 `JobLease.hold()`. 외부 호출(실행기)은 트랜잭션 밖 + 멱등 키 + "요청함 시각 저장·커밋 → 호출 → 결과 저장" 순서(`docs/executors.md`).
- 실행 로그 한 줄은 그 줄을 일으킨 상태 변경과 같은 트랜잭션에서, 변경이 반영됐을 때만 쓴다. `seq`는 서비스 행 잠금 안에서 `max(seq)+1`.
- 시각은 주입 `Clock`(DB `now()` 금지).

### 5.1 계획 만들기 (`POST /api/v1/orgs/{orgId}/services/{serviceId}/migration-plans`)
1. 권한: `HumanDeveloperAccess`. 에이전트 403 `AGENT_READ_ONLY`, 뷰어 403 `ROLE_FORBIDDEN`(거부 기록).
2. 바디 검증(422 `VALIDATION_ERROR`): `targetEnvironmentId`(uuid, 필수), `observeSeconds`(정수 1~600, 기본 설정값 5), `rollbackConditions`(선택: `maxErrorRatePercent` 0~100 기본 1.0, `maxP95Ms` 1~60000 기본 800, `maxReplicationLagSeconds` 0~600 기본 1.0), `simulate`(선택 `{failAtStep: 1~4, reason: ERROR_RATE|LATENCY|HEALTH|DATA_LAG|TRAFFIC}`, `executor_mode = fake`일 때만, 아니면 422 `SIMULATE_NOT_ALLOWED`). `reason = DATA_LAG`는 `failAtStep = 3`만, 그 밖의 reason은 데이터 단계(3)를 고를 수 없다(422).
3. 쓰기 트랜잭션(§5.0 순서): 도착 환경 `FOR SHARE` → 서비스 잠금 → 살아 있는 연결 전체 잠금.
   - 서비스 없음·남의 조직 → 404 `SERVICE_NOT_FOUND`. `DELETING` → 409 `SERVICE_DELETING`. 도착 환경 없음·남의 조직 → 404 `ENVIRONMENT_NOT_FOUND`. 도착 환경 `connection_status = FAILED` → 409 `ENVIRONMENT_UNHEALTHY`.
   - 이 서비스에 승인 대기 계획이 있고 `expires_at <= now`면 먼저 `EXPIRED`로 끝낸다(로그 `EXPIRED`). 같은 요청이 뒤의 다른 409로 끝나면 이 정리도 되돌려지지만 무해하다(다음 판정 때 다시 처리된다). 그래도 끝나지 않은 계획(`PENDING_APPROVAL`·`APPROVED`·`RUNNING`·`ROLLING_BACK`)이 있으면 409 `MIGRATION_PLAN_OPEN`(`details` = `[{field: "planId", reason: "MIGRATION_PLAN_OPEN", value: <uuid>}]`).
   - 출발 연결 = `traffic_weight = 100`인 연결 하나. 서비스 중 연결이 정확히 하나가 아니거나 그 비율이 100이 아니면 409 `SOURCE_NOT_SINGLE`. 도착 연결 = 이 서비스와 도착 환경의 살아 있는 연결. 출발과 같으면 422 `VALIDATION_ERROR`(`reason = SAME_ENVIRONMENT`).
   - 열린 배포 요청 검사(이 서비스의 **어느 연결이든** `QUEUED`·실행 중 요청): 도착 연결의 `STANDBY_SYNC`면 409 `TARGET_SYNCING`, 그 밖의 열린 요청(출발·도착·다른 연결의 `MANUAL`·`ROLLBACK`·`STANDBY_SYNC`)이면 409 `DEPLOY_IN_PROGRESS`(`details` = `[{field: "deployRequestId", reason: "DEPLOY_IN_PROGRESS", value: <uuid>}]`). 그래서 계획을 만든 순간 열린 요청 목록은 항상 비어 있다.
   - 도착 연결 검사(D2): 연결이 없거나 현재 배포가 없음 → 409 `TARGET_NOT_READY`(`details` = `[{field: "targetEnvironmentId", reason: "NO_DEPLOYMENT"}]`). 도착 현재 배포의 `image_digest`가 출발 현재 배포와 다름 → 409 `TARGET_NOT_READY`(`[{field: "targetEnvironmentId", reason: "VERSION_MISMATCH"}]`, 그 연결의 마지막 `STANDBY_SYNC`가 실패했으면 `{field: "lastSyncError", reason: "LAST_SYNC_FAILED", value: <에러 코드>}`를 더함).
   - 지문 계산(§5.6), 단계·예상 시간 계산(§5.5), 행 삽입(`PENDING_APPROVAL`, `created_by_user_id`, `expires_at = now + migration_plan_approval_ttl_seconds`, `simulated = (executor_mode == fake)`), 로그 `PLAN_CREATED`.
4. 201 + `MigrationPlanDetail`.

거절 순서: 권한 → 바디(422) → 서비스·환경(404) → 상태 검사(409, 위 순서).

### 5.2 승인 (`POST …/migration-plans/{planId}/approve`)
1. 권한: `HumanDeveloperAccess`. 계획 `find_in_organization`(서비스의 조직으로 거름) 없으면 404 `MIGRATION_PLAN_NOT_FOUND`.
2. 쓰기 트랜잭션: 조직 행 읽기(잠금 없음) → 서비스 → 연결 → 계획 잠금.
   - `status != PENDING_APPROVAL` → 409 `PLAN_NOT_PENDING`.
   - 승인자 = 만든 사람이고 `organization.allow_self_approval = false` → 403 `SELF_APPROVAL_FORBIDDEN`(`AuditedForbiddenError` 하위, 거부 기록). 상태는 바꾸지 않는다.
   - `expires_at <= now` → `EXPIRED`로 전환(로그 `EXPIRED`) 후 409 `PLAN_EXPIRED`.
   - 지문 재계산이 저장값과 다름 → `STALE`로 전환(로그 `STALE`, `data.changed` = 달라진 항목 이름 목록) 후 409 `PLAN_STALE`(`details` = 달라진 항목마다 `{field: <serviceStatus|connections|sourceEnvironment|targetEnvironment|openDeployRequests>, reason: "CHANGED"}`).
   - **구현 규칙**: `EXPIRED`·`STALE` 전환은 `begin()` 블록 안에서 적고 결과 코드만 돌려받아 블록을 정상 종료(커밋)한 뒤, 블록 **밖에서** 409를 raise한다(블록 안에서 raise하면 전환과 로그가 되돌려진다). 403 `SELF_APPROVAL_FORBIDDEN`은 블록 안에서 raise해도 된다(상태를 바꾸지 않고, 거부 기록은 별도 세션).
   - 통과: `APPROVED`, `approved_by_user_id`, `approved_at`, `self_approved = (승인자 == 만든 사람)`, 할 일 `MIGRATION_PLAN`(`subject_id` = 계획 id, `run_after = now`) 삽입, 로그 `APPROVED`(자기 승인이면 `SELF_APPROVED`).
3. 202 + `MigrationPlanDetail`.

### 5.3 거절·취소·중단
- 거절·취소도 승인과 같이 먼저 만료를 판정한다(`expires_at <= now`면 `EXPIRED`로 바꾸고 커밋 뒤 409 `PLAN_EXPIRED`).
- 거절 `POST …/reject`: `HumanDeveloperAccess`. `PENDING_APPROVAL`에서만(아니면 409 `PLAN_NOT_PENDING`). 거절자 = 만든 사람이고 자기 승인이 막혀 있으면 403 `SELF_APPROVAL_FORBIDDEN`(만든 사람은 취소를 쓴다). → `REJECTED`, `rejected_by_user_id`, `finished_at`, 로그 `REJECTED`. 200.
- 취소 `POST …/cancel`: `HumanDeveloperAccess`(누구나). `PENDING_APPROVAL`에서만. → `CANCELLED`, `cancelled_by_user_id`, 로그 `CANCELLED`. 200.
- 중단 `POST …/abort`: `HumanDeveloperAccess`. `APPROVED`·`RUNNING`에서만(아니면 409 `PLAN_NOT_RUNNING`). `abort_requested_at`·`aborted_by_user_id`를 적고 그 계획의 열린 할 일(`QUEUED`·`RUNNING`)의 `run_after = now`로 당긴다. 열린 할 일이 없으면(비정상) `APPROVED`는 그 자리에서 `CANCELLED`, `RUNNING`은 할 일을 새로 넣는다. 로그 `ABORT_REQUESTED`. 상태 전환은 일꾼이 한다(§5.4). 같은 계획에 다시 중단 → 202(멱등, 기록 변경 없음). 202.

### 5.4 일꾼 (`MigrationPlanProcessor.advance`)
상태와 단계 위치 칸: `status`, `current_step`(0~4), `step_phase`(`APPLY`·`OBSERVE`·`JUDGE`), `phase_started_at`, `observe_until`, `traffic_requested_at`, `rollback_requested_at`, `rollback_attempts`.

**디스패치**: `app/worker/runner.py::_advance`를 `match lease.job.kind`로 바꿔 `DEPLOY_REQUEST` → 배포 처리기, `SERVICE_TEARDOWN` → 내리기 처리기, `MIGRATION_PLAN` → `MigrationPlanProcessor`로 보내고, 모르는 kind는 예외를 낸다(조용히 DONE으로 닫지 않는다). 처리기는 계획이 없으면 DONE.

**한 번의 `advance`에서 이어 가는 범위**(시험이 `run_once` 횟수에 기대지 않게, 아래 묶음은 한 실행 안에서 끝까지 간다. 묶음 사이에서만 할 일을 `run_after`로 돌려놓는다):

| 시작 위치 | 한 실행에서 하는 일 | 끝나고 돌려놓는 시각 |
|---|---|---|
| `APPROVED` | 지문 확인 → `RUNNING` → 단계 0 사전 확인 → (통과 시) 단계 1 `APPLY`까지 | `observe_until` |
| `APPLY`(비율 단계) | `set_weights` 호출·결과 반영 → `OBSERVE` | `observe_until` |
| `OBSERVE`, `now < observe_until` | 아무것도 바꾸지 않음 | `observe_until` |
| `OBSERVE`, 만료 | `JUDGE` → 통과 시 다음 단계로 넘어가 그 단계의 첫 동작까지(데이터 확인은 확인까지, 비율 단계는 `APPLY`까지) / 마지막 단계 통과면 `SUCCEEDED` | 다음 `observe_until` 또는 DONE |
| 판정 실패·중단 감지·적용 실패 | `ROLLING_BACK` 전환 → 롤백 `set_weights` 1회 시도 → 적용되면 `ROLLED_BACK` | 실패 시 `now + worker_poll_interval_seconds` |
| `ROLLING_BACK` | 롤백 `set_weights` 1회 시도 | 같음 |

시험은 `run_until_idle`(시계를 `run_after`까지 진행하며 할 일이 없을 때까지 `run_once`) 헬퍼로 끝 상태를 확인하고, 시계 경계(지켜보기 4초 시점에는 판정하지 않음, 5초에는 판정)만 따로 정확히 본다.

1. `APPROVED` 시작: 서비스·연결·계획 잠금 → 서비스 `DELETING`이면 `CANCELLED`(로그 `CANCELLED_SERVICE_DELETED`)는 생기지 않는다(삭제 접수가 막히므로, 방어용으로만 `FAILED/INTERNAL_ERROR`). 지문 재계산이 다르면 `STALE`(로그 `STALE`) 후 할 일 DONE. `abort_requested_at`이 있으면 `CANCELLED`(로그 `CANCELLED`, 트래픽 변경 없음). 같으면 `RUNNING`, `started_at`, `current_step = 0`, 로그 `STARTED`.
2. 단계 0 사전 확인(`PREFLIGHT`): 헬스체커 `probe` 한 번(url = `public_url(도착 연결 public_host)`, path = 도착 현재 배포 `config_snapshot`의 헬스 경로(`SNAPSHOT_HEALTH_CHECK_PATH`), timeout = `health_check_request_timeout_seconds` — §5.4 판정의 헬스도 같은 값) + 도착 현재 배포 digest가 출발과 같은지. 통과 → 단계 결과 `PASSED`, 로그 `PREFLIGHT_OK`, 단계 1. 실패 → `FAILED`, `error_code = PREFLIGHT_FAILED`, `rollback_status = NOT_NEEDED`(트래픽을 바꾼 적 없음), 로그 `PREFLIGHT_FAILED`.
3. 비율 단계(1 = 10%, 2 = 50%, 4 = 100%):
   - `APPLY`: `traffic_requested_at`이 없으면 적고 커밋 → 트랜잭션 밖에서 `TrafficExecutor.set_weights(route, [(출발, 100 - w), (도착, w)], key = "{plan_id}:{step}")` → `TrafficApplied(applied_at)`이면 한 트랜잭션에서 두 연결의 `traffic_weight`·`traffic_applied_at`을 함께 적고(합계 100), 로그 `WEIGHT_APPLIED`(두 비율), `step_phase = OBSERVE`, `observe_until = now + observe_seconds`, 할 일 `run_after = observe_until`. `TrafficFailed(code)` → 단계 결과 `FAILED`(`failureCode = TRAFFIC_APPLY_FAILED`, `metrics = null`), 로그 `WEIGHT_APPLY_FAILED`(단계 번호·요청한 비율) → 롤백 시작(원인 `TRAFFIC_APPLY_FAILED`). 일꾼이 다시 시작해 `traffic_requested_at`은 있고 결과가 없으면 같은 키로 다시 부른다.
   - `OBSERVE`: `now < observe_until`이면 `run_after = observe_until`로 돌려놓는다. 지나면 `JUDGE`.
   - `JUDGE`: (a) 헬스체커로 도착 환경별 주소 `probe`, (b) `MetricsProvider.read(도착 target, since = phase 시작, until = now)`. 실패 판정: 헬스가 OK가 아님 → `HEALTH_CHECK_FAILED`; 지표 없음(`MetricsUnavailable`) → `METRICS_UNAVAILABLE`; `error_rate_percent > maxErrorRatePercent` 또는 `p95_ms > maxP95Ms` → `THRESHOLD_EXCEEDED`. 통과 → 단계 결과 `PASSED`(지표 값 저장), 로그 `JUDGE_PASSED`, 다음 단계(`traffic_requested_at` 초기화). 실패 → 단계 결과 `FAILED`, 로그 `JUDGE_FAILED`(원인 코드·값), 롤백 시작.
4. 단계 3 데이터 확인(`DATA_SYNC_CHECK`): `DataSyncChecker.replication_lag(출발 target, 도착 target)` → `DataSyncLag(seconds)`가 `maxReplicationLagSeconds` 이하면 통과(로그 `DATA_SYNC_OK`), 넘거나 `DataSyncUnavailable`이면 실패(`DATA_LAG_EXCEEDED` / `METRICS_UNAVAILABLE`) → 롤백 시작.
5. 단계 4 판정 통과 → `SUCCEEDED`, `finished_at`, 로그 `SUCCEEDED`. 출발 연결은 비율 0(예비)으로 남는다. 할 일 DONE.
6. 중단 확인: 매 실행 맨 앞(잠금 안)에서 `RUNNING`이고 `abort_requested_at`이 있으면 롤백 시작(원인 `ABORTED`).
7. 롤백 시작: `RUNNING → ROLLING_BACK`, `error_code` = 원인, 로그 `ROLLBACK_STARTED`. 롤백 동작: `rollback_requested_at` 적고 커밋 → `set_weights(route, [(출발, 100), (도착, 0)], key = "{plan_id}:rollback:{rollback_attempts}")` → 적용되면 두 연결 비율을 적고 `ROLLED_BACK`, `rollback_status = SUCCEEDED`, 로그 `ROLLBACK_APPLIED`. 실패면 `rollback_attempts += 1`, 3회 미만이면 `run_after = now + worker_poll_interval_seconds`로 다시, 3회째 실패면 `FAILED`, `rollback_status = FAILED`, 로그 `ROLLBACK_FAILED`(원인 `error_code`는 그대로). 비율 칸은 마지막으로 적용된 값 그대로.
8. 임대 상실(`lease.is_exhausted`, 한 번 참이 되면 계속 참): `APPROVED` → `FAILED/WORKER_LOST`(트래픽 변경 없음), 할 일 `JobOutcome.failed(WORKER_LOST)`. `RUNNING` → `ROLLING_BACK`(원인 `WORKER_LOST`)으로 바꾸고 같은 실행에서 롤백 `set_weights`를 시도. `ROLLING_BACK` → 바로 실패로 끝내지 않고 `rollback_attempts < migration_rollback_max_attempts`이면 롤백 동작을 계속, 다 쓰면 `FAILED`(`rollback_status = FAILED`). 끝 상태가 되면 할 일은 `JobOutcome.failed(WORKER_LOST)`로 닫는다. 즉 exhausted여도 롤백 시도는 남은 횟수만큼 한다.
9. `simulate`: `failAtStep = n`이면 그 단계의 판정에서 `reason`대로 실패를 만든다(ERROR_RATE → 지표 오류율 5.0%로 판정, LATENCY → p95 5000ms, HEALTH → 헬스 실패, DATA_LAG → 지연 30초, TRAFFIC → 그 단계 `APPLY`에서 `TRAFFIC_APPLY_FAILED`). 실행기는 부르지 않고 처리기가 결과를 만든다(배포의 `failAt`과 같은 방식). 로그와 단계 결과에는 만든 값이 그대로 남는다.

### 5.5 단계·예상 시간
- 단계는 고정 5개: `0 PREFLIGHT`, `1 SHIFT(10)`, `2 SHIFT(50)`, `3 DATA_SYNC_CHECK`, `4 SHIFT(100)`.
- 단계 예상 = `migration_step_overhead_seconds`(기본 2) + (SHIFT면 `observe_seconds`). 계획 예상 = 합. 기본값이면 2 + 7 + 7 + 2 + 7 = 25초.
- 계획 행 `steps`(JSONB 배열, 만들 때 한 번 쓰고 바꾸지 않음): `[{index, kind, targetWeight | null, sourceWeight | null, observeSeconds | null, expectedSeconds}]`. `step_results`(JSONB 배열, 같은 길이, 갱신할 때 새 리스트로 통째로 바꿔 넣음): `[{index, status: PENDING|RUNNING|PASSED|FAILED|SKIPPED, startedAt, finishedAt, metrics: {healthStatus, errorRatePercent, p95Ms, requestCount, replicationLagSeconds} | null, failureCode | null}]`. 롤백·실패로 끝나면 남은 단계는 `SKIPPED`.

### 5.6 지문 (낡은 계획 판정)
계획을 만들 때 아래 값을 JSONB `fingerprint`로 저장하고, 승인과 일꾼 시작에서 같은 함수로 다시 계산해 **항목별로** 비교한다(달라진 항목 이름을 `details.changed`와 로그에 남긴다).
- `service.status`
- 이 서비스의 살아 있는 연결 전체: `[(id, current_deployment_id, traffic_weight)]`(id 정렬)
- 출발·도착 환경: `config`·`secret_ref`의 SHA-256(값 자체는 넣지 않음), `connection_status`, `deleted_at is null`
- 이 서비스의 열린 배포 요청 id 목록(정렬)
`environments.updated_at`·`connection_checked_at`은 넣지 않는다(접속 재확인만으로 계획이 낡지 않게).

### 5.7 기존 접수 경로 거절 (실행 중 겹침)
`create_deploy_request`·`create_rollback`·`delete_service`는 서비스 행을 잠근 뒤, 그 서비스에 `APPROVED`·`RUNNING`·`ROLLING_BACK` 계획이 있으면 409 `MIGRATION_IN_PROGRESS`(`details` = `[{field: "planId", reason: "MIGRATION_IN_PROGRESS", value: <uuid>}]`). `PENDING_APPROVAL` 계획은 막지 않는다(그 접수가 열린 요청 목록·연결 값을 바꿔 계획이 낡는다). 일꾼이 만드는 `STANDBY_SYNC`는 이 검사를 받지 않는다. 계획 생성 때 열린 요청이 0개여야 하고(§5.1) 실행 중에는 접수가 막히므로, 실행 중에 배포가 끝나 연결의 현재 배포가 바뀌는 일은 없다. `docs/api/services.md`의 `details.value` 사용 범위 문구에 이 `planId`·`deployRequestId`·`lastSyncError`를 더한다.

### 5.8 조직 설정 (`PATCH /api/v1/orgs/{orgId}/settings`)
- 권한: `HumanAdminAccess`. 바디 `{allowSelfApproval: boolean}`(필수). 조직 행 `FOR NO KEY UPDATE` → 값 변경 → 200 `OrganizationResponse`.
- `OrganizationResponse`(조직 GET·목록·`/me`의 조직 항목 공통)에 `allowSelfApproval` 칸을 더한다. 새 조직(개인 조직 포함) 기본값 `false`.

### 5.9 서비스 상세 확장 (B1·P3)
- 연결 항목에 `lastSyncSeconds`(number | null): 그 연결의 마지막 `SUCCEEDED` `STANDBY_SYNC` 요청의 `finished_at − 원본 요청 finished_at`(초, 소수 1자리). 없으면 null.
- 서비스 상세에 `activeMigrationPlanId`(uuid | null): 끝나지 않은 계획 id. 단 `PENDING_APPROVAL`은 `expires_at > now`일 때만 센다(저장된 상태 값은 조회에서 바꾸지 않는다).
- 기존 `versionStatus`·`lastSyncError`·`activeRequestId`·`queuedRequestId`는 그대로(예비가 따라가는 중인지는 `activeRequestId`/`queuedRequestId`로 보인다).

## 6. 기능 요구사항

- `FR-1` 계획은 출발(지금 100% 연결)·도착(같은 이미지의 예비 연결)·고정 5단계·단계별 예상 시간·롤백 조건(헬스, 오류율, p95, 복제 지연)을 저장한다. 계획의 `steps`는 만든 뒤 바뀌지 않는다.
- `FR-2` 도착 예비가 따라가는 중이면 `TARGET_SYNCING`, 앱이 없거나 다른 버전이면 `TARGET_NOT_READY`로 계획 생성을 거절한다.
- `FR-3` 승인은 개발자 이상의 사람만 한다. 조직의 `allowSelfApproval = false`(기본)이면 만든 사람의 승인·거절은 403 `SELF_APPROVAL_FORBIDDEN`(거부 기록). `true`면 허용하고 `selfApproved = true`로 표시한다.
- `FR-4` 승인과 일꾼 시작 두 시점에 지문을 다시 계산해 다르면 계획을 `STALE`로 끝내고 실행하지 않는다.
- `FR-5` 승인하면 바로 실행이 시작된다(별도 실행 버튼 없음). 승인 대기는 24시간 뒤 만료된다(승인·새 계획 만들기 때 판정).
- `FR-6` 일꾼은 단계마다 비율 적용 → `observeSeconds` 지켜보기 → 판정을 하고, 통과하면 사람 개입 없이 다음 단계로 간다. 실패·판단 불가·중단·일꾼 상실은 출발 100%로 되돌린다.
- `FR-7` 비율은 트래픽 실행기가 "적용했다"고 답한 때에만 두 연결에 함께 적는다(서비스 합계 100% 유지).
- `FR-8` 실행 로그는 계획 단계 번호를 달고 상태 변경과 같은 트랜잭션에서 DB에 쌓인다. 일꾼이 다시 시작해도 줄이 중복·누락되지 않는다. 계획당 500줄 상한(`LOG_TRUNCATED`).
- `FR-9` 단건 응답은 단계마다 계획 값과 실제 결과(상태·시각·지표)를 나란히 싣는다.
- `FR-10` 실행 중(`APPROVED`·`RUNNING`·`ROLLING_BACK`)에는 그 서비스의 배포 요청·되돌리기·서비스 삭제 접수를 409 `MIGRATION_IN_PROGRESS`로 거절한다. 서비스당 끝나지 않은 계획은 1개다.
- `FR-11` 실패 원인 코드를 나눈다: `PREFLIGHT_FAILED`·`TRAFFIC_APPLY_FAILED`·`HEALTH_CHECK_FAILED`·`METRICS_UNAVAILABLE`·`THRESHOLD_EXCEEDED`·`DATA_LAG_EXCEEDED`·`ABORTED`·`WORKER_LOST`·`INTERNAL_ERROR`. 롤백 결과는 `rollbackStatus`(`SUCCEEDED`·`FAILED`·`NOT_NEEDED`)로 따로 둔다.
- `FR-12` 가짜 실행기로 만든 결과는 `simulated: true`를 계획 행에 저장해 응답에 싣는다.
- `FR-13` 권한: 조회(목록·단건·로그) = 뷰어 이상 + 에이전트, 만들기·승인·거절·취소·중단 = 개발자 이상 사람, 설정 변경 = 어드민 사람.

## 7. 상태와 예외

### 7.1 계획 상태 (`MigrationPlanStatus`)
`PENDING_APPROVAL` → `APPROVED`(승인) → `RUNNING`(일꾼 시작) → `SUCCEEDED`.
`RUNNING` → `ROLLING_BACK` → `ROLLED_BACK` | `FAILED`.
`PENDING_APPROVAL` → `REJECTED` | `CANCELLED` | `EXPIRED` | `STALE`.
`APPROVED` → `STALE`(시작 때 지문 다름) | `CANCELLED`(시작 전 중단) | `FAILED`(`WORKER_LOST`).
`RUNNING`(단계 0 실패) → `FAILED`(`PREFLIGHT_FAILED`, `rollbackStatus = NOT_NEEDED`).
끝 상태: `SUCCEEDED`·`ROLLED_BACK`·`FAILED`·`REJECTED`·`CANCELLED`·`EXPIRED`·`STALE`. DB CHECK로 값 목록을 고정한다.

### 7.2 HTTP 오류 코드(새)
404 `MIGRATION_PLAN_NOT_FOUND`; 403 `SELF_APPROVAL_FORBIDDEN`; 409 `MIGRATION_PLAN_OPEN`·`SOURCE_NOT_SINGLE`·`TARGET_NOT_READY`·`TARGET_SYNCING`·`DEPLOY_IN_PROGRESS`·`PLAN_NOT_PENDING`·`PLAN_NOT_RUNNING`·`PLAN_STALE`·`PLAN_EXPIRED`·`MIGRATION_IN_PROGRESS`. 기존 재사용: `SERVICE_NOT_FOUND`·`SERVICE_DELETING`·`ENVIRONMENT_NOT_FOUND`·`ENVIRONMENT_UNHEALTHY`·`SIMULATE_NOT_ALLOWED`·`VALIDATION_ERROR`·`ROLE_FORBIDDEN`·`AGENT_READ_ONLY`.

### 7.3 경계 조건
- 이전이 끝난 뒤(도착 100%, 출발 0%) 도착에 새 배포가 나가면 출발(이제 예비)이 따라간다(기존 API 3 동작, 시험으로 확인).
- 되돌아가기(Failback)는 출발·도착을 바꾼 새 계획이다.
- 롤백 뒤 출발 100%·도착 0%이고 도착은 다시 예비다.
- 중단을 시작 전(`APPROVED`)에 누르면 트래픽 변경 없이 `CANCELLED`.

## 8. 계약

### 8.1 엔드포인트 (`/api/v1/orgs/{orgId}`, `ApiResponse` 봉투, camelCase)

| 메서드 · 경로 | 성공 | 대표 실패 | 최소 역할 |
|---|---|---|---|
| `POST /services/{serviceId}/migration-plans` | 201 `MigrationPlanDetail` | 404·409(§5.1)·422 | 개발자(사람) |
| `GET /services/{serviceId}/migration-plans?status&page&size` | 200 `Page[MigrationPlanSummary]`(최신순) | 404·422 | 뷰어·에이전트 |
| `GET /migration-plans/{planId}` | 200 `MigrationPlanDetail` | 404 | 뷰어·에이전트 |
| `POST /migration-plans/{planId}/approve` | 202 | 403 `SELF_APPROVAL_FORBIDDEN`, 409 `PLAN_NOT_PENDING`·`PLAN_STALE`·`PLAN_EXPIRED` | 개발자(사람) |
| `POST /migration-plans/{planId}/reject` | 200 | 403·409 `PLAN_NOT_PENDING` | 개발자(사람) |
| `POST /migration-plans/{planId}/cancel` | 200 | 409 `PLAN_NOT_PENDING` | 개발자(사람) |
| `POST /migration-plans/{planId}/abort` | 202 | 409 `PLAN_NOT_RUNNING` | 개발자(사람) |
| `GET /migration-plans/{planId}/logs?cursor&limit` | 200 `MigrationLogPage` | 404·422 | 뷰어·에이전트 |
| `PATCH /settings` | 200 `OrganizationResponse` | 403 `ROLE_FORBIDDEN` | 어드민(사람) |

새 라우트 9개는 검사기 의존성을 하나씩 달고 `ROLE_TABLE`에 행을 더한다. 남의 조직 계획 → 404.

### 8.2 응답 칸
- `MigrationPlanSummary`: `id`, `serviceId`, `sourceEnvironmentId`, `targetEnvironmentId`, `status`, `currentStep`, `expectedSeconds`, `createdBy{id, login}`, `approvedBy | null`, `selfApproved`, `errorCode | null`, `rollbackStatus | null`, `simulated`, `createdAt`, `approvedAt`, `startedAt`, `finishedAt`, `expiresAt`.
- `MigrationPlanDetail` = Summary + `rollbackConditions{maxErrorRatePercent, maxP95Ms, maxReplicationLagSeconds, healthCheck: true}`, `observeSeconds`, `steps[{index, kind, sourceWeight, targetWeight, observeSeconds, expectedSeconds, result{status, startedAt, finishedAt, metrics, failureCode}}]`, `rejectedBy`, `cancelledBy`, `abortedBy`, `abortRequestedAt`, `errorMessage`(코드별 고정 문장).
- `MigrationLogPage`: `simulated`, `items[{seq, step | null, at, level, code, message, data}]`(`data` = 비율·지표 숫자만), `nextCursor`, `hasMore`. 커서 = `seq`, `limit` 1~500 기본 200(API 4 `DEPLOY_JOB`과 같은 규칙).
- 서비스 상세: §5.9. 조직: `allowSelfApproval`.

### 8.3 데이터 (모델 → revision → `db-schema.sql`)
- `migration_plans`: `id`(uuid PK), `service_id` FK, `source_service_environment_id` FK, `target_service_environment_id` FK(`<> source` CHECK), `status`(CHECK 값 목록), `steps` JSONB, `step_results` JSONB, `rollback_conditions` JSONB, `observe_seconds` int(1~600 CHECK), `expected_seconds` int, `fingerprint` JSONB, `simulate` JSONB NULL, `simulated` bool, `current_step` smallint(0~4), `step_phase` varchar NULL(CHECK), `phase_started_at`·`observe_until`·`traffic_requested_at`·`rollback_requested_at` timestamptz NULL, `rollback_attempts` smallint default 0, `error_code` varchar NULL(CHECK), `rollback_status` varchar NULL(CHECK), `created_by_user_id`·`approved_by_user_id`·`rejected_by_user_id`·`cancelled_by_user_id`·`aborted_by_user_id` FK users NULL(만든 사람 NOT NULL), `self_approved` bool default false, `approved_at`·`started_at`·`finished_at`·`abort_requested_at`·`expires_at`, `created_at`·`updated_at`. 부분 unique `(service_id) WHERE status IN ('PENDING_APPROVAL','APPROVED','RUNNING','ROLLING_BACK')`. 인덱스 `(service_id, created_at)`. DELETE 금지 트리거(`prevent_append_only_delete()` 재사용). 모든 FK `ON DELETE RESTRICT`.
- `migration_plan_logs`: `id`, `migration_plan_id` FK, `seq` int(≥1), `step` smallint NULL, `logged_at`, `level`(CHECK `INFO`·`WARN`·`ERROR`), `code` varchar(48), `message` varchar(500), `data` JSONB NULL, `simulated` bool. unique(`migration_plan_id`, `seq`). DELETE 금지 트리거.
- `organizations.allow_self_approval` bool NOT NULL default false(`sort_order=APPENDED_COLUMN_SORT_ORDER`).
- `jobs.kind` CHECK에 `MIGRATION_PLAN` 추가(DROP + ADD).
- downgrade: 새 표 2개·칸·CHECK를 되돌린다. `MIGRATION_PLAN` 할 일 행이 있으면 분명한 메시지로 실패한다.
- 시험 준비: TRUNCATE 목록, factories, `test_migrations.py` 기대 목록(CHECK·부분 unique·트리거·칸 순서).

### 8.4 실행기 계약 (`app/executors/base.py`, 가짜 `fake.py`, 계약 시험 `tests/executor_contract.py`)
- `TrafficExecutor.set_weights(route: MainRoute, weights: tuple[WeightedTarget, ...], idempotency_key: str) -> TrafficApplied(applied_at) | TrafficFailed(code)`. `WeightedTarget(target: DeployTarget, weight: int)`. 합계 100이 아니면 `ValueError`(호출 쪽 버그). 같은 키면 같은 결과. 실제 구현은 Envoy 가중치(인프라 6).
- `MetricsProvider.read(target: DeployTarget, since: datetime, until: datetime) -> Metrics(error_rate_percent, p95_ms, request_count) | MetricsUnavailable`. 실제 구현은 OpenTelemetry(인프라 9).
- `DataSyncChecker.replication_lag(source: DeployTarget, target: DeployTarget) -> DataSyncLag(seconds) | DataSyncUnavailable`. 실제 구현은 인프라 7.
- 모두 `simulated` 속성. 가짜: 트래픽 = 즉시 `TrafficApplied(clock.now())`, 지표 = 오류율 0.1% · p95 120ms · 요청 수 `max(1, round(10 × 비율/100 × 창 초))`, 복제 지연 0.3초(결정적). `ExecutorSet`에 `traffic`·`metrics`·`data_sync`를 더한다.
- `docs/executors.md`에 멱등 키(`{plan_id}:{step}`, `{plan_id}:rollback:{n}`)와 호출 순서를 적는다.

### 8.5 설정 (`app/core/config.py`, `.env.example` 주석)
`migration_observe_seconds_default = 5`(1~600), `migration_step_overhead_seconds = 2`(0~60), `migration_plan_approval_ttl_seconds = 86400`, `migration_rollback_max_attempts = 3`. 기본 롤백 조건 상수(1.0%, 800ms, 1.0초)는 코드 상수.

## 9. 호환성·배포·복구
- 기존 응답에는 칸만 더한다(`OrganizationResponse.allowSelfApproval`, 서비스 상세 `lastSyncSeconds`·`activeMigrationPlanId`). 키 집합을 고정한 기존 시험은 갱신한다.
- 기존 조직 행은 `allow_self_approval = false`로 채워진다.
- 일꾼과 API를 같은 이미지로 함께 올린다. 옛 일꾼(`else` 디스패치)이 `MIGRATION_PLAN` 할 일을 집으면 내리기 처리기가 조용히 DONE으로 닫아 계획이 `APPROVED`에 남고 그 서비스가 `MIGRATION_IN_PROGRESS`로 막힌다. 이 경우는 중단(abort)이 `CANCELLED`로 풀어 준다(§5.3). README 배포 메모에 적는다.
- 실제 트래픽 실행기가 붙기 전 운영(`api.echoport.uk`)은 가짜 모드라 비율 칸만 바뀐다(`simulated: true`).
- 롤백(코드): revision downgrade(§8.3 조건).

## 10. Acceptance criteria

시험은 가짜 실행기·주입 시계·`run_once()`로 결정적으로 돌린다. 통합 시험 건너뜀 0(로컬 DB).

- `AC-1` 성공 경로: aws 서비스 중(100) + gcp 예비(같은 digest)에서 개발자 A가 계획 생성 → 201, `steps` 5개, `expectedSeconds = 25`, `rollbackConditions` 기본값, `status = PENDING_APPROVAL`. 개발자 B 승인 → 202 `APPROVED`. 일꾼 진행 → 비율이 (90,10) → (50,50) → (0,100)으로 바뀌고 매 단계 두 연결 합계 100, `SUCCEEDED`. 끝난 뒤 gcp `SERVING`, aws `STANDBY`(`versionStatus = IN_SYNC`).
- `AC-2` 실행 로그 = 계획: AC-1의 로그가 `PLAN_CREATED, APPROVED, STARTED, PREFLIGHT_OK, (WEIGHT_APPLIED, JUDGE_PASSED)×2, DATA_SYNC_OK, WEIGHT_APPLIED, JUDGE_PASSED, SUCCEEDED` 순서이고 `seq` 1부터 빠짐없음. 각 `WEIGHT_APPLIED`의 `step`·비율이 `steps[i]`와 같고, 단건 응답의 `steps[i].result`가 모두 `PASSED`, 지켜보기 구간(`WEIGHT_APPLIED` → `JUDGE_PASSED` 시각 차)이 `observeSeconds` 이상.
- `AC-3` 지켜보기 시간: `observeSeconds = 5`면 `observe_until` 전에는 판정하지 않는다(시계를 4초 진행 → `run_once` 해도 단계 유지, 5초 → 판정). `observeSeconds` 0·601 → 422.
- `AC-4` 자기 승인: 기본 조직에서 만든 사람 승인 → 403 `SELF_APPROVAL_FORBIDDEN` + `audit_logs` 1행, 계획은 `PENDING_APPROVAL` 그대로. 만든 사람 거절도 403. 어드민이 `PATCH /settings {allowSelfApproval: true}` → 같은 사람 승인 202, `selfApproved = true`, 로그 `SELF_APPROVED`. 개발자의 `PATCH /settings` → 403 `ROLE_FORBIDDEN`. 조직 응답에 `allowSelfApproval`.
- `AC-5` 낡은 계획: (a) 계획 생성 뒤 aws에 새 배포 성공 → 승인 시 409 `PLAN_STALE`, 계획 `STALE`, `details`에 `{field: "connections", reason: "CHANGED"}`. (b) 승인 뒤 일꾼 시작 전에 지문을 바꿈(시험에서 직접 연결 비율 변경) → 시작 때 `STALE`, 트래픽 실행기 호출 0회. (c) 도착 환경 `config` 수정 → 승인 409 `PLAN_STALE`. (d) 환경 접속 재확인(`connection_checked_at`만 바뀜)은 낡게 하지 않는다.
- `AC-6` 생성 거절: 도착에 앱 없음·다른 digest → 409 `TARGET_NOT_READY`(마지막 따라가기 실패면 `details.lastSyncError`); 도착에 열린 `STANDBY_SYNC` → 409 `TARGET_SYNCING`; 서비스 중 연결 0개·2개(이전 중간 비율) → 409 `SOURCE_NOT_SINGLE`; 같은 환경 → 422; 열린 계획 있음 → 409 `MIGRATION_PLAN_OPEN`; 만료된 대기 계획은 `EXPIRED`로 바뀌고 새 계획 201; 출발에 열린 배포 요청 → 409 `DEPLOY_IN_PROGRESS`; 도착에 열린 `MANUAL` 요청 → 409 `DEPLOY_IN_PROGRESS`; 각 409의 `details`가 §5.1의 `{field, reason, value}` 형식과 같음; 서비스 `DELETING` → 409; 환경 `FAILED` → 409 `ENVIRONMENT_UNHEALTHY`; 가짜가 아닌 모드 `simulate` → 422 `SIMULATE_NOT_ALLOWED`.
- `AC-7` 판정 실패 → 자동 롤백: `simulate {failAtStep: 2, reason: ERROR_RATE}` → 단계 2 `FAILED`(`errorRatePercent = 5.0`), `ROLLING_BACK` → `ROLLED_BACK`, `errorCode = THRESHOLD_EXCEEDED`, `rollbackStatus = SUCCEEDED`, 비율 (100, 0), 단계 3·4 `SKIPPED`, 로그 `JUDGE_FAILED, ROLLBACK_STARTED, ROLLBACK_APPLIED`. `LATENCY`·`HEALTH`·`DATA_LAG`(단계 3)도 각 원인 코드로 같은 확인. `TRAFFIC`은 로그가 `WEIGHT_APPLY_FAILED, ROLLBACK_STARTED, ROLLBACK_APPLIED`이고 그 단계 결과는 `FAILED`·`failureCode = TRAFFIC_APPLY_FAILED`·`metrics = null`. 지표 공급자가 `MetricsUnavailable`이면 `METRICS_UNAVAILABLE`로 롤백.
- `AC-8` 중단: 단계 1 지켜보기 중 `abort` → 202, 할 일 `run_after`가 지금으로 당겨지고 `run_until_idle` 뒤 `ROLLED_BACK`/`ABORTED`, 비율 (100, 0). `APPROVED`(시작 전) 중단 → `CANCELLED`, 트래픽 실행기 호출 0. `PENDING_APPROVAL`·끝난 계획 중단 → 409 `PLAN_NOT_RUNNING`. 중단 두 번 → 둘 다 202, 기록 1개.
- `AC-9` 사전 확인 실패: 도착 헬스 실패(가짜 헬스체커 주입) → `FAILED/PREFLIGHT_FAILED`, `rollbackStatus = NOT_NEEDED`, 비율 그대로, 트래픽 실행기 호출 0.
- `AC-10` 롤백 실패: 롤백 `set_weights`가 3번 `TrafficFailed` → `FAILED`, `rollbackStatus = FAILED`, `errorCode`는 원래 원인 유지, 비율은 마지막 적용 값.
- `AC-11` 일꾼 재시작(P1): 단계 2의 `traffic_requested_at` 저장 직후 임대를 만료시켜 다른 일꾼이 이어 감 → 같은 멱등 키로 다시 불러 결과가 한 번만 반영, 로그 줄 중복 없음, 계획 끝까지 성공. 임대 3번 상실 → `RUNNING`(단계 2, 비율 50/50)이면 `ROLLED_BACK`, `errorCode = WORKER_LOST`, 비율 (100, 0), 할 일 FAILED. `APPROVED`면 `FAILED/WORKER_LOST`, 트래픽 실행기 호출 0. `ROLLING_BACK` 중 상실이어도 남은 롤백 시도를 한다. 디스패치 시험: `MIGRATION_PLAN` 할 일이 내리기 처리기로 가지 않고, 모르는 kind는 예외(할 일이 조용히 DONE이 되지 않음).
- `AC-12` 원인 코드 구분(P2): AC-7·AC-9·AC-10·AC-11의 `errorCode`가 서로 다르고 `errorMessage`가 코드별 고정 문장이다.
- `AC-13` 실행 중 겹침: `RUNNING` 동안 같은 서비스의 배포 요청·되돌리기·서비스 삭제 → 409 `MIGRATION_IN_PROGRESS`. `PENDING_APPROVAL` 동안 배포 요청은 202이고 그 계획은 승인 때 `STALE`. 계획 동시 생성 2건 → 하나만 201(부분 unique, 500 없음).
- `AC-14` 권한: 만들기·승인·거절·취소·중단은 개발자·어드민 사람 2xx, 뷰어 403 `ROLE_FORBIDDEN`, 에이전트 403 `AGENT_READ_ONLY`(거부 기록). 조회 3개는 뷰어·에이전트 200. 다른 조직 계획 → 404. `test_role_table_covers_every_api_route`·`test_every_route_has_guards` 통과.
- `AC-15` 만료: 승인 대기 24시간 + 1초 뒤 승인·거절·취소 → 409 `PLAN_EXPIRED`, 계획 `EXPIRED`(다시 읽어 저장 확인, 로그 `EXPIRED`). 만료 뒤 서비스 상세 `activeMigrationPlanId = null`. `STALE`도 409 응답 뒤 다시 읽으면 저장돼 있다.
- `AC-16` 예비 따라가기 표시(B1·P3): aws 서비스 중 + gcp 예비에서 aws 배포 성공 → `STANDBY_SYNC`의 할 일 `run_after`가 원본 `finished_at`과 같고, 따라가기 성공 뒤 서비스 상세 gcp `lastSyncSeconds` = 두 요청 `finished_at` 차. 따라가기 진행 중 계획 생성 → `TARGET_SYNCING`. 이전 성공 뒤 gcp(이제 서비스 중) 배포 성공 → aws(예비)에 `STANDBY_SYNC` 생성.
- `AC-17` `simulated`: 가짜 모드 계획·로그 응답 `simulated = true`(행에서 읽음).
- `AC-18` 실행기 계약: 세 실행기 가짜가 `tests/executor_contract.py`의 계약(같은 키 같은 결과, 합계 100 아니면 `ValueError`, 결정적 값)을 통과.
- `AC-19` 스키마: revision 왕복, `alembic check` 차이 0, `dump_schema.sh`와 `db-schema.sql` 일치, `test_migrations.py`가 새 CHECK·부분 unique·트리거·칸 순서를 비교해 통과. `MIGRATION_PLAN` 할 일 행이 있으면 downgrade 실패.
- `AC-20` 문서: §3 포함의 문서 목록 갱신(새 `docs/api/migration-plans.md`: 흐름·상태·오류 코드·`simulate`·예시 응답), README에 데모 순서(계획 → 승인 → 약 25초 → 완료 / 자동 롤백 시연)와 "실제 반영 지연은 Argo CD 동기화(인프라 3 웹훅)" 메모.
- `AC-21` 품질: ruff·format·mypy(`app`)·pytest 통과, 건너뜀 0. 이전 시험(동시성·재시작 포함) 5회 반복 통과.

## 11. 핵심 엔티티

| 이름 | 필드 | 관계 |
|---|---|---|
| `MigrationPlan`(신규) | §8.3 | N:1 `Service`, 출발·도착 → `ServiceEnvironment`, 사람 칸 → `User` |
| `MigrationPlanLog`(신규) | `seq`, `step`, `logged_at`, `level`, `code`, `message`, `data`, `simulated` | N:1 `MigrationPlan` |
| `Organization`(확장) | `allow_self_approval` | — |
| `Job`(확장) | `kind = MIGRATION_PLAN`, `subject_id` = 계획 id | — |
| 값 객체 | `WeightedTarget`, `TrafficApplied/Failed`, `Metrics/MetricsUnavailable`, `DataSyncLag/Unavailable` | 실행기 ↔ 처리기 |

## 12. 리스크 (사전 부검, 사용자 채택 D1 = P1~P3)

| 리스크 | 처리 | 연결 |
|---|---|---|
| P1 일꾼이 10%에서 죽어 방치 | 요구사항화: 단계 저장·같은 키로 이어 가기·임대 상실 시 롤백 | FR-6, AC-11 |
| P2 비율 적용 실패가 판정 실패와 섞임 | 요구사항화: 원인 코드 분리 | FR-11, AC-12 |
| P3 발표 직전 예비가 옛 버전 | 요구사항화: `TARGET_SYNCING` 구분, `lastSyncSeconds`·`activeRequestId` 표시 | FR-2, AC-16 |
| P4 실제 지표 + 5초면 표본 부족 | 수용(미채택). 실제 지표 연결 때 다시 본다 | §3 제외 |
| 감수: 실제 Envoy·지표·복제는 가짜 | 수용(인프라 6·9·7, 시연용 Envoy는 후속) | §3 제외 |

## 13. 결정 기록

| ID | 결정 | 근거 | 출처 |
|---|---|---|---|
| D-1 | 일꾼이 단계 진행, 나눠 보내기는 트래픽 실행기(실제 = Envoy), 이번에는 틀 + 가짜. 데이터 확인 단계는 가짜 자리로 포함 | 인프라 기한과 무관하게 끝까지 시연, 실행기만 교체 | 사용자(R1 Q1) + C0 |
| D-2 | 같은 이미지의 예비만 도착 가능. 따라가는 중이면 `TARGET_SYNCING`, 없거나 다른 버전이면 `TARGET_NOT_READY` | 지민 확인(R1 Q2), 배포 버튼 불필요한 경우 구분 | 사용자(R1·R2) + C0 |
| D-3 | 승인 기본 = 만든 사람 아닌 개발자 이상 사람, 조직 스위치 `allowSelfApproval`로 자기 승인 허용 | GitHub prevent self-review와 같은 모양 | 사용자(R1 Q3, R2 3-1) |
| D-4 | 시간 정한 멈춤 + 판정 통과 시 자동 진행 + 중단 버튼, 지켜보기 기본 5초 | 5분 데모 | 사용자(R2 Q4) |
| D-5 | 판정 = 헬스체크(실제) + 오류율·p95 기준값(가짜 지표 → 인프라 9) | 원장 F7 | 사용자(R1 Q5) |
| D-6 | 예비 따라가기는 지금 순서 유지 + 걸린 시간 기록·표시 + Argo CD 웹훅은 인프라 3에 요청 | 우리 쪽 대기는 이미 거의 없음 | 사용자(B1) |
| D-7 | 사전 부검 P1~P3 요구사항 | — | 사용자(D1) |
| D-8 | 이슈 완료 기준 문구 수정(11/2 반영 완료) | 역할 이름 변경 | 사용자(E1) |
| AD-1~13 | 체크포인트 7절 기본값 13개(출발 자동, 고정 단계, 롤백 기본값, 예상 시간 공식, 지문, 승인 = 실행·만료 24h, 스위치 기본 막기·어드민, 계획 1개·실행 중 409, 끝난 뒤 역할, 실행기 3종 가짜, simulate, 권한, 사람 칸) | — | AI-default (C0 승인) |
| AD-14 | 사전 확인(단계 0) 실패는 트래픽을 바꾸지 않았으므로 `FAILED/PREFLIGHT_FAILED` + `rollbackStatus = NOT_NEEDED` | 상태 그림의 FAILED를 "되돌리기 실패"에서 "끝까지 못 감"으로 넓힘 | AI-default — 결정의 세부(거부권) |
| AD-15 | 지문에서 `environments.updated_at`·접속 확인 시각을 빼고 `config`·`secret_ref` 해시·`connection_status`만 | 접속 재확인만으로 계획이 낡지 않게 | AI-default — 결정의 세부(거부권) |
| AD-16 | 만든 사람의 거절도 자기 승인 스위치를 따른다(막혀 있으면 403, 취소를 쓰게 함) | 체크포인트 AD-6 "거절은 만든 사람 밖"의 구체화 | AI-default — 결정의 세부(거부권) |
| AD-17 | 출발 쪽에 열린 배포 요청이 있으면 생성 409 `DEPLOY_IN_PROGRESS` | 곧 낡을 계획을 만들지 않게 | AI-default(거부권) |
| AD-18 | 서비스 상세에 `lastSyncSeconds`·`activeMigrationPlanId` 추가, `versionStatus` 값은 그대로 | B1 "기록·표시"의 구체화, 기존 계약 값 불변 | AI-default(거부권) |
| AD-19 | 롤백 `set_weights` 최대 3회 시도 후 `FAILED` | 일시 오류 흡수 | AI-default(거부권) |
| AD-20 | (검토 반영) 일꾼 디스패치를 `match kind`로 바꾸고 모르는 kind는 예외, 중단 때 열린 할 일이 없으면 `APPROVED`는 즉시 `CANCELLED`·`RUNNING`은 할 일 재삽입 | 지금 `else`가 모르는 할 일을 내리기 처리기로 보내 조용히 DONE → 서비스가 영구히 막힘(runner.py L111-118) | AI-default (검토 반영) |
| AD-21 | (검토 반영) 임대 상실 뒤에도 롤백 시도를 남은 횟수만큼 함 | `is_exhausted`가 한 번 참이면 계속 참이라, 원안대로면 비율이 50/50에 남음(job_lease.py L57-59) | AI-default (검토 반영) |
| AD-22 | (검토 반영, AD-17을 넓힘) 계획 생성 때 서비스의 **어느 연결이든** 열린 배포 요청이 있으면 거절(도착 `STANDBY_SYNC`만 `TARGET_SYNCING`, 나머지 `DEPLOY_IN_PROGRESS`) | 도착 연결의 수동 배포가 실행 중에 끝나 "같은 이미지" 전제가 깨지는 구멍 | AI-default — 결정의 세부(거부권) |
| AD-23 | (검토 반영) 오류 `details`를 기존 `[{field, reason, value}]` 형식으로 매핑(§5.1·§5.2·§5.7) | 기존 계약(ErrorField) | AI-default (검토 반영) |
| AD-24 | (검토 반영) `EXPIRED`·`STALE` 전환은 커밋한 뒤 블록 밖에서 409 | 블록 안 raise면 전환이 되돌려짐 | AI-default (검토 반영) |
| AD-25 | (검토 반영) 한 `advance`에서 이어 가는 범위 표(§5.4), 시험은 `run_until_idle` | AC 기대값이 구현마다 달라지지 않게 | AI-default (검토 반영) |
| AD-26 | (검토 반영) 헬스 확인 path = 도착 현재 배포 `config_snapshot`의 헬스 경로, timeout = 기존 설정 / 적용 실패 로그 `WEIGHT_APPLY_FAILED` / 거절·취소도 만료 판정 / `activeMigrationPlanId`는 만료 안 된 대기만 / 두 번째 중단 202 | 구현자 질문 제거 | AI-default (검토 반영) |

## 14. 구현 계획으로 위임한 low-impact 세부사항
- 로그 `code` 집합의 정확한 이름과 고정 문장(영어, 기존 `deploy_log_messages`와 같은 방식), `data` 키 이름.
- 모듈·클래스 이름(`MigrationPlanService`, `MigrationPlanProcessor`, `migration_plan_router`, 저장소), 예외 클래스 이름과 `details` 칸.
- 칸 길이·인덱스 이름, `step_phase` 값 문자열.
- 목록 쿼리 구성(상수 개 쿼리), `createdBy{id, login}`을 위한 사용자 일괄 조회 방식.
