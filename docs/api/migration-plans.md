# 이전 계획 API — 만들기 · 승인 · 실행 (API 서버 8 · 8-1 · 보안 5 게이트)

서비스 중인 앱의 트래픽을 지금 환경(**출발**, 비율 100%)에서 같은 이미지가 올라간 예비 환경(**도착**)으로 옮긴다. 계획을 만들고, 다른 개발자가 **승인하면 바로 실행**되고, 일꾼이 단계마다 비율을 바꾼 뒤 **도착이 요청 N건을 5xx 허용 안에서 처리했는지 세어** 자동으로 다음 단계로 간다. 실패·판단 불가·중단이면 출발 100% 로 되돌린다. 근거: SPEC [specs/api-8-migration-plans.md](../../specs/api-8-migration-plans.md), 판정 방식은 [specs/api-8-1-request-count-judgement.md](../../specs/api-8-1-request-count-judgement.md)(다르면 8-1 이 우선).

- 경로는 `/api/v1/orgs/{orgId}` 아래, 공통 봉투 `ApiResponse`, camelCase, 모든 응답 칸은 항상 있고 값이 없으면 null([environments.md](environments.md#공통-규칙)).
- 트래픽 나눠 보내기·요청 카운터·지표·데이터 복제 확인은 실행기 틀 + **가짜**(`simulated: true`)다. 실제 연결은 고정 진입점(Envoy, `GET /counters`·`GET /summary`)·인프라 7(복제)이 실행기만 바꿔 붙인다([executors.md](../executors.md)). 실제 연결 전 운영은 비율 칸만 바뀐다.

## 엔드포인트

| 메서드 · 경로 | 성공 | 대표 실패 | 최소 역할 |
|---|---|---|---|
| `POST /services/{serviceId}/migration-plans` | 201 `MigrationPlanDetail` | 404 `SERVICE_NOT_FOUND`·`ENVIRONMENT_NOT_FOUND`, 409(아래 "만들기"), 422 `VALIDATION_ERROR`·`SIMULATE_NOT_ALLOWED` | 개발자(사람) |
| `GET /services/{serviceId}/migration-plans?status&page&size` | 200 `Page[MigrationPlanSummary]`(최신순) | 404 `SERVICE_NOT_FOUND`, 422 | 뷰어·에이전트 |
| `GET /migration-plans/{planId}` | 200 `MigrationPlanDetail` | 404 `MIGRATION_PLAN_NOT_FOUND` | 뷰어·에이전트 |
| `POST /migration-plans/{planId}/approve` | 202 `MigrationPlanDetail`(`APPROVED`) | 403 `SELF_APPROVAL_FORBIDDEN`, 409 `PLAN_NOT_PENDING`·`PLAN_STALE`·`PLAN_EXPIRED`·`SECURITY_GATE_BLOCKED`(게이트를 켰을 때) | 개발자(사람) |
| `POST /migration-plans/{planId}/reject` | 200 `MigrationPlanDetail`(`REJECTED`) | 403 `SELF_APPROVAL_FORBIDDEN`, 409 `PLAN_NOT_PENDING`·`PLAN_EXPIRED` | 개발자(사람) |
| `POST /migration-plans/{planId}/cancel` | 200 `MigrationPlanDetail`(`CANCELLED`) | 409 `PLAN_NOT_PENDING`·`PLAN_EXPIRED` | 개발자(사람) |
| `POST /migration-plans/{planId}/abort` | 202 `MigrationPlanDetail`(멱등) | 409 `PLAN_NOT_RUNNING` | 개발자(사람) |
| `GET /migration-plans/{planId}/logs?cursor&limit` | 200 `MigrationLogPage` | 404, 422(cursor·limit) | 뷰어·에이전트 |
| `PATCH /settings` `{allowSelfApproval}` | 200 `Organization` | 403 `ROLE_FORBIDDEN` | 어드민(사람) — [organizations.md](organizations.md) |

에이전트 토큰은 조회만 한다(쓰기·승인 403 `AGENT_READ_ONLY`). 다른 조직의 계획은 404 `MIGRATION_PLAN_NOT_FOUND`. 코드: `app/routers/migration_plan_router.py`, `app/services/migration_plan_service.py`(API), `app/services/migration_plan_processor.py`(일꾼), `app/services/migration_counting.py`(건수 세기 규칙).

## 만들기 `POST /services/{serviceId}/migration-plans`

```json
{
  "targetEnvironmentId": "e2…",
  "requiredRequests": [20, 50, 50],
  "maxWaitSeconds": 30,
  "rollbackConditions": {"maxErrorRatePercent": 1.0, "maxP95Ms": 800, "maxReplicationLagSeconds": 1.0},
  "simulate": {"failAtStep": 2, "reason": "ERROR_RATE"}
}
```

| 칸 | 규칙 |
|---|---|
| `targetEnvironmentId` | 필수 uuid. 도착 환경 |
| `requiredRequests` | 선택. 정수 3개(비율 10%·50%·100% 단계 순), 각 1~10000. 빼면 `MIGRATION_REQUIRED_REQUESTS_DEFAULT`(`20,50,50`) |
| `maxWaitSeconds` | 선택. 5~300, 비율 단계마다 건수를 기다리는 최대 시간. 빼면 `MIGRATION_MAX_WAIT_SECONDS_DEFAULT`(30) |
| `rollbackConditions` | 선택. `maxErrorRatePercent` 0~100(기본 1.0, 허용 5xx 계산에 쓴다), `maxP95Ms` 1~60000(기본 800, **참고용** — 판정에 쓰지 않는다), `maxReplicationLagSeconds` 0~600(기본 1.0). 헬스체크 실패는 늘 롤백 조건 |
| `simulate` | `EXECUTOR_MODE=fake` 일 때만(아니면 422 `SIMULATE_NOT_ALLOWED`). 진입점·헬스체커 모드(`ENTRYPOINT_EXECUTOR_MODE`·`HEALTH_CHECKER_MODE`)는 보지 않는다 — 실제 진입점(`http`)에서도 받는다(아래 "`simulate`"). `failAtStep` 1~4, `reason` 아래 표. 이유와 단계가 맞지 않으면 422 `VALIDATION_ERROR`(`reason = SIMULATE_REASON_STEP_MISMATCH`), `LATENCY` 는 422(`reason = SIMULATE_REASON_NOT_SUPPORTED`) |

8-1 에서 `observeSeconds` 를 뺐다. 보내면 422 `VALIDATION_ERROR`(모르는 칸). 계획 종류 `mode` 도 요청 칸이 아니다(서버가 정한다). 보내면 422 `VALIDATION_ERROR`(`details[0].field = "mode"`, 모르는 칸 — `observeSeconds` 와 같은 규칙)이고 계획은 만들어지지 않는다.

거절 순서: 권한 → 바디(422) → 서비스·환경(404, 서비스 `DELETING` 409 `SERVICE_DELETING`, 환경 마지막 접속 확인 `FAILED` 409 `ENVIRONMENT_UNHEALTHY`) → 아래 상태 검사(409). 상태 검사는 환경 `FOR SHARE` → 서비스 → 연결 전체를 잠근 한 트랜잭션에서 한다.

1. 이 서비스의 승인 대기 계획이 만료됐으면(`expiresAt <= now`) 먼저 `EXPIRED` 로 끝낸다(로그 `EXPIRED`).
2. 끝나지 않은 계획(`PENDING_APPROVAL`·`APPROVED`·`RUNNING`·`ROLLING_BACK`)이 있으면 409 `MIGRATION_PLAN_OPEN` + `details = [{field: "planId", reason: "MIGRATION_PLAN_OPEN", value: <id>}]`. 동시에 두 건이 와도 하나만 201(부분 unique).
3. 출발 = 비율 100 인 연결 하나. 서비스 중 연결이 정확히 하나가 아니거나 100 이 아니면(없음·이전 중간 비율) 409 `SOURCE_NOT_SINGLE`.
4. 도착 환경이 출발과 같으면 422 `VALIDATION_ERROR`(`{field: "targetEnvironmentId", reason: "SAME_ENVIRONMENT"}`).
5. 도착 환경이 ACTIVE 이고 기준을 넘은 재난의 피해 환경이면 409 `DISASTER_ACTIVE`(`{field: "disasterId", reason: "DISASTER_ACTIVE", value: <최신 재난 id>}`, 수동 복귀도 같다. [disasters.md](disasters.md#복귀-차단)).
6. 서비스의 **어느 연결이든** 열린(대기·실행 중) 배포 요청이 있으면: 도착 연결의 `STANDBY_SYNC` 면 409 `TARGET_SYNCING`(`{field: "deployRequestId", reason: "TARGET_SYNCING", value}` — 예비가 아직 새 버전을 따라가는 중, 끝나면 다시), 그 밖은 409 `DEPLOY_IN_PROGRESS`(`{field: "deployRequestId", reason: "DEPLOY_IN_PROGRESS", value}`). 그래서 계획을 만든 순간 열린 요청은 늘 0개다.
7. 도착 연결이 없거나 현재 배포가 없으면 409 `TARGET_NOT_READY`(`{field: "targetEnvironmentId", reason: "NO_DEPLOYMENT"}`). 도착의 현재 이미지 digest 가 출발과 다르면 409 `TARGET_NOT_READY`(`reason: "VERSION_MISMATCH"`, 그 연결의 마지막 `STANDBY_SYNC` 가 실패했으면 `{field: "lastSyncError", reason: "LAST_SYNC_FAILED", value: <에러 코드>}` 를 더함).
8. 지문·단계·종류(`mode`, 아래)를 계산해 `PENDING_APPROVAL` 로 저장하고(`expiresAt = 지금 + 24시간`, `simulated` = 가짜 모드인가) 로그 `PLAN_CREATED` → 201.

### 계획 종류 `mode` (8-3)

서버가 만들 때 위 잠금 트랜잭션 안에서 한 번 판정해 저장한다. 응답 때 다시 계산하지 않는다(서비스당 열린 계획은 하나라 만든 뒤 실행 전에 기준이 바뀌지 않는다). 클라이언트는 정할 수 없다. 근거: [specs/api-8-3-plan-mode.md](../../specs/api-8-3-plan-mode.md).

| 값 | 뜻 |
|---|---|
| `PLANNED` | 사람이 만든 보통 이전(기본값). 이 칸이 생기기 전 계획도 `PLANNED` |
| `FAILBACK` | 앞선 성공 이전을 거꾸로 하는 복귀 |
| `EMERGENCY` | 재난 트리거가 만든 긴급 이전([disasters.md](disasters.md), API 서버 9). 이 경로로는 만들어지지 않는다 |

판정(사람이 만든 계획): 같은 서비스의 `SUCCEEDED` 계획 중 `finishedAt` 이 가장 늦은 것(같으면 `createdAt`, 그다음 `id` 가 큰 것) 하나를 기준으로 삼는다. 그 계획의 출발 연결 = 새 계획의 도착 연결이고, 그 계획의 도착 연결 = 새 계획의 출발 연결이고, 그 계획이 `FAILBACK` 이 아니면 `FAILBACK`, 그 밖은 `PLANNED`.

- 성공한 계획만 본다. 롤백·실패·취소·거절·만료된 계획은 트래픽을 옮기지 않았다.
- 복귀의 복귀(aws→gcp 성공, gcp→aws 복귀 성공, 다시 aws→gcp)는 새 이전이라 `PLANNED` 다.
- 성공 이력이 여럿이면 가장 늦게 끝난 것만 본다(aws→gcp 성공 뒤 aws→onprem 성공이면 gcp→aws 는 `PLANNED`, onprem→aws 는 `FAILBACK`).

### 단계와 예상 시간 (고정)

| index | kind | 출발 / 도착 비율 | 하는 일 | 예상 시간(기본) |
|---|---|---|---|---|
| 0 | `PREFLIGHT` | — | 도착 환경별 주소 헬스체크 1번 + 도착 이미지가 출발과 같은가 | 2 |
| 1 | `SHIFT` | 90 / 10 | 비율 적용 → 도착 요청 20건 세기(최대 30초) → 헬스체크 | 2 + 4 |
| 2 | `SHIFT` | 50 / 50 | 같음(50건) | 2 + 2 |
| 3 | `DATA_SYNC_CHECK` | — | 출발 → 도착 복제 지연 ≤ `maxReplicationLagSeconds` | 2 |
| 4 | `SHIFT` | 0 / 100 | 같음(50건). 통과하면 `SUCCEEDED` | 2 + 1 |

단계 예상 = `MIGRATION_STEP_OVERHEAD_SECONDS`(2) + (SHIFT 면 min(`maxWaitSeconds`, ceil(필요 건수 ÷ (`MIGRATION_EXPECTED_RPS` × 비율 ÷ 100)))). 계획 예상 = 합(기본 50 rps 기준 **17초**). 비율 목록은 바꿀 수 없고, 단계별 사람 승인은 없다.

## 승인 · 거절 · 취소

- **승인 = 실행 시작**(별도 실행 버튼 없음). 개발자 이상의 사람만. 기본은 **만든 사람이 아닌 사람**이다. 조직 설정 `allowSelfApproval = false`(기본)이면 만든 사람의 승인·거절은 403 `SELF_APPROVAL_FORBIDDEN`(거부 기록 남음, 계획은 그대로). `true` 면 허용하고 `selfApproved = true`, 로그 `SELF_APPROVED`.
- 승인 순서(서비스 → 연결 → 계획 잠금): `PENDING_APPROVAL` 아님 409 `PLAN_NOT_PENDING` → 자기 승인 403 → 만료면 `EXPIRED` 로 바꾸고 커밋한 뒤 409 `PLAN_EXPIRED` → **지문**이 다르면 `STALE` 로 바꾸고 커밋한 뒤 409 `PLAN_STALE`(`details` = 달라진 항목마다 `{field, reason: "CHANGED"}`) → (보안 게이트를 켰을 때) 판정이 막으면 로그 `SECURITY_GATE_BLOCKED` 를 남기고 커밋한 뒤 409 `SECURITY_GATE_BLOCKED`(계획은 `PENDING_APPROVAL` 그대로, 아래 "보안 게이트") → 통과면 (게이트를 켰을 때 로그 `SECURITY_GATE_PASSED`) `APPROVED` + 할 일 `MIGRATION_PLAN` + 로그 `APPROVED` → 202.
- 거절(`REJECTED`)·취소(`CANCELLED`)는 `PENDING_APPROVAL` 에서만이고 먼저 만료를 판정한다. 만든 사람의 거절도 자기 승인 스위치를 따른다(막혀 있으면 403 — 만든 사람은 취소를 쓴다). 취소는 개발자 이상 누구나.
- 승인 대기는 24시간(`MIGRATION_PLAN_APPROVAL_TTL_SECONDS`) 뒤 만료다. 만료 판정은 승인·거절·취소·새 계획 만들기 때 하고, 조회는 저장된 상태를 바꾸지 않는다(서비스 상세 `activeMigrationPlanId` 는 만료된 대기 계획을 세지 않는다).

### 지문(낡은 계획 판정)

계획을 만들 때 저장하고, **승인 때와 일꾼 시작 때** 같은 함수로 다시 계산해 항목별로 비교한다. 다르면 `STALE`(실행하지 않음).

| `field` | 담는 값 |
|---|---|
| `serviceStatus` | 서비스 상태 |
| `connections` | 살아 있는 연결의 `(id, currentDeploymentId, trafficWeight)`. 배포 없는 연결(현재 배포 없음 + 비율 0, 첫 배포 전 변수 넣기·첫 배포 실패가 만든다)은 이전에 관여하지 않으므로 뺀다(이 규칙 전에 저장한 지문도 비교 때 같은 규칙으로 거른다). 그 연결에 배포가 생기면 항목이 새로 나타나 잡힌다 |
| `sourceEnvironment` · `targetEnvironment` | 환경 `config`·`secret_ref` 의 SHA-256(값 자체는 없음), 접속 상태, 살아 있는가 |
| `openDeployRequests` | 서비스의 열린 배포 요청 id 목록 |

접속 재확인 시각(`connection_checked_at`)·`updated_at` 은 넣지 않는다 — 접속 재확인만으로는 낡지 않는다. 승인 대기 동안 배포를 접수하면(막지 않는다) 그 계획은 승인 때 `STALE` 이 된다.

### 보안 게이트 (보안·거버넌스 5, `SECURITY_GATE_ENABLED`, 기본 꺼짐)

켜면 **승인 때와 일꾼 시작 때** 같은 판정(규칙표 `evaluate_security`, 실행 시점 `EXECUTION`)을 한다. 사람 승인과 앞으로의 사전 승인 자동 DR 이 같은 판정을 쓴다. 꺼져 있으면 판정·네트워크·로그 줄이 모두 없다. 근거: [specs/sec-5-migration-gate.md](../../specs/sec-5-migration-gate.md).

- **필수 통제 하나라도 FAIL·UNKNOWN 이고 유효한 기한부 예외가 없으면 막는다.** 승인은 409 `SECURITY_GATE_BLOCKED`(계획은 승인 대기 그대로라 고친 뒤 다시 승인할 수 있다), 일꾼 시작은 트래픽을 바꾸지 않고 `FAILED`(`SECURITY_GATE_BLOCKED`, `rollbackStatus = NOT_NEEDED`).
- `details` 한 칸 = 막은 통제 하나의 사유 하나: `{field: 통제 ID, reason: 사유 코드}`. 예: `{"field": "C-AI-1", "reason": "PRECHECK_MISSING"}`, `{"field": "C-DB-1", "reason": "EVIDENCE_MISSING"}`.
- 판정 직전에 두 가지를 새로 확인해 증거(`findings`, `sourceRef = migration-plan:{planId}`)로 남긴다. 이 두 통제는 방금 확인한 증거만 쓴다.
  - **C-IMG-3** 출발·도착 현재 배포의 digest 가 같은가(WAS 상태).
  - **C-TLS-1** 대표 주소·도착 환경 주소의 TLS 접속 시험(5초). Echo 가 만든 공개 도메인 아래 주소만, 443 만, DNS 로 받은 IP 를 확인한 뒤 그 IP 로 접속한다. 사설 대역만 나오면 접속하지 않고 NA, 루프백·링크 로컬·예약 대역이나 공인·사설이 섞이면 접속하지 않고 UNKNOWN.
- 그 밖의 증거: 조직의 유효한 증거(빌드 검사·사람 점검 기록 등), 승인됐고 철회되지 않은 기한부 예외, 동일 조직·서비스·출발·도착 환경에서 24시간 미만에 성공한 실제 AI 사전 점검 중 최신 실행(C-AI-1). 더 최신의 모의 실행은 실제 결과를 가리지 않는다. 실제 결과가 없으면 같은 범위의 모의 결과는 `PRECHECK_MISSING`·`EVIDENCE_SIMULATED` 차단 사유에만 사용한다. 출발 누락·범위 불일치·만료·실패·다른 종류의 실행은 제외하며 EMERGENCY도 같은 기준이다.
- 판정은 로그 줄 `SECURITY_GATE_PASSED`·`SECURITY_GATE_BLOCKED` 의 `data` 에 정책 버전·판정 시점·막은 통제와 사유·쓴 증거 ID·예외 ID 로 남는다.
- 판정을 만든 뒤 바뀐 상태(현재 배포·환경 설정)는 지문이 잡는다(승인·시작 모두 잠금 안에서 지문을 먼저 비교한다).

## 실행 (일꾼)

- 비율 단계마다 트래픽 실행기로 두 연결 비율을 적용(멱등 키 `{planId}:{index}`)하고, **적용했다고 답했을 때만** 두 연결의 `trafficWeight` 를 한 트랜잭션에서 함께 적는다(서비스 합계 100). 그 뒤 **건수를 센다**(아래). 필요 건수를 채우면 헬스체크(도착 환경별 주소 + 도착 현재 배포의 헬스 경로) 한 번으로 판정하고, 통과하면 사람 개입 없이 다음 단계로 간다.
- 판정 실패 원인: 도착 5xx 가 허용 초과 `THRESHOLD_EXCEEDED`(그 순간 바로), 도착 미수신 `TARGET_NOT_RECEIVING`, 카운터를 읽지 못함·오래됨 `METRICS_UNAVAILABLE`(판단 불가), 헬스 2xx 아님 `HEALTH_CHECK_FAILED`, 복제 지연 초과 `DATA_LAG_EXCEEDED`(없으면 `METRICS_UNAVAILABLE`), 비율 적용 실패 `TRAFFIC_APPLY_FAILED`.
- 실패·판단 불가·적용 실패·**중단**(`ABORTED`)·**일꾼 상실**(`WORKER_LOST`, 임대 3번 상실)은 `ROLLING_BACK` 으로 가서 출발 100%·도착 0% 를 적용한다(키 `{planId}:rollback:{n}`). 적용되면 `ROLLED_BACK`(`rollbackStatus = SUCCEEDED`), 3번(`MIGRATION_ROLLBACK_MAX_ATTEMPTS`) 다 실패하면 `FAILED`(`rollbackStatus = FAILED`, `errorCode` 는 원래 원인, 비율은 마지막으로 적용된 값). 임대를 잃은 뒤에도 남은 롤백 시도는 한다.
- 단계 0 사전 확인 실패는 트래픽을 바꾼 적이 없어 `FAILED`(`PREFLIGHT_FAILED`, `rollbackStatus = NOT_NEEDED`). 시작 전(`APPROVED`) 일꾼 상실도 `FAILED`(`WORKER_LOST`, `NOT_NEEDED`).
- 성공하면 도착이 서비스 중(100), 출발은 예비(0)로 남는다. 그 뒤 도착에 새 배포가 나가면 출발이 따라간다(API 서버 3). 되돌아가기(Failback)는 출발·도착을 바꾼 새 계획이다.
- 일꾼이 죽어도 이어 간다: 단계 위치·"트래픽 요청함 시각"·센 값을 저장하고, 다시 시작한 일꾼은 결과가 없으면 같은 키로 다시 부르고 저장된 센 값에서 이어 센다(결과는 한 번만 반영, 로그 줄 중복 없음).

### 건수 세기 (비율 단계, 8-1)

위치는 `APPLY → OBSERVE(건수 세기) → JUDGE(헬스체크)` 다. `OBSERVE` 는 비율을 적용한 순간부터 최대 `maxWaitSeconds` 동안이다.

- 일꾼은 `MIGRATION_COUNT_POLL_SECONDS`(1초)마다 출발·도착 환경의 누적 카운터(요청·5xx, 진입점이 켜진 뒤 누적)를 읽는다. 비율을 적용한 직후 첫 읽기가 **기준값**이고, 센 값 = 지금 값 − 기준값이다. 출발·도착은 따로 갱신한다(한쪽만 읽지 못해도 다른 쪽은 반영). 매 읽기마다 중단 요청·임대 상실도 본다(중단은 다음 읽기, 1초 안에 롤백 시작).
- **허용 5xx** = max(1, floor(필요 건수 × `maxErrorRatePercent` ÷ 100)). 소수 계산은 정확히 한다(0.57% × 10000 = 57). 필요 건수 기준으로 고정한다.
- 결론 순서(읽을 때마다):
  1. 도착 5xx > 허용 → 그 순간 `THRESHOLD_EXCEEDED` 롤백(건수를 채우기 전이라도).
  2. 도착 센 요청 ≥ 필요 건수 → `JUDGE`(세기 끝 이유 `COUNT_REACHED`) → 헬스체크 → 통과면 다음 단계, 실패면 `HEALTH_CHECK_FAILED`.
  3. 최대 대기가 끝나지 않았으면 → 진행 숫자만 적고 다음 읽기(로그 줄 없음).
  4. 최대 대기 끝(건수 못 채움) → 마감 판정:

| 상태 | 결과 |
|---|---|
| 도착 기준값 없음(단계 내내 도착 읽기 실패) | `METRICS_UNAVAILABLE` 롤백 |
| 도착 마지막 성공 읽기가 `MIGRATION_COUNTS_STALE_SECONDS`(5초)보다 오래됨 | `METRICS_UNAVAILABLE` 롤백 |
| 출발·도착 기준값 모두 있고 **도착 미수신** | `TARGET_NOT_RECEIVING` 롤백 |
| 출발 기준값 없음(출발 읽기 실패) | 미수신 판정을 건너뛰고 아래로 |
| 그 밖(서비스 전체가 조용함) | **트래픽 적음** — 헬스체크 통과면 `PASSED` + `lowTraffic: true`(로그 `JUDGE_PASSED`, data `lowTraffic: true`), 실패면 `HEALTH_CHECK_FAILED` |

- **도착 미수신**(정수식, src = 출발 센 요청, tgt = 도착 센 요청, N = 필요 건수, w = 도착 비율): w < 100 이면 `src × w ≥ N × (100 − w)`(도착에 N건 이상 갔어야 함) **그리고** `4 × tgt × (100 − w) < src × w`(실제가 기대의 4분의 1 미만). w = 100 이면 `src ≥ N` **그리고** `4 × tgt < src`(출발 비율이 0인데 출발로 계속 간다 = 진입점이 비율을 적용하지 않음). 예: 10% 단계 출발 180건 → 도착 4건 실패, 5건 트래픽 적음 통과.
- **카운터 재시작**: 지금 읽은 값이 직전 값보다 작으면(요청·5xx 중 하나라도) 진입점이 재시작해 0부터 다시 쌓였다고 보고, 그때까지 센 값을 이어 간다(로그 `COUNTERS_RESET`, 화면 숫자는 줄지 않는다). 예: 14건 센 뒤 읽기가 3 → 17.
- **p95** 는 판정에 쓰지 않는다. 단계를 끝낼 때(통과·실패 모두) 지표 공급자에서 최근 `MIGRATION_P95_REFERENCE_WINDOW_SECONDS`(30초) p95 를 한 번 읽어 `metrics.p95Ms` 에 참고로 남긴다(못 읽으면 null).
- 같은 환경을 쓰는 다른 서비스의 요청도 카운터에 섞인다(카운터·가중치가 환경 단위). 지금은 "환경당 고객 앱 1개" 를 전제로 한다(앱별 라우트·카운터는 진입점 후속).
- 8-1 전에 만든 계획이 세는 위치(`OBSERVE`·`JUDGE`)에 센 값 없이 남아 있으면 새 일꾼이 그 자리에서 세기를 다시 시작한다(최대 대기 = 그 계획의 지켜보기 초, 필요 건수 = 설정 기본값).

### 진행 숫자 `progress` (단건 조회만)

`status = RUNNING` 이고 지금 단계가 비율 단계의 `OBSERVE`·`JUDGE` 일 때만 값이 있고, 그 밖(대기·끝·사전 확인·데이터 확인 단계)은 `null` 이다. 목록 응답에는 이 칸이 없다. 일꾼이 1초마다 갱신하므로 화면은 단건을 1초 간격으로 폴링한다(SSE 없음).

```json
"progress": {
  "step": 2, "targetWeight": 50,
  "requests": 14, "requiredRequests": 50,
  "errors5xx": 0, "allowedErrors5xx": 1,
  "sourceRequests": 15,
  "startedAt": "2026-11-03T03:01:06Z", "deadlineAt": "2026-11-03T03:01:36Z", "updatedAt": "2026-11-03T03:01:07Z"
}
```

`requests`·`errors5xx` = 도착이 센 요청·5xx, `sourceRequests` = 출발이 센 요청, `startedAt` = 비율을 적용해 세기 시작한 시각, `deadlineAt` = 최대 대기 끝, `updatedAt` = 마지막 카운터 읽기 시각. 화면 예: "GCP 처리 14/50건 · 오류 0/허용 1".

프론트 참고(API 서버 8-4 로컬 리허설, 실제 Envoy 진입점 + 50 rps):

- 기준값을 읽는 순간 처리 중이던 요청 때문에 도착 `requests` 가 `errors5xx` 보다 잠깐 작게 나올 수 있다(예: 요청 2 < 5xx 3). 요청과 5xx 카운터가 같은 순간에 올라가지 않아서이고, 판정은 각 값의 증가분만 따로 쓰므로 영향이 없다. 화면은 이 경우를 오류로 다루지 않는다.
- 단계별 실제 시간은 10% 3.5~6.7초, 50% 2.3~3.5초, 100% 약 1.2초였다(예상 시간 `expectedSeconds` 는 보수적으로 둔다). 1초 폴링이면 100% 단계의 진행 숫자는 0~1번만 보인다. 그 단계의 마지막 숫자는 단계 결과(`steps[4].result.metrics`)에 남는다.

### 컷오버 결과 `result` (단건 조회만, API 서버 16)

인프라 스크립트(`cutover.sh`·`rollback.sh`)가 고객 DB 표 `echo_ops.cutover_runs` 에 남긴 쓰기 정지·누락·중복 값을 이전 계획 상세에 붙인다. 화면이 그 값을 "이 이전의 결과"로 보여 주게 하려는 칸이다. 계획 단계와 따로 도는 작업의 결과이고, echo-was 는 읽기만 한다(저장하지 않고 고객 DB 에 쓰지 않는다).

| 칸 | 타입 | 뜻 |
|---|---|---|
| `writePauseMs` | int \| null | 쓰기 정지 시간(ms) |
| `missingRows` · `duplicateRows` | int \| null | 장부 대조로 찾은 누락·중복 행 수 |
| `rowsTotal` | int \| null | 대조한 장부 행 수 |
| `kind` | `CUTOVER` \| `ROLLBACK` \| null | 행의 종류 |
| `measuredBy` | `INFRA_SCRIPT` \| null | 행이 있으면 늘 `INFRA_SCRIPT` |
| `pausedAt` · `resumedAt` | datetime(UTC) \| null | 쓰기를 멈춘 시각·다시 연 시각(스크립트를 돌린 컴퓨터의 시계) |
| `drainMs` · `catchupMs` · `switchMs` | int \| null | 정지 시간의 구간별 값(ms) |

```json
"result": {"writePauseMs": 5817, "missingRows": 0, "duplicateRows": 0, "rowsTotal": 168,
           "kind": "CUTOVER", "measuredBy": "INFRA_SCRIPT",
           "pausedAt": "2026-11-04T13:51:59.670Z", "resumedAt": "2026-11-04T13:52:05.487Z",
           "drainMs": 774, "catchupMs": 428, "switchMs": 4615}
```

null 규칙(고객 DB 를 읽지 못해도 상세 조회는 늘 200 이다):

| 상황 | `result` |
|---|---|
| 끝나지 않은 계획(`PENDING_APPROVAL`·`APPROVED`·`RUNNING`·`ROLLING_BACK`) | `null`. 고객 DB 를 읽지 않는다 |
| 끝난 계획 + 행 있음 | 위 칸 모두 값. 표에서 NULL 을 허용하는 칸(`drainMs`·`catchupMs`·`switchMs`·`rowsTotal`·`missingRows`·`duplicateRows`)이 NULL 이면 그 칸만 null |
| 끝난 계획 + 행 없음 · 읽지 못함 · 읽는 조건 밖 | 객체, 안의 칸 전부 null. `measuredBy == null` 로 구분한다 |

- **읽는 조건**: `DATA_SYNC_CHECKER_MODE=postgres` · 끝 상태 7종 중 하나 · 계획 행 `simulated = false` · `CUTOVER_RUNS_DATABASE` 가 비어 있지 않음 · `finishedAt >= CUTOVER_RESULTS_NOT_BEFORE`(설정했을 때). 하나라도 아니면 고객 DB 에 붙지 않는다. 가짜 숫자를 만드는 모드는 없다.
- **찾는 규칙**: 행에는 서비스·계획 id 가 없어 시간 창으로 찾는다. 창 = [그 계획 `finishedAt`, 같은 서비스의 다음 계획 `createdAt`)(다음 계획이 없으면 끝 없음). 종류 = `mode` 가 `FAILBACK` 이면 `ROLLBACK`, 그 밖(`PLANNED`·`EMERGENCY`)은 `CUTOVER`. 창 안에서 `pausedAt` 이 가장 이른 행 하나다(같으면 `created_at`·`id` 순).
- **읽는 곳**: 도착 환경의 PostgreSQL(`DATA_SYNC_DATABASES` 매핑의 host·port). 응답 하나당 최대 3초(`DATA_SYNC_TIMEOUT_SECONDS`)다.
- **캐시 5초**: API 프로세스 메모리, 계획 id 별로 최대 512개(먼저 넣은 것부터 버린다). 찾았고 창이 닫힌 결과는 버려질 때까지 쓴다. 못 찾음·창 열림·실패는 `CUTOVER_RESULT_CACHE_SECONDS`(기본 5) 뒤 다시 읽으므로, 스크립트를 돌린 뒤 값이 보일 때까지 최대 5초 걸린다. 같은 계획의 동시 요청은 한 번만 읽는다.
- **상세 GET 만 읽는다.** 만들기·승인·거절·취소·중단 응답도 같은 모양이지만 고객 DB 를 읽지 않아, 끝나지 않았으면 null, 끝났으면(거절·취소) 칸 전부 null 이다. 목록·로그 응답에는 이 칸이 없다.
- **주의 — 다른 서비스에 같은 행이 붙을 수 있다.** 매핑이 환경 종류 단위이고 행에 서비스 구분이 없어, 같은 시간대에 끝난 다른 서비스의 계획에도 같은 행이 붙는다. 서비스 하나를 시연하는 지금은 막지 않는다.
- 읽지 못하면 경고 로그 `cutover result unavailable` 에 `plan_id`·`target_environment_id`·`error_code`(`CUTOVER_RESULT_ENV_UNMAPPED`·`CUTOVER_RESULT_SECRET_MISSING`·`CUTOVER_RESULT_UNREACHABLE`·`CUTOVER_RESULT_QUERY_FAILED`(권한 없음·표 없음 포함))만 남긴다. 주소·비밀값은 남기지 않고, 행이 없는 것은 로그를 남기지 않는다.

### 중단 `POST /migration-plans/{planId}/abort`

`APPROVED`·`RUNNING` 에서만(아니면 409 `PLAN_NOT_RUNNING`). `abortRequestedAt`·`abortedBy` 를 적고 그 계획의 할 일을 지금으로 당긴다 → 202. 다시 중단해도 202(기록은 그대로). 일꾼이 다음 실행 맨 앞에서 보고 `RUNNING` 이면 롤백(`ABORTED`), `APPROVED` 면 트래픽을 바꾸지 않고 `CANCELLED` 로 끝낸다. 열린 할 일이 없으면(옛 일꾼이 할 일을 닫아 버린 경우 등) `APPROVED` 는 그 자리에서 `CANCELLED`, `RUNNING` 은 할 일을 새로 넣는다.

### 실행 중 겹침

계획이 `APPROVED`·`RUNNING`·`ROLLING_BACK` 이면 그 서비스의 배포 요청·되돌리기·서비스 삭제 접수와 연결을 새로 만드는 환경 변수 넣기([variables.md](variables.md))는 409 `MIGRATION_IN_PROGRESS`(`details = [{field: "planId", reason: "MIGRATION_IN_PROGRESS", value}]`)다. 일꾼이 만드는 예비 따라가기는 막지 않는다(실행 중에는 생기지 않는다).

## 상태

| 값 | 뜻 | 다음 |
|---|---|---|
| `PENDING_APPROVAL` | 승인 대기 | `APPROVED`·`REJECTED`·`CANCELLED`·`EXPIRED`·`STALE` |
| `APPROVED` | 승인됨, 일꾼 시작 전 | `RUNNING`·`STALE`·`CANCELLED`(시작 전 중단)·`FAILED`(`WORKER_LOST`·`SECURITY_GATE_BLOCKED`) |
| `RUNNING` | 단계 진행 중 | `SUCCEEDED`·`ROLLING_BACK`·`FAILED`(`PREFLIGHT_FAILED`) |
| `ROLLING_BACK` | 출발 100% 로 되돌리는 중 | `ROLLED_BACK`·`FAILED` |
| `SUCCEEDED`·`ROLLED_BACK`·`FAILED`·`REJECTED`·`CANCELLED`·`EXPIRED`·`STALE` | 끝 | — |

단계 결과(`steps[i].result.status`): `PENDING`·`RUNNING`·`PASSED`·`FAILED`·`SKIPPED`. 롤백·실패로 끝나면 남은 단계는 `SKIPPED`, 중단·일꾼 상실로 멈춘 단계는 `FAILED`(`failureCode` = 그 원인).

원인 코드(`errorCode`·`failureCode`, `errorMessage` 는 코드별 고정 문장): `PREFLIGHT_FAILED` · `TRAFFIC_APPLY_FAILED` · `HEALTH_CHECK_FAILED` · `METRICS_UNAVAILABLE` · `THRESHOLD_EXCEEDED` · `TARGET_NOT_RECEIVING` · `DATA_LAG_EXCEEDED` · `ABORTED` · `WORKER_LOST` · `INTERNAL_ERROR` · `SECURITY_GATE_BLOCKED`(일꾼 시작 때 보안 게이트가 막음, 트래픽 그대로). 롤백 결과 `rollbackStatus`: `SUCCEEDED` · `FAILED` · `NOT_NEEDED`.

## `simulate` (가짜 모드 시연)

`failAtStep = n` 이면 그 단계에서 실행기를 부르지 않고 처리기가 결과를 만든다. 비율 단계의 카운터는 "`MIGRATION_EXPECTED_RPS` × 비율 × 적용 뒤 지난 초"로 만든다(시계 주입으로 결정적). 만든 값은 단계 결과·로그에 그대로 남는다. 만든 카운터도 실제 카운터처럼 **첫 읽기가 기준값**이고 그 뒤 증가분만 센다.

**실제 진입점(`ENTRYPOINT_EXECUTOR_MODE=http`)에서의 `simulate`**(API 서버 8-4, [specs/api-8-4-rehearsal-fixes.md](../../specs/api-8-4-rehearsal-fixes.md) §2.2): 받는 조건은 위와 같다(`EXECUTOR_MODE=fake`). 이때 **트래픽 비율은 실제로 진입점에 적용되고**(`PUT /weights`), 주입은 `failAtStep` 단계의 판정 입력만 바꾼다(카운터 읽기 대신 만든 값, `HEALTH` 는 헬스체크 대신 503). 롤백하면 실제 가중치도 출발 100 으로 돌아간다. 시연에서 "실제 트래픽이 옮겨지다가 실패 → 자동 롤백"을 보여 주는 용도다. 예외로 `TRAFFIC` 은 그 단계에서 `PUT /weights` 를 부르지 않고 적용 실패로 만든다(앞 단계에서 실제로 옮긴 비율은 롤백이 되돌린다).

| `reason` | 고를 수 있는 단계 | 결과 |
|---|---|---|
| `ERROR_RATE` | 1·2·4 | 첫 읽기(기준값)는 5xx 0, 그다음 읽기부터 도착 5xx 누적 = 허용 + 1 → 적용 약 1초 뒤 `THRESHOLD_EXCEEDED` |
| `TARGET_NOT_RECEIVING` | 1·2 | 도착 카운터 고정, 출발은 100% 몫으로 증가(비율 미적용 흉내) → 최대 대기 끝에 `TARGET_NOT_RECEIVING` |
| `LOW_TRAFFIC` | 1·2·4 | 두 카운터 고정 → 최대 대기 끝에 트래픽 적음 통과(`lowTraffic: true`) |
| `HEALTH` | 1·2·4 | 건수는 정상, 헬스 503 → `HEALTH_CHECK_FAILED` |
| `TRAFFIC` | 1·2·4 | 그 단계 비율 적용 실패(`metrics = null`) → `TRAFFIC_APPLY_FAILED` |
| `DATA_LAG` | 3 | 복제 지연 30초 → `DATA_LAG_EXCEEDED` |

`LATENCY` 는 8-1 에서 뺐다(요청 422). 저장된 옛 계획의 `LATENCY` 는 아무 실패도 만들지 않는다.

## 응답

`MigrationPlanSummary`: `id`, `serviceId`, `sourceEnvironmentId`·`targetEnvironmentId`(환경 id), `sourceServiceEnvironmentId`·`targetServiceEnvironmentId`(연결 id), `status`, `currentStep`, `expectedSeconds`, `createdBy{id, login}`, `approvedBy`, `selfApproved`, `errorCode`, `rollbackStatus`, `simulated`, `mode`(`PLANNED`·`EMERGENCY`·`FAILBACK`, 위 "계획 종류"), `disasterId`(이 계획을 만든 재난, 재난 트리거가 아니면 null), `createdAt`·`approvedAt`·`startedAt`·`finishedAt`·`expiresAt`.

`MigrationPlanDetail` = Summary + `rollbackConditions{…, healthCheck: true, p95Advisory: true}`, `maxWaitSeconds`, `steps[]`(계획 값과 실제 결과를 나란히), `progress`, `rejectedBy`·`cancelledBy`·`abortedBy`, `abortRequestedAt`, `errorMessage`, `result`(위 "컷오버 결과"), `failback{allowed, reason, disasterId}`.

- `failback`: 이 계획을 거꾸로 하는 복귀(도착 = 이 계획의 출발 환경)를 지금 만들 수 있는가. 출발 환경이 ACTIVE 이고 기준을 넘은 재난의 피해 환경이면 `{allowed: false, reason: "DISASTER_ACTIVE", disasterId}`, 아니면 `{allowed: true, reason: null, disasterId: null}`. 모든 계획에 같은 규칙이다([disasters.md](disasters.md#복귀-차단)).
- `maxWaitSeconds` 는 계획 행의 `observe_seconds` 칸이다(새 계획은 단계 최대 대기, 8-1 전 계획은 그 계획의 지켜보기 초). 8-1 에서 `observeSeconds` 칸을 뺐다.
- `steps[i]`: `requiredRequests`·`maxWaitSeconds` 는 비율 단계만 값이 있다(8-1 전 계획의 `requiredRequests` 는 null).
- `steps[i].result.metrics`: 비율 단계는 `requestCount`(도착 센 요청)·`errors5xx`·`allowedErrors5xx`·`errorRatePercent`(5xx ÷ 요청 × 100, 최대 100, 요청 0이면 null)·`sourceRequestCount`·`lowTraffic`·`p95Ms`(참고)·`healthStatus`. 데이터 확인 단계는 `replicationLagSeconds`, 사전 확인은 `healthStatus`. 재지 않은 값은 null. `p95Ms` 는 p95 를 못 읽었으면 null 이다(진입점 `/summary` 의 그 환경 `p95_ms` 가 null 인 경우 포함, 0 으로 바꾸지 않는다).
- `simulated`(계획·로그 페이지): 계획을 만들 때 한 번 정해 계획 행에 저장한 값이고 응답 때 다시 계산하지 않는다. 규칙은 `EXECUTOR_MODE = fake` 이면 true 다. 이 모드의 실행기 묶음에는 빌드·배포·내리기·데이터 복제 확인이 늘 가짜라, **계획을 실행할 묶음에 가짜가 하나라도 있으면 true** 와 같다. 그래서 트래픽·카운터가 실제 진입점(`ENTRYPOINT_EXECUTOR_MODE=http`)이고 헬스체크가 실제(`HEALTH_CHECKER_MODE=http`)여도 지금은 true 이며, 데이터 복제 확인이 가짜라는 것을 정직하게 표시한다([executors.md](../executors.md)).

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
    "mode": "PLANNED",
    "disasterId": null,
    "currentStep": 2,
    "expectedSeconds": 17,
    "createdBy": {"id": "u1…", "login": "dev-a"},
    "approvedBy": {"id": "u2…", "login": "dev-b"},
    "selfApproved": false,
    "errorCode": null,
    "rollbackStatus": null,
    "simulated": true,
    "createdAt": "2026-11-03T03:00:00Z",
    "approvedAt": "2026-11-03T03:01:00Z",
    "startedAt": "2026-11-03T03:01:00Z",
    "finishedAt": null,
    "expiresAt": "2026-11-04T03:00:00Z",
    "rollbackConditions": {"maxErrorRatePercent": 1.0, "maxP95Ms": 800, "maxReplicationLagSeconds": 1.0, "healthCheck": true, "p95Advisory": true},
    "maxWaitSeconds": 30,
    "steps": [
      {"index": 0, "kind": "PREFLIGHT", "sourceWeight": null, "targetWeight": null, "requiredRequests": null, "maxWaitSeconds": null, "expectedSeconds": 2,
       "result": {"status": "PASSED", "startedAt": "2026-11-03T03:01:00Z", "finishedAt": "2026-11-03T03:01:00Z",
                  "metrics": {"healthStatus": "OK", "errorRatePercent": null, "p95Ms": null, "requestCount": null, "errors5xx": null, "allowedErrors5xx": null, "sourceRequestCount": null, "lowTraffic": null, "replicationLagSeconds": null},
                  "failureCode": null}},
      {"index": 1, "kind": "SHIFT", "sourceWeight": 90, "targetWeight": 10, "requiredRequests": 20, "maxWaitSeconds": 30, "expectedSeconds": 6,
       "result": {"status": "PASSED", "startedAt": "2026-11-03T03:01:00Z", "finishedAt": "2026-11-03T03:01:04Z",
                  "metrics": {"healthStatus": "OK", "errorRatePercent": 0.0, "p95Ms": 120.0, "requestCount": 20, "errors5xx": 0, "allowedErrors5xx": 1, "sourceRequestCount": 180, "lowTraffic": false, "replicationLagSeconds": null},
                  "failureCode": null}},
      {"index": 2, "kind": "SHIFT", "sourceWeight": 50, "targetWeight": 50, "requiredRequests": 50, "maxWaitSeconds": 30, "expectedSeconds": 4,
       "result": {"status": "RUNNING", "startedAt": "2026-11-03T03:01:04Z", "finishedAt": null, "metrics": null, "failureCode": null}},
      {"index": 3, "kind": "DATA_SYNC_CHECK", "sourceWeight": null, "targetWeight": null, "requiredRequests": null, "maxWaitSeconds": null, "expectedSeconds": 2,
       "result": {"status": "PENDING", "startedAt": null, "finishedAt": null, "metrics": null, "failureCode": null}},
      {"index": 4, "kind": "SHIFT", "sourceWeight": 0, "targetWeight": 100, "requiredRequests": 50, "maxWaitSeconds": 30, "expectedSeconds": 3,
       "result": {"status": "PENDING", "startedAt": null, "finishedAt": null, "metrics": null, "failureCode": null}}
    ],
    "progress": {
      "step": 2, "targetWeight": 50, "requests": 25, "requiredRequests": 50, "errors5xx": 0, "allowedErrors5xx": 1,
      "sourceRequests": 25, "startedAt": "2026-11-03T03:01:04Z", "deadlineAt": "2026-11-03T03:01:34Z", "updatedAt": "2026-11-03T03:01:05Z"
    },
    "rejectedBy": null,
    "cancelledBy": null,
    "abortedBy": null,
    "abortRequestedAt": null,
    "errorMessage": null,
    "result": null,
    "failback": {"allowed": true, "reason": null, "disasterId": null}
  }
}
```

### 실행 로그 `GET /migration-plans/{planId}/logs?cursor&limit`

`MigrationLogPage`: `simulated`(계획 행 값), `items[{seq, step, at, level, code, message, data}]`, `nextCursor`, `hasMore`. 커서 = 마지막으로 받은 `seq`(숫자가 아니면 422 `VALIDATION_ERROR`), `limit` 1~500 기본 200. 줄은 상태 변경과 같은 트랜잭션에서 반영됐을 때만 쓰고(그래서 일꾼이 다시 시작해도 겹치거나 빠지지 않는다) 계획당 500줄(`LOG_TRUNCATED`)이다. `step` 은 계획 단계 번호라 응답의 `steps[i]` 와 맞춰 "계획 / 실제"를 볼 수 있다. `data` 는 비율·건수·지표 숫자(`STALE` 은 `changed` 항목 이름, `COUNTERS_RESET` 은 `counter`, 비율 단계 판정은 `lowTraffic`, 보안 게이트 판정은 정책 버전·통제 ID·사유 코드·증거·예외 ID)만 담는다. 1초마다의 진행 숫자 갱신은 로그 줄을 남기지 않는다(`progress` 로 본다).

| code | level | step | data |
|---|---|---|---|
| `DISASTER_TRIGGERED` | WARN | — | `disasterId`, `magnitude`, `distanceKm`(진원지 → 출발 환경). 재난이 만든 계획만, `PLAN_CREATED` 앞 |
| `PLAN_CREATED` | INFO | — | `expectedSeconds`, `requiredRequests`(3개), `maxWaitSeconds`, `mode`(8-3 부터) |
| `APPROVED` · `SELF_APPROVED` · `REJECTED` · `CANCELLED` · `EXPIRED` | INFO | — | |
| `STALE` | WARN | — | `changed`(항목 이름 목록) |
| `ABORT_REQUESTED` | WARN | — | |
| `STARTED` · `PREFLIGHT_OK` | INFO | — · 0 | |
| `PREFLIGHT_FAILED` | ERROR | 0 | |
| `WEIGHT_APPLIED` | INFO | 1·2·4 | `sourceWeight`, `targetWeight` |
| `WEIGHT_APPLY_FAILED` | WARN | 1·2·4 | `sourceWeight`, `targetWeight`(요청한 비율) |
| `COUNTERS_RESET` | INFO | 1·2·4 | `counter`(`source`·`target`), `previousRequests`, `previousErrors5xx`, `requests`, `errors5xx` |
| `JUDGE_PASSED` · `JUDGE_FAILED` | INFO · WARN | 1·2·4 | `requestCount`, `requiredRequests`, `errors5xx`, `allowedErrors5xx`, `sourceRequestCount`, `lowTraffic`. 트래픽 적음 통과면 문장 끝에 "(Low traffic: passed on the health check.)" |
| `DATA_SYNC_OK` · `DATA_SYNC_FAILED` | INFO · WARN | 3 | `replicationLagSeconds` |
| `SUCCEEDED` | INFO | — | |
| `ROLLBACK_STARTED` | WARN | 멈춘 단계 | |
| `ROLLBACK_APPLIED` | INFO | — | `sourceWeight: 100`, `targetWeight: 0` |
| `ROLLBACK_APPLY_FAILED` · `ROLLBACK_FAILED` | WARN · ERROR | — | `attempt`(·`maxAttempts`) |
| `FAILED` | ERROR | — | (일꾼 상실·내부 오류·보안 게이트. 문장 = 원인 코드 문장) |
| `SECURITY_GATE_PASSED` · `SECURITY_GATE_BLOCKED` | INFO · WARN | — | `policyVersion`, `phase`(`EXECUTION`), `blocking[{controlId, reasons}]`, `findingIds`, `exceptionIds`. 게이트를 켰을 때만. 승인 때 막히면 계획 상태는 그대로이고 이 줄만 남는다 |

성공한 계획의 줄 순서(게이트를 켜면 `APPROVED`·`STARTED` 앞에 `SECURITY_GATE_PASSED` 가 하나씩 더 있다): `PLAN_CREATED, APPROVED, STARTED, PREFLIGHT_OK, WEIGHT_APPLIED(1), JUDGE_PASSED(1), WEIGHT_APPLIED(2), JUDGE_PASSED(2), DATA_SYNC_OK(3), WEIGHT_APPLIED(4), JUDGE_PASSED(4), SUCCEEDED`(카운터가 재시작하면 그 단계 판정 줄 앞에 `COUNTERS_RESET`).

## HTTP 오류

| 상황 | HTTP | code |
|---|---|---|
| 바디 위반(필수 칸·범위·모르는 칸(`observeSeconds` 포함)·`requiredRequests` 개수·`simulate` 단계와 이유 불일치·`LATENCY`)·같은 환경 | 422 | `VALIDATION_ERROR` |
| 가짜가 아닌 실행기 모드의 `simulate` | 422 | `SIMULATE_NOT_ALLOWED` |
| 뷰어 · 에이전트 | 403 | `ROLE_FORBIDDEN` · `AGENT_READ_ONLY`(거부 기록) |
| 자기 승인·거절이 막힌 조직 | 403 | `SELF_APPROVAL_FORBIDDEN`(거부 기록) |
| 서비스·환경·계획 없음, 다른 조직 | 404 | `SERVICE_NOT_FOUND` · `ENVIRONMENT_NOT_FOUND` · `MIGRATION_PLAN_NOT_FOUND` |
| 서비스 삭제 중 · 도착 환경 접속 실패 | 409 | `SERVICE_DELETING` · `ENVIRONMENT_UNHEALTHY` |
| 끝나지 않은 계획 있음 | 409 | `MIGRATION_PLAN_OPEN`(+`planId`) |
| 출발이 하나(100%)가 아님 | 409 | `SOURCE_NOT_SINGLE` |
| 도착이 ACTIVE 이고 기준을 넘은 재난의 피해 환경 | 409 | `DISASTER_ACTIVE`(+`disasterId`) |
| 도착 예비가 따라가는 중 · 그 밖의 열린 배포 요청 | 409 | `TARGET_SYNCING` · `DEPLOY_IN_PROGRESS`(+`deployRequestId`) |
| 도착에 앱 없음·다른 버전 | 409 | `TARGET_NOT_READY`(+`NO_DEPLOYMENT`·`VERSION_MISMATCH`·`lastSyncError`) |
| 승인 대기가 아님 · 실행 중이 아님 | 409 | `PLAN_NOT_PENDING` · `PLAN_NOT_RUNNING` |
| 지문 다름 · 만료 | 409 | `PLAN_STALE`(+달라진 항목) · `PLAN_EXPIRED` |
| 보안 게이트가 승인을 막음(게이트를 켰을 때) | 409 | `SECURITY_GATE_BLOCKED`(+통제 ID·사유 코드) |
| 실행 중 그 서비스의 배포 요청·되돌리기·삭제·새 연결을 만드는 변수 넣기 | 409 | `MIGRATION_IN_PROGRESS`(+`planId`) |

## 설정

| 환경변수 | 기본 | 뜻 |
|---|---|---|
| `MIGRATION_REQUIRED_REQUESTS_DEFAULT` | `20,50,50` | 비율 10·50·100% 단계의 필요 건수(각 1~10000) |
| `MIGRATION_MAX_WAIT_SECONDS_DEFAULT` | 30 | 비율 단계 최대 대기(5~300) |
| `MIGRATION_EXPECTED_RPS` | 50 | 예상 rps(1~10000). 예상 시간·가짜 카운터·`simulate` 에 쓴다 |
| `MIGRATION_COUNT_POLL_SECONDS` | 1 | 카운터 읽기 주기(1~10) |
| `MIGRATION_COUNTS_STALE_SECONDS` | 5 | 마감 때 도착 마지막 성공 읽기가 이보다 오래되면 판단 불가(1~60) |
| `MIGRATION_P95_REFERENCE_WINDOW_SECONDS` | 30 | p95 참고 읽기 창(5~300) |
| `MIGRATION_STEP_OVERHEAD_SECONDS` | 2 | 단계 예상 시간에 더하는 여유(0~60) |
| `MIGRATION_PLAN_APPROVAL_TTL_SECONDS` | 86400 | 승인 대기 만료 |
| `MIGRATION_ROLLBACK_MAX_ATTEMPTS` | 3 | 롤백 비율 적용 최대 시도 |
| `SECURITY_GATE_ENABLED` | `false` | 보안 게이트(승인·일꾼 시작 판정, 실행 직전 TLS 시험). API·일꾼 모두 같은 값. 운영 켜짐 11/5(`deploy/base.yaml`, `WAS_VERSION=2026.11.05`) |
| `WAS_VERSION` | (없음) | 판정 범위의 WAS 버전(C-IAM-2·C-AUD-1 의 증거·예외와 맞춘다). 비면 그 두 통제는 확인 불가 |
| `CUTOVER_RUNS_DATABASE` | `app` | 컷오버 결과 표가 있는 고객 DB 이름. 빈 값이면 읽지 않는다(`result` 칸 전부 null) |
| `CUTOVER_RESULTS_NOT_BEFORE` | (없음) | 이보다 먼저 끝난 계획은 읽지 않는다(시간대가 있는 ISO 8601, 없으면 시작 때 설정 오류). 표가 생기기 전 계획에 나중 행이 붙지 않게 한다. 운영 `2026-11-04T13:30:00Z` |
| `CUTOVER_RESULT_CACHE_SECONDS` | 5 | 못 찾음·창 열림·실패 결과를 다시 쓰는 시간(초, 1 이상) |

`MIGRATION_OBSERVE_SECONDS_DEFAULT` 는 8-1 에서 없앴다(설정에 남아 있어도 무시한다).
