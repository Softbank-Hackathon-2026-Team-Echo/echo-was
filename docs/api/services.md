# 서비스 API (API 서버 3)

배포할 앱 하나를 **서비스**로 등록·조회·수정·삭제한다. 근거: SPEC [specs/api-3-one-touch-deploy.md](../../specs/api-3-one-touch-deploy.md), 원장 F1·F2. 배포 요청은 [deploy-requests.md](deploy-requests.md).

- 실행 중인 서버의 OpenAPI: `GET /docs` · `GET /openapi.json`. 이 문서와 같은 계약이다.
- **로그인·조직 범위(API 서버 6).** 경로는 `/api/v1/orgs/{orgId}` 아래이고 그 조직 멤버만 본다. 조회는 뷰어(에이전트 토큰 가능), 등록·수정은 개발자, 삭제는 어드민이다([auth.md](auth.md)). 서비스 이름은 **조직 안에서만** 유일하다(같은 조직에서 겹치면 409, 다른 조직과는 겹쳐도 된다).
- **주소(API 서버 3-1, [SPEC](../../specs/api-3-1-public-host-format.md))**: 대표 `https://{name}-{suffix}.{PUBLIC_BASE_DOMAIN}`, 환경별 `https://{name}-{environment}-{같은 suffix}.{PUBLIC_BASE_DOMAIN}`. `suffix` 는 서비스를 만들 때 서버가 정하는 소문자·숫자 무작위 8자이고 바꾸지 않는다. 도메인 바로 아래 한 단계라 `*.도메인` 하나로 덮는다. 레이블(첫 `.` 앞)이 63자를 넘으면 suffix 앞부분을 54자로 자르고 끝의 `-` 를 뗀다. 같은 이름으로 지우고 다시 만들면 주소가 바뀐다.
- 공통 봉투·camelCase·null 규칙·`X-Request-ID` 는 [environments.md](environments.md#공통-규칙)와 같다. 목록은 `Page`(`items`, `total`, `page` 0부터, `size` 1~100 기본 20).
- **`simulated: true` 는 목업 표시다.** 서비스의 `simulated` 는 가짜 소스 저장소 클라이언트(`SOURCE_CLIENT_MODE=fake`, 기본)로 등록했다는 뜻이다. 연결의 `simulated` 는 현재 배포를 만든 요청이 가짜 실행기로 처리됐다는 뜻이다(이번에는 빌드·배포·대표 주소 연결·내리기가 모두 가짜). 값은 행에 저장하고 응답 때 설정으로 다시 계산하지 않는다.

## 엔드포인트

| 메서드 · 경로 | 요청 | 성공 | 실패 | 최소 역할 |
|---|---|---|---|---|
| `POST /api/v1/orgs/{orgId}/services` | `CreateServiceRequest` | 201 `ServiceDetail` | 422 `VALIDATION_ERROR`, 404 `REPO_NOT_ACCESSIBLE`(+`installUrl`), 502 `SOURCE_PROVIDER_UNAVAILABLE`, 409 `SERVICE_NAME_CONFLICT`·`PUBLIC_HOST_CONFLICT` | 개발자 |
| `GET /api/v1/orgs/{orgId}/services?page&size` | — | 200 `Page[ServiceSummary]` | 422(page·size 범위) | 뷰어·에이전트 |
| `GET /api/v1/orgs/{orgId}/services/{id}` | — | 200 `ServiceDetail` | 404 `SERVICE_NOT_FOUND` | 뷰어·에이전트 |
| `PATCH /api/v1/orgs/{orgId}/services/{id}` | `UpdateServiceRequest` | 200 `ServiceDetail` | 404, 409 `SERVICE_DELETING`, 422 `SERVICE_NAME_IMMUTABLE`·`VALIDATION_ERROR`, 404·502(새 `repoUrl` 확인) | 개발자 |
| `DELETE /api/v1/orgs/{orgId}/services/{id}` | — | 202 `ServiceDetail`(`status = DELETING`) | 404 `SERVICE_NOT_FOUND`, 409 `MIGRATION_IN_PROGRESS`(+`planId`) | 어드민 |

코드 클래스: `CreateServiceRequest`·`UpdateServiceRequest`, `ServiceSummary` = `ServiceSummaryResponse`, `ServiceDetail` = `ServiceDetailResponse`(`app/schemas/service.py`).

### 등록 `POST /api/v1/orgs/{orgId}/services`

```json
{
  "name": "shop-api",
  "description": "데모 쇼핑몰 API",
  "repoUrl": "https://github.com/team-echo/shop-api",
  "healthCheckPath": "/healthz",
  "healthCheckTimeoutSeconds": 300,
  "containerPort": 8080
}
```

| 칸 | 규칙 |
|---|---|
| `name` | 필수. 소문자·숫자·하이픈 1~63자(Kubernetes 이름 규칙). 살아 있는 서비스 중 유일. **바꿀 수 없다**(주소가 이름에서 나온다) |
| `description` | 선택 |
| `repoUrl` | 필수. `https://github.com/<owner>/<repo>`(끝 `/`·`.git` 허용, 정규형으로 저장). owner·repo 는 영문·숫자·`-`·`_`·`.` 100자 이하. 다른 호스트·`http`·자격 증명이 든 주소는 422 `VALIDATION_ERROR`(`reason = INVALID_REPO_URL`) |
| `healthCheckPath` | 선택, 기본 `/healthz`. `/` 로 시작, 공백 없음, 255자 이하 |
| `healthCheckTimeoutSeconds` | 선택, 기본 300, 10~1800 |
| `containerPort` | 선택, 기본 8080, 1~65535 |

흐름: 저장소 접근 확인(트랜잭션 밖) → 무작위 suffix 를 정해 서비스 저장(`status = ACTIVE`, 대표 주소 `https://{name}-{suffix}.{PUBLIC_BASE_DOMAIN}`) → 201. 그 대표 주소를 살아 있는 서비스·연결이 이미 쓰면 새 suffix 로 3번까지 다시 시도하고, 그래도 겹치면 409 `PUBLIC_HOST_CONFLICT`. `private` 는 GitHub 가 알려 준 값이다.

저장소에 접근하지 못하면(없거나, 비공개인데 GitHub App 이 설치되지 않음 — GitHub 가 둘을 같은 404 로 주어 구분하지 않는다):

```json
{
  "success": false,
  "code": "REPO_NOT_ACCESSIBLE",
  "message": "repository not found or not accessible",
  "details": [
    {"field": "installUrl", "reason": "INSTALL_GITHUB_APP", "value": "https://github.com/apps/echo-deploy/installations/new"}
  ]
}
```

`details[].value` 는 안내 값이 있을 때만 싣는 칸이다. 지금 값을 싣는 곳은 `installUrl`(App 설치 주소), 이전 계획(API 서버 8)의 `planId`·`deployRequestId`(가리키는 계획·배포 요청 id), `lastSyncError`(마지막 예비 따라가기 실패 코드)뿐이다. 비공개 저장소라면 이 주소로 App 을 설치한 뒤 다시 등록한다.

가짜 소스 저장소 규칙(`SOURCE_CLIENT_MODE=fake`): owner 가 `private-` 로 시작하면 비공개 + 설치 있음, `denied-`·`missing-` 이면 404 `REPO_NOT_ACCESSIBLE`, `flaky-` 면 502 `SOURCE_PROVIDER_UNAVAILABLE`.

### 목록 `GET /api/v1/orgs/{orgId}/services`

살아 있는 서비스(`DELETING` 포함, 소프트 삭제 제외)를 `createdAt` 오름차순(같으면 `id`)으로 준다. 항목은 `ServiceSummary`(연결 정보 없음).

### 단건 `GET /api/v1/orgs/{orgId}/services/{id}`

`ServiceDetail` = `ServiceSummary` + `environments[]`(이 서비스가 올라간 연결, 만든 순서). `DELETING` 서비스도 조회된다. 소프트 삭제가 끝나면 404.

### 수정 `PATCH /api/v1/orgs/{orgId}/services/{id}`

- 바꿀 수 있는 칸: `description`, `repoUrl`(바뀌면 접근을 다시 확인하고 `private` 를 갱신), `healthCheckPath`, `healthCheckTimeoutSeconds`, `containerPort`. 들어온 칸만 바꾼다.
- `name` 이 지금과 다르면 422 `SERVICE_NAME_IMMUTABLE`, 같으면 무시한다.
- `null` 은 `description` 만 받는다. 다른 칸의 `null` 은 422(`reason = NULL_NOT_ALLOWED`).
- `DELETING` 이면 409 `SERVICE_DELETING`.
- 바뀐 값은 **다음 배포 요청부터** 쓴다. 이미 접수된 요청은 접수 때 사본(`repoUrl`·설정)을 쓴다.

### 삭제 `DELETE /api/v1/orgs/{orgId}/services/{id}`

1. 서비스를 `DELETING` 으로 바꾸고 대기(`QUEUED`) 요청을 모두 `CANCELLED`(`cancelReason = SERVICE_DELETED`)로 취소한 뒤 내리기 할 일을 만든다 → **202** + `ServiceDetail`.
2. 일꾼이 진행 중 요청(`BUILDING`·`DEPLOYING`·`VERIFYING`)이 끝나기를 기다린 뒤 연결마다 클러스터에서 내리고, 내린 연결은 바로 소프트 삭제한다. 진행 중이던 요청의 성공은 예비 따라가기를 만들지 않는다.
3. 모든 연결이 내려가면 서비스를 소프트 삭제한다. 이후 단건·수정·삭제·배포 요청은 404 이고 같은 이름으로 다시 등록할 수 있다. 배포 기록·배포 요청 이력은 남는다.
4. 한 연결이라도 내리기에 실패하면 서비스는 `DELETING` 에 남고 `deleteError`(`TEARDOWN_FAILED`·`TEARDOWN_TIMEOUT`·`WORKER_LOST`)와 그 연결의 `teardownError` 가 보인다. **다시 DELETE** 를 보내면 남은 연결만 다시 내린다(새 멱등 키).

`DELETING` 중에는 단건·목록 조회만 되고, PATCH·배포 요청은 409 `SERVICE_DELETING` 이다.

이전 계획이 실행 중(`APPROVED`·`RUNNING`·`ROLLING_BACK`, API 서버 8)이면 삭제는 409 `MIGRATION_IN_PROGRESS`(`details = [{field: "planId", reason: "MIGRATION_IN_PROGRESS", value: <계획 id>}]`)다. 승인 대기 계획은 막지 않는다([migration-plans.md](migration-plans.md)).

## 응답 스키마

`ServiceSummary` — 모든 칸이 항상 있다.

| 칸 | 타입 | 설명 |
|---|---|---|
| `id` | uuid | |
| `name` | string | |
| `description` | string \| null | |
| `repoUrl` | string | 정규형 |
| `private` | boolean | GitHub 응답의 비공개 여부 |
| `healthCheckPath`·`healthCheckTimeoutSeconds`·`containerPort` | string·int·int | 다음 배포 요청에 쓸 값 |
| `url` | string | 대표 주소 `https://{name}-{suffix}.{PUBLIC_BASE_DOMAIN}`(suffix 는 무작위 8자, 바뀌지 않음) |
| `status` | `ACTIVE`·`DELETING` | |
| `deleteError` | 코드 \| null | `DELETING` 일 때만 값 |
| `simulated` | boolean | 가짜 소스 저장소로 등록했는지 |
| `createdAt`·`updatedAt` | string | |

`ServiceDetail.environments[]`

| 칸 | 타입 | 설명 |
|---|---|---|
| `serviceEnvironmentId` | uuid | 연결 id |
| `environment` | `{id, name, kind}` | |
| `role` | `SERVING`·`STANDBY`·`NONE` | 저장하지 않고 계산. `SERVING`: `trafficWeight > 0`. `STANDBY`: 0 이고 현재 배포 있음(재난 때 넘어갈 예비). `NONE`: 현재 배포 없음 |
| `trafficWeight` | int | 앞단 프록시에 마지막으로 적용한 비율(0~100). 이번에는 가짜 대표 주소 라우터가 적용한 값 |
| `url` | string | 환경별 주소 `https://{service}-{environment}-{서비스의 suffix}.{PUBLIC_BASE_DOMAIN}`(연결을 만들 때 정해 저장, 환경 이름이 바뀌어도 그대로) |
| `currentDeployment` | `{id, commitSha, imageDigest, finishedAt}` \| null | 지금 돌고 있는 배포 |
| `versionStatus` | `IN_SYNC`·`OUT_OF_SYNC` \| null | `STANDBY` 만. 기준 연결(비율이 가장 큰 연결, 같으면 `traffic_applied_at` 이 늦은 쪽, 그다음 id)의 현재 이미지 digest 와 같은가. 서비스 중 연결이 없으면 null |
| `lastSyncError` | 코드 \| null | 이 연결의 가장 최근 예비 따라가기(`STANDBY_SYNC`) 요청이 실패했으면 그 `errorCode` |
| `activeRequestId`·`queuedRequestId` | uuid \| null | 실행 중·대기 중 배포 요청(예비가 따라가는 중이면 여기 `STANDBY_SYNC` 요청이 보인다) |
| `lastSyncSeconds` | number \| null | 이 연결의 마지막 **성공** 예비 따라가기가 원본 요청이 끝난 뒤 끝나기까지 걸린 초(소수 1자리). 없으면 null(API 서버 8) |
| `simulated` | boolean \| null | 현재 배포를 만든 요청의 값. 현재 배포가 없으면 null |
| `teardownError` | 코드 \| null | 이 연결 내리기 실패 이유 |

`ServiceDetail.activeMigrationPlanId`(uuid \| null): 이 서비스의 끝나지 않은 이전 계획 id(API 서버 8). 승인 대기(`PENDING_APPROVAL`)는 만료 전일 때만 센다(조회는 저장된 상태를 바꾸지 않는다).

예 (aws 서비스 중 + onprem 예비):

```json
{
  "success": true,
  "data": {
    "id": "6f1d…",
    "name": "shop-api",
    "description": null,
    "repoUrl": "https://github.com/team-echo/shop-api",
    "private": false,
    "healthCheckPath": "/healthz",
    "healthCheckTimeoutSeconds": 300,
    "containerPort": 8080,
    "url": "https://shop-api-a1b2c3d4.apps.echo.test",
    "status": "ACTIVE",
    "deleteError": null,
    "simulated": true,
    "createdAt": "2026-11-02T03:00:00Z",
    "updatedAt": "2026-11-02T03:00:00Z",
    "environments": [
      {
        "serviceEnvironmentId": "a1…",
        "environment": {"id": "e1…", "name": "aws-seoul", "kind": "aws"},
        "role": "SERVING",
        "trafficWeight": 100,
        "url": "https://shop-api-aws-seoul-a1b2c3d4.apps.echo.test",
        "currentDeployment": {"id": "d1…", "commitSha": "9534efe0…", "imageDigest": "sha256:…", "finishedAt": "2026-11-02T03:00:12Z"},
        "versionStatus": null,
        "lastSyncError": null,
        "activeRequestId": null,
        "queuedRequestId": null,
        "simulated": true,
        "teardownError": null,
        "lastSyncSeconds": null
      },
      {
        "serviceEnvironmentId": "b2…",
        "environment": {"id": "e2…", "name": "onprem-k3s", "kind": "onprem"},
        "role": "STANDBY",
        "trafficWeight": 0,
        "url": "https://shop-api-onprem-k3s-a1b2c3d4.apps.echo.test",
        "currentDeployment": {"id": "d2…", "commitSha": "9534efe0…", "imageDigest": "sha256:…", "finishedAt": "2026-11-02T03:01:02Z"},
        "versionStatus": "IN_SYNC",
        "lastSyncError": null,
        "activeRequestId": null,
        "queuedRequestId": null,
        "simulated": true,
        "teardownError": null,
        "lastSyncSeconds": 4.2
      }
    ],
    "activeMigrationPlanId": null
  }
}
```

## HTTP 오류

| 상황 | HTTP | code |
|---|---|---|
| 요청 스키마 위반(이름·repoUrl 형식, 범위, null, 모르는 칸) | 422 | `VALIDATION_ERROR` + `details[{field, reason}]` |
| 이름 변경 | 422 | `SERVICE_NAME_IMMUTABLE` |
| 저장소 없음·비공개인데 App 설치 없음 | 404 | `REPO_NOT_ACCESSIBLE` + `details[{field: "installUrl", reason, value}]` |
| GitHub 한도 초과(403·429 + 한도 헤더)·5xx·시간 초과 | 502 | `SOURCE_PROVIDER_UNAVAILABLE` |
| 같은 조직의 살아 있는 서비스와 이름 중복 | 409 | `SERVICE_NAME_CONFLICT` |
| 대표 주소가 새 suffix 로 4번(첫 시도 + 3번) 모두 이미 쓰임 | 409 | `PUBLIC_HOST_CONFLICT` |
| 삭제 중 수정 | 409 | `SERVICE_DELETING` |
| 이전 계획 실행 중 삭제 | 409 | `MIGRATION_IN_PROGRESS` + `details[{field: "planId", reason, value}]` |
| 없거나 삭제된 서비스, 다른 조직의 서비스 | 404 | `SERVICE_NOT_FOUND` |
| 로그인 안 함 · 조직 멤버 아님 · 역할 모자람 | 401 · 404 · 403 | `UNAUTHENTICATED` · `ORGANIZATION_NOT_FOUND` · `ROLE_FORBIDDEN` 등([auth.md](auth.md)) |
