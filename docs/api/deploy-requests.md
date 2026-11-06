# 배포 요청 API — 원터치 배포 (API 서버 3) · 이력·되돌리기·로그 (API 서버 4)

등록한 서비스의 Git 브랜치나 커밋을 **요청 한 번**으로 빌드해 고른 환경 1곳에 배포한다. 헬스체크를 통과해야 새 버전으로 바꾸고, 대표 주소와 환경별 주소를 돌려준다. 근거: SPEC [specs/api-3-one-touch-deploy.md](../../specs/api-3-one-touch-deploy.md), 원장 F1. 서비스 등록은 [services.md](services.md).

API 서버 4 는 같은 요청 목록을 **배포 이력**으로 쓰고(현재 버전·되돌리기 가능 여부·필터), 이력에서 고른 이전 성공 버전으로 **되돌리기**(`trigger = ROLLBACK` 요청)와 **세 가지 로그**(배포 작업·빌드·실행) 조회를 더한다. 근거: SPEC [specs/api-4-history-rollback-logs.md](../../specs/api-4-history-rollback-logs.md). 배포 실패 진단(컨테이너 로그·종료 코드·사유, `failure` 칸, "실행 중인 버전 없음")은 API 서버 4-1 — SPEC [specs/api-4-1-failed-container-logs.md](../../specs/api-4-1-failed-container-logs.md).

