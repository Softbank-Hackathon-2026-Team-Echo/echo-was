# 시연 조직 스냅샷 · 복귀 API

시연 연습을 몇 번 돌려도 같은 시연 환경으로 돌아오게 한다. 조직 ADMIN 이 깨끗한 상태에서 스냅샷을 한 번 따 두고, 시연 뒤 `restore` 를 부르면 정해진 순서로 **기존 서비스 함수만** 불러 되돌린다. 프론트 버튼은 없다(API 만). 코드: `app/routers/demo_snapshot_router.py`, `app/services/demo_snapshot_service.py`, `app/repositories/demo_snapshot_repository.py`.

- 경로는 `/api/v1/orgs/{orgId}/demo` 아래, 공통 봉투 `ApiResponse`, camelCase.
- 새 표·마이그레이션 없음. 스냅샷은 감사 줄 하나(`action = DEMO_SNAPSHOT`, 대상 = 조직)의 `details.snapshot` 이다. 감사 줄은 고칠 수도 지울 수도 없어 스냅샷이 바뀌지 않고, 옛 스냅샷도 이력으로 남는다. **결과가 `SUCCEEDED` 이고 `details.snapshot` 이 있는 줄** 중 가장 늦은 줄(`seq` 최대)이 지금 스냅샷이다. 권한 없는 `POST /demo/snapshot` 도 같은 action 이름으로 거부(`DENIED`, details 빈 값) 줄을 남기므로 그 줄은 고르지 않는다. 비밀값은 담지 않는다(id·이름·비율·커밋·digest·상태만).

## 엔드포인트

| 메서드 · 경로 | 성공 | 대표 실패 | 최소 역할 |
|---|---|---|---|
| `POST /demo/snapshot` | 201 `DemoSnapshot` | 409 `DEMO_STATE_NOT_CLEAN`(details 에 이유), 403 | 어드민(사람) |
| `GET /demo/snapshot` | 200 `DemoSnapshot`(가장 최근) | 404 `DEMO_SNAPSHOT_NOT_FOUND`, 403(에이전트 포함) | 어드민 |
| `POST /demo/restore` 본문 `{"dryRun": false}`(생략 가능) | 200 `DemoRestore` | 404 `DEMO_SNAPSHOT_NOT_FOUND`, 409 `DEMO_RESTORE_IN_PROGRESS`(같은 조직의 다른 복귀가 도는 중, 끝난 뒤 다시 부름), 403 | 어드민(사람) |

## 스냅샷 조건 (깨끗한 상태에서만)

아래 중 하나라도 걸리면 409 `DEMO_STATE_NOT_CLEAN` 이고 아무것도 쓰지 않는다. `details[] = {field, reason, value(대상 id)}`.

| reason | 뜻 |
|---|---|
| `OPEN_MIGRATION_PLAN` | 열린 이전 계획(승인 대기·승인됨·실행 중·롤백 중) |
| `ACTIVE_DISASTER` | 진행 중 재난 |
| `OPEN_DEPLOY_REQUEST` | 끝나지 않은 배포 요청 |
| `SERVICE_NOT_SINGLE_SERVING` | 배포된 서비스인데 비율 100 연결이 정확히 하나가 아님 |
| `DEPLOYMENT_SOURCE_MISSING` | 현재 배포를 만든 성공 배포 요청이 없음(되돌리기 대상 없음) |
| `SERVICE_DELETING` | 지우는 중인 서비스 |

기록 내용: 서비스별 연결(환경·비율·현재 배포·그 배포를 만든 배포 요청·커밋·이미지 digest)과 서비스 중 환경, 환경 목록(이름·종류·접속 상태), 보안 예외(id·통제·범위·승인/철회·만료), 조직 `allowSelfApproval`.

## 복귀 단계 (순서 고정)

