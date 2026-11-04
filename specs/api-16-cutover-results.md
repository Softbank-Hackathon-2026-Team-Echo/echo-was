# API 서버 16: 컷오버 결과(쓰기 정지·누락·중복)를 이전 계획 결과에 붙이기 — SPEC

- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/63 (Project #4, 담당 김지민)
- 저장소 경로: `specs/api-16-cutover-results.md`
- 기준: echo-was main `43c39c00514e62cf7f0c2565787cd02e01330268`(11/5, #66 감사 로그·#61 보안 게이트 병합 뒤), echo-infra main `1a1d553`, echo-frontend main `b2b2c96`. Alembic head `286d9ea982d6`(이 작업은 새 리비전을 만들지 않는다). 조사는 `def6052`에서 했고, 2절은 `git diff def6052..43c39c0`로 다시 맞췄다
- 선행: echo-infra#57(인프라·이전 엔진 7.2, 닫힘) — 표 `echo_ops.cutover_runs`와 `echo_monitor` 읽기 권한(11/4 22:52 운영 확인)
- 관련: echo-was #61(보안 게이트)·#66(감사 로그)은 병합됨. #64(기한부 예외)·#59(보안 매트릭스)는 열림 — 충돌 범위는 2절 끝
- 상태: **확정**(11/5 지민 결정 D-①~④ = 모두 A, 캐시 5초. AI 기본값 AD-1~12 거부 없음)

## 1. 완료 기준 (이슈 원문)

- [ ] 인프라 7.2 표가 생긴 뒤, 운영에서 이전 → `cutover.sh`를 돌리면 그 이전 계획 상세에 `writePauseMs`·`missingRows`·`duplicateRows`가 스크립트 값과 같게 나온다
- [ ] 복귀 → `rollback.sh` 뒤에는 복귀 계획 상세에 ROLLBACK 값이 나온다
- [ ] 컷오버 행이 없는 계획과 DB를 읽지 못하는 경우에는 칸이 `null`이고 응답은 200이다(자동 시험)
- [ ] `docs/api/migration-plans.md`에 칸 설명을 적고, 프론트 담당에게 응답 모양을 이슈 댓글로 알린다

이번 범위의 읽기: "이전 계획 상세" = `GET /api/v1/orgs/{orgId}/migration-plans/{planId}`(실제 경로, 서비스 아래가 아님). "스크립트 값과 같게" = 표 행의 `write_pause_ms`·`missing_rows`·`duplicate_rows`(스크립트가 같은 값을 찍고 같은 값을 INSERT한다)와 같다. 화면 반영은 프론트 몫이다.

## 2. 확인된 사실 (main `43c39c0`)

자세한 인용은 조사 노트 `facts.md`에 있다.

- 표(echo-infra `replication/k8s/scripts/common.sh` 30~57행): `echo_ops.cutover_runs(id uuid PK, kind text CHECK IN ('CUTOVER','ROLLBACK'), from_side/to_side text CHECK IN ('A','B'), paused_at timestamptz NOT NULL, resumed_at timestamptz NOT NULL, write_pause_ms integer NOT NULL, drain_ms/catchup_ms/switch_ms integer, rows_total/missing_rows/duplicate_rows/max_n bigint, created_at timestamptz NOT NULL DEFAULT now())`. `GRANT USAGE ON SCHEMA echo_ops`·`GRANT SELECT ON echo_ops.cutover_runs TO echo_monitor`.
- 표는 **`app` DB**에 있다(`pg()`가 `-d app`). `cutover.sh`는 B에, `rollback.sh`는 A에 RESUME 뒤 한 줄을 쓰고(`record_run`), 복제로 양쪽에 같은 행이 있다. 기록이 실패하면 경고만 찍는다(행 없음 가능). 행에는 서비스·계획 id가 없다. `paused_at`은 스크립트를 돌린 컴퓨터의 시계(`date +%s.%N`)다.
- echo-was 데이터 확인: `app/executors/postgres_data_sync.py::PostgresDataSyncChecker`가 asyncpg로 호출마다 연결 → `fetchrow` → `close` 한다. 시간 제한은 3초이고, 실패는 예외 대신 `DataSyncUnavailable`이다. 접속처 `endpoint(target)`는 환경 id 매핑 → 종류 매핑 순서로 찾는다. 설정은 `DATA_SYNC_DATABASES`·`DATA_SYNC_USER`(echo_monitor)·`DATA_SYNC_PASSWORD_REF`·`DATA_SYNC_SSL_MODE`·`DATA_SYNC_TIMEOUT_SECONDS`·`DATA_SYNC_DATABASE`(기본 `postgres`, **운영에 설정 없음**).
- 운영 `deploy/base.yaml`: `DATA_SYNC_DATABASES: aws=<A NLB>:5432/echo_sub_rev,gcp=echo-pg.echo-data.svc.cluster.local:5432/echo_sub`. 비밀번호는 `echo-was-env`이고 API·일꾼 파드 둘 다 읽는다.
- API 프로세스도 실행기 묶음을 가진다(`dependencies.py::get_executor_set` 248~262행, fake 모드는 요청마다 새 묶음, real 모드는 `executor_set_holder`로 프로세스당 하나이고 lifespan이 닫는다). API `replicas: 1`. `ExecutorSet`(`app/executors/__init__.py` 67행)의 칸은 `build·deploy·main_route·health·teardown·traffic·metrics·data_sync·closers`이고, `build_executor_set`은 `data_sync_checker_mode is POSTGRES`일 때 `PostgresDataSyncChecker`를 만든다(151행).
- 상세 응답 `MigrationPlanDetailResponse`(`app/schemas/migration_plan.py` 283~321행, `from_view(view)`)에 계획 단위 `result`는 없다. `steps[i].result`(단계 결과)만 있다. 서비스 `MigrationPlanService.get_plan`(455~458행)은 `begin()` 안에서 view만 만든다. 라우터 `get_migration_plan`(`migration_plan_router.py` 85~89행)은 `access: ViewerAccess, plan_id, migration_plans: MigrationPlans`를 받아 `get_plan` → `from_view` 한다.
- 복귀 구분: `migration_plans.mode`(`PLANNED`·`EMERGENCY`·`FAILBACK`, `decide_plan_mode`)만 있다. 원 계획을 가리키는 칸은 없다.
- 창 계산 재료: `finished_at`(끝 상태 7종일 때만 NOT NULL, CHECK), `created_at` + 인덱스 `(service_id, created_at)`. 서비스당 끝나지 않은 계획은 하나라서, 다음 계획 `created_at`은 앞 계획 `finished_at`보다 늦다. "다음 계획" 조회 함수는 아직 없다.
- 프론트: 계약 4-6절 `result{…writePauseMs, missingRows, duplicateRows…}`. `ResultTab.tsx`·`CompletionCard.tsx`는 그 칸을 `null`/`—`로 박아 두었다. `useMigrationPlan`은 끝난 계획을 다시 부르지 않는다(`refetchOnWindowFocus false`, `staleTime 10초`). 타입은 OpenAPI 생성이다.
- 운영에 이미 있는 행: 11/4 22:51:59 CUTOVER 5817ms·22:52:13 ROLLBACK 6645ms(장부 168, 누락·중복 0). 이전 계획 없이 돌린 것이다.
- #61·#66 병합으로 바뀐 점(`git diff def6052..43c39c0`):
  - 라우터 `migration_plan_router.py`, 스키마 `app/schemas/migration_plan.py`, `app/executors/postgres_data_sync.py`, `app/executors/__init__.py`, `app/repositories/migration_plan_repository.py`, `deploy/base.yaml`은 **바뀌지 않았다**. 위 행 번호는 그대로다.
  - #66(감사 로그)은 `MigrationPlanService`에 `AuditRecorder`를 넣어 **만들기·승인·거절·취소·중단 같은 쓰기에만** 감사 줄을 남긴다. 상세 조회(GET)·라우터에는 감사 기록이 없다. 그래서 이 작업은 감사 기록을 더하지 않는다(읽기 전용).
  - #61(보안 게이트)은 `get_migration_plan_service`에 인자 `tls_checker: PublicHostTlsChecker | None = Depends(get_public_tls_checker)`를 더했고(`MigrationPlanService(security_gate=…)`), `dependencies.py` 끝(396~429행)에 `get_public_tls_checker`·`get_audit_log_service`가 생겼다. `get_environment_service`에는 `clock`이 더해졌다. `get_executor_set`(248~262행)은 그대로다.
  - `app/core/config.py`는 `security_gate_enabled`·`was_version`(231행 근처)만 더했다. `DATA_SYNC_*` 설정(132~138행)과 검사기(383~413행)는 그대로다.
  - `app/enums.py`는 `ActorType.SYSTEM`, `AuditResult.SUCCEEDED/FAILED`, `MigrationErrorCode.SECURITY_GATE_BLOCKED`, `MigrationLogCode.SECURITY_GATE_PASSED/BLOCKED`, `SecurityFindingReason` 몇 개를 더했다. `MigrationPlanMode`(319행)·`MigrationPlanStatus`는 그대로다.
  - `docs/api/migration-plans.md`는 제목에 "보안 5 게이트", 승인 순서·"보안 게이트" 소절·상태표·원인 코드·로그 표·설정 표(`SECURITY_GATE_ENABLED`·`WAS_VERSION`)가 더해졌다. `progress` 절 위치는 그대로다.
  - `app/main.py`에 `audit_log_router`가 더해졌다. 이 작업은 `main.py`를 건드리지 않는다(새 라우트 없음, 풀 없는 읽기라 lifespan 정리 불필요).
  - Alembic head는 `286d9ea982d6`(보안 게이트) ← `b7d41e6f2a93`(감사 로그) ← `48b2126a9c37`.
- 열린 PR과 겹침(11/5):
  - #64(기한부 예외, CONFLICTING)·#59(보안 매트릭스)는 둘 다 `app/dependencies.py`·`app/main.py`·`CLAUDE.md`·`docs/api/auth.md`·`tests/integration/test_authorization.py`를 바꾼다. #64는 `app/enums.py`·`app/core/exceptions.py`도 바꾼다.
  - **이 SPEC은 `main.py`·`CLAUDE.md`·`docs/api/auth.md`·`test_authorization.py`·`exceptions.py`·서비스/processor 파일·마이그레이션을 건드리지 않는다.** `dependencies.py`는 `get_executor_set` 바로 아래에 함수 하나만, `enums.py`는 (enum을 둔다면) `MigrationPlanMode` 아래 작은 enum 하나만 더한다.

## 3. 범위

한다:
1. 고객 DB 결과 표 읽기 클라이언트(`echo_monitor` 연결 재사용, 읽기만).
2. 상세 응답에 계획 단위 `result` 칸 추가(끝난 계획만 값 계산).
3. 창 계산(같은 서비스의 다음 계획 조회)과 프로세스 캐시(5초).
4. 설정 2개(`CUTOVER_RUNS_DATABASE`, `CUTOVER_RESULTS_NOT_BEFORE`) + 캐시 초 1개, `deploy/base.yaml`에 `CUTOVER_RUNS_DATABASE: app`·`CUTOVER_RESULTS_NOT_BEFORE: "2026-11-04T13:30:00Z"` 두 줄(AD-2·AD-5·AD-11).
5. 문서: `docs/api/migration-plans.md`(칸 설명), `docs/executors.md`(읽기 클라이언트), `.env.example`, `deploy/README.md`.

하지 않는다:
- 화면(프론트 Result 탭·완료 카드) — 응답 모양을 이슈 댓글로 알린다(AC-6).
- 컷오버·롤백 실행을 이전 계획 단계에 넣기(이슈에서 버린 안).
- 기록 POST API·쓰기 토큰(이슈에서 버린 안). 고객 DB에 쓰기.
- 목록(Summary)·로그 API 변경(AD-7).
- `totalSeconds`·`rtoSeconds`·`rpoSeconds` 등 계약의 나머지 `result` 칸(지금 프론트가 계산. 나중에 칸 더하기로 가능).
- 서비스별 DB 구분(AD-10).

## 4. 결정

| ID | 결정 | 상태 |
|---|---|---|
| D-1 | 방식: 스크립트가 고객 DB 표에 쓰고, echo-was는 `echo_monitor` 읽기 연결로 읽기만 한다. 새 쓰기 토큰 없음 | 확정(11/4 지민, 이슈 본문) |
| D-2 | 저장 방식 = **조회 때 읽기 + 짧은 프로세스 캐시(5초)**. 일꾼 변경·DB 저장 없음 | 확정(11/5 지민, ①=A, 캐시 15초→5초) |
| D-3 | 행 매칭 = 이슈 규칙 그대로. 창 `[finished_at, 같은 서비스 다음 계획 created_at)`(다음 없으면 끝 없음). 종류는 `mode=FAILBACK`이면 `ROLLBACK`, 그 밖은 `CUTOVER`. 창 안 `paused_at`이 가장 이른 행. 끝 상태 7종 모두 대상 | 확정(11/5 지민, ②=A. 30분 상한·성공만 안은 버림) |
| D-4 | 응답 = 상세 `result` 한 묶음, 프론트 계약 이름 그대로. 끝나지 않은 계획 `null`, 끝난 계획은 늘 객체이고 행이 없거나 못 읽으면 안의 칸 전부 `null` | 확정(11/5 지민, ③=A) |
| D-5 | 새 DB 칸 없음(마이그레이션 0) | 확정(11/5 지민, ④=A) |
| D-6 | 행이 없거나 DB를 읽지 못하면 칸은 null, 상세 조회는 200 | 확정(이슈 본문) |

AI 기본값(11/5 지민 거부 없음 — 채택):

| ID | 내용 |
|---|---|
| AD-1 | 읽는 조건: `DATA_SYNC_CHECKER_MODE=postgres` ∧ 계획 끝 상태 ∧ `simulated=false` ∧ `CUTOVER_RUNS_DATABASE` 비어 있지 않음 ∧ (AD-5 기준 이후 종료). 하나라도 아니면 고객 DB에 붙지 않고 `result`의 칸은 전부 null. 가짜 실행기는 늘 "행 없음"이고 가짜 숫자를 만들지 않는다 |
| AD-2 | 접속처 = 도착 환경(`target_service_environment_id`의 환경)의 `DATA_SYNC_DATABASES` 매핑(환경 id → 종류). host·port만 쓰고 구독 이름은 쓰지 않는다. 계정·비밀번호·TLS·시간 제한은 `DATA_SYNC_*` 재사용. DB 이름은 `CUTOVER_RUNS_DATABASE`(기본 `app`) |
| AD-3 | 질의(5-2절). 비교 칸은 `paused_at`, 같으면 `created_at`·`id` 순 |
| AD-4 | 캐시: 프로세스 메모리, 키 = 계획 id, 최대 512개(오래된 것부터 버림). 찾음 ∧ 창 닫힘 → 버려질 때까지 유지. 그 밖(못 찾음·창 열림·실패) → `CUTOVER_RESULT_CACHE_SECONDS`(기본 **5**, 11/5 지민 결정). 같은 계획의 동시 요청은 한 번만 읽는다(계획 id별 잠금) |
| AD-5 | `CUTOVER_RESULTS_NOT_BEFORE`(기본 없음 = 제한 없음). 운영 `deploy/base.yaml`에 `2026-11-04T13:30:00Z`. `finished_at`이 이보다 이른 계획은 읽지 않는다(표가 생기기 전 계획에 11/4 22:52 행이 붙지 않게) |
| AD-6 | `result` 칸: `writePauseMs`·`missingRows`·`duplicateRows`·`rowsTotal`·`kind`·`measuredBy`·`pausedAt`·`resumedAt`·`drainMs`·`catchupMs`·`switchMs`. `measuredBy`는 행이 있으면 `INFRA_SCRIPT` |
| AD-7 | 상세만 바꾼다. 목록·로그·만들기·승인 응답의 `result`: 만들기·승인·거절·취소·중단 응답도 `MigrationPlanDetailResponse`라 칸은 있다. 이 쓰기 응답에서는 고객 DB를 읽지 않고, 끝나지 않았으면 null, 끝났으면(거절·취소) 칸 전부 null로 둔다 |
| AD-8 | 실패 로그: `logger.warning("cutover result unavailable", extra={action, plan_id, target_environment_id, error_code})`. 코드는 `CUTOVER_RESULT_ENV_UNMAPPED`·`_SECRET_MISSING`·`_UNREACHABLE`·`_QUERY_FAILED`(권한 없음·표 없음 포함). 주소·비밀값 없음. 행 없음은 로그 없음 |
| AD-9 | 파일 배치: 새 `app/executors/postgres_cutover_runs.py`(읽기) + `ExecutorSet.cutover_runs`(기본값 있는 칸, 기본 `NoCutoverRuns`; `build_executor_set`이 postgres 모드일 때 실제 읽기를 넣음) + 새 `app/services/migration_plan_result.py`(창·캐시) + `migration_plan_router.get_migration_plan`(85~89행)에서 붙인다. 새 의존 함수는 `dependencies.py`의 `get_executor_set`(248~262행) 바로 아래에 둔다 — 파일 끝 396~429행(#61의 `get_public_tls_checker`·`get_audit_log_service`·`get_migration_plan_service(tls_checker)`, #64·#59가 덧붙이는 자리)은 건드리지 않는다. `get_migration_plan_service`의 시그니처를 바꾸지 않는다. `main.py`는 건드리지 않는다. `migration_plan_service.py`·`migration_plan_processor.py`는 건드리지 않는다(창 계산용 "다음 계획" 조회는 `MigrationPlanRepository`에 메서드 하나). 종류 enum을 둔다면 `enums.py`의 `MigrationPlanMode` 아래. 감사 기록은 더하지 않는다(#66은 쓰기만 기록) |
| AD-10 | 매핑이 환경 종류 단위라 aws·gcp를 쓰는 다른 서비스의 계획에도 같은 행이 붙을 수 있다. 문서에 적고 막지 않는다(서비스 1개 시연) |
| AD-11 | 별도 켜기 스위치 PR 없음(읽기 전용·실패 시 null). 같은 PR의 `deploy/base.yaml` ConfigMap에 `CUTOVER_RUNS_DATABASE: app`·`CUTOVER_RESULTS_NOT_BEFORE: "2026-11-04T13:30:00Z"`를 한 줄씩 더한다(`DATA_SYNC_*` 줄 근처). 끄는 법: `CUTOVER_RUNS_DATABASE: ""` |
| AD-12 | 프론트 인계 댓글에 "끝난 계획에서 `result.measuredBy == null`이면 10분 동안 5초마다 다시 부르기"를 제안한다 |

## 5. 동작 흐름

```text
GET /orgs/{org}/migration-plans/{plan}
  ├─ (트랜잭션) 계획 읽기·조직 확인 → view                       ← 지금과 같음
  ├─ (트랜잭션) 끝난 계획이면: 다음 계획 created_at, 도착 환경(id·종류) 읽기
  ├─ 캐시 확인 (계획 id) ── 맞으면 그대로
  ├─ (트랜잭션 밖) AD-1 조건 → 아니면 칸 전부 null
  │     └─ cutover_runs.find_first(target, kind, since=finished_at, until=next.created_at|None)
  │           ├─ 행 → result 채움, 창 닫힘이면 오래 캐시
  │           ├─ 행 없음 → 칸 null, 5초 캐시
  │           └─ 실패(매핑·비밀·접속·질의·3초) → 칸 null, 경고 로그, 5초 캐시
  └─ 200 MigrationPlanDetail{…, result}
```

시연 순서에서 붙는 모습(규칙 D-3):

| 시각(예) | 사건 | 이전 계획 P1(PLANNED aws→gcp) | 복귀 계획 P2(FAILBACK gcp→aws) |
|---|---|---|---|
| t0 | P1 SUCCEEDED (`finished_at`) | 창 열림, 행 없음 → 칸 null | — |
| t0+1분 | `cutover.sh` → B에 CUTOVER 행(복제로 A에도) | 다음 조회(≤5초 캐시 뒤)에 CUTOVER 값 | — |
| t0+3분 | P2 만듦 (`created_at`) | 창 닫힘 → 값 고정, 다시 안 붙음 | 아직 끝나지 않음 → `result: null` |
| t0+4분 | P2 SUCCEEDED | 그대로 | 창 열림 → A에서 ROLLBACK 찾음, 없음 → null |
| t0+5분 | `rollback.sh` → A에 ROLLBACK 행 | 그대로(P1은 CUTOVER만 봄, 창도 닫힘) | 다음 조회에 ROLLBACK 값 |

읽는 DB: P1의 도착 = gcp → B(`echo-pg.echo-data…`), P2의 도착 = aws → A(NLB). 두 행 모두 양쪽에 있지만, 이슈대로 도착 쪽을 읽는다.

## 6. 계약 변경

### 6-1. 응답 (D-4)

`MigrationPlanDetailResponse`에 칸 하나를 더한다(맨 뒤): `result: MigrationPlanResultResponse | null`.

| 상황 | `result` |
|---|---|
| 끝나지 않은 계획(PENDING_APPROVAL·APPROVED·RUNNING·ROLLING_BACK) | `null` |
| 끝난 계획 + 행 있음 | 아래 칸 모두 값(표의 NULL 허용 칸이 NULL이면 그 칸만 null) |
| 끝난 계획 + 행 없음·못 읽음·AD-1 조건 밖 | 객체, 안의 칸 전부 `null` |

`MigrationPlanResultResponse`(camelCase):

| 칸 | 타입 | 출처 |
|---|---|---|
| `writePauseMs` | int \| null | `write_pause_ms` |
| `missingRows` | int \| null | `missing_rows` |
| `duplicateRows` | int \| null | `duplicate_rows` |
| `rowsTotal` | int \| null | `rows_total` |
| `kind` | `"CUTOVER"` \| `"ROLLBACK"` \| null | `kind` |
| `measuredBy` | `"INFRA_SCRIPT"` \| null | 행이 있으면 고정값 |
| `pausedAt` | datetime(UTC) \| null | `paused_at` |
| `resumedAt` | datetime(UTC) \| null | `resumed_at` |
| `drainMs`·`catchupMs`·`switchMs` | int \| null | `drain_ms`·`catchup_ms`·`switch_ms` |

예(11/4 22:51 행):

```json
"result": {"writePauseMs": 5817, "missingRows": 0, "duplicateRows": 0, "rowsTotal": 168,
           "kind": "CUTOVER", "measuredBy": "INFRA_SCRIPT",
           "pausedAt": "2026-11-04T13:51:59.670Z", "resumedAt": "2026-11-04T13:52:05.487Z",
           "drainMs": 774, "catchupMs": 428, "switchMs": 4615}
```

- 오류 응답은 새로 생기지 않는다. 고객 DB를 읽지 못해도 200이다.
- 목록(Summary)·로그 응답은 바뀌지 않는다.

### 6-2. 데이터

- echo-was DB(`migration_plans` 등): **변경 없음**(D-5). 마이그레이션 0개.
- 고객 DB(읽기만, `echo_monitor`, DB `CUTOVER_RUNS_DATABASE`):

```sql
SELECT kind, paused_at, resumed_at, write_pause_ms, drain_ms, catchup_ms, switch_ms,
       rows_total, missing_rows, duplicate_rows
FROM echo_ops.cutover_runs
WHERE kind = $1 AND paused_at >= $2 AND ($3::timestamptz IS NULL OR paused_at < $3)
ORDER BY paused_at, created_at, id
LIMIT 1
```

- echo-was DB 읽기 1개 더: 같은 서비스에서 `created_at > 이 계획 created_at`인 계획 중 가장 이른 것의 `created_at`(인덱스 `(service_id, created_at)`). `MigrationPlanRepository`에 메서드 하나를 더한다.

### 6-3. 읽기 클라이언트

```text
class CutoverRunReader(Protocol):
    async def find_first(self, target: DeployTarget-like(environment_id, environment_kind),
                         kind: CutoverKind, since: datetime, until: datetime | None) -> CutoverRunResult
# CutoverRunResult = CutoverRun(…칸…) | CutoverRunNotFound | CutoverRunUnavailable(code)
```

- Postgres 구현:
  - `PostgresDataSyncChecker`와 같은 매핑·비밀 참조·TLS·시간 제한을 쓴다. 같은 파싱 함수를 재사용해도 된다.
  - 호출마다 연결 → `fetchrow` → `close`(풀 없음, 캐시가 횟수를 줄인다).
  - 잡는 예외는 `OSError`·`TimeoutError`·`asyncpg.PostgresError`·`asyncpg.InterfaceError`이고, 결과는 `Unavailable(code)`다.
- 기본 구현 `NoCutoverRuns`: 늘 `NotFound`(가짜 값 없음). 시험은 행 목록을 주입하는 시험용 읽기를 쓴다(런타임에 쓰지 않음).

### 6-4. 설정

| 이름 | 기본 | 뜻 |
|---|---|---|
| `CUTOVER_RUNS_DATABASE` | `app` | 결과 표가 있는 고객 DB 이름. 빈 값이면 기능 끔 |
| `CUTOVER_RESULTS_NOT_BEFORE` | 없음 | 이보다 먼저 끝난 계획은 읽지 않음(UTC ISO 8601). 운영 `2026-11-04T13:30:00Z` |
| `CUTOVER_RESULT_CACHE_SECONDS` | 5 | 못 찾음·창 열림·실패 결과를 다시 쓰는 시간(초, 1 이상) |

`deploy/base.yaml`(ConfigMap)에는 `CUTOVER_RUNS_DATABASE: app`과 `CUTOVER_RESULTS_NOT_BEFORE: "2026-11-04T13:30:00Z"`를 한 줄씩 더한다(캐시 초는 기본값 5를 쓰므로 넣지 않는다). `.env.example`·`deploy/README.md`에 설명을 적는다.

## 7. 호환성 (롤링 배포·옛 행)

- **옛 파드:** 롤링 동안 옛 API 파드의 상세 응답에는 `result` 칸이 없다. DB 변경이 없어서 옛 파드가 새 데이터를 읽다 깨지는 경우는 없다. 프론트는 `result`가 없거나 null인 경우를 같게 다룬다(지금 화면은 이 칸을 읽지 않음).
- **옛 일꾼:** 바뀌는 것이 없다(일꾼 코드 변경 없음).
- **옛 계획 행:**
  - 기능 이전에 끝난 계획도 규칙대로 계산한다.
  - 표가 생기기 전(11/4 22:30 KST 전)에 끝난 계획은 AD-5로 읽지 않는다. 그래서 11/4 21:14 복귀 계획에 22:52 롤백 행이 붙지 않는다.
  - 22:30 뒤에 끝났고 창이 열린 계획이 있으면 22:51·22:52 행이 붙을 수 있다. 운영 DB에서 그런 계획이 있는지 배포 전에 읽기 전용으로 확인한다(6절 M1 할 일 1).
- **고객 DB 재구축:** A를 새로 세우면 A의 옛 행이 사라진다. 그 뒤 aws가 도착인 옛 계획(복귀)의 `result` 칸은 null이 된다. 새 행은 정상으로 붙는다.
- **OpenAPI:** 칸 더하기만 한다. 프론트 타입을 다시 만들기 전까지 화면은 그대로다.

## 8. 이 PR 밖에서 M1이 할 일 (구현 뒤)

1. (배포 전, 읽기 전용) 운영 echo-was DB에서 `finished_at >= 2026-11-04T13:30Z`인 끝난 계획과 그 뒤 계획을 읽어, 22:51·22:52 행이 붙을 계획이 있는지 본다. 있으면 보고에 적는다.
2. (배포 뒤) API 파드에서 A NLB·B `echo-pg.echo-data`로 `echo_monitor`/`app` 접속이 되는지 본다. 끝난 계획 상세를 GET해 `result`가 오고 경고 로그가 없는지 확인한다(지금까지는 일꾼 파드만 붙었다).
3. 운영 시연 리허설(AC-4·AC-5): 시간을 정해 팀에 알린다(쓰기 정지 약 6~9초). 지민님 세션(M5 Aside `app.echoport.uk`)으로 이전 → `cutover.sh` → 복귀 → `rollback.sh`를 돌리고, 각 계획 상세 `result`를 스크립트 출력·표 행과 비교한다.
4. 이슈 #63 댓글로 프론트 담당(@kimdoyoung1110)에게 응답 모양과 AD-12 제안을 알린다(초안을 지민님께 먼저 보임).

## 9. 완료 판정 (AC)

| AC | 내용 | 확인 | 이슈 완료 기준 |
|---|---|---|---|
| AC-1 | 끝난 계획 + 창 안 행 → `result`의 11칸이 행 값과 같다(`writePauseMs`=`write_pause_ms` 등, `measuredBy`=`INFRA_SCRIPT`, 시각 UTC) | 통합 시험(시험용 읽기 주입) | 1 |
| AC-2 | 창 규칙: ① `finished_at` 전 행 제외 ② 다음 계획 `created_at` 이후 행 제외 ③ 창 안 여러 행이면 `paused_at` 가장 이른 것 ④ FAILBACK은 ROLLBACK만, PLANNED·EMERGENCY는 CUTOVER만 ⑤ 다음 계획이 없으면 끝 없음 ⑥ 다른 서비스의 다음 계획은 창을 닫지 않음 | 통합 시험 | 1·2 |
| AC-3 | null·200: ① 행 없음 ② 매핑 없음 ③ 비밀 없음 ④ 접속 실패·3초 초과 ⑤ 권한 없음·표 없음(`PostgresError`) ⑥ 끝나지 않은 계획(`result: null`, 읽기 미호출) ⑦ `simulated=true`·fake 모드·`CUTOVER_RUNS_DATABASE=""`·AD-5 이전 계획(읽기 미호출). 모두 응답 200, 경고 로그에 주소·비밀값 없음 | 통합 + 단위 시험(가짜 `connect`) | 3 |
| AC-4 | (운영) 이전(aws→gcp) SUCCEEDED → `cutover.sh` → 5초(캐시) 안에 다음 조회에서 그 계획 상세 `writePauseMs`·`missingRows`·`duplicateRows`가 스크립트 출력(`쓰기 정지 시간 X.XXXs` ×1000, 누락, 중복)·B 표 행과 같다 | M1 운영 확인 | 1 |
| AC-5 | (운영) 복귀(gcp→aws, `mode=FAILBACK`) SUCCEEDED → `rollback.sh` → 복귀 계획 상세 `kind=ROLLBACK` + 값이 출력·A 표 행과 같다. 앞 이전 계획 값은 그대로(CUTOVER) | M1 운영 확인 | 2 |
| AC-6 | `docs/api/migration-plans.md` 새 소절(`progress` 절 뒤): 칸 표·null 규칙·창 규칙·캐시 5초·AD-10 주의·설정 표 3줄. 프론트 담당에게 응답 모양 이슈 댓글 | 문서 검토 + 댓글 링크 | 4 |
| AC-7 | 캐시: 같은 계획 두 번 GET(5초 안) → 읽기 1번. 찾음 + 창 닫힘 → 시간이 지나도 다시 안 읽음. 못 찾음 → 5초 뒤 다시 읽음. 실행 중 계획 1초 폴링 → 읽기 0번 | 통합 시험(주입 시계) | 3 보조 |
| AC-8 | 질의 고정: 단위 시험이 SQL 문자열·인자(`kind`, `since`, `until`=None 포함)·DB 이름 `app`·`echo_monitor`·TLS 옵션을 확인 | 단위 시험 | 1 보조 |
| AC-9 | 변경 범위: 마이그레이션 0(Alembic head `286d9ea982d6` 하나 그대로), `migration_plan_service.py`·`migration_plan_processor.py`·`main.py`·`get_migration_plan_service` 무변경, 새 라우트 없음(`ROLE_TABLE`·`test_authorization.py` 그대로), OpenAPI에 `MigrationPlanResultResponse`, `deploy/base.yaml`에 두 줄 | 검사(`git diff --stat`)·OpenAPI 시험 | — |
| AC-10 | 저장소 검사 전체 통과(ruff·mypy·pytest·`alembic check`) | CI | — |

자동 시험은 실제 고객 DB에 붙지 않는다. 가짜·목업 숫자를 화면·운영 응답에 내지 않는다(시험 주입 값만).

## 10. 위임한 세부사항 (워커가 정하고 보고)

- 클래스·함수 이름, 결과 타입 모양(저장소 규칙을 따름).
- 매핑·비밀 처리를 `PostgresDataSyncChecker`와 공유하는 방법(공통 헬퍼 추출 여부. 단 기존 데이터 확인 동작·시험은 바뀌지 않아야 함).
- 캐시 자료구조(크기 512 이하, 계획 id별 동시 요청 합치기).
- `CutoverKind`를 enum으로 둘지 Literal로 둘지(enum이면 `MigrationPlanMode` 아래).

## 11. 결정 기록

- 11/4 지민: D-1(방식, 이슈 본문), D-6(null·200, 이슈 본문).
- 11/5 00:0x 지민(채팅): D-①=A(조회 때 읽기 + 캐시, 15초→5초), D-②=A(이슈 규칙 그대로), D-③=A(`result` 평평), D-④=A(새 DB 칸 없음). AI 기본값 AD-1~12 거부 없음.
- 11/5 M1: 기준 main을 `def6052` → `43c39c0`로 옮기고 #61·#66 병합 차이를 2절·AD-9·AD-11·AC-9에 반영(결정 변경 없음). `deploy/base.yaml`에 `CUTOVER_RUNS_DATABASE: app`도 명시(기본값과 같지만 운영 설정을 눈에 보이게).