- 공통 봉투·camelCase·null 규칙·`X-Request-ID` 는 [environments.md](environments.md#공통-규칙)와 같다. 경로는 `/api/v1/orgs/{orgId}` 아래이고 그 조직 멤버만 본다. 조회는 뷰어(에이전트 토큰 가능), 접수는 **개발자 이상의 사람**이다(에이전트 토큰은 403 `AGENT_READ_ONLY`, [auth.md](auth.md)).
- **접수만 동기다.** API 는 요청을 `QUEUED` 로 저장하고 202 를 준다. 빌드·배포·헬스체크는 별도 일꾼(`python -m app.worker`)이 진행한다. **일꾼이 없으면 요청은 `QUEUED` 에 남는다.** 진행 상황은 단건 조회로 폴링한다(실시간 푸시 없음).
- **기본 실행은 가짜다(`simulated: true`).** 빌드·배포·되돌리기·내리기는 `EXECUTOR_MODE=fake`(운영 기본값)면 가짜 실행기가 흉내 낸다. `EXECUTOR_MODE=real` 이면 GitHub Actions 빌드 + 쿠버네티스 직접 반영(API 서버 3-2, [executors.md](../executors.md))이고 기록은 `simulated: false` 다. 흐름·상태·DB 기록·계약은 실제와 같다. 헬스체크는 기본 가짜(늘 통과)이고 `HEALTH_CHECKER_MODE=http` 로 실제 HTTP 확인을 켤 수 있다. `simulated` 는 요청 행에 저장한 값이다.

## 엔드포인트

| 메서드 · 경로 | 요청 | 성공 | 실패 | 최소 역할 |
|---|---|---|---|---|
| `POST /api/v1/orgs/{orgId}/services/{id}/deploy-requests` | `CreateDeployRequest` | 202 `DeployRequestDetail` | 404 `SERVICE_NOT_FOUND`·`ENVIRONMENT_NOT_FOUND`·`REPO_NOT_ACCESSIBLE`, 409 `SERVICE_DELETING`·`ENVIRONMENT_UNHEALTHY`·`PUBLIC_HOST_CONFLICT`·`MIGRATION_IN_PROGRESS`, 422 `VALIDATION_ERROR`·`REF_NOT_FOUND`·`SIMULATE_NOT_ALLOWED`, 502 `SOURCE_PROVIDER_UNAVAILABLE` | 개발자 |
| `GET /api/v1/orgs/{orgId}/services/{id}/deploy-requests?page&size&environmentId&trigger&status` | — | 200 `Page[DeployRequestSummary]`(최신순) | 404 `SERVICE_NOT_FOUND`, 422 `VALIDATION_ERROR` | 뷰어·에이전트 |
| `GET /api/v1/orgs/{orgId}/deploy-requests/{id}` | — | 200 `DeployRequestDetail` | 404 `DEPLOY_REQUEST_NOT_FOUND` | 뷰어·에이전트 |
| `POST /api/v1/orgs/{orgId}/deploy-requests/{id}/rollback` | `RollbackRequest`(바디 선택) | 202 `DeployRequestDetail` | 403 `AGENT_READ_ONLY`·`ROLE_FORBIDDEN`, 404 `DEPLOY_REQUEST_NOT_FOUND`·`SERVICE_NOT_FOUND`·`ENVIRONMENT_NOT_FOUND`, 409 `NOT_ROLLBACKABLE`·`ALREADY_CURRENT`·`SERVICE_DELETING`·`ENVIRONMENT_UNHEALTHY`·`MIGRATION_IN_PROGRESS`, 422 `VALIDATION_ERROR`·`SIMULATE_NOT_ALLOWED` | 개발자 |
| `GET /api/v1/orgs/{orgId}/deploy-requests/{id}/logs?source&cursor&limit` | — | 200 `LogPage` | 404 `DEPLOY_REQUEST_NOT_FOUND`, 422 `VALIDATION_ERROR` | 뷰어·에이전트 |

코드 클래스: `CreateDeployRequest`, `RollbackRequest`, `DeployRequestSummary` = `DeployRequestSummaryResponse`, `DeployRequestDetail` = `DeployRequestDetailResponse`, `LogPage` = `LogPageResponse`(`app/schemas/deploy_request.py`).

### 접수 `POST /api/v1/orgs/{orgId}/services/{id}/deploy-requests`

```json
{"environmentId": "b87d8364-7724-4cd1-a59d-306860497d18", "branch": "main"}
```

| 칸 | 규칙 |
|---|---|
| `environmentId` | 필수. 환경 하나(요청 1번 = 환경 1개) |
| `branch` · `commitSha` | **정확히 하나.** 둘 다·둘 다 없음은 422(`reason = EXACTLY_ONE_OF_BRANCH_OR_COMMIT_SHA`). `branch` 는 1~255자, 공백·제어 문자·`~^:?*[\`·`..`·`@{`·끝 `/`·끝 `.lock` 금지. `commitSha` 는 7~40자 소문자 16진수 |
| `simulate` | 선택. `{"failAt": "BUILDING" \| "DEPLOYING" \| "VERIFYING"}` — 그 단계에서 이 요청을 일부러 실패시킨다. 가짜 실행기 모드에서만 받는다(아니면 422 `SIMULATE_NOT_ALLOWED`) |
| `standbyEnvironmentIds` | 선택(이슈 #99, "GCP도 같이 띄우기"). 환경 id 0~5개. **주 환경이 성공한 뒤** 각 환경에 연결을 만들고(없으면) 기존 예비 따라가기와 같은 `STANDBY_SYNC` 를 시작한다 — 아래 "예비 환경 함께 띄우기". 주 환경 `environmentId` 와 같으면 422 `VALIDATION_ERROR`(`reason = SAME_AS_ENVIRONMENT`), 중복이면 422(`reason = DUPLICATE`), 같은 조직에 없거나 지운 환경이면 404 `ENVIRONMENT_NOT_FOUND`, 마지막 접속 확인 `FAILED` 면 409 `ENVIRONMENT_UNHEALTHY`(모두 접수 거절, 아무것도 만들지 않는다) |

접수 순서:

1. 서비스(없음 404, `DELETING` 409)·환경(없음·삭제 404, 마지막 접속 확인 `FAILED` 409 `ENVIRONMENT_UNHEALTHY`)을 확인한다.
2. 브랜치·커밋을 **이 순간 40자 커밋 SHA 로 고정**한다(없으면 422 `REF_NOT_FOUND`). 저장소 주소와 서비스 설정도 이 순간 사본을 저장한다 — 뒤에 서비스가 바뀌어도 이 요청은 이 값을 쓴다.
3. 그 환경과 첫 요청이고 연결이 없으면 연결을 만든다. 첫 배포 전 환경 변수 넣기가 먼저 만든 배포 없는 연결이 있으면 그것을 그대로 쓴다(API 서버 10-4, [variables.md](variables.md)). 만들 때(`trafficWeight = 0`, 주소는 환경 config 에 `ingressDomain`(클라우드 환경의 하위 도메인)이 있으면 `https://{service}-{서비스의 suffix}.{ingressDomain}`, 없으면 `https://{service}-{environment}-{서비스의 suffix}.{PUBLIC_BASE_DOMAIN}`(온프레·예전 환경, API 서버 3-2), API 서버 3-1). 이 주소를 쓰는 살아 있는 연결이 이미 있으면 409 `PUBLIC_HOST_CONFLICT`(환경 이름을 바꾼 뒤 옛 이름으로 새 환경을 만든 경우, 레이블 63자 자르기로 같은 주소가 된 경우, 두 환경이 같은 `ingressDomain` 을 쓰는 경우). 이미 만든 연결의 주소는 환경에 `ingressDomain` 을 나중에 더해도 바뀌지 않는다. 레이블이 63자를 넘으면 `{service}-{environment}` 를 54자로 자르고 끝의 `-` 를 뗀 뒤 `-{suffix}` 를 붙인다. 서비스 이름이 53자 이상이면 환경별 주소가 대표 주소와 같아지고 그 서비스는 환경 하나에만 배포할 수 있다.
4. **같은 연결에는 실행 중 1개 + 대기 1개까지만 둔다.** 대기 요청이 있으면 그 요청을 `CANCELLED`(`cancelReason = SUPERSEDED`, `supersededById = 새 요청`)로 밀어내고 새 요청이 대기한다. 실행 중 요청은 끝까지 진행되고, 대기 요청은 그 뒤에 시작한다. 사용자가 요청을 직접 취소하는 API 는 없다.
5. 202 + `DeployRequestDetail`(`status = QUEUED`).

### 진행 (일꾼)

```mermaid
stateDiagram-v2
    [*] --> QUEUED
    QUEUED --> CANCELLED : SUPERSEDED · SERVICE_DELETED · BASE_CHANGED(되돌리기)
    QUEUED --> BUILDING
    BUILDING --> DEPLOYING : 빌드 성공(같은 커밋 빌드가 있으면 재사용)
    DEPLOYING --> VERIFYING : 배포 반영
    VERIFYING --> SUCCEEDED : 헬스체크 2xx
    BUILDING --> FAILED
    DEPLOYING --> FAILED
    VERIFYING --> FAILED
```

- **빌드**: 같은 서비스·저장소·커밋의 빌드가 있으면 다시 빌드하지 않는다(`build.reused = true`). 빌드 결과는 이미지 주소 + `sha256` digest 이고, 배포는 digest 로만 한다. 비공개 저장소는 빌드 직전에 GitHub App 설치 토큰(1시간)을 새로 받아 메모리로만 넘긴다. 제한 시간은 빌드 시작 기준 `BUILD_TIMEOUT_SECONDS`(900).
- **배포**: 빌드가 성공하면 배포 기록(`deploymentId`)을 만들고 배포 실행기가 반영한다. 제한 시간은 `DEPLOYING` 시작 기준 `DEPLOY_TIMEOUT_SECONDS`(300).
- **헬스체크**: 일꾼이 실행될 때마다 **환경별 주소 + `healthCheckPath`** 를 한 번 GET 한다(요청 하나 5초, 리다이렉트 따르지 않음). 2xx 면 통과, 아니면 `HEALTH_CHECK_INTERVAL_SECONDS`(5) 뒤 다시 확인한다. `VERIFYING` 시작 + `healthCheckTimeoutSeconds`(접수 때 사본)를 넘기면 마지막 결과로 끝낸다: 2xx 가 아닌 응답이면 `HEALTH_CHECK_FAILED`, 연결 실패면 `PUBLIC_URL_UNREACHABLE`, 한 번도 확인하지 못했으면 `HEALTH_CHECK_TIMEOUT`. 대표 주소는 성공 처리 뒤에야 이 환경을 가리키므로 이 단계에서 확인하지 않는다.
- **성공 처리**: 연결의 현재 배포를 새 배포 기록으로 바꾼다. 이 서비스에 서비스 중(`trafficWeight > 0`) 연결이 없으면 대표 주소 라우터가 이 연결로 주소를 보내고, "적용했다"고 답하면 `trafficWeight = 100` 이 된다(**서비스 중**). 이미 서비스 중인 연결이 있으면 이 연결은 **예비**(`trafficWeight = 0`)로 남는다. 트래픽 전환·이전은 API 서버 8 이 한다.
- **예비 환경 함께 띄우기**(이슈 #99, SPEC [api-99-vars-copy-standby.md](../../specs/api-99-vars-copy-standby.md)): 접수 때 받은 `standbyEnvironmentIds` 는 요청의 설정 사본(`config_snapshot.standbyEnvironmentIds`)에만 적는다(표 칸 추가 없음, 배포 기록·따라가기 요청에는 옮기지 않아 되돌리기가 다시 띄우지 않는다). 수동 요청이 성공하고 이 연결이 **서비스 중**이면 같은 성공 처리 트랜잭션에서 환경마다: 이미 따라가는 예비 연결이면 아래 자동 따라가기가 한 번만 넣고, 연결이 없으면 배포 없는 연결을 만든 뒤(환경을 `FOR SHARE` 로 잡고 살아 있는지 다시 확인, 주소 규칙은 접수와 같다), 배포 없는 연결이면 그대로 `STANDBY_SYNC` 를 넣는다(같은 빌드·설정 사본, 앱 환경 변수는 **그 예비 연결의** 값 — 첫 배포 전에 넣은 변수가 있으면 그것). 따라가기가 성공하면 그 연결은 **예비**(`role = STANDBY`)다. 띄우지 못하면 그 환경만 원본 요청에 `STANDBY_SKIPPED`(WARN) 줄을 쓴다: 주 환경이 서비스 중이 아님(예비 환경에 직접 배포)·서비스 삭제 중·실행 중 이전 계획이 있음(하나도 띄우지 않음)·그 사이 환경 삭제·주소 충돌. **주 환경이 실패·취소되면 연결도 요청도 만들지 않는다.** 결과 요청 id 는 단건 응답 `standbyDeployRequestIds`(가칭)에 나온다.
- **예비 자동 따라가기**: 서비스 중 연결의 수동 요청·되돌리기 요청이 성공하면, 현재 배포가 있는 예비 연결마다 `trigger = STANDBY_SYNC` 요청이 생긴다(`sourceRequestId` = 원본, 같은 커밋·같은 빌드 — 재빌드 없음, 설정 사본은 환경별 주소만 다름). 이 요청도 같은 겹침 규칙을 따라 예비 연결의 대기 요청을 밀어낸다. 예비 연결 요청의 성공·실패는 서비스 중 연결에 영향이 없고, 예비 요청은 다시 따라가기를 만들지 않는다. 서비스가 `DELETING` 이면 만들지 않는다. 예비 연결에 사용자가 다른 커밋을 직접 배포하는 것은 막지 않는다(그때는 서비스 상세에서 `OUT_OF_SYNC` 로 보인다).
- **실패와 되돌리기**: 배포 기록이 생긴 뒤의 모든 실패(배포 실패·시간 초과, 헬스 실패 3종, 일꾼 상실)는 되돌리기를 불러 직전 버전을 유지하고 결과를 `revertStatus`(`SUCCEEDED`·`FAILED`)에 적는다. 되돌리기가 실패해도 `errorCode` 는 바꾸지 않는다. 연결의 현재 배포·비율은 그대로다.
- **일꾼 복구**: 일꾼이 죽어도 임대(30초)가 끝나면 다른 일꾼이 같은 단계부터 이어 간다. 외부 작업은 멱등 키로 두 번 만들지 않는다([executors.md](../executors.md)). 같은 요청에서 임대를 3번 잃으면 `WORKER_LOST` 로 끝난다.

### 목록(이력) `GET /api/v1/orgs/{orgId}/services/{id}/deploy-requests`

- 쿼리: `page`(0부터), `size`(1~100, 기본 20), `environmentId`(선택, 그 환경 요청만), `trigger`(선택, 반복 가능: `MANUAL`·`STANDBY_SYNC`·`ROLLBACK`), `status`(선택, 반복 가능: 7개 상태). 모르는 값은 422 `VALIDATION_ERROR`. 예: `?environmentId=…&status=SUCCEEDED&status=FAILED`.
- `createdAt` 내림차순(최신 먼저), 같으면 `id` 내림차순. 성공·실패·취소·대기 요청이 모두 나온다. `total` 은 필터를 적용한 개수. 삭제가 끝난 서비스는 404(그 요청들은 단건으로 조회).
- 각 항목의 `isCurrent`·`rollbackable` 은 한 페이지의 연결·서비스를 한 번에 읽어 계산한다(요청 수와 상관없이 쿼리 수가 같다).

### 단건 `GET /api/v1/orgs/{orgId}/deploy-requests/{id}`

이력이므로 서비스가 삭제된 뒤에도 조회된다(이때 `rollbackable = false`). 배포 요청 행은 지우지 않는다(DB 트리거가 DELETE 를 막는다).

### 되돌리기 `POST /api/v1/orgs/{orgId}/deploy-requests/{id}/rollback`

이력에서 고른 **성공한 요청 하나**(`{id}`)가 만든 배포 기록의 **이미지(digest)와 설정 사본을 함께** 그 연결(같은 서비스 × 같은 환경)에 다시 올린다. 새 빌드를 만들지 않는다.

```json
{}
```

```json
{"simulate": {"failAt": "VERIFYING"}}
```

| 칸 | 규칙 |
|---|---|
| (바디) | 비어도 된다(바디 없이 보내도 된다). `simulate` 밖의 칸은 422 |
| `simulate` | 선택. `{"failAt": "DEPLOYING" \| "VERIFYING"}`. 되돌리기에는 빌드 단계가 없어 `BUILDING` 은 422 `VALIDATION_ERROR`. 가짜 실행기 모드에서만 받는다(아니면 422 `SIMULATE_NOT_ALLOWED`) |

접수(거절 순서: 권한 → 바디 → 대상 요청 → 서비스·환경 → 상태):

1. 권한: **개발자 이상의 사람**(배포 요청 접수와 같은 검사기). 뷰어 403 `ROLE_FORBIDDEN`, 에이전트 토큰 403 `AGENT_READ_ONLY`(둘 다 거부 기록).
2. 대상 요청이 없거나 다른 조직이면 404 `DEPLOY_REQUEST_NOT_FOUND`.
3. 서비스가 삭제를 마쳤으면 404 `SERVICE_NOT_FOUND`, `DELETING` 이면 409 `SERVICE_DELETING`. 환경이 삭제됐으면 404 `ENVIRONMENT_NOT_FOUND`, 마지막 접속 확인 `FAILED` 면 409 `ENVIRONMENT_UNHEALTHY`.
4. 대상이 `SUCCEEDED` 가 아니거나 배포 기록이 없으면(또는 연결이 내려갔으면) 409 `NOT_ROLLBACKABLE`. 대상의 배포 기록이 이미 그 연결의 현재 배포면 409 `ALREADY_CURRENT`. 판정은 **배포 기록 id** 로 한다 — 현재와 이미지·설정이 같은 다른 행(예: 되돌리기의 원본)은 되돌릴 수 있고, 그때는 같은 이미지를 다시 반영한다.
5. 같은 연결의 대기 요청은 `SUPERSEDED` 로 밀어내고 새 요청을 넣는다: `trigger = ROLLBACK`, `rollbackOfRequestId = {id}`, `ref = {type: COMMIT, value: 대상 커밋}`, 커밋·저장소 = 대상 값, `build` = 대상의 빌드(`reused = true`), 설정 사본 = 대상 배포 기록의 사본. 접수 순간 연결의 현재 배포를 함께 기억한다.
6. 202 + `DeployRequestDetail`(`status = QUEUED`, `trigger = ROLLBACK`).

진행(일꾼, 원터치 배포와 같은 흐름에 아래만 다르다):

- **시작 때 다시 확인**: 같은 연결에 실행 중 요청이 끝나 이 요청이 시작할 때, 연결의 현재 배포가 접수 때 기억한 값과 **다르면** `CANCELLED`(`cancelReason = BASE_CHANGED`)로 끝낸다. 그사이 다른 배포가 성공했다면 사용자가 보지 못한 새 버전을 되돌리기로 덮어쓰지 않는다. 같으면 시작한다.
- `BUILDING` 은 재사용한 빌드를 바로 지난다. 새 배포 기록의 `rollback_of_id` 는 대상의 배포 기록이고, 이미지·digest·설정 사본은 대상과 같다.
- 헬스체크·실패·되돌리기(실패한 새 버전을 거두는 것)·일꾼 상실·겹침 규칙은 원터치 배포와 같다. **배포·헬스체크에 실패하면 연결의 현재 배포와 `trafficWeight` 는 그대로**이고, 새 배포 기록은 `FAILED`, `revertStatus` 와 `errorCode` 가 남는다.
- **예비 따라가기**: 서비스 중 연결(`trafficWeight > 0`)의 되돌리기가 성공하면 현재 배포가 있는 예비 연결마다 `STANDBY_SYNC` 요청이 생긴다(`sourceRequestId` = 되돌리기 요청, 같은 빌드). 예비 연결을 직접 되돌리면 따라가기를 만들지 않는다. 서비스가 `DELETING` 이면 만들지 않는다.
- 대상 요청이 `STANDBY_SYNC` 이거나 이미 `ROLLBACK` 이어도 `SUCCEEDED` 면 대상이 될 수 있다.

### 로그 `GET /api/v1/orgs/{orgId}/deploy-requests/{id}/logs`

| 쿼리 | 규칙 |
|---|---|
| `source` | 필수. `DEPLOY_JOB`(배포 작업 로그) · `BUILD`(빌드 로그) · `RUNTIME`(실행 로그) |
| `cursor` | 선택. 앞 페이지의 `nextCursor` 를 그대로 넘긴다. `DEPLOY_JOB` 은 0 이상 정수 문자열, `BUILD`·`RUNTIME` 은 실행기가 정한 값. 잘못된 값은 422 `VALIDATION_ERROR`(`details: [{field: cursor, reason: INVALID_CURSOR}]`) |
| `limit` | 선택. 1~500, 기본 200 |

- 요청이 없거나 다른 조직이면 404 `DEPLOY_REQUEST_NOT_FOUND`. 서비스가 삭제된 뒤에도 읽는다.
- `DEPLOY_JOB`: 일꾼이 DB 에 저장한 줄(아래 "배포 작업 로그 줄")을 `seq` 순으로 준다. `nextCursor` = 돌려준 마지막 줄의 `seq`.
- `BUILD`: 그 요청이 쓰는 빌드(재사용 빌드 포함 — 되돌리기·예비 따라가기는 원본 빌드의 로그)를 빌드 실행기가 즉석으로 읽는다. 빌드가 아직 없거나 시작 전이면 빈 페이지.
- `RUNTIME`: 그 요청의 배포(배포 기록 + 배포 실행기 번호)의 실행 로그를 배포 실행기가 즉석으로 읽는다. 배포 요청 전이거나 연결을 내렸으면 빈 페이지.
- 실행기를 쓸 수 없으면(실제 실행기 미구현 모드) 또는 실행기의 `simulated` 가 기록과 다르면 빈 페이지(오류 아님).
- 모든 줄의 `message` 는 응답 직전에 알려진 비밀값 모양(GitHub 토큰, AWS 액세스 키, `Bearer` 토큰, `sk-` 키, 개인 키 블록)을 `***` 로 바꾼다. 실행기도 가려서 돌려줘야 한다([executors.md](../executors.md)).
- 실시간 스트리밍·검색·보존 기간은 없다(폴링).

```json
{
  "success": true,
  "data": {
    "source": "DEPLOY_JOB",
    "simulated": true,
    "items": [
      {"at": "2026-11-02T03:00:00Z", "level": "INFO", "message": "Deploy request accepted.", "code": "REQUEST_ACCEPTED"},
      {"at": "2026-11-02T03:00:01Z", "level": "INFO", "message": "Deploy request started.", "code": "STARTED"}
    ],
    "nextCursor": "2",
    "hasMore": true
  }
}
```

### 배포 작업 로그 줄

상태 전환과 **같은 트랜잭션**에서, 전환이 실제로 반영됐을 때만 한 줄씩 쓴다(일꾼이 다시 시작해도 줄이 겹치거나 빠지지 않는다). 문장은 코드별 고정 영어 문장이고 외부 응답 원문을 넣지 않는다. 요청당 500줄까지이며 500번째 자리에 `LOG_TRUNCATED` 를 쓰고 그 뒤 줄은 버린다(상태 진행은 계속).

| 코드 | 수준 | 단계(`stage`) | 언제 |
|---|---|---|---|
| `REQUEST_ACCEPTED` | INFO | QUEUED | 접수·되돌리기 접수·예비 따라가기 요청 생성 |
| `STARTED` | INFO | BUILDING | `QUEUED → BUILDING` |
| `BUILD_ATTACHED` · `BUILD_REUSED` | INFO | BUILDING | 새 빌드를 만들었다 · 있는 빌드를 쓴다(되돌리기·따라가기는 시작 직후) |
| `DEPLOYMENT_CREATED` | INFO | DEPLOYING | 배포 기록 생성(`BUILDING → DEPLOYING`) |
| `DEPLOY_REQUESTED` · `DEPLOY_APPLIED` | INFO | DEPLOYING | 배포 실행기 요청 · 반영(`DEPLOYING → VERIFYING`) |
| `ENV_VARS_APPLIED` | INFO | DEPLOYING | 앱 환경 변수 적용. 키 개수·이름만(값 없음). 변수가 있을 때만 `DEPLOY_REQUESTED` 와 `DEPLOY_APPLIED` 사이에 온다 |
| `HEALTH_PROBE_HTTP_ERROR` · `HEALTH_PROBE_UNREACHABLE` | WARN | VERIFYING | 헬스 결과 종류가 **바뀔 때만**(같은 실패가 이어지면 한 줄) |
| `HEALTH_PROBE_OK` · `SUCCEEDED` | INFO | VERIFYING · SUCCEEDED | 성공 처리 |
| `STANDBY_SYNC_REQUESTED` | INFO | SUCCEEDED | 원본 요청에, 예비 연결마다 한 줄 |
| `STANDBY_SKIPPED` | WARN | SUCCEEDED | 요청한 예비 환경(`standbyEnvironmentIds`)을 띄우지 못했다. 환경마다 한 줄, 문장 = `Requested standby environment <환경 id> was not started: <고정 사유>`(이슈 #99) |
| `CONTAINER_DIAGNOSTICS` | ERROR | FAILED | 실행기가 컨테이너 실패(재시작 반복·이미지 받기 실패)를 판정했을 때 `FAILED` 바로 앞에 한 줄. 문장 = `Container app: reason=CrashLoopBackOff, terminationReason=Error, exitCode=1, restartCount=3, logLines=7, truncated=false.`(모르는 값은 `-`). API 서버 4-1 |
| `CONTAINER_LOG` | INFO | FAILED | 위 줄 뒤, 컨테이너 로그 마지막 줄(최대 50줄, 한 줄 400자). 비밀값을 가린 문장. 재시작했으면 직전 컨테이너 로그 |
| `FAILED` | ERROR | FAILED | 문장 = 그 `errorCode` 의 `errorMessage`. 컨테이너 진단이 있으면 뒤에 원인 한 줄 ` Last error: …`(예 `ValidationError: database_url`, 로그가 없으면 `OOMKilled (exit code 137)`) |
| `REVERT_SUCCEEDED` · `REVERT_FAILED` | INFO · ERROR | REVERT | 실패한 새 버전을 거둔 결과. `REVERT_SUCCEEDED` 문장은 "이전 버전 유지" |
| `REVERT_SUCCEEDED_NO_PREVIOUS` | INFO | REVERT | 되돌릴 이전 배포가 없었다(첫 배포 실패). 실패한 버전만 거뒀고 **실행 중인 버전 없음**(API 서버 4-1) |
| `CANCELLED_SUPERSEDED` · `CANCELLED_SERVICE_DELETED` · `CANCELLED_BASE_CHANGED` | INFO | CANCELLED | 취소 3종(서비스 삭제 API·내리기 일꾼·일꾼 시작 경로 포함) |
| `LOG_TRUNCATED` | INFO | (대신한 줄의 단계) | 500줄 상한 |

정상 순서: 새 빌드 수동 = `REQUEST_ACCEPTED, STARTED, BUILD_ATTACHED, DEPLOYMENT_CREATED, DEPLOY_REQUESTED, DEPLOY_APPLIED, HEALTH_PROBE_OK, SUCCEEDED`. 재사용 수동·되돌리기·예비 따라가기는 `BUILD_ATTACHED` 대신 `BUILD_REUSED`. 빌드 자체의 시작·성공·실패는 이 로그에 쓰지 않는다(공유 빌드를 여러 요청이 기다리므로) — 빌드 진행은 `source = BUILD` 로 본다. `simulate.failAt = VERIFYING` 은 헬스 줄 없이 `FAILED`·`REVERT_*` 로 끝난다. `simulate.failAt = DEPLOYING`·가짜 실행기 배포 실패는 실제와 같은 모양의 가짜 진단(`CONTAINER_DIAGNOSTICS`·`CONTAINER_LOG` 7줄, `ValidationError: database_url`)을 `FAILED` 앞에 쓴다(시연·시험용, `simulated = true`).

## 응답 스키마

`DeployRequestSummary`(목록 항목) — 모든 칸이 항상 있다.

| 칸 | 타입 | 설명 |
|---|---|---|
| `id` | uuid | 접수 번호 |
| `serviceId` | uuid | |
| `environment` | `{id, name, kind}` | |
| `serviceEnvironmentId` | uuid | 연결 id |
| `trigger` | `MANUAL`·`STANDBY_SYNC`·`ROLLBACK` | |
| `sourceRequestId` | uuid \| null | 따라가기의 원본 요청 |
| `ref` | `{type: BRANCH \| COMMIT, value}` | 사용자가 준 값 |
| `commitSha` | string | 접수 때 고정한 40자 SHA |
| `status` | `QUEUED`·`BUILDING`·`DEPLOYING`·`VERIFYING`·`SUCCEEDED`·`FAILED`·`CANCELLED` | |
| `cancelReason` | `SUPERSEDED`·`SERVICE_DELETED`·`BASE_CHANGED` \| null | `CANCELLED` 일 때만. `BASE_CHANGED` = 되돌리기 시작 때 현재 배포가 접수 때와 달랐다 |
| `errorCode` | 코드 \| null | `FAILED` 일 때만 |
| `revertStatus` | `SUCCEEDED`·`FAILED` \| null | 되돌리기가 필요 없었으면 null |
| `stageTimes` | `{queuedAt, buildingAt, deployingAt, verifyingAt, finishedAt}` | 지나지 않은 단계는 null |
| `simulated` | boolean | 가짜 실행기로 처리한 요청인지 |
| `isCurrent` | boolean | 이 요청이 만든 배포 기록이 지금 그 연결의 현재 배포인가(API 서버 4) |
| `rollbackable` | boolean | 이 요청을 골라 되돌릴 수 있는가: `SUCCEEDED` · 배포 기록 있음 · 현재 아님 · 연결 살아 있음 · 서비스 `ACTIVE`. 환경 접속 상태는 보지 않는다(접수에서 409) |
| `rollbackOfRequestId` | uuid \| null | 되돌리기 요청이면 대상 요청 id |

`DeployRequestDetail` = 위 + 아래.

| 칸 | 타입 | 설명 |
|---|---|---|
| `repoUrl` | string | 접수 때 고정한 저장소 |
| `supersededById` | uuid \| null | 이 요청을 밀어낸 요청 |
| `errorMessage` | string \| null | 오류 코드별 **고정 문장**(외부 응답 원문은 싣지 않는다) |
| `failure` | `Failure` \| null | `FAILED` 일 때만. 원인과 컨테이너 진단(API 서버 4-1, 아래). 다른 상태는 null. OpenAPI 에서는 선택 칸(required 아님)이지만 응답에는 늘 실린다 |
| `diagnosis` | `{runId, status}` \| null | 이 요청의 AI 진단 실행(이슈 #98, `POST /deploy-requests/{id}/diagnosis`). 진단 전이면 null. 결과는 `GET /prechecks/{runId}` — [deploy-diagnosis.md](deploy-diagnosis.md). `failure` 와 같이 선택 칸이고 응답에는 늘 실린다 |
| `build` | `{id, status, imageRepository, imageDigest, reused}` \| null | 쓰는 빌드. `reused = true` 면 이 요청이 새로 만든 빌드가 아니다(되돌리기·예비 따라가기는 늘 true) |
| `deploymentId` | uuid \| null | 빌드 성공 뒤 만든 배포 기록 |
| `urls` | `{main, environment}` | 대표 주소, 환경별 주소(접수 때 사본) |
| `standbyEnvironmentIds` | uuid[] | (이슈 #99, 칸 이름 가칭) 접수 때 받은 예비 환경 id. 없으면 빈 목록. OpenAPI 에서는 선택 칸 |
| `standbyDeployRequestIds` | uuid[] | (이슈 #99, 칸 이름 가칭) 이 요청이 성공해 만든 예비 따라가기(`STANDBY_SYNC`) 요청 id — 요청한 예비와 기존 예비 따라가기 모두. 접수 응답(202)에서는 아직 성공 전이라 늘 빈 목록이고, 성공 뒤 단건 조회에 나온다. OpenAPI 에서는 선택 칸 |

`Failure` — 실패 원인. **에러 수정 에이전트(echo-ai-agent #22)가 이 칸을 그대로 입력으로 읽는다.** 배포 작업 로그의 `CONTAINER_DIAGNOSTICS`·`CONTAINER_LOG`·`FAILED`·`REVERT_*` 줄로 만든다(저장 칸 추가 없음).

| 칸 | 타입 | 설명 |
|---|---|---|
| `code` | 코드 | `errorCode` 와 같다 |
| `message` | string | `errorMessage` + 원인 한 줄(` Last error: ValidationError: database_url`). 진단이 없으면 `errorMessage` 와 같다 |
| `container` | string \| null | 실패한 컨테이너 이름(`app`) |
| `containerReason` | string \| null | 쿠버네티스 대기 이유: `CrashLoopBackOff`·`ImagePullBackOff`·`ErrImagePull`·`InvalidImageName`·`ErrImageNeverPull` |
| `terminationReason` | string \| null | 마지막 종료 이유: `OOMKilled`·`Error`·`Completed` 등 |
| `exitCode` | integer \| null | 마지막 종료 코드(137 = OOM·SIGKILL) |
| `restartCount` | integer \| null | 재시작 횟수 |
| `logTail` | string[] | 컨테이너 로그 마지막 50줄 이하(재시작했으면 직전 컨테이너). **비밀값을 가린 값**: 그 배포의 앱 환경 변수 값(6자 이상은 모두, 6자 미만은 키 이름이 민감 키 모양 — `password`·`secret`·`token`·`api_key`·`auth`·`session` 등 — 일 때만, 빈 값 제외), 단독 JWT(`eyJ….….…`), 접속 문자열 비밀번호(`scheme://user:***@host`), `password=`·`token:` 꼴 값, 토큰 모양(GitHub·AWS·`Bearer`·`sk-`·개인 키). 이미지 받기 실패·로그를 못 읽었을 때는 빈 목록 |
| `logTruncated` | boolean | 50줄보다 앞 줄이 더 있었다 |
| `previousVersionRunning` | boolean \| null | 되돌린 뒤 이전 버전이 실행 중인가. 이전 배포가 없었으면 `false`(실행 중인 버전 없음), 되돌리기 전·되돌리기 실패·빌드 단계 실패면 `null` |

컨테이너 진단은 실제 실행기가 **되돌리기(실패한 Deployment·파드 정리) 전에** 읽는다. 로그 읽기가 막혀도(권한 `pods/log` 없음·닿지 못함) 배포는 같은 코드로 실패하고 `logTail` 만 빈다(서버 경고 `CONTAINER_LOGS_UNAVAILABLE`). 앱 환경 변수 값을 보관함에서 다시 못 꺼내면 가릴 수 없으므로 `logTail` 을 버리고 사유·종료 코드만 남긴다.

예 (첫 배포가 필수 변수 누락으로 실패):

```json
"failure": {
  "code": "CONTAINER_START_FAILED",
  "message": "The container kept crashing after it started. Last error: ValidationError: database_url",
  "container": "app",
  "containerReason": "CrashLoopBackOff",
  "terminationReason": "Error",
  "exitCode": 1,
  "restartCount": 3,
  "logTail": [
    "Starting application",
    "Traceback (most recent call last):",
    "  File \"/app/main.py\", line 7, in <module>",
    "    settings = Settings()",
    "pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings",
    "database_url",
    "  Field required [type=missing, input_value={}, input_type=dict]"
  ],
  "logTruncated": false,
  "previousVersionRunning": false
}
```

예 (성공):

```json
{
  "success": true,
  "data": {
    "id": "5c0e…",
    "serviceId": "6f1d…",
    "environment": {"id": "e1…", "name": "aws-seoul", "kind": "aws"},
    "serviceEnvironmentId": "a1…",
    "trigger": "MANUAL",
    "sourceRequestId": null,
    "ref": {"type": "BRANCH", "value": "main"},
    "repoUrl": "https://github.com/team-echo/shop-api",
    "commitSha": "9534efe0e0be2170f510a9b7a2886f6b18110c24",
    "status": "SUCCEEDED",
    "cancelReason": null,
    "supersededById": null,
    "errorCode": null,
    "errorMessage": null,
    "failure": null,
    "revertStatus": null,
    "build": {"id": "b1…", "status": "SUCCEEDED", "imageRepository": "registry.echo.test/shop-api", "imageDigest": "sha256:…", "reused": false},
    "deploymentId": "d1…",
    "urls": {"main": "https://shop-api-a1b2c3d4.apps.echo.test", "environment": "https://shop-api-aws-seoul-a1b2c3d4.apps.echo.test"},
    "stageTimes": {"queuedAt": "2026-11-02T03:00:00Z", "buildingAt": "2026-11-02T03:00:01Z", "deployingAt": "2026-11-02T03:00:05Z", "verifyingAt": "2026-11-02T03:00:08Z", "finishedAt": "2026-11-02T03:00:09Z"},
    "simulated": true,
    "isCurrent": true,
    "rollbackable": false,
    "rollbackOfRequestId": null
  }
}
```

`LogPage`(로그 한 페이지) — `source`(`DEPLOY_JOB`·`BUILD`·`RUNTIME`), `simulated`(행에 저장한 값: `DEPLOY_JOB`·`RUNTIME` 은 요청, `BUILD` 는 빌드), `items[{at, level(INFO·WARN·ERROR), message, code}]`(시간 순, `code` 는 `DEPLOY_JOB` 줄의 코드 — `BUILD`·`RUNTIME` 은 null, API 서버 4-1. OpenAPI 선택 칸, 응답에는 늘 실린다), `nextCursor`(string \| null, 줄이 없으면 받은 cursor), `hasMore`.

## 실패 이유 코드 (`errorCode`)

| 코드 | 단계 | 뜻 |
|---|---|---|
| `REPO_NOT_ACCESSIBLE` | 빌드 | 빌드 직전 저장소에 접근하지 못함(App 설치가 사라짐 등) |
| `REF_NOT_FOUND` | 접수 | 브랜치·커밋 없음(접수 응답 422 로만 나온다) |
| `SOURCE_PROVIDER_UNAVAILABLE` | 빌드 | 빌드 직전 GitHub 한도 초과·5xx·시간 초과 |
| `BUILD_FAILED` | 빌드 | 빌드 실패(`simulate.failAt = BUILDING` 포함) |
| `BUILD_TIMEOUT` | 빌드 | 빌드 시작 뒤 `BUILD_TIMEOUT_SECONDS` 초과. 같은 커밋 다음 요청은 새 빌드를 만든다 |
| `DEPLOY_FAILED` | 배포 | 배포 실행기 실패(`simulate.failAt = DEPLOYING` 포함) |
| `DEPLOY_TIMEOUT` | 배포 | `DEPLOYING` 시작 뒤 `DEPLOY_TIMEOUT_SECONDS` 초과 |
| `HEALTH_CHECK_FAILED` | 헬스 | 마감까지 2xx 가 아님(`simulate.failAt = VERIFYING` 은 첫 확인에서) |
| `PUBLIC_URL_UNREACHABLE` | 헬스 | 마감 때 마지막 확인이 연결 실패 |
| `HEALTH_CHECK_TIMEOUT` | 헬스 | 마감까지 한 번도 확인하지 못함 |
| `WORKER_LOST` | 아무 단계 | 일꾼이 같은 요청에서 임대를 3번 잃음 |
| `INTERNAL_ERROR` | — | 예약(지금은 쓰지 않음) |
| `BUILD_DISPATCH_FAILED` | 빌드 | 실제 실행기만: 빌드 워크플로를 시작하지 못함(워크플로·App 권한·설치 없음). 다시 보내도 같다 |
| `CLUSTER_UNAUTHORIZED` | 배포 | 실제 실행기만: 환경 클러스터(또는 클라우드 자격 발급)가 실행기를 거절함(401·403) |
| `IMAGE_PULL_FAILED` | 배포 | 실제 실행기만: 환경이 빌드한 이미지를 받지 못함(`ImagePullBackOff`, 30초 넘은 `ErrImagePull`) |
| `CONTAINER_START_FAILED` | 배포 | 실제 실행기만: 컨테이너가 시작 뒤 계속 죽음(`CrashLoopBackOff`) |
| `ENV_SECRET_UNAVAILABLE` | 배포 | 배포 기록에 고정한 앱 환경 변수 버전을 보관함에서 영구히 꺼내지 못함(없음·권한·사용 중지). 앱을 반영하지 않는다([variables.md](variables.md)) |

실제 실행기에서 환경 설정이 비어 반영할 수 없을 때(온프레 `apiUrl`·`tunnelTarget`·`secretRef`, aws `certificateArn`·`ingressGroup`)는 `DEPLOY_FAILED` 다(서버 경고 로그 `ENVIRONMENT_NOT_CONFIGURED`). 새 코드 4개는 `errorMessage` 가 코드별 고정 영어 문장이다.

## HTTP 오류 (되돌리기)

| 상황 | HTTP | code |
|---|---|---|
| 모르는 칸, `failAt` 이 `DEPLOYING`·`VERIFYING` 이 아님(`BUILDING` 포함) | 422 | `VALIDATION_ERROR` |
| `simulate` 를 가짜가 아닌 실행기 모드에서 보냄 | 422 | `SIMULATE_NOT_ALLOWED` |
| 에이전트 토큰 · 뷰어 | 403 | `AGENT_READ_ONLY` · `ROLE_FORBIDDEN`(거부 기록 남음) |
| 대상 요청 없음·다른 조직 | 404 | `DEPLOY_REQUEST_NOT_FOUND` |
| 서비스 삭제를 마침 · 환경 삭제됨 | 404 | `SERVICE_NOT_FOUND` · `ENVIRONMENT_NOT_FOUND` |
| 서비스 삭제 중 · 환경 마지막 접속 확인 실패 | 409 | `SERVICE_DELETING` · `ENVIRONMENT_UNHEALTHY` |
| 그 서비스의 이전 계획이 실행 중(`APPROVED`·`RUNNING`·`ROLLING_BACK`, 대상 상태 검사보다 먼저) | 409 | `MIGRATION_IN_PROGRESS` + `details[{field: "planId", reason, value}]` |
| 대상이 `SUCCEEDED` 가 아님(실패·취소·대기·진행 중)·배포 기록 없음·연결이 내려감 | 409 | `NOT_ROLLBACKABLE` |
| 대상의 배포 기록이 이미 현재 배포 | 409 | `ALREADY_CURRENT` |

## HTTP 오류 (접수)

| 상황 | HTTP | code |
|---|---|---|
| 요청 스키마 위반(branch·commitSha 둘 다·둘 다 없음, 형식, 모르는 `failAt`, 모르는 칸) | 422 | `VALIDATION_ERROR` |
| `simulate` 를 가짜가 아닌 실행기 모드에서 보냄 | 422 | `SIMULATE_NOT_ALLOWED` |
| 브랜치·커밋 없음 | 422 | `REF_NOT_FOUND` |
| 서비스 없음·삭제됨 | 404 | `SERVICE_NOT_FOUND` |
| 환경 없음·삭제됨·서비스와 다른 조직의 환경 | 404 | `ENVIRONMENT_NOT_FOUND` |
| 에이전트 토큰으로 접수 | 403 | `AGENT_READ_ONLY`(거부 기록 남음) |
| 뷰어의 접수 | 403 | `ROLE_FORBIDDEN`(거부 기록 남음) |
| 저장소 접근 불가 | 404 | `REPO_NOT_ACCESSIBLE` (+ `installUrl`) |
| 서비스 삭제 중 | 409 | `SERVICE_DELETING` |
| 환경 마지막 접속 확인 실패 | 409 | `ENVIRONMENT_UNHEALTHY` |
| 그 서비스의 이전 계획이 실행 중(API 서버 8. 승인 대기 계획은 막지 않고, 그 계획이 승인 때 `STALE` 이 된다) | 409 | `MIGRATION_IN_PROGRESS` + `details[{field: "planId", reason, value}]` |
| 같은 환경별 주소를 가진 살아 있는 연결 있음(옛 환경 이름 재사용, 63자 자르기) | 409 | `PUBLIC_HOST_CONFLICT` |
| GitHub 한도 초과·5xx·시간 초과 | 502 | `SOURCE_PROVIDER_UNAVAILABLE` |