| # | `step` | 하는 일 | 부르는 기존 함수 |
|---|---|---|---|
| 1 | `MIGRATION_PLANS` | 승인 대기 → 취소, 승인됨·실행 중 → 중단(일꾼이 롤백). 롤백·중단 중이면 `WAITING`. 스냅샷 서비스 중 환경으로 돌아가는 **DB 쓰기 전환 없는** 승인 대기 계획만 남긴다(`KEEP_FAILBACK_PLAN`). DB 쓰기 전환이 든 계획은 복귀 계획이라도 취소한다(`detail = includeDbCutover=true`, 승인하면 일꾼이 rollback Job 을 만든다) | `cancel_plan` · `abort_plan` |
| 2 | `DISASTERS` | 진행 중 재난 종료(복귀 잠금 해제) | `end_disaster` |
| 3 | `AI_RUNS` | 열린 사전 점검·배포 실패 진단 취소, 스냅샷 뒤 끝난 실행의 결정 안 된 수정안 거절. 반영 실행(APPLY_PATCH)은 취소 함수가 없어 `WAITING` | `cancel_precheck` · `reject_patch` |
| 4 | `DEPLOY_REQUESTS` | 열린 배포 요청은 취소 API 가 없어 끝나길 `WAITING` | — |
| 5 | `TRAFFIC` | 서비스 중 환경이 100 이 아니면 그 환경으로 **DB 쓰기 전환 없는** 이전 계획 만들기 + 승인(일꾼이 진입점 실행기로 비율 적용). 자기 승인이 꺼져 있으면 `MANUAL_REQUIRED`(`APPROVAL_REQUIRED`, 다른 개발자 이상이 승인). 7 단계가 쓰기 담당을 모른다고 했으면 되돌리지 않고 `MANUAL_REQUIRED`(`HOLD_FOR_DB_WRITER` · `DB_WRITER_UNCONFIRMED`). 승인 직전에 계획을 다시 읽어 DB 쓰기 전환이 들었으면 승인하지 않는다(`FAILBACK_PLAN_INCLUDES_DB_CUTOVER`). 읽은 뒤 누가 계획을 열었거나(`MIGRATION_PLAN_OPEN`) 승인·취소했으면(`PLAN_NOT_PENDING`) `WAITING` | `create_plan` · `approve_plan` |
| 6 | `DEPLOYMENTS` | 연결의 현재 이미지가 스냅샷과 다르면 스냅샷 배포 요청으로 되돌리기(빌드 재사용). 서비스 중을 먼저, 예비는 그 뒤. 5 단계가 DB 쓰기 담당 때문에 트래픽을 두었으면 `MANUAL_REQUIRED`(`TRAFFIC_HELD_FOR_DB_WRITER`) | `create_rollback` |
| 7 | `DB_WRITER` | **하지 않음**(컷오버 Job 영역), 읽기만. **5 단계보다 먼저 돈다.** 스냅샷 뒤 DB 쓰기 전환 단계가 든 계획 중 그 Job 이 돌았거나 돌 수 있는 것(승인됨·실행 중·롤백 중, 끝났는데 그 단계 결과가 PENDING·SKIPPED 가 아님)이 있으면 `MANUAL_REQUIRED`(`DB_CUTOVER_SINCE_SNAPSHOT`, 대상 = 계획)이고 그 서비스의 트래픽·배포를 되돌리지 않는다. 승인 전 취소·건너뜀처럼 돌지 않은 계획은 `OK`(`DB_CUTOVER_NOT_STARTED`) | — |
| 8 | `DEMO_APP_DATA` | 데모 앱 순번 쓰기 표 비우기: 앱에 reset 엔드포인트가 없고 WAS 는 앱 DB 에 붙지 않아 항상 `MANUAL_REQUIRED`(`APP_RESET_ENDPOINT_MISSING`) | — |
| 9 | `AI_FIX_PULL_REQUESTS` | 스냅샷 뒤 AI 반영이 연 수정 PR 은 WAS 가 닫지 않음 → 있으면 주소와 함께 `MANUAL_REQUIRED` | — |
| 10 | `KEPT_SETTINGS` | 보안 예외·환경·조직 설정은 되돌리지 않음. 달라진 항목만 `KEPT` + 코드(`ENVIRONMENT_ADDED` 등), 같으면 `UNCHANGED` | — |
| 11 | `AUDIT_LOGS` | 감사 로그는 지우지 않음(`AUDIT_LOGS_NOT_DELETABLE`) | — |

