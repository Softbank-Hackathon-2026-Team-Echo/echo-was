# API 서버 3: 원터치 배포 API (소스 → 빌드 → 배포) SPEC

- 상태: Confirmed
- 확인일: 2026-11-02
- Depth: Deep
- Ambiguity: Low (≈ 12% / 목표 ≤ 20%)
- High-impact unresolved decisions: None
- Interview rounds: 7 (R1~R6 + 체크포인트 R7, 상태 파일 `.deep-interview/api-3-one-touch-deploy.state.md`) + fresh-eyes 검토 10건 반영
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/3 (Project #4, `part:API 서버`, `phase:1차`, In Progress)
- 저장소: `echo-was` · 브랜치 `feat/one-touch-deploy` (`main` = `e977f85`에서)
- 체크포인트 그림: `~/archive/softbank-hackathon-2026/api-3/api3-checkpoint.html` (M1, 지민 확인 11/2: A1·C0·D1·E1)

## 1. 목표

등록한 서비스(앱 하나)의 Git 저장소 브랜치나 커밋을 **요청 한 번**으로 빌드해 고른 환경 1곳에 배포하고, 헬스체크를 통과하면 새 버전으로 바꾼 뒤 퍼블릭 URL을 돌려준다. 이미 서비스 중인 환경이 있으면 새 환경은 **예비(트래픽 0)**로 올라가고, 서비스 중인 환경이 새 버전을 받으면 예비 환경도 같은 이미지로 **자동으로 따라간다**. 예비 환경은 재난·장애 때 이전(API 서버 8)이 넘어갈 곳이다.

흐름·상태·계약·DB 기록은 실제로 만든다. 빌드·배포·대표 주소 연결·내리기 실행은 이번에 **가짜 실행기**가 흉내 낸다(D1). 인프라 3(Argo CD + 빌드·배포 파이프라인)과 인프라 5(고정 진입점 프록시)가 준비되면 실행기 구현만 바꾼다.

GitHub 이슈 완료 기준(원문, 11/2 수정): **Git 저장소(공개·비공개)의 브랜치나 커밋을 한 번의 요청으로 빌드해 고른 환경 1곳(클라우드 또는 온프레)에 배포하고, 퍼블릭 URL(대표·환경별)을 돌려준다. 같은 앱을 클라우드와 온프레 양쪽에 올릴 수 있다. 이미 서비스 중인 환경이 있으면 새 환경은 예비(트래픽 0)로 올라가고, 서비스 중인 환경의 새 버전을 자동으로 따라간다. 헬스체크를 통과해야 새 버전으로 교체한다.**

이 이슈의 완료 기준은 실제 배포까지다. 이번 작업을 병합해도 실제 실행기 연결 전에는 이슈를 `In Progress`로 둔다(D1).

## 2. 현재 맥락과 확인된 사실 (`main` = `e977f85`)

- `app/models/service.py`: `Service`(`name`, `description`, 소프트 삭제·타임스탬프 믹스인). 이름 규칙 `^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$`(DNS 레이블 호환, `app/models/base.py:19`), 살아 있는 행 중 이름 유일.
- `app/models/service_environment.py`: `ServiceEnvironment`(`service_id`, `environment_id`, `current_deployment_id`(복합 FK로 같은 연결의 배포만, `use_alter`), `traffic_weight` 0~100(앞단 프록시에 마지막으로 적용한 비율), `traffic_applied_at`). 살아 있는 (service, environment) 쌍 유일. `current_deployment` 관계는 읽기 전용 — 바꿀 때는 `current_deployment_id` 값을 직접 넣는다.
- `app/models/deployment.py`: `Deployment`(`service_environment_id`, `image_repository`, `image_digest` 필수·`^sha256:[0-9a-f]{64}$` CHECK, `config_snapshot` jsonb object, `status` PENDING·IN_PROGRESS·SUCCEEDED·FAILED·CANCELLED, `rollback_of_id`, `started_at`, `finished_at`). DELETE는 DB 트리거(`prevent_deployment_delete()`, 메시지가 deployments 고정)가 막는다. `config_snapshot`에는 비밀값을 넣지 않는다.
- `.claude/rules/term-dictionary.md`: 배포 상태 전환표, "취소는 PENDING에서만", 빌드(`builds`, 빌드 1 : 배포 N)가 API 서버 3 예정. 트래픽 비율 합계는 DB가 검사하지 않고 API 서버 8이 맞춘다. 환경 이름은 바뀔 수 있다.
- `app/models/environment.py`: `Environment`(`name`, `kind`, `config`, `secret_ref`, `connection_status` UNCHECKED·CONNECTED·FAILED 등). API 2 SPEC §5.3: 환경 삭제는 살아 있는 연결이 있으면 409이고, **연결을 만드는 쪽(API 서버 3)도 같은 환경 행을 `FOR UPDATE`로 잠근 뒤 연결을 만든다**.
- `docs/api/environments.md`: 경로 접두어 `/api/v1`, 응답 `ApiResponse` 봉투, camelCase, 목록 `Page`(`items`, `total`, `page` 0부터, `size`), 비밀 참조 `env:ECHO_SECRET_…`.
- `app/schemas/environment.py`: 가짜 어댑터 결과에 `simulated: bool`(목업 표시 선례).
- `app/core/config.py`·`app/core/secrets.py`·`app/clients/secret_client.py`: 설정과 비밀 참조 조회(`env`만 실제).
- `app/main.py`: 라우터 2개, `/healthz`·`/readyz`. 일꾼·작업 큐 없음. `app/dependencies.py`의 `authorize_request`는 권한 검사 자리 표시.
- `tests/integration/conftest.py`: 테스트마다 `TRUNCATE deployments, service_environments, environments, services` — 새 표가 FK로 이 표들을 가리키면 목록을 늘려야 한다. `tests/integration/test_migrations.py`: 칸 순서·CHECK를 비교(기존 표에 더한 칸은 `sort_order=APPENDED_COLUMN_SORT_ORDER` 필요). `alembic check`는 CHECK·부분 인덱스·트리거를 비교하지 않는다.
- `pyproject.toml` 의존성: alembic, asyncpg, fastapi, pydantic-settings, sqlalchemy[asyncio], uvicorn. HTTP 클라이언트·JWT 라이브러리 없음. `compose.yaml`: `db`만.
- `CLAUDE.md`: 레이어 `routers/ → services/ → repositories/`, 외부 호출은 `clients/`·`adapters/`. 스키마 변경은 모델 → Alembic revision → `scripts/dump_schema.sh` 한 세트. `docs/schema.md`가 스키마 설명 원본. 시크릿은 `BaseSettings`, `os.getenv` 직접 호출 금지. 로그·예외에 시크릿 금지. 광범위한 `except` 금지.
- GitHub API 사실: 설치 조회 `GET /repos/{o}/{r}/installation`과 인증 없는 `GET /repos/{o}/{r}`는 "저장소 없음"과 "비공개인데 권한 없음"을 똑같이 404로 준다. 인증 없는 호출은 IP당 시간당 60회 한도. 설치 토큰은 1시간 뒤 만료.
- 예선(iris) 방식: 요청 행과 할 일을 한 트랜잭션으로 적고 접수 번호 반환 → 일꾼이 `FOR UPDATE SKIP LOCKED` + 임대로 집음 → digest로만 배포 → GitOps 커밋(커밋 SHA를 push 전에 DB에 적어 중복 방지) → Argo CD 상태 폴링. 겪은 문제: Argo 반영 지연, 권한 오류가 시간 초과로만 보임.
- 해커톤 규칙: 목업은 목업이라고 밝힌다. 킥오프는 여러 환경 동시 서비스를 요구하지 않는다.

### 앞 태스크에서 넘겨받은 결정
- 인증·역할 → echo-was#6(API 2 D-17). 이번에도 인증 없음, 권한 검사 자리 표시(`authorize_request`)만. #6이 병렬로 진행되므로 새 라우터도 같은 자리 표시를 쓴다.
- 되돌리기(`rollback_of_id`) → API 서버 4. 트래픽 전환·이전 → API 서버 8. 환경 카드 상태 → API 서버 5. 감사 로그 → API 서버 7.

## 3. 범위

### 포함
- 서비스 API 5개(등록·목록·단건·수정·삭제)와 배포 요청 API 3개(§8.1)
- `deploy_requests`·`builds`·`jobs` 새 표, `services`·`service_environments` 칸 추가, Alembic revision 1개, `db-schema.sql` 갱신
- 일꾼 프로세스 `python -m app.worker`(할 일 표 폴링, 임대, 단계 진행, 복구)
- 실행기 인터페이스 5개(빌드, 배포, 대표 주소 라우터, 헬스체크, 내리기)와 가짜 구현, 공통 계약 시험
- 소스 저장소 클라이언트: 인터페이스 + 가짜 구현 + **실제 GitHub App 구현**(시험은 가짜 전송)
- 실제 HTTP 헬스체커 구현(시험은 가짜 전송, `health_checker_mode=http`로 켤 수 있음. 기본은 가짜)
- 예비 연결 자동 따라가기, 배포 겹침 규칙(실행 1 + 대기 1), 서비스 삭제(모든 환경에서 내리기)
- 문서: `docs/api/services.md`, `docs/api/deploy-requests.md`, `docs/schema.md`, 용어 사전(배포 요청·빌드·할 일·서비스 중·예비, 트래픽 비율 줄 보충), `README.md`(일꾼 실행, 가짜 모드, GitHub App 설정 자리), 실행기 계약 문서 `docs/executors.md`
- 테스트: 단위 + DB 통합(API·일꾼, 동시성) + 마이그레이션 왕복. 통합 시험 TRUNCATE 목록·factories 갱신

### 제외
- 실제 빌드(CodeBuild·GitHub Actions)·GitOps 커밋·Argo CD 호출·실제 프록시 라우팅·클러스터 리소스 삭제 → 인프라 3·5 이후 실행기 교체 작업
- 대표 주소의 실제 헬스 확인 → 실제 라우터 연결 뒤(§5.3-4)
- 트래픽 전환·이전·DB 복제 → API 서버 8, F8
- 되돌리기 API·배포 로그 → API 서버 4
- 사용자가 직접 요청을 취소하는 API(AD-2), git push 자동 배포·웹훅(AD-10), github.com 이외 저장소(AD-11), 파일 업로드·이미지 직접 입력(D2)
- 인증·역할(echo-was#6), 실시간 푸시(AD-7)
- GitHub App 실제 생성·조직 설치(별도 승인), 비용이 드는 클라우드 자원
- push, `main` 병합(지민 확인 뒤 M1이 한다)

## 4. 구성 요소

| 구성 요소 | 상태 | 비고 |
|---|---|---|
| 서비스 API | active | routers → services → repositories |
| 배포 요청 API | active | 접수만 동기, 실행은 일꾼 |
| 일꾼 + 할 일 표 | active | `app/worker/`, `jobs` |
| 실행기(빌드·배포·라우터·헬스·내리기) | active(가짜) | 실제 빌드·배포·라우터·내리기는 deferred(인프라 3·5) |
| 소스 저장소 클라이언트 | active | 가짜 + 실제 GitHub App |
| 문서·계약 | active | API 문서 2개, schema.md, 용어 사전, executors.md |

## 5. 흐름

### 5.0 동시성 규칙 (모든 쓰기 경로 공통)
1. **잠금 순서는 하나다**: 환경 행(연결을 만들 때만) → 서비스 행 → 그 서비스의 연결 행들(`id` 오름차순) → 요청 행 → 빌드 행 → 할 일 행. 접수(§5.2), 일꾼의 모든 단계 전환(§5.3), 성공 처리(§5.4), 삭제 접수(§5.5-1), 삭제 일꾼(§5.5-2)이 모두 이 순서를 지킨다. 상태만 바꾸는 잠금은 `FOR NO KEY UPDATE`로 잡는다(다른 트랜잭션의 FK 삽입이 잡는 `FOR KEY SHARE`와 충돌하지 않게).
2. **상태 변경은 조건부 UPDATE**: `UPDATE … SET status = :next WHERE id = :id AND status = :expected`. 영향받은 행이 0이면 그 결과를 버리고 다시 읽는다.
3. **DB 보호망**: `deploy_requests`에 부분 unique 2개 — (`service_environment_id`) WHERE `status IN ('BUILDING','DEPLOYING','VERIFYING')`, (`service_environment_id`) WHERE `status = 'QUEUED'`.
4. **외부 호출은 트랜잭션 밖**에서 한다. 단, 가짜 라우터(§8.6)는 I/O가 없어 성공 처리 트랜잭션 안에서 부른다(실제 라우터의 호출 위치는 실제 구현 작업에서 정한다).
5. **시각은 주입한 시계**로 정해 바인드 값으로 넘긴다. 일꾼·서비스 코드는 DB `now()`에 기대지 않는다(시험에서 임대 만료·제한 시간을 sleep 없이 재현하기 위해).

### 5.1 서비스 등록 (`POST /api/v1/services`)
1. 요청 검사: `name`(이름 규칙), `description`(선택), `repoUrl`(필수, §8.3), `healthCheckPath`(선택, 기본 `/healthz`), `healthCheckTimeoutSeconds`(선택, 기본 300, 10~1800), `containerPort`(선택, 기본 8080, 1~65535). 틀리면 422 `VALIDATION_ERROR`.
2. 소스 저장소 클라이언트 `check_access(repo)`(트랜잭션 밖): 접근 가능 → `private`(GitHub 응답 값)와 기본 브랜치. 저장소가 없거나 비공개인데 App 설치가 없으면 404 `REPO_NOT_ACCESSIBLE`(`details.installUrl`에 설치 안내 주소). GitHub 한도 초과·5xx·시간 초과는 502 `SOURCE_PROVIDER_UNAVAILABLE`.
3. 트랜잭션: 서비스 행 저장(`status = ACTIVE`, `public_host = {name}.{public_base_domain}`, `simulated = 소스 클라이언트가 가짜인지`). 이름 중복은 409 `SERVICE_NAME_CONFLICT`.
4. 201 + `ServiceDetail`.

### 5.2 배포 요청 접수 (`POST /api/v1/services/{id}/deploy-requests`)
1. 요청 검사: `environmentId`(필수), `branch`와 `commitSha` 중 정확히 하나, `simulate`(선택). 둘 다 없거나 둘 다 있으면 422 `VALIDATION_ERROR`. `simulate`는 `executor_mode = fake`일 때만 받고, 아니면 422 `SIMULATE_NOT_ALLOWED`(AD-8).
2. 서비스가 없거나 삭제됐으면 404 `SERVICE_NOT_FOUND`. `DELETING`이면 409 `SERVICE_DELETING`.
3. 환경이 없거나 삭제됐으면 404 `ENVIRONMENT_NOT_FOUND`. `connection_status = FAILED`면 409 `ENVIRONMENT_UNHEALTHY`(AD-5).
4. 소스 저장소 클라이언트 `resolve_ref(repo, branch|commitSha)`(트랜잭션 밖): 40자 SHA. 없으면 422 `REF_NOT_FOUND`, 저장소 접근 불가 404 `REPO_NOT_ACCESSIBLE`, 제공자 장애 502 `SOURCE_PROVIDER_UNAVAILABLE`. 이 순간 커밋이 고정된다(D10).
5. 트랜잭션(§5.0 순서):
   - 환경 행을 잠그고 다시 확인한다: 삭제됐으면 404, `FAILED`면 409. 서비스 행을 잠그고 다시 확인한다: `DELETING`이면 409.
   - 살아 있는 연결(service, environment)이 없으면 만든다(`traffic_weight = 0`, `current_deployment_id = NULL`, `public_host = {service}.{environment}.{public_base_domain}`). 이 주소가 살아 있는 다른 연결과 겹치면(환경 이름을 바꾼 뒤 옛 이름으로 새 환경을 만든 경우) 409 `PUBLIC_HOST_CONFLICT`.
   - 연결 행을 잠근다. 같은 연결의 `QUEUED` 요청을 `CANCELLED`(`cancel_reason = SUPERSEDED`, `superseded_by_id`, `finished_at`)로 바꾼다(조건부). 그 할 일이 `QUEUED`면 `CANCELLED`로 닫고, `RUNNING`이면 그대로 둔다(일꾼이 다음 실행에서 끝 상태를 보고 닫는다).
   - 새 요청 행과 할 일(`kind = DEPLOY_REQUEST`)을 저장한다: `status = QUEUED`, `trigger = MANUAL`, 고정한 `commit_sha`, 정규화한 `repo_url`(접수 순간 저장소, AD-6), 서비스 설정 사본 `config_snapshot`, `simulate`, `simulated = (executor_mode == fake)`.
6. 202 + `DeployRequestDetail`.

### 5.3 일꾼이 요청 하나를 진행하는 방법
일꾼은 할 일을 하나 집어(§8.5) 요청을 **한 단계만** 진행한 뒤, 끝나지 않았으면 `run_after = now + 간격`으로 돌려놓는다. 요청이 이미 끝 상태면 할 일을 `DONE`으로 닫는다.

**외부 호출 멱등 규칙**: 외부 호출에는 우리가 먼저 정한 **멱등 키**를 넘긴다(빌드 = `builds.id`, 배포·되돌리기 = `deployments.id`, 내리기 = 연결 `id` + `teardown_attempt`). 순서는 "요청함 시각 저장·커밋 → 외부 호출(같은 키로 다시 불러도 같은 작업을 돌려줘야 함) → `external_ref` 저장". 다시 시작한 일꾼은 요청함 시각이 있고 `external_ref`가 없으면 같은 키로 다시 부른다.

1. `QUEUED`: 서비스가 `DELETING`이면 `CANCELLED`(`SERVICE_DELETED`). 같은 연결에 `BUILDING`·`DEPLOYING`·`VERIFYING` 요청이 있으면 미룬다(실행 1 + 대기 1). 없으면 조건부로 `BUILDING`, `started_at`, `building_at`.
2. `BUILDING`:
   - `simulate.failAt = BUILDING`이면 빌드 행을 만들거나 붙지 않고 요청을 `FAILED`(`BUILD_FAILED`)로 끝낸다(공유 빌드를 건드리지 않는 요청 단위 가짜 실패).
   - 요청에 `build_id`가 이미 있으면(예비 따라가기, 또는 앞 실행에서 붙음) 그 빌드를 본다.
   - 없으면 같은 서비스·요청의 `repo_url`·커밋으로 `SUCCEEDED` 빌드를 찾아 재사용한다(AD-1). `QUEUED`·`RUNNING` 빌드가 있으면 그 빌드에 붙는다. 둘 다 없으면 새 빌드 행을 만든다. 새 빌드 INSERT가 부분 unique 위반이면(인덱스 이름으로 구분) 다시 읽어 그 빌드에 붙는다.
   - 빌드 진행(이 빌드를 기다리는 요청의 일꾼 누구나 할 수 있다): 빌드 행을 잠그고, `QUEUED`면 시작 절차(멱등 규칙). 비공개 저장소면 빌드 직전에 설치를 다시 조회해 1시간 토큰을 발급하고 실행기에 메모리로만 넘긴다. `RUNNING`이면 `poll`. 끝 상태는 조건부 UPDATE로 한 번만 적는다.
   - 빌드 제한 시간은 `builds.started_at` 기준(`build_timeout_seconds`, 기본 900). 넘기면 빌드 행을 `FAILED`(`BUILD_TIMEOUT`)로 닫는다. 빌드가 `FAILED`면 그 빌드를 기다리는 요청은 모두 같은 코드로 `FAILED`.
   - 빌드가 `SUCCEEDED`면 배포 기록(`deployments`) 행을 `IN_PROGRESS`로 만들고(`image_repository`·`image_digest`·`config_snapshot` 복사) 요청을 `DEPLOYING`, `deploying_at`.
3. `DEPLOYING`: 배포 실행기 `apply(배포 기록, 대상, 멱등 키)` → `poll`. 반영되면 `VERIFYING`, `verifying_at`. 실패는 `DEPLOY_FAILED`, `deploying_at` 기준 제한 시간(`deploy_timeout_seconds`, 기본 300) 초과는 `DEPLOY_TIMEOUT`. `simulate.failAt = DEPLOYING`이면 `DEPLOY_FAILED`.
4. `VERIFYING`(D7): 일꾼 실행마다 헬스체커가 **환경별 퍼블릭 주소 + 헬스 경로**를 한 번 확인한다(요청 하나 제한 5초). 2xx면 통과. 아니면 `health_check_interval_seconds`(기본 5) 뒤 다시 확인한다. `verifying_at + healthCheckTimeoutSeconds`(접수 때 사본)를 넘기면 마지막 확인 결과로 끝낸다: 2xx가 아닌 응답이었으면 `HEALTH_CHECK_FAILED`, 연결 자체가 실패했으면 `PUBLIC_URL_UNREACHABLE`, 한 번도 확인하지 못했으면 `HEALTH_CHECK_TIMEOUT`. `simulate.failAt = VERIFYING`이면 첫 확인에서 `HEALTH_CHECK_FAILED`. 대표 주소는 성공 처리에서 라우터가 이 환경으로 연결한 뒤에야 이 환경을 가리키므로 이 단계에서 확인하지 않는다(실제 라우터 연결 뒤 추가).
5. 성공 처리(§5.4) 뒤 요청 `SUCCEEDED`, 배포 기록 `SUCCEEDED`, `finished_at`.
6. 실패 처리: 요청 `FAILED` + `error_code`, 배포 기록이 있으면 `FAILED`. **배포 기록이 생긴 뒤의 모든 실패**(`DEPLOY_FAILED`·`DEPLOY_TIMEOUT`·헬스 실패 3종·`WORKER_LOST`)는 배포 실행기 `revert(대상, 현재 배포 기록 또는 없음, 멱등 키)`를 불러 직전 버전을 유지하게 하고 결과를 `revert_status`(`SUCCEEDED`·`FAILED`)에 적는다. revert가 실패해도 `error_code`는 바꾸지 않는다. 연결의 현재 배포는 바꾸지 않는다. 오류 메시지는 코드별 고정 문장만 쓴다(AD-19).

### 5.4 성공 처리 — 서비스 중·예비와 자동 따라가기 (D5, D11)
한 트랜잭션에서 서비스 행 → 그 서비스의 살아 있는 연결 전부(`id` 순)를 잠근 뒤:
1. 이 연결의 `current_deployment_id`를 새 배포 기록으로 바꾼다.
2. 역할 판정: `traffic_weight > 0`이면 **서비스 중**. 0인데 다른 서비스 중 연결이 없으면 대표 주소 라우터 `route_main(서비스, 이 연결, 멱등 키)`를 부르고, 라우터가 적용했다고 답하면 `traffic_weight = 100`, `traffic_applied_at = 그 시각`으로 적는다(이번에는 가짜 라우터, `simulated`). 다른 서비스 중 연결이 있으면 **예비**(0 유지).
3. 서비스가 `ACTIVE`이고 이 연결이 서비스 중이면, `traffic_weight = 0`이면서 `current_deployment_id`가 있는 다른 연결(예비)마다 따라가기 요청을 만든다: `trigger = STANDBY_SYNC`, `source_request_id`, 같은 `commit_sha`·`repo_url`·`build_id`(재빌드 없음), `config_snapshot`은 원본 요청 사본에서 `environmentUrl`만 그 예비 연결 주소로 바꾼 값(같은 이미지·같은 설정). 접수와 같은 겹침 규칙(대기 요청 SUPERSEDED)을 따른다 — 예비 연결에 대기 중인 수동 요청도 밀린다(AD-21). 서비스가 `DELETING`이면 만들지 않는다.
4. 예비 연결 요청의 성공·실패는 서비스 중 연결에 영향이 없다. 예비 연결 요청은 다시 따라가기를 만들지 않는다. 예비 연결에 사용자가 직접 다른 커밋을 배포하는 것은 막지 않는다(그 경우 `OUT_OF_SYNC`로 보인다).

### 5.5 서비스 삭제 (`DELETE /api/v1/services/{id}`, D12)
1. 트랜잭션(서비스 행 잠금): 없거나 삭제 완료면 404. `status = DELETING`, `delete_error = NULL`, 이 서비스의 `QUEUED` 요청을 모두 `CANCELLED`(`SERVICE_DELETED`, 할 일 처리는 §5.2-5와 같음). 살아 있는 `SERVICE_TEARDOWN` 할 일이 없으면 하나 만든다. 202 + `ServiceDetail`. 이미 `DELETING`이면 같은 처리(재시도)이고, 실패로 남은 연결의 `teardown_attempt`를 1 늘린다.
2. 일꾼(서비스 행 → 연결들 잠금): 남은 `QUEUED` 요청을 다시 취소한다. `BUILDING`·`DEPLOYING`·`VERIFYING` 요청이 있으면 미룬다. 없으면 살아 있는 연결마다 내리기를 진행한다(멱등 규칙, `teardown_requested_at`·`teardown_external_ref`). 내린 연결은 바로 소프트 삭제한다(배포 기록은 남는다).
3. 연결별 내리기 제한 시간은 `teardown_requested_at` 기준(`teardown_timeout_seconds`, 기본 300). 한 연결이라도 실패하면 서비스는 `DELETING`에 남고 `delete_error`에 `TEARDOWN_FAILED`·`TEARDOWN_TIMEOUT`, 할 일이 임대를 3번 잃었으면 `WORKER_LOST`를 적고 할 일을 끝낸다. 다시 DELETE를 보내면 남은 연결만 다시 내린다(AD-12).
4. 모든 연결이 내려가면 서비스를 소프트 삭제한다. 이후 단건·수정·삭제·배포 요청은 404 `SERVICE_NOT_FOUND`이고 같은 이름을 다시 등록할 수 있다. 삭제된 서비스의 요청을 가리키는 할 일이 남아 있으면 일꾼은 요청을 `CANCELLED`(`SERVICE_DELETED`, `QUEUED`일 때)로 끝내고 할 일을 닫는다.
5. `DELETING` 서비스: 단건·목록 조회는 된다. PATCH와 배포 요청은 409 `SERVICE_DELETING`.

### 5.6 서비스 수정 (`PATCH /api/v1/services/{id}`)
- 바꿀 수 있는 칸: `description`, `repoUrl`(다시 `check_access`, `private` 갱신), `healthCheckPath`, `healthCheckTimeoutSeconds`, `containerPort`. `name`은 바꿀 수 없다(422 `SERVICE_NAME_IMMUTABLE`, 같은 값이면 무시, AD-20).
- `null`은 `description`만 허용, 나머지는 422.
- 바뀐 값은 다음 요청부터 쓴다. 접수된 요청은 접수 때 사본(`repo_url`·`config_snapshot`)을 쓴다(AD-6).

## 6. 기능 요구사항

- `FR-1` 서비스는 등록 API로 먼저 만든다. 저장소 주소·헬스 경로·제한 시간·포트를 저장한다(D6).
- `FR-2` 배포 요청은 서비스 + 환경 1개 + 브랜치 또는 커밋이다. 요청 순간 커밋 SHA와 저장소 주소를 고정한다(D2, D5, D10, AD-6).
- `FR-3` 연결은 그 환경의 첫 요청 때 자동으로 만든다(D6).
- `FR-4` 빌드와 배포는 별도 실행기로 나눈다. 빌드 결과는 `image_repository` + `sha256` digest이고 배포는 digest만 쓴다(D3).
- `FR-5` 오래 걸리는 일은 할 일 표 + 별도 일꾼이 한다. 일꾼이 죽어도 임대가 끝나면 다른 일꾼이 같은 단계부터 이어 가고, 외부 작업은 멱등 키로 두 번 만들지 않는다(D4).
- `FR-6` 같은 연결에는 실행 중 요청 1개 + 대기 요청 1개까지만. 새 요청이 오면 대기 요청은 `SUPERSEDED`로 취소된다(D10). DB 부분 unique로도 막는다.
- `FR-7` 헬스체크를 통과해야 연결의 현재 배포가 바뀐다. 배포 기록이 생긴 뒤 실패하면 되돌리기를 불러 직전 버전을 유지한다(D7).
- `FR-8` 대표 주소와 환경별 주소를 둘 다 돌려준다(D8). 주소는 만들 때 한 번 정해 저장하고, 살아 있는 행 중 유일하다(AD-4).
- `FR-9` 서비스 중 연결이 성공하면 예비 연결이 같은 이미지·같은 설정으로 자동 따라간다(D11).
- `FR-10` 같은 서비스·저장소·커밋의 빌드는 재사용한다(AD-1).
- `FR-11` 비공개 저장소는 GitHub App 설치로 접근한다. 서버에 두는 비밀값은 App 개인키 참조 하나다(D9).
- `FR-12` 서비스 삭제는 모든 환경에서 내린 뒤 지운다(D12).
- `FR-13` 가짜 실행기·가짜 소스 클라이언트가 만든 결과에는 `simulated: true`를 싣는다. 이 값은 행에 저장하고 응답 때 설정으로 다시 계산하지 않는다(AD-13).
- `FR-14` 단계마다 제한 시간과 실패 이유 코드가 있다. 임대를 3번 잃은 할 일은 `WORKER_LOST`로 끝낸다(P1).
- `FR-15` 서비스 상세의 연결마다 역할, `versionStatus`, 마지막 따라가기 실패 이유를 보여 준다(P2).
- `FR-16` GitHub App 개인키·설치 토큰은 로그·응답·DB·예외 메시지에 0회 나온다(P4).

## 7. 상태와 예외

### 7.1 배포 요청 상태 (`DeployRequestStatus`)

| 상태 | 뜻 | 다음 상태 |
|---|---|---|
| `QUEUED` | 접수됨, 시작 전(일꾼 대기 또는 같은 연결의 앞 요청 대기) | `BUILDING`, `CANCELLED` |
| `BUILDING` | 이미지 준비 중(재사용이면 바로 통과) | `DEPLOYING`, `FAILED` |
| `DEPLOYING` | 배포 실행기가 반영 중 | `VERIFYING`, `FAILED` |
| `VERIFYING` | 헬스체크 중 | `SUCCEEDED`, `FAILED` |
| `SUCCEEDED`·`FAILED`·`CANCELLED` | 끝 상태 | 없음 |

- 취소는 `QUEUED`에서만. `cancel_reason`: `SUPERSEDED`, `SERVICE_DELETED`.
- `error_code`: `REPO_NOT_ACCESSIBLE`, `REF_NOT_FOUND`, `SOURCE_PROVIDER_UNAVAILABLE`, `BUILD_FAILED`, `BUILD_TIMEOUT`, `DEPLOY_FAILED`, `DEPLOY_TIMEOUT`, `HEALTH_CHECK_FAILED`, `HEALTH_CHECK_TIMEOUT`, `PUBLIC_URL_UNREACHABLE`, `WORKER_LOST`, `INTERNAL_ERROR`.
- `revert_status`: NULL(되돌리기 필요 없음) · `SUCCEEDED` · `FAILED`.
- DB CHECK: `status`·`trigger`·`cancel_reason`·`revert_status` 값, `CANCELLED ⇔ cancel_reason IS NOT NULL`, `FAILED ⇔ error_code IS NOT NULL`, 끝 상태 ⇔ `finished_at IS NOT NULL`, `trigger = STANDBY_SYNC ⇔ source_request_id IS NOT NULL`.

### 7.2 빌드 상태 (`BuildStatus`): `QUEUED` → `RUNNING` → `SUCCEEDED`·`FAILED`
- `SUCCEEDED ⇔ image_digest IS NOT NULL`(sha256 형식 CHECK). 같은 (service, repo_url, commit_sha)에 `FAILED`가 아닌 빌드는 1개(부분 unique). 실패한 빌드 뒤 같은 커밋 요청은 새 빌드를 만든다.
- 빌드를 기다리던 요청이 모두 끝났는데(`WORKER_LOST` 등) 빌드가 `QUEUED`·`RUNNING`으로 남으면, 마지막 요청을 끝내는 일꾼이 빌드도 `FAILED`(같은 코드)로 닫아 다음 요청이 막히지 않게 한다.

### 7.3 할 일 상태 (`JobStatus`): `QUEUED` → `RUNNING` → `DONE`·`FAILED`·`CANCELLED`
- 미룰 때는 `QUEUED` + `run_after`. 집을 때마다 새 `lease_token`(uuid)을 발급하고, 끝낼 때 같은 토큰인지 조건부 UPDATE로 확인한다(다르면 결과를 버림). 임대 만료(`RUNNING` + `lease_expires_at < now`)는 다른 일꾼이 집을 수 있고 `lease_losses`를 1 늘린다. 3이 되면 대상 요청을 `FAILED`(`WORKER_LOST`, 배포 기록이 있으면 되돌리기)로, 내리기 할 일이면 서비스 `delete_error = WORKER_LOST`로 끝낸다.

### 7.4 서비스 상태 (`ServiceStatus`): `ACTIVE` → `DELETING` → (소프트 삭제)
- `delete_error`: `TEARDOWN_FAILED`·`TEARDOWN_TIMEOUT`·`WORKER_LOST`, `DELETING`일 때만 값이 있다(CHECK).

### 7.5 연결 역할(응답에서 계산, 저장 안 함)
- `SERVING`: `traffic_weight > 0`. `STANDBY`: `traffic_weight = 0`이고 현재 배포 있음. `NONE`: 현재 배포 없음.
- `versionStatus`(STANDBY만): 기준 연결 = `traffic_weight`가 가장 큰 연결(같으면 `traffic_applied_at`이 늦은 쪽, 그다음 `id`). 기준 연결의 현재 배포 `image_digest`와 같으면 `IN_SYNC`, 다르면 `OUT_OF_SYNC`. 서비스 중 연결이 없으면 `null`. 다른 역할은 `null`.
- `lastSyncError`: 그 연결의 가장 최근 `STANDBY_SYNC` 요청이 `FAILED`면 그 `error_code`, 아니면 `null`.
- 트래픽 비율 칸의 뜻은 바꾸지 않는다("앞단 프록시에 마지막으로 적용한 비율"). 이번에는 가짜 라우터가 적용한 값이고 `simulated`로 표시한다(용어 사전에 한 줄 보충).

### 7.6 경계 조건
- 첫 요청이 실패한 연결은 `NONE`으로 남는다. 다음 성공 때 역할을 판정한다.
- 서비스 중 연결 요청이 실패하면 따라가기 요청을 만들지 않는다.
- 요청 접수 중 서비스가 `DELETING`으로 바뀌면 409. 진행 중 요청은 끝까지 진행되고, 삭제가 그 뒤에 내린다. 그 성공은 따라가기를 만들지 않는다.

## 8. 계약

### 8.1 엔드포인트 (`/api/v1`, 응답은 `ApiResponse` 봉투·camelCase)

| 메서드 · 경로 | 요청 | 성공 | 실패 |
|---|---|---|---|
| `POST /services` | `CreateServiceRequest` | 201 `ServiceDetail` | 422, 404 `REPO_NOT_ACCESSIBLE`(+`installUrl`), 502 `SOURCE_PROVIDER_UNAVAILABLE`, 409 `SERVICE_NAME_CONFLICT` |
| `GET /services?page&size` | — | 200 `Page[ServiceSummary]` | — |
| `GET /services/{id}` | — | 200 `ServiceDetail` | 404 `SERVICE_NOT_FOUND` |
| `PATCH /services/{id}` | `UpdateServiceRequest` | 200 `ServiceDetail` | 404, 409 `SERVICE_DELETING`, 422 |
| `DELETE /services/{id}` | — | 202 `ServiceDetail` | 404 |
| `POST /services/{id}/deploy-requests` | `CreateDeployRequest` | 202 `DeployRequestDetail` | 404 서비스·환경·`REPO_NOT_ACCESSIBLE`, 409 `SERVICE_DELETING`·`ENVIRONMENT_UNHEALTHY`·`PUBLIC_HOST_CONFLICT`, 422 `REF_NOT_FOUND`·`SIMULATE_NOT_ALLOWED`, 502 `SOURCE_PROVIDER_UNAVAILABLE` |
| `GET /services/{id}/deploy-requests?page&size&environmentId` | — | 200 `Page[DeployRequestSummary]`(최신순) | 404 |
| `GET /deploy-requests/{id}` | — | 200 `DeployRequestDetail` | 404 `DEPLOY_REQUEST_NOT_FOUND` |

라우터에는 API 2처럼 권한 검사 자리 표시(`authorize_request`)만 둔다.

### 8.2 응답 칸(요약, 정확한 스키마는 `docs/api/*.md`)
- `ServiceDetail`: `id`, `name`, `description`, `repoUrl`, `private`, `healthCheckPath`, `healthCheckTimeoutSeconds`, `containerPort`, `url`(대표), `status`, `deleteError`, `simulated`, `environments[]`, `createdAt`, `updatedAt`.
  - `environments[]`: `serviceEnvironmentId`, `environment{id, name, kind}`, `role`, `trafficWeight`, `url`(환경별), `currentDeployment{id, commitSha, imageDigest, finishedAt}|null`, `versionStatus`, `lastSyncError`, `activeRequestId`, `queuedRequestId`, `simulated`(현재 배포를 만든 요청의 값, 없으면 null), `teardownError`.
- `DeployRequestDetail`: `id`, `serviceId`, `environment{id, name, kind}`, `serviceEnvironmentId`, `trigger`, `sourceRequestId`, `ref{type: BRANCH|COMMIT, value}`, `repoUrl`, `commitSha`, `status`, `cancelReason`, `supersededById`, `errorCode`, `errorMessage`, `revertStatus`, `build{id, status, imageRepository, imageDigest, reused}|null`, `deploymentId`, `urls{main, environment}`, `stageTimes{queuedAt, buildingAt, deployingAt, verifyingAt, finishedAt}`, `simulated`.
- 없는 값은 `null`로 싣는다(API 2 D-15).

### 8.3 입력 규칙
- `repoUrl`: `https://github.com/<owner>/<repo>`(끝 `/`·`.git` 허용, 저장은 정규화). owner·repo는 영문·숫자·`-`·`_`·`.`, 100자 이하. 다른 호스트·`http`·자격 증명 포함 주소는 422(AD-11).
- `branch`: 1~255자, git ref 기본 금지 문자(공백, `~^:?*[\`, `..`, `@{`, 끝 `/`·`.lock`) 금지.
- `commitSha`: 7~40자 소문자 16진수. 40자로 고정한다(AD-15).
- `simulate`: `{ "failAt": "BUILDING" | "DEPLOYING" | "VERIFYING" }` — 가짜 모드에서만(AD-8).

### 8.4 데이터 (모델 → revision → `db-schema.sql`)
- 마이그레이션은 `services`·`service_environments` 행이 0개라고 가정한다(생성 API가 처음 생김). 행이 있으면 분명한 메시지로 실패한다(AD-16). 기존 표에 더한 칸은 모델에 `sort_order=APPENDED_COLUMN_SORT_ORDER`.
- `services` 칸 추가: `repo_url` varchar(300) NOT NULL, `repo_private` bool NOT NULL, `health_check_path` varchar(255) NOT NULL 기본 `/healthz`(`/`로 시작 CHECK), `health_check_timeout_seconds` int NOT NULL 기본 300(10~1800 CHECK), `container_port` int NOT NULL 기본 8080(1~65535 CHECK), `public_host` varchar(253) NOT NULL(살아 있는 행 부분 unique), `status` varchar(16) NOT NULL 기본 `ACTIVE`(CHECK), `delete_error` varchar(32) NULL(`status = DELETING`일 때만, CHECK), `simulated` bool NOT NULL 기본 false.
- `service_environments` 칸 추가: `public_host` varchar(253) NOT NULL(살아 있는 행 부분 unique), `teardown_attempt` int NOT NULL 기본 0, `teardown_requested_at` NULL, `teardown_external_ref` NULL, `teardown_error` varchar(32) NULL.
- 새 표 `builds`: `id`, `service_id` FK, `repo_url`, `commit_sha` char(40)(16진수 CHECK), `status`, `image_repository` NULL, `image_digest` NULL(CHECK), `start_requested_at` NULL, `external_ref` NULL, `error_code` NULL, `simulated` bool, `started_at`, `finished_at`, 타임스탬프. 부분 unique (`service_id`, `repo_url`, `commit_sha`) WHERE `status <> 'FAILED'`.
- 새 표 `deploy_requests`: `id`, `service_id` FK, `environment_id` FK, `service_environment_id` FK, `trigger`, `source_request_id` 자기 참조 FK NULL, `ref_type`, `ref_value`, `repo_url`, `commit_sha`, `status`, `cancel_reason`, `superseded_by_id` NULL, `error_code`, `build_id` FK NULL, `deployment_id` FK NULL, `deploy_requested_at` NULL, `deploy_external_ref` NULL, `revert_status` NULL, `revert_external_ref` NULL, `last_health_result` NULL(마지막 확인 종류), `config_snapshot` jsonb object, `simulate` jsonb NULL, `simulated` bool, `building_at`·`deploying_at`·`verifying_at`·`started_at`·`finished_at`, 타임스탬프. 인덱스: (`service_id`, `created_at`), §5.0-3 부분 unique 2개. 지우지 않는 이력: DELETE 금지 트리거(이 표 전용 함수 또는 `TG_TABLE_NAME`을 쓰는 공용 함수 — 기존 `prevent_deployment_delete()` 메시지는 바꾸지 않는다).
- 새 표 `jobs`: `id`, `kind`(`DEPLOY_REQUEST`·`SERVICE_TEARDOWN`), `subject_id` uuid, `status`, `run_after`, `lease_token` uuid NULL, `lease_owner` NULL, `lease_expires_at` NULL, `lease_losses` int 기본 0, `last_error_code` NULL, 타임스탬프. 인덱스 (`status`, `run_after`). 같은 `subject_id`의 `QUEUED`·`RUNNING` 할 일은 1개(부분 unique).
- `config_snapshot`(요청·배포 기록): `{"healthCheckPath", "healthCheckTimeoutSeconds", "containerPort", "mainUrl", "environmentUrl"}`. 비밀값·토큰 없음.
- `DeploymentStatus`·`deployments` 표는 바꾸지 않는다.
- 시험 준비: `tests/integration/conftest.py`의 TRUNCATE 목록에 새 표를 넣고, `factories`의 `Service`·`ServiceEnvironment` 생성에 새 필수 칸을 채운다. `test_migrations.py` 기대 목록에 새 CHECK·부분 unique·트리거를 넣는다(`alembic check`가 비교하지 못하므로).

### 8.5 일꾼·할 일
- 실행: `python -m app.worker`. 설정: `worker_poll_interval_seconds`(기본 1.0), `worker_lease_seconds`(기본 30), `worker_concurrency`(기본 1). 일꾼 여러 개를 동시에 띄워도 된다.
- 집기: `… WHERE (status = 'QUEUED' AND run_after <= :now) OR (status = 'RUNNING' AND lease_expires_at < :now) ORDER BY run_after LIMIT 1 FOR UPDATE SKIP LOCKED` → `RUNNING`, 새 `lease_token`, `lease_owner`, `lease_expires_at = :now + lease`. 만료된 것을 집으면 `lease_losses + 1`.
- 한 번 실행 = 한 단계 진행 또는 외부 상태 한 번 확인. 실행이 임대보다 길어지면 같은 `lease_token`으로 임대를 연장한다.
- 종료 신호(SIGTERM)를 받으면 지금 실행만 끝내고 멈춘다.
- 시험용으로 `run_once()`(할 일 하나 처리)와 시계 주입을 제공한다.

### 8.6 실행기 인터페이스 (`app/executors/`, 문서 `docs/executors.md`)
- `BuildExecutor`: `start(build, source_access, idempotency_key) -> external_ref`, `poll(external_ref) -> Running | Succeeded(image_repository, image_digest) | Failed(code)`.
- `DeployExecutor`: `apply(deployment, target, idempotency_key) -> external_ref`, `poll(external_ref) -> Pending | Applied | Failed(code)`, `revert(target, previous_deployment|None, idempotency_key) -> Reverted | Failed(code)`.
- `MainRouteExecutor`(대표 주소 라우터): `route_main(service, service_environment, idempotency_key) -> Applied(at) | Failed(code)`. 실제 구현은 echo-infra#5(고정 진입점 프록시) 몫.
- `HealthChecker`: `probe(url, path, timeout) -> Ok | HttpError(status) | Unreachable`. 실제 구현은 HTTP GET, 2xx 통과, 리다이렉트 따르지 않음.
- `TeardownExecutor`: `remove(service_environment, idempotency_key) -> external_ref`, `poll(external_ref) -> Pending | Removed | Failed(code)`.
- 가짜 구현: `fake_executor_step_seconds`(기본 2.0, 시험 0)만큼 지난 뒤 성공. 가짜 digest는 `sha256(서비스 id + 커밋)`, 이미지 주소는 `registry.echo.test/<서비스 이름>`. 시험은 가짜 실행기 인스턴스를 주입해 특정 커밋·연결에서 실패하게 만들 수 있다(따라가기 실패·내리기 실패·빌드 실패 시험용, 운영 설정에는 없음).
- 모드: `executor_mode`는 지금 `fake`만 받는다(`real`은 인프라 3 이후). `health_checker_mode`(`fake`·`http`, 기본 `fake`).
- 실제 구현이 지켜야 할 계약: 같은 멱등 키로 다시 불러도 같은 작업을 돌려줄 것, 단계별 실패 코드를 돌려줄 것(권한 오류를 시간 초과로 뭉개지 않음), digest로만 배포할 것, 비밀값을 `external_ref`·오류에 넣지 않을 것.

### 8.7 소스 저장소 클라이언트 (`app/clients/source_repository.py`)
- 인터페이스: `check_access(repo) -> RepoAccess(private, default_branch)`, `resolve_ref(repo, ref) -> commit_sha`, `issue_read_token(repo) -> 메모리 토큰 | None(공개)`.
- 오류: 없음·비공개인데 설치 없음 → `REPO_NOT_ACCESSIBLE`(구분 불가, `installUrl` 동봉). ref 없음 → `REF_NOT_FOUND`. 한도 초과(403·429 + 한도 헤더)·5xx·시간 초과 → `SOURCE_PROVIDER_UNAVAILABLE`.
- 설정: `source_client_mode`(`fake`·`github`, 기본 `fake`), `github_app_id`, `github_app_private_key_ref`(`env:ECHO_SECRET_…`, 기존 비밀 조회기), `github_app_install_url`, `github_api_base_url`(기본 `https://api.github.com`), 요청 제한 시간 10초.
- 실제 구현(`github`): App JWT(RS256, 유효 10분 이하)로 `GET /repos/{o}/{r}/installation` → 설치가 있으면 설치 토큰으로 저장소·ref 조회, 없으면 인증 없이 공개 저장소 조회. `private`는 저장소 응답의 `private` 값. 의존성 `httpx`, `PyJWT[crypto]`(AD-14). 시험은 `httpx.MockTransport`로만(실제 GitHub 호출 금지).
- 가짜 구현: owner가 `private-`로 시작하면 비공개 + 설치 있음, `denied-`·`missing-`이면 `REPO_NOT_ACCESSIBLE`, `flaky-`면 `SOURCE_PROVIDER_UNAVAILABLE`. ref가 `missing-`로 시작하면 `REF_NOT_FOUND`, 그 밖에는 `sha1(저장소 + ref)` 40자. 결과는 `simulated`.

### 8.8 설정 추가 (`Settings`)
`public_base_domain`(기본 `apps.echo.test`, 125자 이하·도메인 형식 검사), `executor_mode`, `health_checker_mode`, `source_client_mode`, `github_*`, `build_timeout_seconds`(900), `deploy_timeout_seconds`(300), `teardown_timeout_seconds`(300), `health_check_interval_seconds`(5), `fake_executor_step_seconds`(2.0), `worker_*`. `.env.example`에 키만 추가한다.

## 9. 호환성·배포·복구

- 기존 API(환경)는 바뀌지 않는다. 환경 삭제의 `ENVIRONMENT_IN_USE` 규칙은 이번에 만드는 연결에도 그대로 적용된다(환경 행 잠금 순서로 경쟁을 막는다).
- 일꾼이 없으면 요청은 `QUEUED`에 남는다(README에 적는다). `/readyz`는 바꾸지 않는다.
- 롤백(코드): revision `downgrade`로 새 표·칸을 지운다(시험으로 왕복 확인).
- 실제 실행기 교체는 `executor_mode = real` 구현을 더하는 작업이고, 표·API 계약은 바꾸지 않는 것을 목표로 한다. 과거 가짜 기록은 저장된 `simulated = true`로 계속 구분된다.

## 10. Acceptance criteria

시험은 가짜 실행기·가짜 소스 클라이언트, 주입 시계, `run_once()`로 결정적으로 돌린다(실제 sleep·네트워크 없음).

- `AC-1` 서비스 등록: 공개 저장소 → 201(`private=false`, `url = https://<name>.apps.echo.test`, `simulated=true`); `private-` owner → 201(`private=true`); `denied-`·`missing-` → 404 `REPO_NOT_ACCESSIBLE` + `details.installUrl`; `flaky-` → 502 `SOURCE_PROVIDER_UNAVAILABLE`; `gitlab.com`·`http://` 주소 → 422; 같은 이름 → 409.
- `AC-2` 원터치 배포: aws 환경에 `branch=main` 요청 → 202 `QUEUED`(40자 `commitSha`) → 일꾼 진행 → `SUCCEEDED`. 빌드 행 `SUCCEEDED`(sha256 digest), 배포 기록 `SUCCEEDED`, 연결 `current_deployment_id` 교체, `traffic_weight = 100`·`traffic_applied_at` 기록(가짜 라우터), 응답 `urls.main`·`urls.environment`, `simulated = true`. 상태가 `QUEUED → BUILDING → DEPLOYING → VERIFYING → SUCCEEDED` 순서로만 바뀐다.
- `AC-3` 예비: 같은 서비스를 onprem 환경에 배포 → `SUCCEEDED`, onprem `role = STANDBY`(비율 0), aws는 `SERVING` 그대로. 같은 커밋이면 빌드 행이 늘지 않는다(`build.reused = true`).
- `AC-4` 자동 따라가기: aws에 새 커밋 요청 성공 → onprem에 `STANDBY_SYNC` 요청 1개(`sourceRequestId`, 같은 `build_id`, 원본 사본과 `environmentUrl`만 다른 `config_snapshot`)가 생기고 성공 → 빌드 행 1개만 추가, onprem `versionStatus = IN_SYNC`. 주입한 가짜 실행기로 onprem 배포를 실패시키면 onprem `OUT_OF_SYNC` + `lastSyncError`, aws는 영향 없음. 서비스가 `DELETING`이면 따라가기 요청이 생기지 않는다.
- `AC-5` 헬스체크 실패: `simulate.failAt = VERIFYING` → `FAILED`/`HEALTH_CHECK_FAILED`, `revertStatus = SUCCEEDED`, 연결의 현재 배포·비율이 그대로, 따라가기 요청 없음, 배포 기록 `FAILED`. 헬스 판정 규칙(재확인 간격, 마감 뒤 마지막 결과로 코드 결정)을 가짜 헬스체커 주입으로 시험한다.
- `AC-6` 빌드·배포 실패: `failAt = BUILDING` → `BUILD_FAILED`(빌드 행 없음·공유 빌드 영향 없음), `failAt = DEPLOYING` → `DEPLOY_FAILED` + `revertStatus`. 주입한 가짜 빌드 실행기로 빌드를 실패시키면 빌드 행 `FAILED`, 그 빌드를 기다리던 요청 모두 `BUILD_FAILED`, 이후 같은 커밋 요청은 새 빌드를 만든다.
- `AC-7` 겹침: 연결에 `BUILDING` 요청 A가 있을 때 B·C를 접수 → B는 `CANCELLED`/`SUPERSEDED`(`supersededById = C`), C는 A가 끝난 뒤에만 `BUILDING`. 일꾼 2개 + 접수를 동시에 돌리는 통합 시험에서 한 연결에 실행 중 요청이 2개가 되는 순간이 없고, 부분 unique를 우회한 직접 INSERT는 DB가 거부한다. 일꾼이 `QUEUED → BUILDING`으로 올리는 순간 접수가 같은 요청을 취소하려 해도 둘 중 하나만 반영된다(조건부 UPDATE).
- `AC-8` 복구: 단계 중 임대를 만료시키면 다른 일꾼이 같은 단계부터 이어 가고, 가짜 실행기 호출 기록에서 같은 멱등 키의 작업은 1개다(같은 키 재호출은 같은 작업). 낡은 `lease_token`의 결과는 버려진다. 임대를 3번 잃으면 `FAILED`/`WORKER_LOST`(배포 기록이 있으면 되돌리기).
- `AC-9` 제한 시간: 빌드(`builds.started_at` 기준)·배포(`deploying_at`)·헬스(`verifying_at`)·내리기(`teardown_requested_at`) 제한 시간을 넘기면 각 코드로 끝난다. 시간 초과로 닫힌 빌드 뒤 같은 커밋 요청은 새 빌드를 만든다.
- `AC-10` 서비스 삭제: 대기 요청이 있는 서비스 DELETE → 202 `DELETING`, 대기 요청 `CANCELLED`/`SERVICE_DELETED`, 진행 중 요청이 끝난 뒤 연결마다 내리기 → 연결·서비스 소프트 삭제 → 단건 404, 같은 이름 재등록 201. 주입한 가짜 내리기로 한 연결을 실패시키면 `DELETING` + `deleteError = TEARDOWN_FAILED`, 다시 DELETE → 남은 연결만 내리고 삭제 완료. `DELETING` 중 PATCH·배포 요청은 409, 진행 중이던 서비스 중 연결의 성공은 따라가기를 만들지 않는다.
- `AC-11` 접수 거절: `FAILED` 환경 → 409 `ENVIRONMENT_UNHEALTHY`; 삭제된 환경 → 404; `missing-` ref → 422 `REF_NOT_FOUND`; `branch`·`commitSha` 둘 다·둘 다 없음 → 422; `executor_mode`가 `fake`가 아닌 설정에서 `simulate` → 422 `SIMULATE_NOT_ALLOWED`(설정 주입 단위 시험); 같은 `public_host`를 가진 살아 있는 연결이 있으면 409 `PUBLIC_HOST_CONFLICT`.
- `AC-12` 연결 생성 잠금: 환경 삭제와 첫 배포 요청을 동시에 보내도 "삭제된 환경에 살아 있는 연결"이 생기지 않는다(통합 시험).
- `AC-13` 비밀값: 시험용 App 개인키(시험 코드 안 상수)와 발급 토큰 문자열이 로그(caplog)·모든 응답 본문·DB 덤프(모든 표 텍스트)에 0회 나온다.
- `AC-14` 실제 GitHub 클라이언트: `MockTransport`로 설치 있음(비공개)·설치 없음 + 공개·둘 다 404·ref 없음·한도 초과(403/429)·5xx를 시험하고 각각 §8.7 오류로 바뀌는지, JWT가 RS256·`iss = app id`·만료 10분 이하인지 확인한다. 실제 네트워크 호출 0.
- `AC-15` 실제 헬스체커: `MockTransport`로 2xx `Ok`, 5xx `HttpError`, 연결 실패 `Unreachable`, 리다이렉트 비추종.
- `AC-16` 스키마: 새 revision 왕복(upgrade → downgrade → upgrade), `alembic check` 차이 0, `scripts/dump_schema.sh` 결과와 `db-schema.sql` 일치, `test_migrations.py`가 새 CHECK·부분 unique·트리거·칸 순서를 비교해 통과, 행이 있는 `services`에서 upgrade가 분명한 메시지로 실패.
- `AC-17` 문서: `docs/api/services.md`·`docs/api/deploy-requests.md`(모든 엔드포인트·오류 코드·`simulated` 뜻), `docs/schema.md`(새 표·상태 흐름), 용어 사전(새 용어, 트래픽 비율 줄 보충), `docs/executors.md`(멱등 키 계약 포함), `README.md`(일꾼 실행).
- `AC-18` 품질: ruff·format·mypy(strict)·pytest 통과, 통합 시험 건너뜀 0(로컬 DB를 띄운 상태).

## 11. 핵심 엔티티

| 이름 | 뜻 | 관계 |
|---|---|---|
| 서비스 `services` | 배포하는 앱 하나(저장소·헬스 설정·대표 주소) | 연결 N, 빌드 N, 배포 요청 N |
| 서비스-환경 연결 `service_environments` | 서비스가 환경 하나에 올라간 것(역할은 비율로 판단) | 배포 기록 N, 배포 요청 N |
| 배포 요청 `deploy_requests` | 원터치 배포 한 번 | 빌드 0..1, 배포 기록 0..1, 원본 요청(따라가기) 0..1 |
| 빌드 `builds` | 서비스·저장소·커밋으로 이미지 한 개 만들기 | 배포 기록 N(이미지 값만 복사) |
| 배포 기록 `deployments` | 연결에 이미지 하나를 올린 기록(기존) | 요청 1 |
| 할 일 `jobs` | 일꾼이 집어 갈 일 | 대상(요청 또는 서비스) 1 |

## 12. 리스크 (사전 부검, 지민 확인 D1)

| 리스크 | 처리 | 연결 |
|---|---|---|
| P1 배포가 영원히 '진행 중' | 요구사항화: 임대 만료 이어 가기 + 멱등 키 + 단계 제한 시간 + 3회 임대 상실 시 `WORKER_LOST` | FR-5, FR-14, AC-8, AC-9 |
| P2 예비 매장이 몰래 뒤처짐 | 요구사항화: `versionStatus`·`lastSyncError` 노출 | FR-15, AC-4 |
| P3 실제 실행기로 바꿀 때 안 맞음 | 요구사항화: `docs/executors.md` 계약(멱등 키 포함) + 인프라 요청(echo-infra#3·#5 댓글 초안, M1이 지민 승인 뒤) | §8.6, AC-17 |
| P4 GitHub App 개인키 노출 | 요구사항화: 참조만 저장, 0회 노출 시험 | FR-16, AC-13 |

## 13. 결정 기록

| ID | 결정 | 근거 | 출처 |
|---|---|---|---|
| D1 | 흐름·상태·계약은 실제, 실행만 가짜 실행기. 병합 뒤에도 이슈는 In Progress | 인프라 3 미완 | 사용자 R1 |
| D2 | 소스는 Git 저장소(주소 + 브랜치 또는 커밋)만 | 업로드·이미지 입력은 범위 밖 | 사용자 R2 |
| D3 | 빌드·배포 실행기 분리. 실제 연결안은 GitOps 커밋 + Argo 상태 읽기 | 되돌리기·이전이 배포만 다시 씀, 예선 검증 | 사용자 R3 |
| D4 | 할 일 표 + 별도 일꾼(SKIP LOCKED + 임대) | 재시작에도 일이 남음 | 사용자 R4 |
| D5 | 요청 1번 = 환경 1개. 이미 서비스 중인 환경이 있으면 새 환경은 예비(트래픽 0). 트래픽 전환은 API 8 | 킥오프는 동시 서비스를 요구하지 않음, 재난 대비 예비 환경의 준비 단계 | 사용자 R4·R5 |
| D6 | 서비스 등록 API 먼저, 연결은 첫 배포 때 자동 | Railway 방식 | 사용자 R4 |
| D7 | 헬스: 경로(기본 `/healthz`)·제한 시간(기본 300초), 2xx + 퍼블릭 URL 열림 | Railway + 예선 'Healthy인데 주소 안 열림' 대비 | 사용자 R4 |
| D8 | 대표 주소 + 환경별 주소 | 예비 점검 가능, 이전 뒤 주소 유지 | 사용자 R5 |
| D9 | 비공개 저장소 포함, GitHub App 설치 방식 | 비밀값 1개, 저장소별 허용, 1시간 토큰 | 사용자 R5·R6 |
| D10 | 같은 연결은 실행 1 + 대기 1, 새 요청이 대기 요청을 SUPERSEDED. 요청 순간 SHA 고정 | Render·Vercel·GitHub Actions 기본 동작 | 사용자 R5 |
| D11 | 예비는 서비스 중 연결의 성공을 같은 이미지로 자동 따라감 | 재난 때 옛 코드 + 새 스키마 DB 충돌 방지 | 사용자 R6 |
| D12 | 서비스 삭제 = 모든 환경에서 내리고 삭제 | 사용자 선택 | 사용자 R6 |
| AD-1~AD-12 | 체크포인트 목록 그대로(빌드 재사용, 상태 7개·취소 API 없음, 포트 8080, 주소 형식·저장, FAILED 환경 409, 접수 순간 사본, 폴링, `simulate` 가짜 모드만, 별도 일꾼·compose, 웹훅 제외, github.com만, 삭제 재시도) | 체크포인트 근거 | AI-default, 지민 확인 C0 |
| AD-13 | 가짜 결과에 `simulated: true`, 행에 저장 | 해커톤 목업 공개 규칙, API 2 선례, 실제 모드 전환 뒤에도 과거 기록 구분 | AI-default(SPEC 작성·검토 반영) |
| AD-14 | 실제 GitHub App 클라이언트·HTTP 헬스체커 구현(가짜 전송 시험), `httpx`·`PyJWT[crypto]` | 인프라와 무관하게 만들 수 있는 D9·D7의 핵심 | AI-default(SPEC 작성 중) |
| AD-15 | 커밋은 7~40자 받아 40자로 고정 | 짧은 SHA 입력 편의 | AI-default(SPEC 작성 중) |
| AD-16 | 마이그레이션은 서비스·연결 행 0개 가정, 있으면 명확히 실패 | 생성 API가 처음 생김 | AI-default(SPEC 작성 중) |
| AD-17 | `public_base_domain` 기본 `apps.echo.test` | `.test`는 예약 도메인 | AI-default(SPEC 작성 중) |
| AD-18 | 제한 시간 기본 빌드 900·배포 300·내리기 300초, 임대 30초, 헬스 재확인 5초 | 예선 빌드 수 분, 데모 시간 | AI-default(SPEC 작성 중) |
| AD-19 | 오류 메시지는 코드별 고정 문장 | 외부 응답 원문에 비밀값이 섞일 수 있음 | AI-default(SPEC 작성 중) |
| AD-20 | 서비스 이름은 바꿀 수 없음(422) | 주소가 이름에서 나옴 | AI-default(SPEC 작성 중) |
| AD-21 | 따라가기 요청도 예비 연결의 대기 수동 요청을 밀어낸다. 예비 연결에 다른 커밋 수동 배포는 허용(OUT_OF_SYNC로 보임) | D10(최신 요청 우선)·D11(예비는 서비스 중과 같게) | AI-default(검토 반영) |
| AD-22 | 잠금 순서 하나 + 조건부 UPDATE + 부분 unique 2개 + `lease_token` | 경쟁·교착 방지 | AI-default(검토 반영) |
| AD-23 | 외부 호출 멱등 키(우리 행 id)와 "요청함 → 호출 → 번호 저장" 순서, 번호 칸 추가 | 일꾼 재시작 시 중복 빌드·배포 방지 | AI-default(검토 반영) |
| AD-24 | 헬스체크는 일꾼 실행마다 환경별 퍼블릭 주소를 한 번 확인, 마감 뒤 마지막 결과로 코드 결정. 대표 주소 확인은 실제 라우터 연결 뒤 | 대표 주소는 성공 처리 뒤에야 이 환경을 가리킴(D7의 퍼블릭 URL = 환경별 퍼블릭 주소로 충족) | AI-default(검토 반영) |
| AD-25 | 대표 주소 라우터 실행기(가짜)를 두고, 라우터가 적용했다고 답할 때만 `traffic_weight`·`traffic_applied_at`을 적는다 | 칸 뜻("프록시에 적용한 비율")을 지키며 D5 표현 유지 | AI-default(검토 반영) |
| AD-26 | GitHub 오류는 `REPO_NOT_ACCESSIBLE`(404 + 설치 안내) 하나로 합치고, 제공자 장애는 502 `SOURCE_PROVIDER_UNAVAILABLE`. `private`는 GitHub 응답 값. 설치는 빌드 직전에 다시 조회 | GitHub가 '없음'과 '권한 없음'을 같은 404로 줌. D9의 설치 안내 의도 유지 | AI-default(검토 반영) |
| AD-27 | 배포 기록이 생긴 뒤의 모든 실패에서 되돌리기 호출, 결과는 `revert_status` | 반쯤 반영된 새 버전 방지 | AI-default(검토 반영) |
| AD-28 | `simulate.failAt=BUILDING`은 요청 단위 실패(공유 빌드 불변), `TEARDOWN` 값 제거(시험은 실행기 주입) | 공유 빌드 오염 방지 | AI-default(검토 반영) |

## 14. 구현 계획으로 위임한 low-impact 세부사항

- 모듈·클래스 이름, 단계 진행 코드 구조, 임대 연장 방식
- 가짜 실행기 내부 시간 계산, 가짜 digest·이미지 주소 문자열의 세부
- 목록 정렬의 2차 기준, 페이지 크기 기본값(API 2와 같게)
- 오류 코드별 고정 문장 문구
- compose `worker` 서비스 정의 방식(Dockerfile 재사용 여부)
- DELETE 금지 트리거를 전용 함수로 둘지 공용 함수로 둘지
