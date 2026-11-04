# API 서버 10: 고객 앱 환경 변수·API 키를 시크릿 관리 서비스로 관리

- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/10 (Project #4)
- 기준: main `a936eff` (11/4 21:00 KST). 운영 마이그레이션 head `eee7a6c8229f`
- 인터뷰 상태 파일: `.deep-interview/api-10-secret-manager.state.md` (결정 D-1~D-8)
- 관련: echo-frontend #14 [프론트 4.5] 배포 화면 환경 변수 입력(담당 없음), echo-infra #42(손으로 만든 비밀·IAM의 Terraform화), echo-was #52 [API 서버 15](2차: DB 등록·생성)
- 검토: fresh-eyes 1회(11/4) 반영(★ 표시)

## 1. 완료 기준 (이슈 원문)

앱 환경 변수와 API 키가 관리 평면 DB가 아니라 시크릿 서비스에 저장되고, 배포 시 참조(secret_ref)로만 주입된다. 관리 평면 DB와 저장소에 시크릿 값 0건(NFR-17). 시크릿 저장소 프로비저닝은 인프라 파트와 함께.

이번 범위의 읽기: "시크릿 서비스" = GCP Secret Manager(D-2). "배포 시 주입" = echo-was 배포 실행기가 배포 직전에 값을 꺼내 앱 네임스페이스의 `{ns}-env` Secret에 쓴다(D-1). 화면(프론트 4.5)은 이 API를 쓰는 쪽이라 범위 밖이다.

## 2. 확인된 사실 (main `a936eff`)

- 앱 Deployment는 `envFrom: secretRef {ns}-env (optional: true)`를 읽고, `env`(PORT·ECHO_ENVIRONMENT)가 envFrom보다 우선한다 — `app/executors/kubernetes/manifests.py::deployment_manifest`, `app_secret_name`.
- 실행기는 `{ns}-env`를 만들지 않는다(API 3-2 D-3). 11/4 시연 앱 `demo-y6cz3jrc-env`는 EKS·GKE에 손으로 만들었다(`kubectl replace`, 라벨 `managed-by: jimin-manual`, 키 `DATABASE_URL`·`AUTO_MIGRATE`). 운영 시연 앱이 이것으로 돈다.
- 실행기의 쓰기는 서버 측 apply(`PATCH ?fieldManager=echo-was&force=true`, `api.py::KubeApi.apply`)다. SSA는 다른 manager가 가진 키를 지우지 않는다. 온프레 `echo-pull` Secret을 이미 이렇게 쓴다(`executors.py::_apply_pull_secret`).
- 소유 라벨 상수: `manifests.MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"`, `MANAGED_BY_VALUE = "echo-was"`.
- 클러스터 권한: EKS 실행기 역할 `AmazonEKSClusterAdminPolicy`(echo-infra `aws/dev/ci.tf`), GKE GSA `echo-dev-echo-was` `roles/container.developer`, 온프레 `echo-app-deployer`는 앱 네임스페이스 secrets 쓰기 포함. 세 곳 모두 앱 네임스페이스 Secret을 쓸 수 있다.
- 배포 기록 `deployments.config_snapshot`(JSONB 객체)은 요청 snapshot을 복사하고 "비밀값 없이 참조만" 담는다 — `deploy_request_processor.py::_create_deployment`(약 446행). 요청 종류(`DeployRequestTrigger`)는 `MANUAL`·`STANDBY_SYNC`·`ROLLBACK` 셋뿐. 되돌리기 요청은 옛 배포의 snapshot을 복사(`deploy_request_service.py` 약 278행), 예비 동기화는 원본 요청 snapshot을 복사하고 `environmentUrl`만 바꾼다(`deploy_request_processor.py` 약 743행).
- 실패한 새 배포는 `_fail` → `_revert`가 `deployment_spec(previous)`로 실행기 `revert`를 부른다(약 760~860행). 요청은 이미 FAILED이고 `revert_status`만 적는다.
- `DeploymentSpec(deployment_id, image_repository, image_digest, config_snapshot)`가 실행기로 가는 배포 값이다 — `executors/base.py`. 만드는 곳은 `deploy_support.py::deployment_spec`.
- 비밀 참조 형식 `gcp-sm:projects/<p>/secrets/<s>[/versions/<v>]`는 `app/core/secrets.py::parse_secret_ref`가 받는다.
- `GoogleMetadataClient`(토큰 캐시)는 지금 실제 실행기 묶음 안에서만 만든다(`executors/__init__.py` 약 200행). API 프로세스용 인스턴스는 없다.
- GSA `echo-dev-echo-was`의 Secret Manager 권한은 지정 비밀의 `secretAccessor`뿐이다(echo-infra `gcp/dev/control-plane.tf`).
- 운영은 `EXECUTOR_MODE: real`이다(`deploy/base.yaml`).
- 권한 guard `ViewerAccess`(에이전트 GET 허용)·`HumanDeveloperAccess`·`HumanAdminAccess` — `app/auth/guards.py`. 422 처리기는 pydantic `msg`만 응답에 넣는다(`core/exception_handlers.py`).
- 서비스 상세 응답의 환경 칸: `environments[].environment.id`(환경 id), `environments[].serviceEnvironmentId`(연결 id) — `app/schemas/service.py`.
- 새 표·라우트를 더하면 같이 고쳐야 하는 시험: `tests/integration/conftest.py::TABLES`, `tests/integration/test_migrations.py`의 표 목록·`EXPECTED_CONSTRAINTS`·`EXPECTED_INDEXES`, `tests/integration/test_authorization.py::ROLE_TABLE`(모든 API 라우트를 덮는지 시험함). 비밀 노출 시험 선례: `tests/integration/test_secret_exposure.py`(TABLES 전체를 `string_agg(t::text)`로 덤프).

## 3. 결정 (인터뷰)

| ID | 결정 | 출처 |
|---|---|---|
| D-1 | 주입은 echo-was 배포 실행기가 배포 때 한다. 고객 클러스터에 ESO 등 설치 없음 | 사용자 R1 |
| D-2 | 원본은 GCP Secret Manager 한 곳(관제탑 GSA) | 사용자 R1 |
| D-3 | 값은 API로 넣고 쓰기 전용(다시 보이지 않음) | 사용자 R1 |
| D-4 | 값 단위 = 서비스 × 환경(연결 `service_environments` 하나에 키 목록) | 사용자 R2 |
| D-5 | 시연 앱 진짜 DB 주소: 11/4 손으로 먼저 넣음. 운영 반영 뒤 같은 값을 새 길로 다시 넣어 증명 | 사용자 R1·11/4 |
| D-6 | 저장소 분석·DB 생성은 2차(#52). 이번은 값 넣기·주입만 | 사용자 R5 |
| D-7 | 권한: 값 넣기·바꾸기 = 사람 개발자 이상, 키 삭제 = 사람 어드민, 에이전트 토큰 = 키 이름 목록 GET만 | 사용자 R6 |
| D-8 | 기본값: ① 키 이름 규칙·예약어 거절 ② 바꾸면 다음 배포 때 반영(자동 재시작 없음) ③ 배포 기록에 키별 버전, 되돌리기는 그 버전, 새 배포(예비 동기화 포함)는 그 시점 최신 ④ 못 꺼내면 배포 실패 ⑤ 값·값 일부 노출 0 ⑥ 보관함 이름 `echo-app-…`, IAM은 그 접두어만, 서비스 삭제 때 보관함은 즉시 안 지움, DB는 이름·참조·버전·변경 기록만 | 사용자 R6 |

AI 기본값(거부 가능). ★는 검토로 바뀐 것:

| ID | 내용 |
|---|---|
| AD-1 ★ | 저장소 스위치 `APP_SECRET_STORE_MODE = fake \| gcp`(기본 `fake`). 코드 PR은 `fake`로 병합하고, IAM 적용 뒤 `deploy/base.yaml` PR로 `gcp`를 켠다. **`fake` + `EXECUTOR_MODE=real`(지금 운영)에서는 PUT·DELETE가 409 `SECRET_STORE_NOT_CONFIGURED`(행 만들지 않음)이고, 실행기는 `{ns}-env`를 쓰지 않는다.** 병합 직후 누가 PUT해도 시연 앱 손 Secret이 덮이지 않게 하기 위함 |
| AD-2 | 키 이름 `^[A-Z][A-Z0-9_]{0,63}$`. 예약: `PORT`, `ECHO_`로 시작하는 모든 이름 → 422 `ENV_VAR_KEY_RESERVED` |
| AD-3 ★ | 값: UTF-8 문자열 1~16,384바이트, 여러 줄 허용. 연결 하나에 키 최대 40개(합계 640KiB, base64 뒤에도 쿠버 Secret 1MiB 한도 안). 넘으면 422 `ENV_VAR_VALUE_INVALID`·`ENV_VAR_LIMIT_EXCEEDED` |
| AD-4 | 보관함 비밀 이름 `echo-app-{조직 slug}-{서비스 id 앞 8자}-{환경 id 앞 8자}-{키 소문자, _ → -}`. 255자를 넘으면 slug를 자른다. 라벨 `echo-org`·`echo-service`·`echo-environment`(id 앞 8자), `managed-by=echo-was`. 이름은 행에 저장한다 |
| AD-5 ★ | 배포 기록 `config_snapshot.envVars = {KEY: "<참조>"}`(버전 고정). gcp 모드 참조는 `gcp-sm:projects/<APP_SECRET_PROJECT_ID>/secrets/<name>/versions/<n>`, fake 모드 참조는 `fake-sm:<name>/versions/<n>`. gcp 모드 처리기는 `fake-sm:` 참조를 "건드리지 않음"으로 본다(배포 실패 아님, env_values=None) |
| AD-6 ★ | 클러스터 Secret `{ns}-env` 규칙은 5-4 표를 따른다. Echo가 쓸 때는 **통째로 교체**한다(GET → 있으면 PUT replace, 없으면 create). 라벨은 `MANAGED_BY_LABEL=MANAGED_BY_VALUE`를 단다. SSA apply는 다른 manager의 키를 남기므로 쓰지 않는다 |
| AD-7 | Pod 템플릿 annotation `echo.env-revision` = 정렬한 `KEY=version` 목록의 SHA-256 앞 16자(값 아님). env_values=None이면 annotation 없음 |
| AD-8 ★ | 새 실패 코드 `ENV_SECRET_UNAVAILABLE`(DeployRequestErrorCode): 고정 버전을 영구히 못 꺼냄(404·403·FAILED_PRECONDITION). 5xx·429·연결 실패·메타데이터 일시 오류는 1·2·4초 재시도 뒤 `ExecutorUnavailableError`(일꾼이 다시 집는 기존 규칙). 로그에는 키 이름만 |
| AD-9 | 쓰기 API 저장소 오류: 502 `SECRET_STORE_UNAVAILABLE`, 403은 502 `SECRET_STORE_FORBIDDEN`. 응답·로그에 값·비밀 이름 없음 |
| AD-10 ★ | 보관함 비밀은 echo-was가 지우지 않는다. 키 DELETE와 서비스 삭제 모두 행만 삭제 표시한다(옛 배포 기록·되돌리기가 그 버전을 계속 읽어야 해서). 정리는 라벨로 찾는 후속 일로 문서에 적는다 |
| AD-11 ★ | 같은 키 동시 PUT: 서비스 행 → 연결 행 순서로 잠그고(기존 잠금 순서), 보관함 호출 타임아웃은 10초 이하. 잠근 채 외부를 호출하는 예외임을 `docs/executors.md`에 적는다 |
| AD-12 ★ | 이전 계획(migration plan)은 트래픽만 옮기고 값을 바꾸지 않는다. 목록 응답 `pendingRedeploy`로 "다시 배포해야 반영"을 보인다. 이전 계획 API는 이번에 바꾸지 않는다(D-8③의 "이전"은 예비 동기화 배포로 읽는다) |
| AD-13 ★ | `AppSecretStore`는 프로세스마다 하나(API는 lifespan holder, 일꾼은 시작 때). 각자 `GoogleMetadataClient`를 갖고 종료 때 aclose |

## 4. 범위

한다:
1. 표 `service_environment_variables` + 마이그레이션(더하기만).
2. 보관함 클라이언트 `AppSecretStore`(GCP Secret Manager REST + fake).
3. API 3개(목록·넣기/바꾸기·삭제).
4. 배포 기록에 키별 버전 고정.
5. 실행기(`KubernetesDeployExecutor`, EKS·GKE·온프레 공통)가 배포·되돌리기 때 `{ns}-env`를 쓰고, 가짜 실행기는 키 이름만 기록.
6. 문서: `docs/api/services.md`(또는 새 `docs/api/variables.md`), `docs/executors.md`, `docs/schema.md`, `deploy/README.md` 설정 설명, `.env.example`.

하지 않는다:
- 화면(프론트 4.5, echo-frontend #14) — API 계약만 문서로 준다.
- IAM 변경, 운영 스위치 켜기 — M1이 따로 한다(6절).
- 서비스 공통 값·환경 덮어쓰기(D-4 B안), 값 다시 보기, 자동 재시작, ESO, AWS Secrets Manager.
- 이전 계획 API 변경(AD-12), 보관함 비밀 자동 정리(AD-10).

## 5. 계약

### 5-1. 데이터

`service_environment_variables` (UuidPrimaryKey · Timestamp · SoftDelete):

| 칸 | 타입 | 뜻 |
|---|---|---|
| service_environment_id | uuid FK → service_environments.id RESTRICT | 연결 |
| key | varchar(64), CHECK `key ~ '^[A-Z][A-Z0-9_]{0,63}$'` | 이름 |
| secret_name | varchar(255) | 보관함 비밀 id(프로젝트 제외) |
| latest_version | integer, CHECK > 0 | 보관함 최신 버전 번호 |
| updated_by_user_id | uuid FK → users.id | 마지막으로 넣은 사람 |

- 부분 unique: `(service_environment_id, key)` where 살아 있는 행. soft_delete_consistency CHECK는 기존 표와 같게 한다.
- 값 칸은 없다.
- 같은 키를 지운 뒤 다시 넣으면, 새 행이 **이전 행의 `secret_name`을 그대로 쓰고** 보관함에 버전을 더한다(버전 번호가 이어진다, AD-10). 그래서 옛 배포 기록의 `versions/<n>`이 다른 값을 가리키지 않는다.

### 5-2. API

경로 머리: `/api/v1/orgs/{org_id}/services/{service_id}/environments/{environment_id}/variables`. `environment_id`는 환경 id(서비스 상세 `environments[].environment.id`)다. 서비스가 그 환경에 연결돼 있지 않으면 404 `SERVICE_ENVIRONMENT_NOT_FOUND`.

1. `GET …/variables` — `ViewerAccess`(에이전트 포함).
   - 응답 `items[]`(키 이름 순): `key`, `version`, `secretRef`(AD-5 모양), `updatedAt`, `updatedBy {id, githubLogin}`, `appliedVersion`(연결의 현재 배포 기록에 고정된 버전, 없으면 null), `pendingRedeploy`(appliedVersion ≠ version).
   - 함께 `simulated`(fake 모드면 true)를 준다.
   - 값·값 일부·길이는 주지 않는다.
2. `PUT …/variables/{key}` 본문 `{ "value": "<문자열>" }` — 사람 개발자 이상. 새 키면 201, 있으면 200. 응답은 1의 항목 하나 + `simulated`. 앱은 다시 배포해야 바뀐다.
3. `DELETE …/variables/{key}` — 사람 어드민. 204. 행만 삭제 표시한다(AD-10).

오류:
- 422 `ENV_VAR_KEY_INVALID`·`ENV_VAR_KEY_RESERVED`·`ENV_VAR_VALUE_INVALID`·`ENV_VAR_LIMIT_EXCEEDED`
- 404 `ENV_VAR_NOT_FOUND`(삭제)
- 409 `SECRET_STORE_NOT_CONFIGURED`(AD-1)
- 502 `SECRET_STORE_UNAVAILABLE`·`SECRET_STORE_FORBIDDEN`
- 오류 메시지·details에는 값이 들어가지 않는다.

### 5-3. 보관함 클라이언트

```text
class AppSecretStore(Protocol):
    simulated: bool
    async def add_version(self, name: str, value: SecretValue, labels: dict[str, str]) -> int  # 없으면 만들고(409 ALREADY_EXISTS는 성공으로 봄) 버전 추가 → 새 버전 번호
    async def access(self, name: str, version: int) -> SecretValue                            # 고정 버전 읽기
```
- GCP 구현:
  - 주소: `https://secretmanager.googleapis.com/v1/projects/{project}/secrets…`(create `?secretId=`, `:addVersion`, `/versions/{n}:access`)
  - 토큰: `GoogleMetadataClient.access_token()`, 401이면 force로 한 번 재시도
  - 설정: `APP_SECRET_PROJECT_ID`(gcp 모드면 필수). 복제 정책은 automatic, 값은 base64 `payload.data`
  - 버전 번호는 응답 `name`의 마지막 `/versions/<n>`만 정수로 읽는다. 응답 name에는 프로젝트 번호가 올 수 있으므로, 참조 문자열은 항상 `APP_SECRET_PROJECT_ID`로 조립한다.
- fake 구현:
  - 프로세스 메모리에 두고, 시험용 실패 주입(이름 목록)을 둔다. 같은 프로세스에서 없는 이름이면 실패한다.
  - `EXECUTOR_MODE=fake`일 때만, 일꾼이 못 찾은 값을 `SecretValue("simulated")`로 대신한다(일꾼과 API가 다른 프로세스라서).
- 삭제 메서드는 두지 않는다(AD-10).
- 로그·예외에 값·토큰·비밀 이름을 넣지 않는다(행 id만).

### 5-4. 배포 흐름

1. `_create_deployment`(배포 기록 만들 때):
   - 요청이 `ROLLBACK`이고 snapshot에 `envVars` 키가 있으면 그대로 둔다.
   - 그 밖(`MANUAL`·`STANDBY_SYNC`)은 **그 배포의 연결**에 살아 있는 변수 행으로 `envVars`를 새로 만든다. 변수가 0개여도 `envVars: {}`를 넣는다. 예비 동기화가 원본 환경 값을 복사하지 않는다.
   - 같은 배포 기록의 재실행(임대 만료 뒤 다른 일꾼이 집음)은 기록에 고정된 `envVars`를 그대로 쓴다.
2. 처리기는 실행기 `apply`/`revert` 직전에 `envVars`를 값으로 바꿔 `DeploymentSpec.env_values: Mapping[str, SecretValue] | None`에 담는다:

| 배포 기록 | env_values | 실행기의 `{ns}-env` |
|---|---|---|
| `envVars` 키 없음(이 기능 이전 기록) | None | 읽지도 쓰지도 않음 |
| `envVars` 참조가 `fake-sm:`인데 gcp 모드 | None | 읽지도 쓰지도 않음 |
| `fake` 저장소 + `EXECUTOR_MODE=real` | None | 읽지도 쓰지도 않음(AD-1) |
| `envVars: {}` | {} | 기존 Secret이 echo-was 소유(라벨)일 때만 빈 데이터로 교체, 아니면 그대로 |
| `envVars` 1개 이상 | 값 사전 | 통째로 교체(AD-6) + annotation(AD-7) |

3. 새 배포(apply)에서 참조 하나라도 영구히 못 꺼내면, 실행기를 부르지 않고 `ENV_SECRET_UNAVAILABLE`로 실패한다(D-8④). 일시 오류는 AD-8을 따른다.
4. **revert**에서 previous의 값을 못 꺼내면:
   - env_values=None(Secret 건드리지 않음)으로 실행기 revert를 부른다. 직전 앱 Deployment는 되돌린다.
   - 경고 로그 `ENV_SECRET_UNAVAILABLE`(키 이름만)를 남긴다.
5. 가짜 실행기는 키 이름 목록만 로그로 남긴다. 배포 작업 로그에는 "앱 환경 변수 N개 적용(키: A, B)"만 남긴다.

### 5-5. 설정

`APP_SECRET_STORE_MODE`(fake|gcp, 기본 fake), `APP_SECRET_PROJECT_ID`, `APP_SECRET_NAME_PREFIX`(기본 `echo-app-`, IAM 조건과 같아야 함). `.env.example`·`deploy/README.md`에 설명을 적는다. 운영 `deploy/base.yaml`은 바꾸지 않는다.

### 5-6. 호환성 (롤링 배포)

- 옛 파드는 새 표와 새 snapshot 키 `envVars`를 읽지 않는다. 이를 시험으로 확인한다(`config_snapshot`을 읽는 곳이 키를 골라 읽는지 본다).
- 새 일꾼이 옛 배포 기록(envVars 없음)을 집으면 5-4 표 첫 줄(None)로 처리한다.
- 새 실패 코드에는 DB CHECK가 없다.

## 6. 이 PR 밖에서 M1이 할 일 (구현 뒤)

1. GSA `echo-dev-echo-was`에 `roles/secretmanager.admin`과 IAM 조건을 gcloud로 적용한다.
   - 조건: `resource.name.startsWith("projects/573662116412/secrets/echo-app-")`. 안 되면 `resource.name.extract("secrets/{rest}").startsWith("echo-app-")`.
   - 운영 파드에서 만들기·버전 추가·읽기를 시험한다.
   - 같은 값으로 echo-infra `control-plane.tf` PR을 올린다.
   - 조건이 만들기(create)에 안 먹으면 대안: 조건 없는 커스텀 역할(`secretmanager.secrets.create`만)을 더한다(읽기·버전은 조건부 admin이 막음).
2. `deploy/base.yaml`에 `APP_SECRET_STORE_MODE: gcp`·`APP_SECRET_PROJECT_ID` PR을 올린다.
3. 시연:
   - 시연 앱 aws-tokyo·gcp-seoul 연결에 `DATABASE_URL`·`AUTO_MIGRATE`를 API로 넣는다.
   - 서비스 중 환경에 다시 배포한다(예비는 예비 동기화로 따라감).
   - 두 클러스터 `{ns}-env`가 echo-was 소유이고 `/readyz`가 200인지 본다.
4. echo-frontend #14에 API 계약 댓글을 단다.

## 7. 완료 판정 (AC)

| AC | 내용 | 확인 |
|---|---|---|
| AC-1 | PUT 새 키 201, 같은 키 200 + 버전 +1, GET 목록에 key·version·secretRef·pendingRedeploy, 값 없음 | 통합 시험(fake 저장소 + fake 실행기) |
| AC-2 | 권한: 뷰어 PUT 403, 개발자 PUT 성공·DELETE 403, 어드민 DELETE 204, 에이전트 GET 200·PUT 403 `AGENT_READ_ONLY`, 다른 조직 404. 3개 라우트를 `ROLE_TABLE`에 추가 | 통합 시험 |
| AC-3 | 키 규칙: `port`·`1A`·`PORT`·`ECHO_X`·65자 → 422, 빈 값·16,385바이트 → 422, 41번째 키 → 422 | 통합 시험 |
| AC-4 | 새 배포 기록 `envVars`가 그 연결의 최신 버전(0개면 `{}`), 예비 동기화는 예비 연결의 값, 되돌리기는 옛 기록 버전 그대로, 재실행은 고정값 | 통합 시험 |
| AC-5 | 실행기: 5-4 표의 다섯 줄 각각(값 사전 → 교체·라벨·annotation / {} + echo 소유 → 빈 데이터 / {} + 손 Secret → 그대로 / None → GET도 안 함). 기능 이전 기록으로 revert하면 echo 소유 Secret 그대로 | 실행기 단위 시험(가짜 쿠버 API) |
| AC-6 | 값 0건: 무작위 값(`sv-<uuid>`)을 PUT → 배포 요청 → 일꾼이 배포까지 처리한 뒤 ① `conftest.TABLES` 전체 `string_agg(t::text)` 덤프 ② caplog 전체 ③ 모든 응답 본문 ④ 422 응답에 그 값 0회 | 통합 시험 |
| AC-7 | apply 때 고정 버전 영구 실패 → `ENV_SECRET_UNAVAILABLE`, 실행기 apply 미호출. 일시 오류 → `ExecutorUnavailableError`. revert 때 previous 접근 실패 → Deployment 되돌림, Secret 그대로, 경고 로그 | 통합·단위 시험(fake 실패 주입) |
| AC-8 | GCP 클라이언트: 없는 비밀 만들기→버전, 409 ALREADY_EXISTS 뒤 버전 추가, 고정 버전 읽기, 401 한 번 재시도, 403 → FORBIDDEN, 5xx 재시도, 버전 번호 파싱, 요청에 토큰 헤더, 로그에 값 없음 | 단위 시험(httpx MockTransport) |
| AC-9 | 마이그레이션 더하기만, `alembic check` 통과, `db-schema.sql` 일치, down_revision = `eee7a6c8229f`. 새 표를 `TABLES`·`test_migrations` 표 목록·`EXPECTED_CONSTRAINTS`·`EXPECTED_INDEXES`에 추가 | 검사·시험 |
| AC-10 | 병합만으로 운영 불변: `fake` 저장소 + `EXECUTOR_MODE=real`에서 PUT → 409, 실행기는 `{ns}-env` 미변경(손 Secret 그대로) | 통합 시험 |
| AC-11 | 지운 키를 다시 넣으면 같은 `secret_name`에 버전이 이어지고, 옛 기록 되돌리기는 옛 값 | 통합 시험 |
| AC-12 | (운영, 6절 뒤) 시연 앱 두 환경에서 새 길로 넣은 값으로 `/readyz` 200, 운영 DB·저장소에 값 0건 | M1 운영 확인 |

## 8. 위임한 세부사항 (워커가 정하고 보고)

- 파일·클래스 이름, 서비스·저장소 계층 나누기(저장소 규칙 문서를 따름).
- GCP REST 세부(타임아웃 값은 10초 이하).
- 배포 로그 코드 이름.
- 쿠버 Secret 교체 방법의 세부(GET·PUT·create 처리, 409 충돌 재시도).