각 함수의 검사(역할·자기 승인 스위치·보안 게이트·지문·잠금)를 그대로 탄다. 행위자는 `restore` 를 부른 어드민이다. 진입점 실행기를 직접 부르지 않는다(DB 비율 기록과 어긋나므로 이전 계획 경로로만).

- **복귀는 DB 쓰기 전환(컷오버) Job 을 만들지 않는다.** 새 복귀 계획에 DB 쓰기 전환을 넣지 않고, 기존 컷오버 든 계획은 승인하지 않는다(1·5 단계).
- 실행 순서는 1→2→3→4→**7**→5→6→8→9→10→11 이다(응답 `steps` 순서는 위 표 그대로). 컷오버 처리기 원칙(「쓰기 담당을 모르면 가중치를 되돌리지 않는다」, `app/services/migration_db_cutover.py`)과 같다. 컷오버 리허설 뒤에는 사람이 DB 쓰기 담당을 맡고(컷오버 도구), DB 쓰기 전환 없는 복귀 이전을 직접 돌린 다음 `restore` 를 다시 부르면 배포 단계부터 이어 간다.
- 같은 조직의 `restore`(dryRun 제외)는 조직별 PostgreSQL advisory lock 으로 한 번에 하나만 돈다. 겹치면 기다리지 않고 409 `DEMO_RESTORE_IN_PROGRESS` 다. dryRun 은 아무것도 바꾸지 않아 잠그지 않는다.

상태 값: `OK`(이미 같음) · `DONE`(이번에 조치) · `STARTED`(조치 시작, 일꾼이 끝냄) · `WAITING`(앞 단계·진행 중 작업 대기) · `MANUAL_REQUIRED` · `FAILED`(기존 함수가 거절, `code` = 그 오류 코드) · `KEPT` · `WOULD_DO`(dryRun). 단계 상태 = 항목 중 가장 급한 것(`FAILED` > `MANUAL_REQUIRED` > `WAITING` > `STARTED` > `WOULD_DO` > `DONE` > `KEPT` > `OK`).

## 알려진 한계: 스냅샷과 동시에 만든 DB 쓰기 전환 계획

7 단계는 `MigrationPlan.created_at >= 스냅샷 taken_at` 으로 「스냅샷 뒤 계획」을 고른다(`search_plans_created_since`). 그런데 두 시각은 트랜잭션 순서와 맞물리지 않는다.

- `create_plan` 은 `created_at` 시각을 **먼저** 찍고 트랜잭션을 연 뒤 서비스 행 잠금(`FOR UPDATE`)을 기다려 저장한다(`migration_plan_service.py` `now = self._clock.now()`).
- `take_snapshot` 은 잠금 없이 열린 계획을 읽고(READ COMMITTED), 그 뒤에 감사 줄 시각으로 `taken_at` 을 찍는다.

그래서 아래처럼 겹치면 7 단계가 그 계획을 놓친다.

```text
t0  누군가 DB 쓰기 전환이 든 이전 계획 생성 시작 → created_at = t0 (아직 커밋 전)
t1  어드민이 스냅샷 → 열린 계획 없음으로 읽음 → taken_at = t1 (t0 < t1), 「깨끗함」으로 저장
t2  계획 커밋 → 승인 → 실행 → 컷오버로 쓰기 담당 A→B, 계획 끝남
t3  restore: 1 단계는 끝난 계획이라 건너뜀, 7 단계는 created_at(t0) < taken_at(t1) 이라 못 찾음
    → `NO_DB_CUTOVER_SINCE_SNAPSHOT`, 5 단계가 DB 쓰기 전환 없는 복귀 이전을 만들고 승인
```

