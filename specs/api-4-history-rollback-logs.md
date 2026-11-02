# API 서버 4: 배포 이력 · 되돌리기 · 로그 API SPEC

- 상태: Confirmed
- 확인일: 2026-11-02
- Depth: Deep
- Ambiguity: Low (≈ 12% / 목표 ≤ 20%)
- High-impact unresolved decisions: None
- Interview rounds: 7 (R1~R7) + 체크포인트(R8: A1 C0 D1 E1) · 상태 파일 `.deep-interview/api-4-history-rollback-logs.state.md`
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/4 (Project #4, `part:API 서버`, `phase:1차`, In Progress)
- 저장소: `echo-was` · 브랜치 `feat/rollback-history-logs` (`main` = `687f3b5`에서)
- 체크포인트 그림: `~/archive/softbank-hackathon-2026/api-4/api4-checkpoint.html` (지민 확인 11/2: A1·C0·D1·E1)

## 1. 목표

서비스 배포 이력에서 **성공한 이전 버전을 골라 되돌리고**, 배포 과정의 **세 가지 로그**를 조회할 수 있게 한다.

- 이력: 기존 배포 요청 목록·단건 API를 확장한다(현재 버전 표시, 되돌리기 가능 여부, 필터).
- 되돌리기: `trigger = ROLLBACK` 배포 요청으로 접수한다. 기존 대기열·일꾼·헬스체크·겹침 규칙을 재사용하고, 새 빌드 없이 대상 배포 기록의 이미지와 설정 사본을 복원한다. 서비스 중 환경의 되돌리기가 성공하면 예비 환경도 자동으로 같은 버전을 따라간다.
- 로그: 배포 작업 로그는 일꾼이 DB에 실제로 저장하고, 빌드·실행 로그는 실행기의 로그 읽기 메서드로 즉석 조회한다(지금은 가짜 실행기, `simulated: true`).

GitHub 이슈 완료 기준(원문, 11/2 수정): **배포 이력 조회(성공·실패·취소 시도 포함, 현재 버전과 되돌리기 가능 여부 표시), 이력에서 고른 이전 성공 버전으로 되돌리기(이미지와 설정을 함께 복원, 서비스 중인 환경이면 예비 환경도 같은 버전으로 따라감), 배포 작업·빌드·실행 로그 조회.**

이슈 메모: 실제 빌드·클러스터 로그 연결은 인프라 3·5 이후 실행기 교체로 한다. 이번 작업을 병합해도 실제 연결 확인 전에는 이슈를 `In Progress`로 둔다.

## 2. 현재 맥락과 확인된 사실 (`main` = `687f3b5`)

- `app/models/deployment.py`: `Deployment`(`service_environment_id`, `image_repository`, `image_digest`, `config_snapshot`, `status`, `rollback_of_id`(자기 FK, `rollback_of_id <> id` CHECK), `started_at`, `finished_at`). DELETE는 DB 트리거가 막는다. 되돌리기 = 새 행 + `rollback_of_id` + 원본 이미지·설정 복사(CLAUDE.md "공통 모델").
- `app/models/deploy_request.py`: `trigger_valid` CHECK(`MANUAL`·`STANDBY_SYNC`), `cancel_reason_valid` CHECK(`SUPERSEDED`·`SERVICE_DELETED`), `source_request_consistency`(`trigger = STANDBY_SYNC ⇔ source_request_id IS NOT NULL`), `build_reused_consistency`(`build_id IS NULL ⇔ build_reused IS NULL`), 부분 unique 2개(실행 중 1 + 대기 1), DELETE 금지 트리거. `ref_type`·`ref_value`·`commit_sha`·`repo_url`·`config_snapshot`·`simulated`은 NOT NULL.
- `app/services/deploy_request_processor.py`: `advance()`가 상태별로 `_start`(QUEUED→BUILDING, 같은 연결에 실행 중 요청이 있으면 대기) → `_build`(`request.build_id`가 있고 빌드가 `SUCCEEDED`면 바로 `_create_deployment`) → `_deploy` → `_verify` → `_succeed`. `_create_deployment`는 `request.config_snapshot`을 복사한 `Deployment`(`IN_PROGRESS`)를 만들고 요청을 `DEPLOYING`으로 올린다. `_succeed`는 `trigger == MANUAL`일 때만 예비 연결마다 `_enqueue_standby_sync`를 부른다(`build_id=source.build_id`, `build_reused=True`). `_fail`은 배포 기록이 있으면 `FAILED`로 바꾸고 `_revert()`로 `DeployExecutor.revert`(직전=현재 배포 유지)를 부른다. `_lock()` 순서: 서비스 → 연결 → 요청 → (빌드), 마지막에 `JobLease.hold()`. `_simulated_failure(request)`가 `failAt`을 읽고, `BUILDING`이면 `_build` 맨 앞에서 `BUILD_FAILED`로 끝낸다.
- `app/services/deploy_request_service.py`: `create_deploy_request`(환경 `FOR SHARE` → 서비스 `FOR UPDATE` → 연결 잠금 → `enqueue_deploy_request`), `search_deploy_requests`, `get_deploy_request`(`find_in_organization`: 서비스의 조직으로 거름, 삭제된 서비스 포함). `app/services/deploy_support.py::enqueue_deploy_request`가 대기 요청을 `SUPERSEDED`로 밀어낸다.
- `app/routers/service_router.py`: `POST /{service_id}/deploy-requests`는 `DeveloperAccess`(사람만 허용되는 조건은 문서상 에이전트 403 `AGENT_READ_ONLY` — 구현 시 이 라우트가 쓰는 의존성과 **똑같은 것**을 되돌리기에 쓴다), `GET`은 `ViewerAccess`. `app/routers/deploy_request_router.py`: `GET /api/v1/orgs/{org_id}/deploy-requests/{id}`.
- `app/schemas/deploy_request.py`: `DeployRequestSummaryResponse`·`DeployRequestDetailResponse`, `ERROR_MESSAGES`(코드별 고정 문장), `SimulateRequest`(`failAt`), `CreateDeployRequest`.
- `app/executors/base.py`: `BuildExecutor`(`start`·`poll`)·`DeployExecutor`(`apply`·`poll`·`revert`) 등 Protocol, `app/executors/fake.py`의 가짜(external_ref `fake-<종류>|<멱등 키>|<시작 ms>|…`), `app/executors/__init__.py::build_executor_set`·`ExecutorSet`. 로그 읽기 메서드 없음.
- `tests/integration/test_authorization.py`: `ROLE_TABLE`과 `test_role_table_covers_every_api_route`(새 API 라우트가 표에 없으면 실패), `test_every_route_has_guards`.
- 로그 저장 표·로그 API는 없다. `audit_logs`는 403 거부 기록 전용(API 6).
- `deploy_requests`에는 요청자(누가 눌렀나) 칸이 없다 → 감사 로그 API 7 몫.
- 시험 기반: `tests/integration/conftest.py`의 TRUNCATE 목록, `factories.py`, `test_migrations.py`(칸 순서·CHECK·부분 unique·트리거 비교, 기존 표에 더한 칸은 `sort_order=APPENDED_COLUMN_SORT_ORDER`), `deploy_harness.py`. `alembic check`는 CHECK·부분 인덱스·트리거를 비교하지 않는다.
- 최근 revision: `20261102_1229_1d1601c0111a_add_host_suffix_to_services.py`.

### 앞 태스크에서 넘겨받은 결정
- 되돌리기(`rollback_of_id`)·배포 로그는 API 3 SPEC이 API 4로 미룸. 사용자가 요청을 직접 취소하는 API 없음(API 3 AD-2).
- 요청자 기록·감사 로그 조회 → API 7. 트래픽 전환·이전 → API 8. 환경 카드 상태 → API 5.
- 권한은 API 6: 조회=뷰어·에이전트, 쓰기=개발자 이상 사람, 라우트마다 검사기 하나.

## 3. 범위

### 포함
- 되돌리기 API 1개(`POST /deploy-requests/{id}/rollback`), 로그 API 1개(`GET /deploy-requests/{id}/logs`), 요청 목록·단건 응답과 쿼리 확장
- `deploy_requests` 칸 2개·CHECK 변경, 새 표 `deploy_request_logs`, Alembic revision 1개, `db-schema.sql` 갱신
- 일꾼 변경: ROLLBACK 시작 검사, 되돌리기 배포 기록 생성(`rollback_of_id`), 예비 따라가기 조건 확대, 배포 작업 로그 기록
- 실행기 로그 읽기 메서드 2개(인터페이스·가짜·계약 시험), 서버 측 비밀값 마스킹
- 문서: `docs/api/deploy-requests.md`, `docs/schema.md`, `docs/executors.md`, `docs/api/auth.md` 역할 표, 용어 사전(ROLLBACK, 배포 작업 로그 등), `README.md`(데모 순서)
- 시험: 단위 + DB 통합(되돌리기 접수·실행·겹침·재시작·로그·권한) + 마이그레이션 왕복

### 제외
- 실제 빌드·클러스터 로그 읽기 구현, 실제 실행기(인프라 3·5)
- 요청자 칸·감사 로그(API 7), 트래픽 전환(API 8), 사용자 직접 취소 API
- 실시간 로그 스트리밍(SSE·웹소켓), 로그 검색·필터(레벨·시간 범위), 로그 보존 기간 정책
- 새 이력 전용 엔드포인트·통합 타임라인, 배포 기록(`deployments`) 표 변경
- push, `main` 병합(지민 확인 뒤 M1이 한다)

## 4. 구성 요소

| 구성 요소 | 상태 | 비고 |
|---|---|---|
| 되돌리기 접수 API | active | `DeployRequestService`에 추가, 새 라우트 |
| 일꾼 변경(시작 검사·복원·따라가기·로그 기록) | active | `DeployRequestProcessor` |
| 이력 확장(필터·`isCurrent`·`rollbackable`) | active | 기존 목록·단건 응답 |
| 배포 작업 로그 표·저장소·조회 | active | `deploy_request_logs`, 기록 지점은 §5.3(서비스 삭제 API·내리기 일꾼·`enqueue_deploy_request` 포함) |
| 실행기 로그 읽기(빌드·실행) | active(가짜) | 실제 구현은 deferred(인프라 3·5) |
| 비밀값 마스킹(서버) | active | `app/core/log_masking.py` |

## 5. 흐름

### 5.0 잠금 규칙
API 3 규칙을 그대로 따른다: 환경(접수만, `FOR SHARE`) → 서비스 → 그 서비스의 연결(`id` 오름차순) → 요청 → 빌드 → 할 일. 상태 변경은 조건부 UPDATE. **배포 작업 로그 한 줄은 그 줄을 일으킨 상태 전환(또는 요청 삽입)과 같은 트랜잭션에서 쓰고, 전환이 실제로 반영된 경우(영향 행 1)에만 쓴다.** 로그 `seq`는 서비스 행 잠금을 쥔 채 `max(seq) + 1`로 정한다(같은 요청에 쓰는 모든 경로가 서비스 행을 먼저 잠그므로 경쟁이 없다). unique 위반은 버그로 보고 500이 되게 둔다.

### 5.1 되돌리기 접수 (`POST /api/v1/orgs/{orgId}/deploy-requests/{id}/rollback`)
1. 권한: 개발자 이상 사람(`POST …/services/{id}/deploy-requests`와 같은 의존성). 에이전트 토큰은 403 `AGENT_READ_ONLY`(거부 기록). 바디는 비어도 되고 `{"simulate": {"failAt": "DEPLOYING" | "VERIFYING"}}`만 허용한다. `failAt = BUILDING`은 422 `VALIDATION_ERROR`(되돌리기에는 빌드 단계가 없다). `simulate`는 `executor_mode = fake`일 때만 받고 아니면 422 `SIMULATE_NOT_ALLOWED`.
2. 트랜잭션(읽기): 대상 요청 `find_in_organization`. 없거나 남의 조직 → 404 `DEPLOY_REQUEST_NOT_FOUND`.
3. 트랜잭션(쓰기, §5.0 순서): 대상 서비스·환경(대상 요청의 `environment_id`)을 접수와 같은 방식으로 다시 읽고 잠근다.
   - 서비스가 없거나 소프트 삭제됐으면 → 404 `SERVICE_NOT_FOUND`(서비스 삭제가 끝나면 연결도 함께 사라지므로 이 경로가 정상 흐름의 "삭제된 대상" 응답이다). `DELETING` → 409 `SERVICE_DELETING`. 환경이 삭제됐으면 → 404 `ENVIRONMENT_NOT_FOUND`(방어용 검사). 환경 `connection_status = FAILED` → 409 `ENVIRONMENT_UNHEALTHY`.
   - 대상 요청이 `SUCCEEDED`가 아니거나 `deployment_id`가 없으면 → 409 `NOT_ROLLBACKABLE`. 대상 요청의 연결이 삭제돼 있으면(정상 흐름에서는 서비스도 삭제돼 위 404가 먼저 나오므로 방어용) 같은 409.
   - 연결 행을 잠근 뒤 `connection.current_deployment_id == 대상.deployment_id`이면 → 409 `ALREADY_CURRENT`.
   - 대기 요청을 `SUPERSEDED`로 밀어내고(`enqueue_deploy_request`) 새 요청과 할 일을 넣는다: `trigger = ROLLBACK`, `rollback_of_request_id = 대상.id`, `rollback_base_deployment_id = connection.current_deployment_id`(접수 순간 값), `ref_type = COMMIT`, `ref_value = 대상.commit_sha`, `commit_sha`·`repo_url` = 대상 값, `build_id = 대상.build_id`, `build_reused = true`, `config_snapshot` = 대상 배포 기록의 `config_snapshot` 사본, `simulate`(있으면), `simulated = (executor_mode == fake)`.
   - 로그 한 줄: `REQUEST_ACCEPTED`.
4. 202 + `DeployRequestDetail`(`status = QUEUED`, `trigger = ROLLBACK`).

거절 순서: 권한 → 바디 검증(422) → 대상 요청(404) → 서비스·환경 → 상태 검사(409). 접수 로그에 비밀값·외부 원문을 넣지 않는다.

### 5.2 일꾼: ROLLBACK 요청 처리 (기존 `advance()` 흐름에 아래만 더한다)
1. `QUEUED` 시작(`_start`): 서비스 `DELETING`이면 기존대로 `CANCELLED(SERVICE_DELETED)`. 같은 연결에 실행 중 요청이 있으면 기존대로 미룬다. **`trigger = ROLLBACK`이고 `connection.current_deployment_id != request.rollback_base_deployment_id`이면** 요청을 `CANCELLED`(`cancel_reason = BASE_CHANGED`, `finished_at`)로 끝낸다(조건부 UPDATE, 로그 `CANCELLED_BASE_CHANGED`). 같으면 기존대로 `BUILDING`으로 올린다.
2. `BUILDING`: 요청에 `build_id`(성공한 빌드)가 이미 있으므로 기존 `_build`가 `_create_deployment`로 바로 넘어간다(새 빌드·빌드 실행기 호출 없음). `_create_deployment`는 `trigger = ROLLBACK`이면 새 `Deployment`에 `rollback_of_id = 대상 요청의 deployment_id`를 더한다(이미지·digest는 `build`에서, `config_snapshot`은 요청 사본에서 — 둘 다 대상 값과 같다).
3. `DEPLOYING`·`VERIFYING`·성공·실패·`revert`는 기존 코드 그대로다. 헬스체크 실패·배포 실패 때 새 배포 기록은 `FAILED`, `revert`는 현재 배포를 유지하고 `revert_status`를 적고, 연결의 현재 배포·`traffic_weight`는 바뀌지 않는다.
4. 성공 처리(`_succeed`): 예비 따라가기 조건을 `trigger in (MANUAL, ROLLBACK)`으로 넓힌다. 서비스 중 연결(`traffic_weight > 0`)의 성공만 예비마다 `STANDBY_SYNC`를 만든다(원본 요청의 `build_id`·`commit_sha`·`ref`·`config_snapshot`(주소만 예비 것)). 예비 연결을 직접 되돌린 경우(`traffic_weight == 0`)에는 만들지 않는다. 같은 연결의 `ROLLBACK`도 기존 `route_main` 판정을 그대로 받는다.
5. 서비스 삭제·임대 상실·제한 시간·`WORKER_LOST`·겹침 규칙은 `MANUAL` 요청과 같다.

### 5.3 배포 작업 로그 기록 지점 (고정 문장, `app/services/deploy_log_messages.py`)
모든 줄은 §5.0 규칙(같은 트랜잭션·영향 행 1일 때만·서비스 행 잠금 안에서 `seq`)을 따른다. 줄을 쓰는 지점은 **일꾼 밖 경로까지 포함해 아래가 전부**다. 시각(`logged_at`)은 주입한 `Clock.now()`다.

| 지점 | 쓰는 줄(`code`) | 대상 요청 |
|---|---|---|
| 접수 `create_deploy_request`·`create_rollback`(요청 삽입 트랜잭션) | `REQUEST_ACCEPTED` | 새 요청 |
| `deploy_support.enqueue_deploy_request`(API 접수와 `_enqueue_standby_sync`가 공용 — 로그 저장소·시각을 인자로 받는다) | 밀려난 대기 요청에 `CANCELLED_SUPERSEDED` | 밀려난 요청 |
| `_enqueue_standby_sync` | 새 따라가기 요청에 `REQUEST_ACCEPTED`, 원본 요청에 `STANDBY_SYNC_REQUESTED`(예비마다 1줄) | 둘 다 |
| `DeployRequestProcessor._start`(`QUEUED→BUILDING` 전환 성공) | `STARTED`. 이때 `request.build_id`가 이미 있으면(ROLLBACK·STANDBY_SYNC) 이어서 `BUILD_REUSED` | 그 요청 |
| `_start`의 취소 전환 | `CANCELLED_SERVICE_DELETED` / `CANCELLED_BASE_CHANGED` | 그 요청 |
| `_attach_build`(`update_if` 성공, `build_id`가 없던 MANUAL 요청) | 새 빌드면 `BUILD_ATTACHED`, 재사용이면 `BUILD_REUSED` | 그 요청 |
| `_create_deployment`(`BUILDING→DEPLOYING`) | `DEPLOYMENT_CREATED` | 그 요청 |
| `_deploy`: `deploy_requested_at` 적는 전환 / `DeployApplied` 전환 | `DEPLOY_REQUESTED` / `DEPLOY_APPLIED` | 그 요청 |
| `_verify`: 프로브 결과 기록 `update_if`에 `last_health_result IS DISTINCT FROM :새 값` 조건을 더하고 **그 갱신이 반영됐을 때만** | `HEALTH_PROBE_HTTP_ERROR` / `HEALTH_PROBE_UNREACHABLE` | 그 요청 |
| `_succeed`(전환 성공, `SUCCEEDED` 줄 앞) | `HEALTH_PROBE_OK`, 이어서 `SUCCEEDED` | 그 요청 |
| `_fail`(전환 성공) | `FAILED`(`message` = 그 에러 코드의 고정 문장) | 그 요청 |
| `_revert`(`revert_status` 기록 성공) | `REVERT_SUCCEEDED` / `REVERT_FAILED` | 그 요청 |
| `ServiceService.delete_service`의 대기 요청 취소와 `ServiceTeardownProcessor`의 취소 | `CANCELLED_SERVICE_DELETED` | 취소된 요청 |

- 빌드 자체의 시작·성공·실패 줄은 **배포 작업 로그에 쓰지 않는다**(공유 빌드를 여러 요청이 기다려 요청마다 같은 줄이 나오지 않으므로). 빌드 진행은 `source = BUILD` 로그가 보여 주고, 빌드가 실패해 요청이 끝나면 `FAILED` 줄(에러 코드 문장)이 남는다.
- 요청 종류별 정상 순서: 새 빌드 MANUAL = `REQUEST_ACCEPTED, STARTED, BUILD_ATTACHED, DEPLOYMENT_CREATED, DEPLOY_REQUESTED, DEPLOY_APPLIED, HEALTH_PROBE_OK, SUCCEEDED`. 재사용 MANUAL = `BUILD_ATTACHED` 대신 `BUILD_REUSED`. ROLLBACK·STANDBY_SYNC = `REQUEST_ACCEPTED, STARTED, BUILD_REUSED, DEPLOYMENT_CREATED, DEPLOY_REQUESTED, DEPLOY_APPLIED, HEALTH_PROBE_OK, SUCCEEDED`. 헬스체크가 실패 뒤 성공하면 `SUCCEEDED` 앞에 `HEALTH_PROBE_HTTP_ERROR`(또는 `UNREACHABLE`)가 결과 종류가 바뀔 때만 끼고, `simulate.failAt = VERIFYING`은 프로브를 하지 않으므로 헬스 줄 없이 `FAILED`로 끝난다. 줄 사이에 상태 전환이 없는 `DEPLOY_REQUESTED`처럼 한 단계 안에서 일어나는 줄도 위 표의 지점에서만 쓴다.
- 각 줄은 `level`(`INFO`·`WARN`·`ERROR`: `FAILED`·`REVERT_FAILED`·`HEALTH_PROBE_*`는 `WARN`/`ERROR`, 나머지 `INFO`), `stage`(요청 상태 값 또는 `REVERT`), `code`, 고정 `message`를 갖는다. 외부 응답 원문·토큰은 넣지 않는다.
- 요청당 500줄에 도달하면 마지막 줄 자리에 `LOG_TRUNCATED` 한 줄을 쓰고 그 뒤 줄은 쓰지 않는다(상태 전환은 그대로 진행).

### 5.4 로그 조회 (`GET /api/v1/orgs/{orgId}/deploy-requests/{id}/logs`)
- 쿼리: `source`(필수: `DEPLOY_JOB`·`BUILD`·`RUNTIME`), `cursor`(선택), `limit`(1~500, 기본 200). 권한: 뷰어 이상 + 에이전트 토큰.
- 요청을 `find_in_organization`으로 읽는다(서비스가 삭제된 뒤에도 조회된다). 없거나 남의 조직 → 404 `DEPLOY_REQUEST_NOT_FOUND`. `source`·`limit` 위반 → 422 `VALIDATION_ERROR`. 잘못된 `cursor` → 422 `VALIDATION_ERROR`(`reason = INVALID_CURSOR`).
- `DEPLOY_JOB`: `deploy_request_logs`에서 `seq > cursor`(정수, 없으면 0)를 `seq` 오름차순으로 `limit + 1`개 읽어 `hasMore`를 정한다. `nextCursor`는 돌려준 마지막 줄의 `seq`(문자열), 줄이 없으면 받은 `cursor`(없으면 `null`). `simulated = request.simulated`.
- `BUILD`: `request.build_id`가 있고 그 빌드에 `external_ref`가 있으면 `BuildExecutor.read_logs(external_ref, cursor, limit)`. 없으면 빈 페이지(오류 아님). `simulated = build.simulated`. ROLLBACK·STANDBY_SYNC 요청은 재사용한 빌드의 로그를 돌려준다.
- `RUNTIME`: `request.deployment_id`와 `request.deploy_external_ref`가 모두 있으면 `DeployExecutor.read_runtime_logs(target, deployment, external_ref, cursor, limit)`(`target`은 요청의 연결·환경에서 만든다. 연결이 삭제돼 target을 못 만들면 빈 페이지). 둘 중 하나라도 없으면(배포 기록 전, `DEPLOYING` 직전) 빈 페이지. `simulated = request.simulated`.
- 실행기의 `simulated`가 대상 행의 `simulated`와 다르면(가짜로 만든 기록을 실제 실행기가 읽는 경우 등) 실행기를 부르지 않고 빈 페이지를 돌려준다. `executor_mode`가 `fake`가 아니면(`build_executor_set`이 아직 `NotImplementedError`) `BUILD`·`RUNTIME`은 실행기를 만들지 않고 빈 페이지를 돌려준다(500 금지).
- **API 프로세스의 실행기 접근**: 지금 `get_deploy_request_service`(`app/dependencies.py`)는 `executor_mode`만 받고 실행기는 일꾼만 만든다. `app/dependencies.py`에 `get_executor_set(settings, clock) -> ExecutorSet` 의존성을 더하고 `DeployRequestService`가 이를 주입받아 로그 조회에 쓴다(호출 시점에 평가해 시험이 바꾼 실행기를 따라간다).
- 실행기가 돌려준 모든 줄의 `message`는 응답 직전에 `mask_secrets()`를 한 번 거친다(§8.4). `DEPLOY_JOB` 줄에도 적용한다.
- 응답 칸 `LogPage`: `source`, `simulated`, `items[{at, level, message}]`, `nextCursor`, `hasMore`. 빈 페이지는 `items = []`, `hasMore = false`, `nextCursor = 받은 cursor 또는 null`.

### 5.5 이력 확장
- `GET /api/v1/orgs/{orgId}/services/{id}/deploy-requests`: 쿼리 `trigger`(`MANUAL`·`STANDBY_SYNC`·`ROLLBACK`, 반복 가능), `status`(7개 값, 반복 가능)를 더한다. 모르는 값은 422 `VALIDATION_ERROR`. 기존 `environmentId`·`page`·`size`·정렬(최신순)은 그대로.
- 응답(목록·단건 공통) 칸 추가: `isCurrent`(boolean), `rollbackable`(boolean), `rollbackOfRequestId`(uuid | null).
  - `isCurrent = request.deployment_id IS NOT NULL AND request.deployment_id == connection.current_deployment_id`.
  - `rollbackable = status == SUCCEEDED AND deployment_id IS NOT NULL AND 연결이 살아 있음 AND NOT isCurrent AND 서비스 status == ACTIVE`. 환경 접속 상태는 보지 않는다(접수에서 409로 거른다).
  - 연결·서비스 상태는 목록 한 페이지의 요청들에 대해 연결·서비스를 **한 번에** 읽어 계산한다(요청마다 쿼리 금지).
- `trigger`에 `ROLLBACK`, `cancelReason`에 `BASE_CHANGED` 값이 나올 수 있다. 목록 총 개수는 필터를 적용한 값이다.

## 6. 기능 요구사항

- `FR-1` 되돌리기는 이력에서 고른 요청 하나를 대상으로 한다. 대상은 같은 연결(서비스×환경)의 `SUCCEEDED` 요청이고, 그 요청이 만든 배포 기록이 현재 배포가 아니어야 한다.
- `FR-2` 되돌리기는 `trigger = ROLLBACK` 배포 요청이며 기존 대기열·일꾼·헬스체크·겹침 규칙(실행 1 + 대기 1, 대기 요청 `SUPERSEDED`)을 따른다. 새 빌드를 만들지 않고 대상의 빌드를 `reused = true`로 연결한다.
- `FR-3` 되돌리기가 만든 새 배포 기록은 대상 배포 기록의 `image_repository`·`image_digest`·`config_snapshot`을 그대로 복사하고 `rollback_of_id`가 대상 배포 기록을 가리킨다.
- `FR-4` 접수와 실행 시작 두 시점에 조건을 검사한다. 접수: `NOT_ROLLBACKABLE`·`ALREADY_CURRENT`·`SERVICE_DELETING`·`ENVIRONMENT_UNHEALTHY`. 시작: 접수 때 기억한 현재 배포와 지금 현재 배포가 다르면 `CANCELLED(BASE_CHANGED)`.
- `FR-5` 되돌리기 요청이 배포·헬스체크에서 실패하면 연결의 현재 배포·`traffic_weight`를 바꾸지 않고, 새 배포 기록을 `FAILED`로, `revertStatus`를 기록하고, 실패 이유를 `errorCode`와 배포 작업 로그에 남긴다.
- `FR-6` 서비스 중 연결의 `MANUAL`·`ROLLBACK` 성공은 예비 연결마다 `STANDBY_SYNC` 요청을 만든다. 예비 연결을 직접 되돌리면 만들지 않는다.
- `FR-7` 이력 목록·단건은 `isCurrent`·`rollbackable`·`rollbackOfRequestId`를 싣고 `trigger`·`status` 필터를 받는다. 실패·취소 요청도 목록에 나온다.
- `FR-8` 배포 작업 로그는 상태 전환과 같은 트랜잭션에서 한 줄씩 DB에 저장한다. 일꾼이 다시 시작해도 줄이 중복되거나 빠지지 않는다. 요청당 500줄 상한.
- `FR-9` 로그 조회는 `source` 세 가지를 하나의 엔드포인트로 제공하고 `cursor`·`limit`(≤500)로 나눠 읽는다. 응답에 `simulated`를 싣는다.
- `FR-10` 빌드·실행 로그는 실행기의 `read_logs`·`read_runtime_logs`로 읽는다. 실행기 계약은 비밀값을 가려서 돌려주는 것이다. 서버도 응답 직전에 알려진 토큰 모양을 `***`로 한 번 더 바꾼다.
- `FR-11` 권한: 되돌리기 접수는 개발자 이상의 사람(에이전트 토큰 403 `AGENT_READ_ONLY`, 뷰어 403 `ROLE_FORBIDDEN`, 거부 기록 남음). 이력·로그 조회는 뷰어 이상 + 에이전트 토큰.
- `FR-12` 가짜 실행기가 만든 결과(되돌리기 요청, 빌드·실행 로그)에는 `simulated: true`를 싣는다. 값은 행에 저장한 것을 쓰고 응답 때 설정으로 다시 계산하지 않는다.

## 7. 상태와 예외

### 7.1 배포 요청 상태 (`DeployRequestStatus`)
7개 값과 전환은 바뀌지 않는다. 바뀌는 것은 두 가지뿐이다.
- `trigger`에 `ROLLBACK` 추가. `BUILDING`은 ROLLBACK에게 "재사용한 빌드를 즉시 통과"하는 단계다(`build.reused = true`).
- `cancel_reason`에 `BASE_CHANGED` 추가(`QUEUED`에서만).
- DB CHECK 변경: `trigger_valid`에 `ROLLBACK`, `cancel_reason_valid`에 `BASE_CHANGED`. 새 CHECK: `(trigger = 'ROLLBACK') = (rollback_of_request_id IS NOT NULL)`, `(trigger = 'ROLLBACK') = (rollback_base_deployment_id IS NOT NULL)`. `rollback_of_request_id`·`rollback_base_deployment_id`는 FK(`ON DELETE RESTRICT`), `rollback_of_request_id <> id` CHECK.

### 7.2 에러 코드
- 새 HTTP 에러: `NOT_ROLLBACKABLE`(409), `ALREADY_CURRENT`(409). 기존 `DEPLOY_REQUEST_NOT_FOUND`·`SERVICE_DELETING`·`ENVIRONMENT_UNHEALTHY`·`ENVIRONMENT_NOT_FOUND`·`SIMULATE_NOT_ALLOWED`·`VALIDATION_ERROR` 재사용.
- `DeployRequestErrorCode`는 바뀌지 않는다(실패는 기존 `DEPLOY_FAILED`·`DEPLOY_TIMEOUT`·`HEALTH_CHECK_*`·`PUBLIC_URL_UNREACHABLE`·`WORKER_LOST`).

### 7.3 경계 조건
- 대상 요청이 `STANDBY_SYNC`거나 이미 `ROLLBACK`이어도 `SUCCEEDED`면 대상이 될 수 있다(그 요청이 만든 배포 기록을 복원).
- 대기 중인 ROLLBACK은 같은 연결의 더 새 요청(수동·롤백)에 `SUPERSEDED`로 밀린다. 실행 중 요청이 성공해 현재 배포가 바뀌면 그 뒤 시작하는 ROLLBACK은 `BASE_CHANGED`로 취소된다.
- 접수 후 서비스가 `DELETING`이 되면 기존 규칙대로 `CANCELLED(SERVICE_DELETED)`.
- 첫 서비스 중 환경이 되돌리기에 성공해도 `route_main` 판정은 기존과 같다(이미 서비스 중이면 변화 없음).
- 로그 줄이 500줄에 도달해도 상태 진행은 멈추지 않는다.

## 8. 계약

### 8.1 엔드포인트 (`/api/v1/orgs/{orgId}`, 응답 `ApiResponse` 봉투·camelCase)

| 메서드 · 경로 | 요청 | 성공 | 실패 | 최소 역할 |
|---|---|---|---|---|
| `POST /deploy-requests/{id}/rollback` | `RollbackRequest`(바디 선택, `simulate`만) | 202 `DeployRequestDetail` | 403 `AGENT_READ_ONLY`·`ROLE_FORBIDDEN`, 404 `DEPLOY_REQUEST_NOT_FOUND`·`SERVICE_NOT_FOUND`·`ENVIRONMENT_NOT_FOUND`, 409 `NOT_ROLLBACKABLE`·`ALREADY_CURRENT`·`SERVICE_DELETING`·`ENVIRONMENT_UNHEALTHY`, 422 `VALIDATION_ERROR`·`SIMULATE_NOT_ALLOWED` | 개발자(사람) |
| `GET /deploy-requests/{id}/logs?source&cursor&limit` | — | 200 `LogPage` | 404 `DEPLOY_REQUEST_NOT_FOUND`, 422 `VALIDATION_ERROR` | 뷰어·에이전트 |
| `GET /services/{id}/deploy-requests?environmentId&trigger&status&page&size` | — | 200 `Page[DeployRequestSummary]` | 404 `SERVICE_NOT_FOUND`, 422 | 뷰어·에이전트 |
| `GET /deploy-requests/{id}` | — | 200 `DeployRequestDetail` | 404 | 뷰어·에이전트 |

새 라우트 2개는 검사기 의존성을 하나씩 달고 `ROLE_TABLE`에 행을 추가한다. 응답 칸은 항상 있고 값이 없으면 `null`(API 2 D-15).

### 8.2 응답 칸
- `DeployRequestSummary`·`DeployRequestDetail`에 `isCurrent`·`rollbackable`·`rollbackOfRequestId` 추가. `DeployRequestDetail.build.reused`는 ROLLBACK·STANDBY_SYNC에서 `true`.
- `LogPage`: `source`(`DEPLOY_JOB`·`BUILD`·`RUNTIME`), `simulated`, `items[{at, level, message}]`(`level`: `INFO`·`WARN`·`ERROR`), `nextCursor`(string | null), `hasMore`.

### 8.3 데이터 (모델 → revision → `db-schema.sql`)
- `deploy_requests` 칸 추가(모델에 `sort_order=APPENDED_COLUMN_SORT_ORDER`): `rollback_of_request_id` uuid NULL FK, `rollback_base_deployment_id` uuid NULL FK(`deployments.id`). 위 CHECK 변경은 DROP + ADD로 한다. 인덱스: `(rollback_of_request_id)`.
- 새 표 `deploy_request_logs`: `id`(uuid PK, 기존 믹스인), `deploy_request_id` FK(`ON DELETE RESTRICT`), `seq` int NOT NULL(≥ 1 CHECK), `logged_at` timestamptz, `level` varchar(8)(CHECK `INFO`·`WARN`·`ERROR`), `stage` varchar(16), `code` varchar(48), `message` varchar(500), `simulated` bool, `created_at`·`updated_at`는 기존 믹스인 규칙을 따른다(없으면 `created_at`만). unique(`deploy_request_id`, `seq`). DELETE 금지 트리거는 기존 공용 함수 `prevent_append_only_delete()`(API 3 revision 소유, `audit_logs`도 사용)를 재사용한다(새 함수를 만들지 않고, downgrade에서 이 함수를 지우지 않는다). `logged_at`은 주입한 `Clock.now()` 값이다(DB `now()` 금지).
- 마이그레이션 downgrade: 새 표(그 표의 트리거·행 포함)와 새 칸·CHECK를 되돌린다. `trigger = 'ROLLBACK'` 또는 `cancel_reason = 'BASE_CHANGED'`인 `deploy_requests` 행이 있으면 분명한 메시지로 실패하고 아무것도 바꾸지 않는다(요청 이력을 지우지 않는다). 로그 표의 행은 표와 함께 사라진다.
- 시험 준비: `conftest.py` TRUNCATE 목록에 `deploy_request_logs`(FK 순서에 맞게), `test_migrations.py` 기대 목록에 새 CHECK·unique·트리거·칸 순서.

### 8.4 실행기·마스킹 계약
- `app/executors/base.py`에 값 객체 `LogLine(at, level, message)`, `LogChunk(lines, next_cursor, has_more)`와 오류 `InvalidLogCursor`를 더한다. `BuildExecutor.read_logs(external_ref: str, cursor: str | None, limit: int) -> LogChunk`, `DeployExecutor.read_runtime_logs(target: DeployTarget, deployment: DeploymentSpec, external_ref: str, cursor: str | None, limit: int) -> LogChunk`(`external_ref` = 요청의 `deploy_external_ref`). 계약: 상태를 바꾸지 않는 조회, `limit`을 넘지 않는 줄을 시간 순으로, 같은 `cursor`면 같은 줄, 비밀값(설치 토큰·레지스트리 비밀번호·환경변수 값 등)을 가려서 돌려줄 것, 보존 기간이 지나 없으면 빈 청크.
- 가짜 구현(`app/executors/fake.py`): 결정적인 줄(빌드: 저장소 받기·이미지 만들기·digest 기록 등 5~8줄, 실행: 서버 시작·헬스 경로 응답 등 5~8줄)을 만든다. 줄의 시각은 빌드·배포 각각의 `external_ref`에 든 시작 ms부터 줄마다 일정 간격(`clock.now()`를 쓰지 않아 같은 `cursor`면 항상 같은 줄). `cursor`는 줄 번호 문자열, 잘못된 값은 `InvalidLogCursor`. 시험이 가짜에 "토큰처럼 보이는 줄"을 주입할 수 있는 훅을 둔다(운영 설정에는 없음).
- 서버 마스킹(`app/core/log_masking.py::mask_secrets(text) -> str`): 다음 모양을 `***`로 바꾼다 — GitHub 토큰(`gh[pousr]_[A-Za-z0-9]{20,}`), AWS 액세스 키(`AKIA[0-9A-Z]{16}`), `Bearer <토큰>`, `sk-[A-Za-z0-9_-]{20,}`, `-----BEGIN … PRIVATE KEY-----` 블록. 한 번 더 거르는 방어용이며 실행기의 마스킹 책임을 대신하지 않는다.
- 실행기 묶음: `ExecutorSet`은 그대로이고 `build`·`deploy` 구현에 메서드가 더해진다. 시험 `conftest.py`의 harness에 `app.dependency_overrides[get_executor_set] = lambda: harness.executors…`(호출 시점에 읽는 람다)를 더해 API 응답이 시험용 가짜 실행기(토큰 주입 훅 포함)를 쓰게 한다. `tests/executor_contract.py`에 로그 읽기 공통 계약을 더한다.

### 8.5 설정
새 설정 없음(`log_page_default`·상한은 상수). `.env.example` 변경 없음.

## 9. 호환성·배포·복구
- 기존 요청 목록·단건 응답에 칸만 추가한다(필드 삭제·변경 없음). 칸을 무시하는 클라이언트는 영향이 없다. 키 집합을 고정한 기존 시험은 갱신한다(AC-17).
- 기존 `deploy_requests` 행: 새 칸 2개는 NULL, CHECK는 기존 `MANUAL`·`STANDBY_SYNC` 행과 호환된다. 이미 끝난 요청에는 로그 줄이 없다(빈 목록).
- 일꾼과 API를 함께 올린다. 일꾼만 새 코드면 ROLLBACK 요청을 처리하지 못하므로 같은 이미지로 배포한다(README에 적는다).
- 롤백(코드): revision `downgrade`(ROLLBACK 행이 없을 때만).

## 10. Acceptance criteria

시험은 가짜 실행기·주입 시계·`run_once()`로 결정적으로 돌린다. 모든 AC는 통합 시험이 건너뜀 0인 상태(로컬 DB)에서 통과해야 한다.

- `AC-1` 되돌리기 성공: aws 연결에 요청 r1(v1)·r3(v2) 성공 후 r1로 되돌리기 → 202 `QUEUED`·`trigger = ROLLBACK`·`rollbackOfRequestId`(응답에서 대상 요청 id가 보임). 일꾼 진행 → `SUCCEEDED`. 새 배포 기록 `image_digest`·`config_snapshot`이 r1 배포 기록과 같고 `rollback_of_id = r1.deployment_id`, 연결 `current_deployment_id` = 새 기록, `traffic_weight` 유지, 빌드 행 수 불변(`build.reused = true`), 상태 순서 `QUEUED → BUILDING → DEPLOYING → VERIFYING → SUCCEEDED`, 대상 r1은 다시 `rollbackable = true`이고 새 요청은 `isCurrent = true`.
- `AC-2` 접수 거절: 이미 현재인 요청 → 409 `ALREADY_CURRENT`; `FAILED`·`CANCELLED`·`QUEUED` 요청 → 409 `NOT_ROLLBACKABLE`; 없는 id·다른 조직 → 404 `DEPLOY_REQUEST_NOT_FOUND`; 서비스가 삭제를 마친 뒤 그 서비스의 옛 요청으로 접수 → 404 `SERVICE_NOT_FOUND`; 서비스 `DELETING` → 409 `SERVICE_DELETING`; 환경 `FAILED` → 409 `ENVIRONMENT_UNHEALTHY`; `failAt = BUILDING` → 422; 가짜가 아닌 모드에서 `simulate` → 422 `SIMULATE_NOT_ALLOWED`.
- `AC-3` 권한: 개발자·어드민 사람은 202, 뷰어 403 `ROLE_FORBIDDEN`, 에이전트 토큰 403 `AGENT_READ_ONLY`(둘 다 거부 기록 1건). 이력·로그 조회는 뷰어·에이전트 모두 200. `test_role_table_covers_every_api_route`·`test_every_route_has_guards`가 새 라우트 2개를 포함해 통과.
- `AC-4` 예비 따라가기: aws 서비스 중 + gcp 예비(둘 다 v2)에서 aws를 v1로 되돌리기 성공 → gcp에 `STANDBY_SYNC` 요청 1개(`sourceRequestId` = 되돌리기 요청, 원본 `build_id`, 재빌드 없음)가 생기고 성공 → gcp `versionStatus = IN_SYNC`. 예비(gcp)를 직접 되돌리면 `STANDBY_SYNC`가 생기지 않는다. 서비스가 `DELETING`이면 생기지 않는다.
- `AC-5` 되돌린 버전이 안 뜸(사전 부검 2): `simulate.failAt = VERIFYING` → `FAILED`/`HEALTH_CHECK_FAILED`, 새 배포 기록 `FAILED`, `revertStatus = SUCCEEDED`, 연결의 현재 배포·`traffic_weight` 그대로, 따라가기 요청 없음, 배포 작업 로그에 실패 코드·되돌리기 결과 줄이 있다. `failAt = DEPLOYING`도 같은 확인(`DEPLOY_FAILED`). 가짜 실행기에 주입한 연결 실패로도 같다.
- `AC-6` 상황 변화(사전 부검 1): (a) 연결에 `BUILDING` 요청 A(새 커밋)가 실행 중일 때 r1 되돌리기 B를 접수 → A가 성공해 현재 배포가 바뀐 뒤 B가 시작하면 `CANCELLED`/`BASE_CHANGED`이고 현재 배포는 A의 것 그대로, 배포 기록 추가 없음. (b) B가 대기 중일 때 같은 연결에 새 요청 C를 접수하면 B는 `CANCELLED`/`SUPERSEDED`(`supersededById = C`). (c) 되돌리기 두 건을 연달아 접수하면 하나만 대기한다(실행 1 + 대기 1). (d) 일꾼 2개 + 접수 동시 시험에서 한 연결에 실행 중 요청이 2개가 되는 순간이 없고, 접수와 일꾼의 `QUEUED → BUILDING`이 같은 요청에 겹쳐도 하나만 반영된다.
- `AC-7` 이력: 목록은 성공·실패·취소·ROLLBACK·STANDBY_SYNC를 최신순으로 보이고, `trigger`·`status` 필터(반복 가능)와 `environmentId`가 `total`까지 일치한다. `isCurrent`·`rollbackable`은 §5.5 정의와 같다(현재 요청 false/true, 실패 요청 둘 다 false, `DELETING` 서비스의 요청은 `rollbackable = false`, 삭제를 마친 서비스의 요청은 단건 GET에서 `rollbackable = false`). 목록 한 페이지가 요청 수에 관계없이 상수 개의 쿼리로 끝난다(쿼리 수 시험).
- `AC-8` 배포 작업 로그: 성공 요청 하나의 `DEPLOY_JOB` 로그가 §5.3의 "요청 종류별 정상 순서"(새 빌드 MANUAL·재사용 MANUAL·ROLLBACK·STANDBY_SYNC 각각)대로 `seq` 1부터 빠짐없이 나온다. 헬스체크 3회 실패 후 성공 시나리오에서 `HEALTH_PROBE_HTTP_ERROR`는 1줄, 이어서 `HEALTH_PROBE_OK`·`SUCCEEDED`. `simulate.failAt = VERIFYING`이면 헬스 줄 없이 `FAILED`. 취소 3종(`SUPERSEDED`·`SERVICE_DELETED`(접수 밖 경로인 서비스 삭제 API·내리기 일꾼 경로 포함)·`BASE_CHANGED`), 실패(에러 코드 문장), `REVERT_*`, 예비 따라가기 요청의 `REQUEST_ACCEPTED`와 원본의 `STANDBY_SYNC_REQUESTED` 줄이 각각 있다. 공유 빌드를 기다리는 두 요청의 로그는 서로 같은 구조다(빌드 줄이 한쪽에만 생기지 않는다).
- `AC-9` 재시작·중복(사전 부검 3): 단계 중간에 임대를 만료시켜 다른 일꾼이 이어 가는 시험에서 `(요청, seq)`가 연속이고 같은 전환 줄이 두 번 쓰이지 않는다. 조건부 UPDATE가 0행이면 로그가 쓰이지 않는다. 500줄 상한 시험: 줄을 499개 채운 요청이 다음 전환에서 `LOG_TRUNCATED`를 쓰고 이후 줄은 쓰지 않지만 상태는 정상 진행한다.
- `AC-10` 페이징: `limit = 2`로 `DEPLOY_JOB`을 읽으면 `hasMore`·`nextCursor`가 맞고 `cursor`로 이어 읽으면 모든 줄이 겹침·누락 없이 한 번씩 나온다. `limit = 0`·`501`, 모르는 `source`, 숫자가 아닌 `cursor`(`DEPLOY_JOB`) → 422. `BUILD`·`RUNTIME`도 가짜 실행기로 같은 페이징 시험. 빌드 번호 없는 `QUEUED` 요청의 `BUILD`, 배포 기록 없는 요청의 `RUNTIME`은 200 빈 페이지.
- `AC-11` 로그 조회 권한·범위: 다른 조직 요청 → 404, 삭제된 서비스의 요청 로그도 조회된다. 실행기의 `simulated`와 행의 `simulated`가 다르면 빈 페이지.
- `AC-12` `simulated`(사전 부검 4): 가짜 실행기 모드에서 `BUILD`·`RUNTIME`·`DEPLOY_JOB` 응답의 `simulated = true`, 이 값은 행에서 읽은 것이다(설정을 바꿔도 과거 기록 응답이 변하지 않는 시험).
- `AC-13` 비밀값: 시험용 토큰 문자열(`ghs_…`, `AKIA…`, `Bearer …`, `sk-…`, 개인 키 블록)을 가짜 실행기에 주입해 `BUILD`·`RUNTIME` 응답에 `***`만 나오고 원문은 0회, 로그(caplog)·모든 응답 본문·DB 덤프(모든 표 텍스트)에도 0회. `mask_secrets` 단위 시험(여러 줄·경계·일반 문장 불변).
- `AC-14` 실행기 계약: `tests/executor_contract.py`의 로그 읽기 공통 계약(같은 `cursor` 같은 줄, `limit` 준수, 시간 순, 잘못된 `cursor` → `InvalidLogCursor`)을 가짜가 통과한다.
- `AC-15` 스키마: revision 왕복(upgrade → downgrade → upgrade), `alembic check` 차이 0, `scripts/dump_schema.sh` 결과와 `db-schema.sql` 일치, `test_migrations.py`가 새 칸·CHECK·FK·unique·인덱스·DELETE 금지 트리거를 비교해 통과, `ROLLBACK` 행이 있으면 downgrade가 분명한 메시지로 실패. 새 CHECK가 `trigger = ROLLBACK`과 두 칸의 일치를 강제함(직접 INSERT 거부 시험).
- `AC-16` 문서: `docs/api/deploy-requests.md`(되돌리기·로그·이력 확장·새 칸·오류 코드), `docs/schema.md`(새 표·칸·상태), `docs/executors.md`(로그 읽기 계약·마스킹), `docs/api/auth.md` 역할 표 2행, 용어 사전(ROLLBACK, 배포 작업 로그, BASE_CHANGED), `README.md`(되돌리기·로그 데모 순서, 일꾼·API 함께 배포).
- `AC-17` 품질: ruff·format·mypy(strict, `app`)·pytest 통과, 통합 시험 건너뜀 0. 응답에 칸 3개를 더하므로 응답 키 집합을 고정한 시험(`tests/integration/test_deploy_flow.py`의 `DETAIL_KEYS` 등)은 새 3칸을 더해 갱신한다. 그 밖의 기존 시험은 수정 없이 통과하고, 깨지는 시험이 더 있으면 이유를 보고한다. 로그 시험을 위해 `conftest.py`의 harness에 `get_executor_set` 오버라이드를 더한다.

## 11. 핵심 엔티티

| 이름 | 필드 | 관계 |
|---|---|---|
| `DeployRequest`(확장) | `trigger`(+`ROLLBACK`), `cancel_reason`(+`BASE_CHANGED`), `rollback_of_request_id`, `rollback_base_deployment_id` | `rollback_of_request_id` → `deploy_requests.id`, `rollback_base_deployment_id` → `deployments.id` |
| `DeployRequestLog`(신규) | `deploy_request_id`, `seq`, `logged_at`, `level`, `stage`, `code`, `message`, `simulated` | N:1 `DeployRequest` |
| `Deployment`(변경 없음) | `rollback_of_id` | 되돌리기 배포 기록이 대상 배포 기록을 가리킴 |
| `LogLine`·`LogChunk`(값 객체) | `at`, `level`, `message` / `lines`, `next_cursor`, `has_more` | 실행기 → 서비스 |

## 12. 리스크 (사전 부검 결과, 사용자 채택 4개)

| 리스크 | 처리 | 연결 |
|---|---|---|
| 되돌리기 중 상황 변화로 의도와 다른 버전이 올라감 | 요구사항화: 접수·시작 이중 검사, `BASE_CHANGED` | FR-4, AC-2, AC-6 |
| 되돌린 버전이 지금 환경에서 안 뜸 | 요구사항화: 현재 유지, 실패 기록, 실패 주입 | FR-5, AC-5 |
| 로그 폭주·중복·누락 | 요구사항화: 같은 트랜잭션·unique·상한·페이징 | FR-8, FR-9, AC-8~10 |
| 가짜 로그 오해·비밀값 노출 | 요구사항화: `simulated` 저장·표시, 이중 마스킹 | FR-10, FR-12, AC-12, AC-13 |
| 감수: 로그 보존 기간·검색·스트리밍 없음 | 수용(범위 밖) | §3 제외 |
| 감수: 요청자 칸 없음(누가 되돌렸는지 알 수 없음) | 수용(API 7) | §3 제외 |

## 13. 결정 기록

| ID | 결정 | 근거 | 출처 |
|---|---|---|---|
| D-1 | 되돌리기 = `trigger = ROLLBACK` 배포 요청, 기존 일꾼·대기열·헬스체크 재사용, 빌드만 건너뜀 | 겹침 규칙·실패 시 직전 버전 유지·가짜 실패 주입 재사용 | 사용자(R1) |
| D-2 | 서비스 중 환경의 되돌리기 성공 → 예비도 자동으로 따라감 | 안 따라가면 재난 이전 때 깨진 버전이 서비스 중이 됨 | 사용자(R2) |
| D-3 | 로그 하이브리드: 배포 작업 로그 DB 저장, 빌드·실행 로그 실행기 즉석 조회(가짜 simulated) | 실행 로그 무한 증가 회피, 실제 연결 때 구현만 교체 | 사용자(R3) |
| D-4 | 이력 한 줄 = 배포 요청 1개, 기존 목록 API 확장 | 실패·취소 포함, 새 엔드포인트 최소 | 사용자(R4) |
| D-5 | 대상 = 이력에서 고른 한 행(`POST …/deploy-requests/{id}/rollback`), 같은 연결 SUCCEEDED·현재 아님, 설정은 그 사본 그대로 | 이력 화면 자연스러운 흐름 | 사용자(R5) |
| D-6 | 세 로그 조회는 뷰어 이상 + 에이전트, 실행기 계약에 마스킹 의무 | AI 파트 데모가 로그를 읽어야 함 | 사용자(R6) |
| D-7 | 사전 부검 4개 모두 요구사항 | 데모 실패 비용 대비 추가분 작음 | 사용자(R7) |
| AD-1 | 되돌리기 접수 = 개발자 이상 사람(에이전트 403), 조회 = 뷰어+에이전트 | 지민 권한 원칙("되돌릴 수 있는 일은 개발자") | AI-default(규칙) |
| AD-2 | ROLLBACK 요청은 대상 요청의 `build_id`를 `build_reused = true`로 연결해 기존 `_build`가 즉시 통과 | 전환표·CHECK·예비 따라가기의 `build_id` 유지 | AI-default (사용자 승인 C0) |
| AD-3 | `rollback_of_request_id`·`rollback_base_deployment_id` 칸 추가, `ref = COMMIT` + 대상 커밋, `config_snapshot` = 대상 배포 사본 | `ref_*` NOT NULL, 접수 시점 값 보존 | AI-default (C0) |
| AD-4 | 접수 거절 규칙(§5.1) | D-5 조건의 구체화 | AI-default (C0) |
| AD-5 | 시작 때 현재 배포가 접수 때와 다르면 `CANCELLED(BASE_CHANGED)` | 사용자가 못 본 새 배포를 덮어쓰지 않음(D-7-1) | AI-default (C0) |
| AD-6 | 따라가기 조건을 `MANUAL`·`ROLLBACK`으로 확대, 예비 직접 되돌리기는 따라가기 없음 | D-2의 구체화 | AI-default (C0) |
| AD-7 | 목록에 `trigger`·`status` 필터, `isCurrent`·`rollbackable`·`rollbackOfRequestId` | D-4의 구체화 | AI-default (C0) |
| AD-8 | `deploy_request_logs` 표, 같은 트랜잭션, 요청당 500줄 상한, 삭제 금지 | D-7-3 | AI-default (C0) |
| AD-9 | 로그 API 1개(`source`·`cursor`·`limit`), 응답 `LogPage` | D-3의 구체화 | AI-default (C0) |
| AD-10 | 실행기 로그 읽기 메서드 + 서버 이중 마스킹(★방어용) | D-6·D-7-4 | AI-default (C0) |
| AD-11 | 로그 기록 시점·헬스 줄은 결과 종류가 바뀔 때만 | 폭주 방지(D-7-3) | AI-default (C0) |
| AD-12 | ROLLBACK도 `simulate.failAt`을 받음(가짜 모드만) | D-7-2 시험 수단 | AI-default (C0) |
| AD-13 | 권한 표·역할 표 시험 갱신 | D-6, AD-1 | AI-default (C0) |
| AD-14 | 문서·마이그레이션·시험 갱신 목록 | 저장소 규칙 | AI-default (C0) |
| AD-15 | (검토 전 초안에서 변경) `failAt = BUILDING`은 ROLLBACK에서 422(빌드 단계가 없어 `BUILD_FAILED`가 의미 없음). 체크포인트의 AD-12는 `DEPLOYING`·`VERIFYING`만 허용으로 좁힌다 | 코드 확인: `_build`가 `failAt = BUILDING`이면 배포 기록 없이 `BUILD_FAILED`로 끝냄 | AI-default — 결정의 세부를 바꾼 것(거부권) |
| AD-16 | (체크포인트 AD-9에서 변경) `LogPage.items`에 항목별 `cursor`를 두지 않고 페이지 `nextCursor`만 둔다 | 실행기가 줄마다 커서를 주지 않아도 되게 단순화 | AI-default — 결정의 세부를 바꾼 것(거부권) |

| AD-17 | (검토 반영) 로그 기록 지점 전체표·요청 종류별 정상 순서(§5.3), 빌드 시작·성공·실패 줄은 DEPLOY_JOB에서 제외, `read_runtime_logs`에 `external_ref` 인자, API 프로세스용 `get_executor_set` 의존성, `logged_at`은 주입 Clock, 트리거는 기존 `prevent_append_only_delete()` 재사용, downgrade 문구 | 코드 확인(공유 빌드·서비스 삭제 경로·`DeployTarget`에 `external_ref` 없음·API에 `ExecutorSet` 없음) | AI-default (검토 반영) |
| AD-18 | "현재 배포가 아님"은 **배포 기록 id**로 판정한다(D-5 문구 그대로). 현재와 이미지·설정이 같은 다른 행(예: 되돌리기 원본)도 `rollbackable = true`일 수 있고, 그때 되돌리기는 같은 이미지를 다시 반영한다 | 검토가 지적(digest·설정 비교로 넓히면 이력에서 "이미 그 버전"이 숨겨짐). 단순한 쪽을 유지, 지민 판단 가능 | AI-default — 결정의 세부(거부권) |

## 14. 구현 계획으로 위임한 low-impact 세부사항
- 로그 `message` 문구 원문(영어/한국어 선택은 기존 `ERROR_MESSAGES`와 같은 영어 고정 문장), 가짜 로그 줄의 문구·간격, `deploy_log_messages` 모듈 구성.
- `deploy_request_logs` 컬럼 길이·믹스인 사용 여부, 인덱스 이름.
- 이력 응답 계산을 위한 저장소 메서드 이름·쿼리 구성(상수 개 쿼리 조건만 지킨다).
- `NOT_ROLLBACKABLE`·`ALREADY_CURRENT` 예외 클래스 이름과 `details` 칸(대상 요청 id 등).
- 로그 `stage` 값의 정확한 문자열 집합(요청 상태 값 + `REVERT`).
