# 앱 환경 변수 API (API 서버 10)

고객 앱의 환경 변수·API 키(예: `DATABASE_URL`)를 **서비스 × 환경** 단위로 넣는다. 값은 관리 평면 DB 가 아니라 비밀 보관함(GCP Secret Manager)에만 있고, 배포할 때 일꾼이 배포 기록에 고정된 버전을 꺼내 앱 네임스페이스의 Secret `{ns}-env` 에 쓴다. 근거: SPEC [specs/api-10-app-env-secrets.md](../../specs/api-10-app-env-secrets.md), 이슈 #10, NFR-17.

- 화면(echo-frontend #14)이 쓰는 계약이다. 공통 봉투·camelCase·`X-Request-ID` 는 [environments.md](environments.md#공통-규칙)와 같다.
- **쓰기 전용이다.** 값·값 일부·길이는 어떤 응답·오류·로그에도 없다. 다시 보는 API 는 없다.
- 넣기·지우기·복사는 감사 로그에 남는다(`SERVICE_VARIABLE_SET`·`SERVICE_VARIABLE_DELETED`·`SERVICE_VARIABLE_COPIED`, [audit-logs.md](audit-logs.md)). 누가 어떤 키를 바꿨는지만 적고 값·비밀 참조·버전은 적지 않는다.
- **앱은 다시 배포해야 바뀐다**(자동 재시작 없음). 응답 `pendingRedeploy` 로 보인다.
- `environmentId` 는 환경 id(서비스 상세의 `environments[].environment.id`)다. 연결(`serviceEnvironmentId`)이 아니다.
- **첫 배포 전에도 넣을 수 있다**(API 서버 10-4, [SPEC](../../specs/api-10-4-vars-before-first-deploy.md), 이슈 #93). 서비스 × 환경 연결이 아직 없으면 `PUT` 이 **배포 없는 연결**(`trafficWeight = 0`, 현재 배포 없음, 서비스 상세 `role = NONE`)을 만든 뒤 저장하고, 그 환경의 첫 배포 요청은 이 연결을 그대로 쓴다(넣은 값이 첫 배포의 `config_snapshot.envVars` 에 고정된다). 조건은 환경이 **서비스와 같은 조직에 살아 있는 것**뿐이다(환경 접속 확인 실패여도 넣을 수 있다 — 배포 접수가 따로 막는다). 다른 조직·없는·지운 환경이면 404 `SERVICE_ENVIRONMENT_NOT_FOUND` 다.
  - `GET` 은 연결을 만들지 않는다. 연결이 없으면 `{ "items": [], "deployed": false }` 다. `DELETE` 도 만들지 않고 404 `ENV_VAR_NOT_FOUND` 다.
  - 배포 없는 연결은 서비스 중·예비가 아니다: 대표 주소·진입점 가중치·예비 따라가기·보안 매트릭스·C-SEC-1 기준·이전 도착(409 `TARGET_NOT_READY`)·이전 계획 지문(`connections`)·대시보드 지표 시리즈 어디에도 잡히지 않는다. 첫 배포에 실패한 연결과 같은 모양이다.
  - 서비스에 실행 중인 이전 계획(APPROVED·RUNNING·ROLLING_BACK, 재난 자동 대피 포함)이 있으면 **연결을 새로 만드는** `PUT` 은 첫 배포 접수와 같은 409 `MIGRATION_IN_PROGRESS` 다. 이미 연결이 있는 환경의 `PUT`·`DELETE` 는 막지 않는다(값은 다시 배포해야 반영되므로 이전에 영향이 없다). 승인 대기 계획은 막지 않고, 그때 만든 배포 없는 연결은 지문에 잡히지 않아 계획을 STALE 로 만들지 않는다.
  - 연결을 만들 때 환경별 주소가 정해진다(배포 접수와 같은 규칙, 겹치면 409 `PUBLIC_HOST_CONFLICT`). 살아 있는 연결이 있는 환경은 지울 수 없으므로(409 `ENVIRONMENT_IN_USE`) 변수만 넣은 환경도 서비스를 지워야 환경을 지울 수 있다.
  - **필수 변수 누락 검사는 후속이다.** 배포 요청 전에 저장소의 `.env.example`·`echo.yaml` 선언과 비교해 빠진 키를 알리고 막는 일은 이 범위가 아니다(별도 이슈).
- **이전 보안 통제 C-SEC-1 이 키 이름을 본다**(값은 안 본다). 서비스 중 환경에 있는 키가 이전 도착 환경에 없으면 그 도착으로의 이전이 막힌다(매트릭스 `encryption` 칸 FAIL, 게이트 409). 대기 환경에도 같은 키를 넣어 둔다([security.md](security.md)).

## 엔드포인트

경로 머리: `/api/v1/orgs/{orgId}/services/{serviceId}/environments/{environmentId}/variables`

| 메서드 · 경로 | 요청 | 성공 | 실패 | 최소 역할 |
|---|---|---|---|---|
| `GET …/variables` | — | 200 `{ items, simulated, deployed }` | 404 `SERVICE_NOT_FOUND`·`SERVICE_ENVIRONMENT_NOT_FOUND` | 뷰어·에이전트 |
| `PUT …/variables/{key}` | `{ "value": "<문자열>" }` | 새 키 201, 있던 키 200. 항목 + `simulated`·`deployed` | 아래 표 | 개발자(사람) |
| `DELETE …/variables/{key}` | — | 204 | 404 `ENV_VAR_NOT_FOUND` 외 아래 표 | 어드민(사람) |
| `POST /api/v1/orgs/{orgId}/services/{serviceId}/variables:copy` | `{ sourceEnvironmentId, targetEnvironmentIds[], keys[], overwrite? }` | 200 `{ copied[], failed[], simulated }` | 아래 "다른 환경으로 복사" | 개발자(사람) |

에이전트 토큰은 키 이름 목록(`GET`)만 볼 수 있고 `PUT`·`DELETE` 는 403 `AGENT_READ_ONLY` 다.

## 응답 항목

```json
{
  "key": "DATABASE_URL",
  "version": 3,
  "secretRef": "gcp-sm:projects/team-echo-final/secrets/echo-app-team-echo-1a2b3c4d-5e6f7a8b-database-url/versions/3",
  "updatedAt": "2026-11-04T12:00:00Z",
  "updatedBy": { "id": "…", "githubLogin": "octocat" },
  "appliedVersion": 2,
  "pendingRedeploy": true
}
```

| 칸 | 뜻 |
|---|---|
| `key` | 키 이름. 목록은 이름 순이다 |
| `version` | 보관함의 최신 버전 번호 |
| `secretRef` | 위 버전을 고정한 참조. gcp 보관함은 `gcp-sm:projects/<프로젝트>/secrets/<이름>/versions/<n>`, 가짜 보관함은 `fake-sm:<이름>/versions/<n>` |
| `updatedAt`·`updatedBy` | 마지막으로 넣은 시각·사람 |
| `appliedVersion` | 연결의 현재 배포 기록에 고정된 버전. 없으면(아직 배포 안 함, 이 키가 없던 배포) null |
| `pendingRedeploy` | `appliedVersion ≠ version`. 다시 배포해야 반영된다 |
| `simulated` | (목록·PUT 응답) 가짜 보관함(`APP_SECRET_STORE_MODE=fake`)이면 true |
| `deployed` | (목록·PUT 응답) 그 환경 연결에 현재 배포가 있는가. 첫 배포 전(연결 없음·배포 없는 연결)이면 false — 넣은 값은 첫 배포 때 적용된다(API 서버 10-4) |

지운 키는 목록에서 사라지지만 지금 돌고 있는 앱의 Secret 에서는 **다음 배포 때** 빠진다.

## 규칙

- **키 이름**: `^[A-Z][A-Z0-9_]{0,63}$`. 예약: `PORT`, `ECHO_` 로 시작하는 모든 이름(실행기가 넣는 값을 앱이 덮지 못하게).
- **값**: UTF-8 문자열 1~16,384바이트. 여러 줄 허용.
- **개수**: 연결 하나에 키 40개까지.
- **삭제**: 행만 삭제 표시한다. 보관함 비밀은 지우지 않는다(옛 배포 기록·되돌리기가 그 버전을 읽어야 한다). 같은 키를 다시 넣으면 같은 보관함 비밀에 버전이 이어진다.
- **이전 계획**은 트래픽만 옮기고 값을 바꾸지 않는다. 그래서 값이 바뀐 연결은 이전 뒤에도 `pendingRedeploy` 로 보인다.

## 오류

| 상태 · 코드 | 언제 |
|---|---|
| 422 `ENV_VAR_KEY_INVALID` · `ENV_VAR_KEY_RESERVED` | 키 이름 규칙 위반 · 예약 이름. `details[].field = "key"` |
| 422 `ENV_VAR_VALUE_INVALID` | 빈 값, 16,384바이트 초과. `details[].field = "value"` |
| 422 `ENV_VAR_LIMIT_EXCEEDED` | 41번째 키 |
| 422 `VALIDATION_ERROR` | 본문 형식 오류(`value` 가 문자열이 아님, 모르는 칸은 칸 이름을 되돌리지 않고 `field = "body"`) |
| 404 `SERVICE_NOT_FOUND` · `SERVICE_ENVIRONMENT_NOT_FOUND` · `ENV_VAR_NOT_FOUND` | 서비스 없음(다른 조직 포함) · 환경이 서비스와 같은 조직에 없음(없음·지움·다른 조직, 연결도 없음) · 지울 키 없음(첫 배포 전 연결이 없는 환경 포함) |
| 409 `PUBLIC_HOST_CONFLICT` | (`PUT` 이 연결을 만들 때) 환경별 주소가 살아 있는 다른 연결과 겹친다. 배포 접수와 같은 규칙([deploy-requests.md](deploy-requests.md)) |
| 409 `MIGRATION_IN_PROGRESS` | (`PUT` 이 연결을 만들 때) 서비스에 실행 중인 이전 계획이 있다. `details[0].value` 는 계획 id. 배포 접수와 같은 규칙 |
| 409 `SERVICE_DELETING` | 서비스 삭제 중 |
| 409 `SECRET_STORE_NOT_CONFIGURED` | 실제 실행기인데 보관함이 가짜다(`APP_SECRET_STORE_MODE=fake` + `EXECUTOR_MODE=real`). 값을 받지 않고 행도 만들지 않는다 |
| 502 `SECRET_STORE_UNAVAILABLE` · `SECRET_STORE_FORBIDDEN` | 보관함 장애·10초 초과 · 권한 거절. 응답에 값·비밀 이름 없음. 다시 시도하면 새 버전이 더해질 수 있다 |

## 다른 환경으로 복사 (이슈 #99)

값을 읽는 API 가 없어 화면이 할 수 없는 "환경마다 다시 입력"을 줄인다. 근거: SPEC [specs/api-99-vars-copy-standby.md](../../specs/api-99-vars-copy-standby.md).

```json
POST /api/v1/orgs/{orgId}/services/{serviceId}/variables:copy
{ "sourceEnvironmentId": "uuid", "targetEnvironmentIds": ["uuid"], "keys": ["API_KEY"], "overwrite": false }

200
{
  "copied": [{ "environmentId": "uuid", "key": "API_KEY", "version": 1, "pendingRedeploy": true }],
  "failed": [{ "environmentId": "uuid", "key": "DATABASE_URL", "code": "ENV_VAR_ALREADY_EXISTS" }],
  "simulated": true
}
```

- 원본 연결의 **최신 버전 값**을 보관함에서 읽어(메모리로만) 대상마다 **새 버전**으로 쓴다. 쓰기는 `PUT` 과 같은 저장 경로다: 서비스 → 연결 잠금, 대상에 연결이 없으면 배포 없는 연결을 만든다(첫 배포 전 환경도 대상이 된다), 40개 한도, 보관함 이름 규칙(지운 키의 옛 비밀에 버전을 잇는다).
- **값은 응답·감사 로그·로그 어디에도 없다.** 감사 줄은 복사한 대상·키마다 `SERVICE_VARIABLE_COPIED` 한 줄(`{environmentId, key, sourceEnvironmentId}`).
- `overwrite` 기본 false: 대상에 같은 키가 있으면 보관함을 부르지 않고 `failed` `ENV_VAR_ALREADY_EXISTS`(`DATABASE_URL` 처럼 환경마다 다른 값을 덮지 않게). true 면 새 버전으로 덮는다.
- 앱은 다시 배포해야 바뀐다(`pendingRedeploy` 규칙 그대로 — 복사 직후는 늘 true). 원본은 바뀌지 않는다.
- 결과는 키 × 대상마다 `copied` 나 `failed` 중 하나다. `failed[].code`:

| 코드 | 언제 |
|---|---|
| `ENV_VAR_ALREADY_EXISTS` | 대상에 이미 있고 `overwrite = false` |
| `ENV_VAR_NOT_FOUND` | 원본(연결 없음 포함)에 그 키가 없다 — 대상마다 한 줄 |
| `ENV_SECRET_UNAVAILABLE` | 원본 고정 버전을 영구히 못 읽었다(파기·사용 중지) |
| `ENV_VAR_LIMIT_EXCEEDED` | 대상 연결이 40개를 넘는다 |
| `MIGRATION_IN_PROGRESS` · `PUBLIC_HOST_CONFLICT` · `SERVICE_ENVIRONMENT_NOT_FOUND` | 쓰는 도중 이전이 시작됨·새 연결 주소 충돌·대상 환경이 지워짐 |
| `SECRET_STORE_UNAVAILABLE` · `SECRET_STORE_FORBIDDEN` | 쓰는 도중 보관함 장애·권한 거절. **남은 항목은 부르지 않고** 같은 코드로 싣는다 |

통째 거절(아무것도 쓰지 않음):

| 상태 · 코드 | 언제 |
|---|---|
| 422 `VALIDATION_ERROR` | 원본 환경이 대상에 있음(`details[0] = {field: targetEnvironmentIds, reason: SOURCE_IN_TARGETS}`), 대상·키 중복(`reason = DUPLICATE`), 대상 1~10개·키 1~40개 밖, 모르는 칸 |
| 422 `ENV_VAR_KEY_INVALID` · `ENV_VAR_KEY_RESERVED` | 키 이름 규칙 위반 · 예약 이름 |
| 404 `SERVICE_NOT_FOUND` · `SERVICE_ENVIRONMENT_NOT_FOUND` | 서비스 없음 · 원본이나 대상 환경이 서비스와 같은 조직에 없음(없음·지움·다른 조직) |
| 409 `SERVICE_DELETING` · `SECRET_STORE_NOT_CONFIGURED` | `PUT` 과 같다 |
| 409 `MIGRATION_IN_PROGRESS` | 연결을 새로 만들어야 하는 대상이 있는데 서비스에 실행 중인 이전 계획이 있다(`PUT` 과 같은 규칙. 이미 연결이 있는 대상만이면 막지 않는다) |
| 502 `SECRET_STORE_UNAVAILABLE` · `SECRET_STORE_FORBIDDEN` | 원본 값을 읽을 때 보관함 장애·권한 거절 |

## 배포와의 관계

- **새 배포**(사람 배포·예비 따라가기): 배포 기록을 만들 때 **그 연결의** 최신 버전을 `config_snapshot.envVars` 에 고정한다. 예비 따라가기가 서비스 중 환경의 값을 복사하지 않는다.
- **되돌리기**: 대상 배포 기록의 `envVars` 를 그대로 쓴다(그때의 버전).
- **꺼내지 못하면**: 영구 실패(없음·권한·사용 중지)는 요청이 `FAILED`(`ENV_SECRET_UNAVAILABLE`)로 끝나고 앱을 반영하지 않는다. 일시 오류는 일꾼이 다시 시도한다.

실행기 쪽 규칙(`{ns}-env` 를 언제 어떻게 쓰는지)은 [executors.md](../executors.md#앱-환경-변수-ns-env-api-서버-10)에 있다.
