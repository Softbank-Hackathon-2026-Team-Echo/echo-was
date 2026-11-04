# 앱 환경 변수 API (API 서버 10)

고객 앱의 환경 변수·API 키(예: `DATABASE_URL`)를 **서비스 × 환경** 단위로 넣는다. 값은 관리 평면 DB 가 아니라 비밀 보관함(GCP Secret Manager)에만 있고, 배포할 때 일꾼이 배포 기록에 고정된 버전을 꺼내 앱 네임스페이스의 Secret `{ns}-env` 에 쓴다. 근거: SPEC [specs/api-10-app-env-secrets.md](../../specs/api-10-app-env-secrets.md), 이슈 #10, NFR-17.

- 화면(echo-frontend #14)이 쓰는 계약이다. 공통 봉투·camelCase·`X-Request-ID` 는 [environments.md](environments.md#공통-규칙)와 같다.
- **쓰기 전용이다.** 값·값 일부·길이는 어떤 응답·오류·로그에도 없다. 다시 보는 API 는 없다.
- **앱은 다시 배포해야 바뀐다**(자동 재시작 없음). 응답 `pendingRedeploy` 로 보인다.
- `environmentId` 는 환경 id(서비스 상세의 `environments[].environment.id`)다. 연결(`serviceEnvironmentId`)이 아니다.
- **연결은 그 환경의 첫 배포 요청 때 생긴다.** 그 전에는 404 `SERVICE_ENVIRONMENT_NOT_FOUND` 다.

## 엔드포인트

경로 머리: `/api/v1/orgs/{orgId}/services/{serviceId}/environments/{environmentId}/variables`

| 메서드 · 경로 | 요청 | 성공 | 실패 | 최소 역할 |
|---|---|---|---|---|
| `GET …/variables` | — | 200 `{ items, simulated }` | 404 `SERVICE_NOT_FOUND`·`SERVICE_ENVIRONMENT_NOT_FOUND` | 뷰어·에이전트 |
| `PUT …/variables/{key}` | `{ "value": "<문자열>" }` | 새 키 201, 있던 키 200. 항목 + `simulated` | 아래 표 | 개발자(사람) |
| `DELETE …/variables/{key}` | — | 204 | 404 `ENV_VAR_NOT_FOUND` 외 아래 표 | 어드민(사람) |

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
| 404 `SERVICE_NOT_FOUND` · `SERVICE_ENVIRONMENT_NOT_FOUND` · `ENV_VAR_NOT_FOUND` | 서비스 없음(다른 조직 포함) · 그 환경에 연결 없음 · 지울 키 없음 |
| 409 `SERVICE_DELETING` | 서비스 삭제 중 |
| 409 `SECRET_STORE_NOT_CONFIGURED` | 실제 실행기인데 보관함이 가짜다(`APP_SECRET_STORE_MODE=fake` + `EXECUTOR_MODE=real`). 값을 받지 않고 행도 만들지 않는다 |
| 502 `SECRET_STORE_UNAVAILABLE` · `SECRET_STORE_FORBIDDEN` | 보관함 장애·10초 초과 · 권한 거절. 응답에 값·비밀 이름 없음. 다시 시도하면 새 버전이 더해질 수 있다 |

## 배포와의 관계

- **새 배포**(사람 배포·예비 따라가기): 배포 기록을 만들 때 **그 연결의** 최신 버전을 `config_snapshot.envVars` 에 고정한다. 예비 따라가기가 서비스 중 환경의 값을 복사하지 않는다.
- **되돌리기**: 대상 배포 기록의 `envVars` 를 그대로 쓴다(그때의 버전).
- **꺼내지 못하면**: 영구 실패(없음·권한·사용 중지)는 요청이 `FAILED`(`ENV_SECRET_UNAVAILABLE`)로 끝나고 앱을 반영하지 않는다. 일시 오류는 일꾼이 다시 시도한다.

실행기 쪽 규칙(`{ns}-env` 를 언제 어떻게 쓰는지)은 [executors.md](../executors.md#앱-환경-변수-ns-env-api-서버-10)에 있다.
