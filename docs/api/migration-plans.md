# 이전 계획 API — 만들기 · 승인 · 실행 (API 서버 8)

서비스 중인 앱의 트래픽을 지금 환경(**출발**, 비율 100%)에서 같은 이미지가 올라간 예비 환경(**도착**)으로 옮긴다. 계획을 만들고, 다른 개발자가 **승인하면 바로 실행**되고, 일꾼이 단계마다 비율을 바꾸고 지켜본 뒤 판정해 자동으로 다음 단계로 간다. 실패·판단 불가·중단이면 출발 100% 로 되돌린다. 근거: SPEC [specs/api-8-migration-plans.md](../../specs/api-8-migration-plans.md).

- 경로는 `/api/v1/orgs/{orgId}` 아래, 공통 봉투 `ApiResponse`, camelCase, 모든 응답 칸은 항상 있고 값이 없으면 null([environments.md](environments.md#공통-규칙)).
- 트래픽 나눠 보내기·지표·데이터 복제 확인은 실행기 틀 + **가짜**(`simulated: true`)다. 실제 연결은 인프라 6(Envoy)·9(OpenTelemetry)·7(복제)이 실행기만 바꿔 붙인다([executors.md](../executors.md)). 실제 연결 전 운영은 비율 칸만 바뀐다.

## 엔드포인트

| 메서드 · 경로 | 성공 | 대표 실패 | 최소 역할 |
|---|---|---|---|
| `POST /services/{serviceId}/migration-plans` | 201 `MigrationPlanDetail` | 404 `SERVICE_NOT_FOUND`·`ENVIRONMENT_NOT_FOUND`, 409(아래 "만들기"), 422 `VALIDATION_ERROR`·`SIMULATE_NOT_ALLOWED` | 개발자(사람) |
| `GET /services/{serviceId}/migration-plans?status&page&size` | 200 `Page[MigrationPlanSummary]`(최신순) | 404 `SERVICE_NOT_FOUND`, 422 | 뷰어·에이전트 |
| `GET /migration-plans/{planId}` | 200 `MigrationPlanDetail` | 404 `MIGRATION_PLAN_NOT_FOUND` | 뷰어·에이전트 |
| `POST /migration-plans/{planId}/approve` | 202 `MigrationPlanDetail`(`APPROVED`) | 403 `SELF_APPROVAL_FORBIDDEN`, 409 `PLAN_NOT_PENDING`·`PLAN_STALE`·`PLAN_EXPIRED` | 개발자(사람) |
| `POST /migration-plans/{planId}/reject` | 200 `MigrationPlanDetail`(`REJECTED`) | 403 `SELF_APPROVAL_FORBIDDEN`, 409 `PLAN_NOT_PENDING`·`PLAN_EXPIRED` | 개발자(사람) |
| `POST /migration-plans/{planId}/cancel` | 200 `MigrationPlanDetail`(`CANCELLED`) | 409 `PLAN_NOT_PENDING`·`PLAN_EXPIRED` | 개발자(사람) |
| `POST /migration-plans/{planId}/abort` | 202 `MigrationPlanDetail`(멱등) | 409 `PLAN_NOT_RUNNING` | 개발자(사람) |
| `GET /migration-plans/{planId}/logs?cursor&limit` | 200 `MigrationLogPage` | 404, 422(cursor·limit) | 뷰어·에이전트 |
| `PATCH /settings` `{allowSelfApproval}` | 200 `Organization` | 403 `ROLE_FORBIDDEN` | 어드민(사람) — [organizations.md](organizations.md) |

에이전트 토큰은 조회만 한다(쓰기·승인 403 `AGENT_READ_ONLY`). 다른 조직의 계획은 404 `MIGRATION_PLAN_NOT_FOUND`. 코드: `app/routers/migration_plan_router.py`, `app/services/migration_plan_service.py`(API), `app/services/migration_plan_processor.py`(일꾼).

## 만들기 `POST /services/{serviceId}/migration-plans`

```json
{
  "targetEnvironmentId": "e2…",
  "observeSeconds": 5,
  "rollbackConditions": {"maxErrorRatePercent": 1.0, "maxP95Ms": 800, "maxReplicationLagSeconds": 1.0},
  "simulate": {"failAtStep": 2, "reason": "ERROR_RATE"}
}
```

| 칸 | 규칙 |
|---|---|
| `targetEnvironmentId` | 필수 uuid. 도착 환경 |
| `observeSeconds` | 1~600, 빼면 `MIGRATION_OBSERVE_SECONDS_DEFAULT`(5) |
| `rollbackConditions` | 선택. `maxErrorRatePercent` 0~100(기본 1.0), `maxP95Ms` 1~60000(기본 800), `maxReplicationLagSeconds` 0~600(기본 1.0). 헬스체크 실패는 늘 롤백 조건 |
| `simulate` | 가짜 실행기 모드에서만(아니면 422 `SIMULATE_NOT_ALLOWED`). `failAtStep` 1~4, `reason` `ERROR_RATE`·`LATENCY`·`HEALTH`·`DATA_LAG`·`TRAFFIC`. `DATA_LAG` 는 단계 3 만, 그 밖의 reason 은 단계 3 을 고를 수 없다(422 `SIMULATE_REASON_STEP_MISMATCH`) |

거절 순서: 권한 → 바디(422) → 서비스·환경(404, 서비스 `DELETING` 409 `SERVICE_DELETING`, 환경 마지막 접속 확인 `FAILED` 409 `ENVIRONMENT_UNHEALTHY`) → 아래 상태 검사(409). 상태 검사는 환경 `FOR SHARE` → 서비스 → 연결 전체를 잠근 한 트랜잭션에서 한다.

1. 이 서비스의 승인 대기 계획이 만료됐으면(`expiresAt <= now`) 먼저 `EXPIRED` 로 끝낸다(로그 `EXPIRED`).
2. 끝나지 않은 계획(`PENDING_APPROVAL`·`APPROVED`·`RUNNING`·`ROLLING_BACK`)이 있으면 409 `MIGRATION_PLAN_OPEN` + `details = [{field: "planId", reason: "MIGRATION_PLAN_OPEN", value: <id>}]`. 동시에 두 건이 와도 하나만 201(부분 unique).
3. 출발 = 비율 100 인 연결 하나. 서비스 중 연결이 정확히 하나가 아니거나 100 이 아니면(없음·이전 중간 비율) 409 `SOURCE_NOT_SINGLE`.
4. 도착 환경이 출발과 같으면 422 `VALIDATION_ERROR`(`{field: "targetEnvironmentId", reason: "SAME_ENVIRONMENT"}`).
5. 서비스의 **어느 연결이든** 열린(대기·실행 중) 배포 요청이 있으면: 도착 연결의 `STANDBY_SYNC` 면 409 `TARGET_SYNCING`(`{field: "deployRequestId", reason: "TARGET_SYNCING", value}` — 예비가 아직 새 버전을 따라가는 중, 끝나면 다시), 그 밖은 409 `DEPLOY_IN_PROGRESS`(`{field: "deployRequestId", reason: "DEPLOY_IN_PROGRESS", value}`). 그래서 계획을 만든 순간 열린 요청은 늘 0개다.
6. 도착 연결이 없거나 현재 배포가 없으면 409 `TARGET_NOT_READY`(`{field: "targetEnvironmentId", reason: "NO_DEPLOYMENT"}`). 도착의 현재 이미지 digest 가 출발과 다르면 409 `TARGET_NOT_READY`(`reason: "VERSION_MISMATCH"`, 그 연결의 마지막 `STANDBY_SYNC` 가 실패했으면 `{field: "lastSyncError", reason: "LAST_SYNC_FAILED", value: <에러 코드>}` 를 더함).
7. 지문·단계를 계산해 `PENDING_APPROVAL` 로 저장하고(`expiresAt = 지금 + 24시간`, `simulated` = 가짜 모드인가) 로그 `PLAN_CREATED` → 201.

### 단계와 예상 시간 (고정)

| index | kind | 출발 / 도착 비율 | 하는 일 | 예상 시간(기본) |
|---|---|---|---|---|
| 0 | `PREFLIGHT` | — | 도착 환경별 주소 헬스체크 1번 + 도착 이미지가 출발과 같은가 | 2 |
| 1 | `SHIFT` | 90 / 10 | 비율 적용 → `observeSeconds` 지켜보기 → 판정 | 2 + 5 |
| 2 | `SHIFT` | 50 / 50 | 같음 | 2 + 5 |
| 3 | `DATA_SYNC_CHECK` | — | 출발 → 도착 복제 지연 ≤ `maxReplicationLagSeconds` | 2 |
| 4 | `SHIFT` | 0 / 100 | 같음. 통과하면 `SUCCEEDED` | 2 + 5 |

단계 예상 = `MIGRATION_STEP_OVERHEAD_SECONDS`(2) + (SHIFT 면 `observeSeconds`). 계획 예상 = 합(기본 25초). 비율 목록은 바꿀 수 없고, 단계별 사람 승인은 없다.

## 승인 · 거절 · 취소

- **승인 = 실행 시작**(별도 실행 버튼 없음). 개발자 이상의 사람만. 기본은 **만든 사람이 아닌 사람**이다. 조직 설정 `allowSelfApproval = false`(기본)이면 만든 사람의 승인·거절은 403 `SELF_APPROVAL_FORBIDDEN`(거부 기록 남음, 계획은 그대로). `true` 면 허용하고 `selfApproved = true`, 로그 `SELF_APPROVED`.
- 승인 순서(서비스 → 연결 → 계획 잠금): `PENDING_APPROVAL` 아님 409 `PLAN_NOT_PENDING` → 자기 승인 403 → 만료면 `EXPIRED` 로 바꾸고 커밋한 뒤 409 `PLAN_EXPIRED` → **지문**이 다르면 `STALE` 로 바꾸고 커밋한 뒤 409 `PLAN_STALE`(`details` = 달라진 항목마다 `{field, reason: "CHANGED"}`) → 통과면 `APPROVED` + 할 일 `MIGRATION_PLAN` + 로그 `APPROVED` → 202.
- 거절(`REJECTED`)·취소(`CANCELLED`)는 `PENDING_APPROVAL` 에서만이고 먼저 만료를 판정한다. 만든 사람의 거절도 자기 승인 스위치를 따른다(막혀 있으면 403 — 만든 사람은 취소를 쓴다). 취소는 개발자 이상 누구나.
- 승인 대기는 24시간(`MIGRATION_PLAN_APPROVAL_TTL_SECONDS`) 뒤 만료다. 만료 판정은 승인·거절·취소·새 계획 만들기 때 하고, 조회는 저장된 상태를 바꾸지 않는다(서비스 상세 `activeMigrationPlanId` 는 만료된 대기 계획을 세지 않는다).

### 지문(낡은 계획 판정)

계획을 만들 때 저장하고, **승인 때와 일꾼 시작 때** 같은 함수로 다시 계산해 항목별로 비교한다. 다르면 `STALE`(실행하지 않음).

| `field` | 담는 값 |
|---|---|
| `serviceStatus` | 서비스 상태 |
| `connections` | 살아 있는 연결 전체의 `(id, currentDeploymentId, trafficWeight)` |
| `sourceEnvironment` · `targetEnvironment` | 환경 `config`·`secret_ref` 의 SHA-256(값 자체는 없음), 접속 상태, 살아 있는가 |
| `openDeployRequests` | 서비스의 열린 배포 요청 id 목록 |

접속 재확인 시각(`connection_checked_at`)·`updated_at` 은 넣지 않는다 — 접속 재확인만으로는 낡지 않는다. 승인 대기 동안 배포를 접수하면(막지 않는다) 그 계획은 승인 때 `STALE` 이 된다.

## 실행 (일꾼)

- 단계마다 트래픽 실행기로 두 연결 비율을 적용(멱등 키 `{planId}:{index}`)하고, **적용했다고 답했을 때만** 두 연결의 `trafficWeight` 를 한 트랜잭션에서 함께 적는다(서비스 합계 100). 그 뒤 `observeSeconds` 동안 지켜보고(`observeUntil` 전에는 판정하지 않는다), 헬스체크(도착 환경별 주소 + 도착 현재 배포의 헬스 경로) + 지표(구간 오류율·p95)로 판정한다. 통과하면 사람 개입 없이 다음 단계로 간다.
- 판정 실패 원인: 헬스 2xx 아님 `HEALTH_CHECK_FAILED`, 지표 없음 `METRICS_UNAVAILABLE`(판단 불가), 오류율·p95 초과 `THRESHOLD_EXCEEDED`, 복제 지연 초과 `DATA_LAG_EXCEEDED`(없으면 `METRICS_UNAVAILABLE`), 비율 적용 실패 `TRAFFIC_APPLY_FAILED`.
- 실패·판단 불가·적용 실패·**중단**(`ABORTED`)·**일꾼 상실**(`WORKER_LOST`, 임대 3번 상실)은 `ROLLING_BACK` 으로 가서 출발 100%·도착 0% 를 적용한다(키 `{planId}:rollback:{n}`). 적용되면 `ROLLED_BACK`(`rollbackStatus = SUCCEEDED`), 3번(`MIGRATION_ROLLBACK_MAX_ATTEMPTS`) 다 실패하면 `FAILED`(`rollbackStatus = FAILED`, `errorCode` 는 원래 원인, 비율은 마지막으로 적용된 값). 임대를 잃은 뒤에도 남은 롤백 시도는 한다.
- 단계 0 사전 확인 실패는 트래픽을 바꾼 적이 없어 `FAILED`(`PREFLIGHT_FAILED`, `rollbackStatus = NOT_NEEDED`). 시작 전(`APPROVED`) 일꾼 상실도 `FAILED`(`WORKER_LOST`, `NOT_NEEDED`).
- 성공하면 도착이 서비스 중(100), 출발은 예비(0)로 남는다. 그 뒤 도착에 새 배포가 나가면 출발이 따라간다(API 서버 3). 되돌아가기(Failback)는 출발·도착을 바꾼 새 계획이다.
- 일꾼이 죽어도 이어 간다: 단계 위치와 "트래픽 요청함 시각"을 저장하고, 다시 시작한 일꾼은 결과가 없으면 같은 키로 다시 부른다(결과는 한 번만 반영, 로그 줄 중복 없음).

### 중단 `POST /migration-plans/{planId}/abort`

`APPROVED`·`RUNNING` 에서만(아니면 409 `PLAN_NOT_RUNNING`). `abortRequestedAt`·`abortedBy` 를 적고 그 계획의 할 일을 지금으로 당긴다 → 202. 다시 중단해도 202(기록은 그대로). 일꾼이 다음 실행 맨 앞에서 보고 `RUNNING` 이면 롤백(`ABORTED`), `APPROVED` 면 트래픽을 바꾸지 않고 `CANCELLED` 로 끝낸다. 열린 할 일이 없으면(옛 일꾼이 할 일을 닫아 버린 경우 등) `APPROVED` 는 그 자리에서 `CANCELLED`, `RUNNING` 은 할 일을 새로 넣는다.

### 실행 중 겹침

계획이 `APPROVED`·`RUNNING`·`ROLLING_BACK` 이면 그 서비스의 배포 요청·되돌리기·서비스 삭제 접수는 409 `MIGRATION_IN_PROGRESS`(`details = [{field: "planId", reason: "MIGRATION_IN_PROGRESS", value}]`)다. 일꾼이 만드는 예비 따라가기는 막지 않는다(실행 중에는 생기지 않는다).

## 상태

| 값 | 뜻 | 다음 |
|---|---|---|
| `PENDING_APPROVAL` | 승인 대기 | `APPROVED`·`REJECTED`·`CANCELLED`·`EXPIRED`·`STALE` |
| `APPROVED` | 승인됨, 일꾼 시작 전 | `RUNNING`·`STALE`·`CANCELLED`(시작 전 중단)·`FAILED`(`WORKER_LOST`) |
| `RUNNING` | 단계 진행 중 | `SUCCEEDED`·`ROLLING_BACK`·`FAILED`(`PREFLIGHT_FAILED`) |
| `ROLLING_BACK` | 출발 100% 로 되돌리는 중 | `ROLLED_BACK`·`FAILED` |
| `SUCCEEDED`·`ROLLED_BACK`·`FAILED`·`REJECTED`·`CANCELLED`·`EXPIRED`·`STALE` | 끝 | — |

단계 결과(`steps[i].result.status`): `PENDING`·`RUNNING`·`PASSED`·`FAILED`·`SKIPPED`. 롤백·실패로 끝나면 남은 단계는 `SKIPPED`, 중단·일꾼 상실로 멈춘 단계는 `FAILED`(`failureCode` = 그 원인).

원인 코드(`errorCode`, `errorMessage` 는 코드별 고정 문장): `PREFLIGHT_FAILED` · `TRAFFIC_APPLY_FAILED` · `HEALTH_CHECK_FAILED` · `METRICS_UNAVAILABLE` · `THRESHOLD_EXCEEDED` · `DATA_LAG_EXCEEDED` · `ABORTED` · `WORKER_LOST` · `INTERNAL_ERROR`. 롤백 결과 `rollbackStatus`: `SUCCEEDED` · `FAILED` · `NOT_NEEDED`.

## `simulate` (가짜 모드 시연)

`failAtStep = n` 이면 그 단계 판정에서 실행기를 부르지 않고 처리기가 실패 값을 만든다: `ERROR_RATE` 오류율 5.0%, `LATENCY` p95 5000ms, `HEALTH` 헬스 503, `DATA_LAG`(단계 3) 지연 30초, `TRAFFIC` 그 단계 비율 적용 실패(`metrics = null`). 만든 값은 단계 결과·로그에 그대로 남는다.

## 응답

`MigrationPlanSummary`: `id`, `serviceId`, `sourceEnvironmentId`·`targetEnvironmentId`(환경 id), `sourceServiceEnvironmentId`·`targetServiceEnvironmentId`(연결 id), `status`, `currentStep`, `expectedSeconds`, `createdBy{id, login}`, `approvedBy`, `selfApproved`, `errorCode`, `rollbackStatus`, `simulated`, `createdAt`·`approvedAt`·`startedAt`·`finishedAt`·`expiresAt`.

`MigrationPlanDetail` = Summary + `rollbackConditions{…, healthCheck: true}`, `observeSeconds`, `steps[]`(계획 값과 실제 결과를 나란히), `rejectedBy`·`cancelledBy`·`abortedBy`, `abortRequestedAt`, `errorMessage`.

```json
{
  "success": true,
  "data": {
    "id": "7c1e…",
    "serviceId": "6f1d…",
    "sourceEnvironmentId": "e1…",
    "targetEnvironmentId": "e2…",
    "sourceServiceEnvironmentId": "a1…",
    "targetServiceEnvironmentId": "b2…",
    "status": "RUNNING",
    "currentStep": 2,
    "expectedSeconds": 25,
    "createdBy": {"id": "u1…", "login": "dev-a"},
    "approvedBy": {"id": "u2…", "login": "dev-b"},
    "selfApproved": false,
    "errorCode": null,
    "rollbackStatus": null,
    "simulated": true,
    "createdAt": "2026-11-02T03:00:00Z",
    "approvedAt": "2026-11-02T03:01:00Z",
    "startedAt": "2026-11-02T03:01:00Z",
    "finishedAt": null,
    "expiresAt": "2026-11-03T03:00:00Z",
    "rollbackConditions": {"maxErrorRatePercent": 1.0, "maxP95Ms": 800, "maxReplicationLagSeconds": 1.0, "healthCheck": true},
    "observeSeconds": 5,
    "steps": [
      {"index": 0, "kind": "PREFLIGHT", "sourceWeight": null, "targetWeight": null, "observeSeconds": null, "expectedSeconds": 2,
       "result": {"status": "PASSED", "startedAt": "2026-11-02T03:01:00Z", "finishedAt": "2026-11-02T03:01:00Z",
                  "metrics": {"healthStatus": "OK", "errorRatePercent": null, "p95Ms": null, "requestCount": null, "replicationLagSeconds": null},
                  "failureCode": null}},
      {"index": 1, "kind": "SHIFT", "sourceWeight": 90, "targetWeight": 10, "observeSeconds": 5, "expectedSeconds": 7,
       "result": {"status": "PASSED", "startedAt": "2026-11-02T03:01:00Z", "finishedAt": "2026-11-02T03:01:05Z",
                  "metrics": {"healthStatus": "OK", "errorRatePercent": 0.1, "p95Ms": 120.0, "requestCount": 5, "replicationLagSeconds": null},
                  "failureCode": null}},
      {"index": 2, "kind": "SHIFT", "sourceWeight": 50, "targetWeight": 50, "observeSeconds": 5, "expectedSeconds": 7,
       "result": {"status": "RUNNING", "startedAt": "2026-11-02T03:01:05Z", "finishedAt": null, "metrics": null, "failureCode": null}},
      {"index": 3, "kind": "DATA_SYNC_CHECK", "sourceWeight": null, "targetWeight": null, "observeSeconds": null, "expectedSeconds": 2,
       "result": {"status": "PENDING", "startedAt": null, "finishedAt": null, "metrics": null, "failureCode": null}},
      {"index": 4, "kind": "SHIFT", "sourceWeight": 0, "targetWeight": 100, "observeSeconds": 5, "expectedSeconds": 7,
       "result": {"status": "PENDING", "startedAt": null, "finishedAt": null, "metrics": null, "failureCode": null}}
    ],
    "rejectedBy": null,
    "cancelledBy": null,
    "abortedBy": null,
    "abortRequestedAt": null,
    "errorMessage": null
  }
}
```

### 실행 로그 `GET /migration-plans/{planId}/logs?cursor&limit`

`MigrationLogPage`: `simulated`(계획 행 값), `items[{seq, step, at, level, code, message, data}]`, `nextCursor`, `hasMore`. 커서 = 마지막으로 받은 `seq`(숫자가 아니면 422 `VALIDATION_ERROR`), `limit` 1~500 기본 200. 줄은 상태 변경과 같은 트랜잭션에서 반영됐을 때만 쓰고(그래서 일꾼이 다시 시작해도 겹치거나 빠지지 않는다) 계획당 500줄(`LOG_TRUNCATED`)이다. `step` 은 계획 단계 번호라 응답의 `steps[i]` 와 맞춰 "계획 / 실제"를 볼 수 있다. `data` 는 비율·지표 숫자(`STALE` 은 `changed` 항목 이름)만 담는다.

| code | level | step | data |
|---|---|---|---|
| `PLAN_CREATED` | INFO | — | `expectedSeconds`, `observeSeconds` |
| `APPROVED` · `SELF_APPROVED` · `REJECTED` · `CANCELLED` · `EXPIRED` | INFO | — | |
| `STALE` | WARN | — | `changed`(항목 이름 목록) |
| `ABORT_REQUESTED` | WARN | — | |
| `STARTED` · `PREFLIGHT_OK` | INFO | — · 0 | |
| `PREFLIGHT_FAILED` | ERROR | 0 | |
| `WEIGHT_APPLIED` | INFO | 1·2·4 | `sourceWeight`, `targetWeight` |
| `WEIGHT_APPLY_FAILED` | WARN | 1·2·4 | `sourceWeight`, `targetWeight`(요청한 비율) |
| `JUDGE_PASSED` · `JUDGE_FAILED` | INFO · WARN | 1·2·4 | `errorRatePercent`, `p95Ms`, `requestCount` |
| `DATA_SYNC_OK` · `DATA_SYNC_FAILED` | INFO · WARN | 3 | `replicationLagSeconds` |
| `SUCCEEDED` | INFO | — | |
| `ROLLBACK_STARTED` | WARN | 멈춘 단계 | |
| `ROLLBACK_APPLIED` | INFO | — | `sourceWeight: 100`, `targetWeight: 0` |
| `ROLLBACK_APPLY_FAILED` · `ROLLBACK_FAILED` | WARN · ERROR | — | `attempt`(·`maxAttempts`) |
| `FAILED` | ERROR | — | (일꾼 상실·내부 오류. 문장 = 원인 코드 문장) |

성공한 계획의 줄 순서: `PLAN_CREATED, APPROVED, STARTED, PREFLIGHT_OK, WEIGHT_APPLIED(1), JUDGE_PASSED(1), WEIGHT_APPLIED(2), JUDGE_PASSED(2), DATA_SYNC_OK(3), WEIGHT_APPLIED(4), JUDGE_PASSED(4), SUCCEEDED`.

## HTTP 오류

| 상황 | HTTP | code |
|---|---|---|
| 바디 위반(필수 칸·범위·모르는 칸·`simulate` 단계와 이유 불일치)·같은 환경 | 422 | `VALIDATION_ERROR` |
| 가짜가 아닌 실행기 모드의 `simulate` | 422 | `SIMULATE_NOT_ALLOWED` |
| 뷰어 · 에이전트 | 403 | `ROLE_FORBIDDEN` · `AGENT_READ_ONLY`(거부 기록) |
| 자기 승인·거절이 막힌 조직 | 403 | `SELF_APPROVAL_FORBIDDEN`(거부 기록) |
| 서비스·환경·계획 없음, 다른 조직 | 404 | `SERVICE_NOT_FOUND` · `ENVIRONMENT_NOT_FOUND` · `MIGRATION_PLAN_NOT_FOUND` |
| 서비스 삭제 중 · 도착 환경 접속 실패 | 409 | `SERVICE_DELETING` · `ENVIRONMENT_UNHEALTHY` |
| 끝나지 않은 계획 있음 | 409 | `MIGRATION_PLAN_OPEN`(+`planId`) |
| 출발이 하나(100%)가 아님 | 409 | `SOURCE_NOT_SINGLE` |
| 도착 예비가 따라가는 중 · 그 밖의 열린 배포 요청 | 409 | `TARGET_SYNCING` · `DEPLOY_IN_PROGRESS`(+`deployRequestId`) |
| 도착에 앱 없음·다른 버전 | 409 | `TARGET_NOT_READY`(+`NO_DEPLOYMENT`·`VERSION_MISMATCH`·`lastSyncError`) |
| 승인 대기가 아님 · 실행 중이 아님 | 409 | `PLAN_NOT_PENDING` · `PLAN_NOT_RUNNING` |
| 지문 다름 · 만료 | 409 | `PLAN_STALE`(+달라진 항목) · `PLAN_EXPIRED` |
| 실행 중 그 서비스의 배포 요청·되돌리기·삭제 | 409 | `MIGRATION_IN_PROGRESS`(+`planId`) |

## 설정

| 환경변수 | 기본 | 뜻 |
|---|---|---|
| `MIGRATION_OBSERVE_SECONDS_DEFAULT` | 5 | 지켜보기 기본(1~600) |
| `MIGRATION_STEP_OVERHEAD_SECONDS` | 2 | 단계 예상 시간에 더하는 여유(0~60) |
| `MIGRATION_PLAN_APPROVAL_TTL_SECONDS` | 86400 | 승인 대기 만료 |
| `MIGRATION_ROLLBACK_MAX_ATTEMPTS` | 3 | 롤백 비율 적용 최대 시도 |