- 결과: 쓰기 담당이 B 인데 트래픽만 스냅샷 환경으로 돌아가고, 응답은 「스냅샷 뒤 컷오버 없음」이라고 한다. 원래라면 그 서비스는 `DB_WRITER_UNCONFIRMED` 로 멈춰야 한다.
- 영향이 작은 이유
  - 스냅샷과 계획 생성이 거의 동시에(수 ms, 잠금 대기·WAS 파드 사이 시계 차이만큼) 겹쳐야 한다. 그 계획이 승인·실행돼 컷오버까지 끝난 뒤에 `restore` 를 불러야 한다.
  - 컷오버 뒤에는 양쪽 PgBouncer 가 같은 쓰기 DB 를 가리키므로(echo-infra `replication/k8s/scripts/common.sh` `writer_side`), 트래픽이 돌아가도 데이터가 둘로 갈라지지는 않는다. 지연이 늘고, 운영자가 쓰기 담당을 잘못 알 수 있다. 다음 컷오버는 사전 점검(preflight)에서 어긋남으로 멈출 수 있다.
  - 권한 문제는 아니다. `restore` 는 사람 어드민이 자기 조직에만 부르고, 승인·보안 게이트 검사를 그대로 탄다.
- 운영 수칙: 스냅샷을 따는 동안 같은 조직에서 이전 계획을 만들지 않는다. 직전에 컷오버를 돌렸다면 응답이 `NO_DB_CUTOVER_SINCE_SNAPSHOT` 이어도 쓰기 담당을 컷오버 도구로 직접 확인한다.
- 고칠 방법(시연 뒤): ① `take_snapshot` 이 열린 계획을 읽기 전에 조직의 서비스 행을 `FOR SHARE` 로 잠가 `create_plan` 과 순서를 맞춘다. 또는 ② 7 단계가 `created_at` 대신 「스냅샷 때 열려 있지 않았고 `updated_at >= taken_at` 인 계획」을 고른다.

## 응답 `DemoRestore`

`{snapshotId, snapshotTakenAt, dryRun, restored, callAgain, manualRequired: [step], steps: [{step, status, items: [{action, status, targetType, targetId, label, code, detail}]}]}`

- `restored` = 1~6 단계가 모두 `OK`·`DONE`. `callAgain` = `STARTED`·`WAITING` 이 하나라도 있음(일꾼·다른 호출이 끝나면 다시 불러 이어 갈 수 있음). `MANUAL_REQUIRED` 는 다시 불러도 저절로 풀리지 않으므로 `callAgain` 에 넣지 않는다.
- **여러 번 불러도 안전하다(멱등).** 한 번에 끝나지 않는 순서(중단 롤백 → 복귀 이전 → 되돌리기 배포)는 일꾼이 끝낸 뒤 다시 부르면 이어 간다.
- `dryRun: true` 는 아무 함수도 부르지 않고 할 일을 `WOULD_DO` 로 돌려준다.

## 감사

| action | 언제 | 대상 | details |
|---|---|---|---|
| `DEMO_SNAPSHOT` | 스냅샷 저장(= 저장 그 자체) | `organization` | `{snapshot}` |
| `DEMO_RESTORE` | dryRun 이 아니고 하나라도 조치(`DONE`·`STARTED`)했을 때만 | `organization` | `{snapshotId, restored, callAgain, steps: {step: status}, changedItems}` |

각 기존 함수는 원래 자기 줄(취소·중단·재난 종료·계획 만들기·승인·되돌리기 요청)을 그대로 쓴다. 아무것도 바뀌지 않은 호출과 dryRun 은 `DEMO_RESTORE` 줄을 쓰지 않는다.
