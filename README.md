# echo-was — Echo Control API

Team Echo 멀티 클라우드 관리 플랫폼의 API 서버(관리 평면)다.

## 시스템 내 위치

```text
[웹 콘솔(프론트)] ──HTTP(쿠키)──▶ [echo-was: 관리 평면 API] ──▶ PostgreSQL (조직·사람·환경·서비스·배포 요청·빌드·할 일)
[AI 에이전트] ──HTTP(Bearer 토큰, 읽기만)──▶ ┘
                                     │                        ▲
                                     │                        │ 할 일 폴링(SKIP LOCKED + 임대)
                                     │                 [echo-was 일꾼: python -m app.worker]
                                     │                        │
                                     │                        └─(실행기, 지금은 가짜)──▶ 빌드 · 배포 · 대표 주소 · 헬스체크 · 내리기 · 트래픽 비율 · 지표 · 복제 지연
                                     ├─(GitHub App)──▶ GitHub (로그인·저장소 접근·커밋 고정·읽기 토큰)
                                     └─(어댑터, API 서버 2~)──▶ AWS EKS · GCP GKE · 온프레 k3s
                                                                  ▲
                                         [고정 도메인 + L7 프록시(Envoy)] ── 트래픽 비율 적용 (이전 계획의 트래픽 실행기, 인프라 6)
```

- 사용자는 웹 콘솔에서 클러스터를 **환경**으로 등록하고, **서비스**(Git 저장소)를 등록한 뒤 **원터치 배포**(브랜치·커밋 → 빌드 → 배포 → 헬스체크)로 환경에 올리고, 클라우드 사이를 옮긴다.
- 사람은 **GitHub 로그인**으로 들어와 고객 회사 단위의 **조직** 멤버로서 역할(뷰어 < 개발자 < 어드민)만큼 일한다. 서비스·환경·배포는 조직에 속하고 다른 조직에는 보이지 않는다. AI 에이전트는 조직에서 발급한 토큰으로 그 조직을 **읽기만** 한다. 권한이 없어 막힌 요청은 기록한다.
- 이 서버는 그 기록(무엇이 어디에 어떤 버전으로 몇 % 돌고 있나)의 주인이다. 실제 클러스터 조작은 환경 종류별 어댑터와 실행기가 한다. **지금 어댑터와 실행기는 가짜(simulated)다** — 네트워크 호출 없이 정해진 결과를 돌려주고 응답에 `simulated: true` 를 단다.

## 구성

| 구성 요소 | 상태 | 설명 |
|---|---|---|
| API 골격 | 있음 | 공통 응답 봉투·예외·JSON 로깅·요청 ID·`/healthz`·`/readyz` |
| 공통 모델 | 있음 | `services`·`environments`·`service_environments`·`deployments` + `builds`·`deploy_requests`·`jobs` + `users`·`organizations`·`organization_members`·`invitations`·`sessions`·`agent_tokens`·`audit_logs` + `deploy_request_logs` + `migration_plans`·`migration_plan_logs`, 마이그레이션 7개 |
| 환경 설정 양식 | 있음 | `aws`·`gcp`·`onprem` 설정 검사(저장 전). 새 종류는 파일 하나 |
| 환경 API | 있음 | 등록·목록·단건·수정·삭제·다시 확인·종류 목록 ([docs/api/environments.md](docs/api/environments.md)) |
| 어댑터 동작 | 있음(가짜) | 접속 확인·클러스터 정보 읽기. 실제 EKS·GKE·k3s 접속은 클러스터가 생긴 뒤 별도 작업 |
| 비밀값 조회기 | 있음 | `secretRef` 형식 4종. `env:` 만 실제 조회, 나머지는 형식만 |
| 서비스 API | 있음 | 등록·목록·단건·수정·삭제(모든 환경에서 내린 뒤) ([docs/api/services.md](docs/api/services.md)) |
| 원터치 배포 API | 있음 | 접수·목록·단건. 서비스 중·예비, 예비 자동 따라가기, 실행 1 + 대기 1 ([docs/api/deploy-requests.md](docs/api/deploy-requests.md)) |
| 앱 환경 변수 | 있음(운영 보관함은 설정 전) | 서비스 × 환경 단위 쓰기 전용 API. 값은 GCP Secret Manager 에만 두고 배포 기록에 버전을 고정해 앱 Secret 으로 주입(`APP_SECRET_STORE_MODE`, 기본 `fake`) ([docs/api/variables.md](docs/api/variables.md)) |
| 배포 이력·되돌리기·로그 | 있음 | 이력 필터·`isCurrent`·`rollbackable`, 이력에서 고른 성공 버전으로 되돌리기(`trigger = ROLLBACK`, 예비 따라가기), 배포 작업 로그(DB)·빌드·실행 로그(실행기, 가짜) ([docs/api/deploy-requests.md](docs/api/deploy-requests.md)) |
| 일꾼 | 있음 | `python -m app.worker`. 할 일 표 + 임대 + 멱등 키로 복구 |
| 실행기(빌드·배포·대표 주소·헬스·내리기·트래픽·지표·데이터 확인) | 있음(가짜 + 실제) | 실제 구현: GitHub Actions 빌드 + 쿠버네티스 직접 반영·내리기(`EXECUTOR_MODE=real`, API 서버 3-2, 운영 기본값은 `fake`), HTTP 헬스체커, 고정 진입점 실행기(트래픽 비율·대표 주소·지표, `ENTRYPOINT_EXECUTOR_MODE=http`), 복제 지연 확인(`DATA_SYNC_CHECKER_MODE=postgres`, 인프라 7) ([docs/executors.md](docs/executors.md)) |
| 소스 저장소 클라이언트 | 있음 | 가짜(기본) + 실제 GitHub App(공개·비공개 저장소). 실제 App 생성·설치는 별도 승인 |
| GitHub 로그인·세션 | 있음 | GitHub App 사용자 인증 → httpOnly 세션 쿠키(DB 는 해시, 7일), 로그아웃, `/me`, 변경 요청 Origin 확인 ([docs/api/auth.md](docs/api/auth.md)). 실제 로그인은 App 설정 뒤 |
| 이전 계획 | 있음(트래픽·지표·복제는 가짜) | 계획 만들기(출발 100% → 같은 이미지의 예비, 고정 5단계·예상 시간·롤백 조건)·승인(다른 개발자, 조직 스위치로 자기 승인)·낡은 계획 거부(지문)·일꾼 단계 실행·자동 롤백·중단·실행 로그 ([docs/api/migration-plans.md](docs/api/migration-plans.md)) |
| 조직·역할 권한 | 있음 | 조직·멤버·초대·에이전트 토큰 API, 모든 라우트의 검사기, 403 거부 기록(`audit_logs`) ([docs/api/organizations.md](docs/api/organizations.md)) |
| 감사 로그 | 있음 | 쓰기 24개 경로 + 초대 적용(USER), 배포·이전·서비스 삭제의 끝(SYSTEM), 403 거부, AI 에이전트의 성공한 읽기(AGENT)를 누가·언제·무엇을·결과로 저장. 조직별 조회·거르기. 줄은 고치거나 지울 수 없다 ([docs/api/audit-logs.md](docs/api/audit-logs.md)) |

## 핵심 흐름

공통 모델 하나로 "서비스 하나가 여러 환경에 동시에" 있는 상태를 표현한다. 자세한 그림·행 예시·설계 이유는 [docs/schema.md](docs/schema.md).

```text
환경 등록   environments (kind + config + secret_ref) ── 설정 양식이 저장 전에 검사
   │
서비스 연결 service_environments (서비스 × 환경, 현재 배포, 트래픽 비율)
   │
배포       deployments (image_digest + config_snapshot + 상태) ── 성공하면 연결의 current 갱신
   │
되돌리기   새 deployments 행 (rollback_of_id = 원본, 이미지·설정 복사)
```

원터치 배포(API 서버 3)

```text
서비스 등록   POST /orgs/{orgId}/services (repoUrl 접근 확인, 대표 주소 {name}-{무작위 8자}.{도메인})
   │
배포 요청     POST /orgs/{orgId}/services/{id}/deploy-requests (환경 1개 + 브랜치·커밋 → 40자 SHA 고정, 202 QUEUED)
   │          └ 그 환경과 첫 요청이면 연결 생성(주소 {service}-{env}-{같은 8자}.{도메인}). 같은 연결의 대기 요청은 밀려남
일꾼          QUEUED → BUILDING(같은 커밋 빌드 재사용) → DEPLOYING(배포 기록) → VERIFYING(환경별 주소 헬스체크)
   │
성공 처리     현재 배포 교체 · 서비스 중 연결이 없으면 대표 주소를 이 연결로(비율 100) · 아니면 예비(비율 0)
   │          └ 서비스 중 연결이 새 버전을 받으면 예비 연결마다 STANDBY_SYNC 요청(같은 빌드)
실패          배포 기록이 생긴 뒤 실패면 되돌리기 → 직전 버전 유지(revertStatus)
```

이력·되돌리기·로그(API 서버 4)

```text
이력          GET /orgs/{orgId}/services/{id}/deploy-requests?environmentId&trigger&status (성공·실패·취소, isCurrent·rollbackable)
   │
되돌리기      POST /orgs/{orgId}/deploy-requests/{성공한 요청 id}/rollback (202, trigger=ROLLBACK, 새 빌드 없음)
   │          └ 같은 대기열·헬스체크. 시작 때 현재 배포가 접수 때와 다르면 CANCELLED(BASE_CHANGED)
성공          그 요청의 이미지·설정 사본으로 새 배포 기록(rollback_of_id) · 서비스 중 연결이면 예비도 따라감
실패          현재 배포·비율 그대로, revertStatus·errorCode·로그에 남김
   │
로그          GET /orgs/{orgId}/deploy-requests/{id}/logs?source=DEPLOY_JOB|BUILD|RUNTIME&cursor&limit
```

이전 계획(API 서버 8)

```text
계획 만들기   POST /orgs/{orgId}/services/{id}/migration-plans (도착 환경, 201 PENDING_APPROVAL)
   │          └ 출발 = 비율 100 연결 하나, 도착 = 같은 digest 의 예비. 열린 배포 요청이 있으면 409. 지문 저장
승인          POST /orgs/{orgId}/migration-plans/{id}/approve (다른 개발자, 202 APPROVED = 실행 시작. 지문 다르면 STALE)
   │
일꾼          0 사전 확인 → 1 비율 10% → 2 50% → 3 데이터 확인 → 4 100% (단계마다 적용 → 도착 요청 N건 세기(최대 30초) → 헬스체크 판정)
   │          └ 판정 실패·판단 불가·적용 실패·중단·일꾼 상실 → 출발 100% 로 롤백(ROLLED_BACK)
성공          도착이 서비스 중(100), 출발은 예비(0). 실행 로그 = 계획 단계 번호와 같은 번호
```

## 기술 스택

Python 3.13 · FastAPI · Pydantic v2 · SQLAlchemy 2.0(async) · PostgreSQL 17(asyncpg) · Alembic · uv · ruff · mypy(strict) · pytest(+pytest-asyncio)

## 디렉터리 구조

```text
app/
├─ main.py         # FastAPI 앱, /healthz·/readyz, 라우터 등록, 환경 종류 discover()
├─ routers/        # auth · organization · invitation · agent_token · environment · environment_kind · service · deploy_request · migration_plan · audit_log (/api/v1)
├─ audit/          # 감사 로그: 행동 이름표(actions) · 기록 함수(recorder) · 끝 줄(lifecycle) · 요청 값(context)
├─ auth/           # 권한 검사기(guards) · 거부·에이전트 읽기 감사 줄(audit) · 토큰·쿠키
├─ services/       # Environment*Service · ServiceService · DeployRequestService(접수·되돌리기·이력·로그) · Auth·Organization·Invitation·AgentToken Service
│                  #   DeployRequestProcessor · ServiceTeardownProcessor(일꾼 단계) · service_view(역할·버전 상태)
│                  #   deploy_job_log(배포 작업 로그 기록기) · deploy_log_messages(로그·오류 고정 문장)
│                  #   MigrationPlanService(이전 계획 API) · MigrationPlanProcessor(이전 일꾼 단계) · migration_plan_support(단계·지문)
│                  #   migration_plan_log · migration_log_messages · migration_guard(실행 중 접수 거절)
├─ repositories/   # Environment·Service·ServiceEnvironment·DeployRequest·DeployRequestLog·Build·Deployment·Job·MigrationPlan·MigrationPlanLog Repository
├─ clients/        # secret_client · source_repository(인터페이스 + 가짜) · github_source_repository(GitHub App) · github_user(로그인·초대)
├─ executors/      # 실행기 인터페이스(base, 빌드·실행 로그 읽기·트래픽·지표·데이터 확인 포함) · 가짜(fake) · HTTP 헬스체커 · 고정 진입점(entrypoint)
├─ worker/         # 일꾼 (python -m app.worker): 할 일 폴링·임대·종료 신호
├─ adapters/       # 환경 종류별 설정 양식 + 동작 클래스 (aws.py · gcp.py · onprem.py) + 레지스트리
├─ models/         # SQLAlchemy 모델 (표 17개)
├─ scripts/        # demo_seed (데모 조직·멤버·에이전트 토큰) · issue_runner_token (AI 러너 토큰 발급·철회)
├─ schemas/        # 공통 응답 봉투, 환경·서비스·배포 요청 API 요청·응답
├─ core/           # 설정·DB 엔진·예외·로깅·요청 ID 미들웨어·비밀값 타입·시계·저장소 주소·로그 마스킹
├─ dependencies.py # Depends 주입 함수(세션·서비스·어댑터·소스·GitHub 사용자 클라이언트·시계·로그용 실행기 묶음)
└─ enums.py        # 배포·배포 요청·빌드·할 일·서비스 상태와 코드
alembic/           # 마이그레이션 (공통 모델 표, 환경 접속 상태 칸, 원터치 배포 표·칸, 조직·로그인 표·칸, 주소 suffix 칸, 되돌리기 칸·배포 작업 로그 표, 이전 계획 표·자기 승인 칸)
scripts/           # dump_schema.sh (db-schema.sql 생성)
tests/             # 단위 테스트, adapter_contract.py·executor_contract.py(공통 계약), integration/ (실제 PostgreSQL)
docs/schema.md     # 스키마 설명 원본
docs/api/          # API 계약 문서 (auth · organizations · environments · services · variables · deploy-requests · migration-plans)
docs/executors.md  # 실행기 계약(멱등 키·호출 순서·실패 코드)
.claude/           # 규칙 문서·훅·write-pr 스킬
```

## 빠른 시작

준비물: [uv](https://docs.astral.sh/uv/), Docker. 저장소 루트에서:

```bash
uv sync                                  # 1. 의존성 설치 (.venv)
cp .env.example .env                     # 2. 로컬 설정 (개발용 더미 계정)
docker compose up -d --wait db           # 3. PostgreSQL 17 (localhost:55432), 접속 가능할 때까지 대기
uv run alembic upgrade head              # 4. 표 만들기
uv run --env-file .env uvicorn app.main:app   # 5. API 실행 → http://127.0.0.1:8000/readyz 가 204, /docs 에 Swagger
```

> **모든 업무 API 는 로그인이 필요하다.** 로그인은 GitHub 로그인 하나뿐이다(개발용 로그인 없음). GitHub App 의 로그인 설정(아래 "GitHub 로그인 설정")을 채우기 전에는 로그인·콜백이 503 `LOGIN_NOT_CONFIGURED` 이고, 업무 API 는 401 이다. 로컬에서 API 를 직접 불러 보려면 GitHub App 을 설정해 로그인하거나, 시험(`tests/integration/auth_support.py`)처럼 세션 행을 넣는다.

로컬에서 환경 비밀값 넣기 — API 는 비밀값을 받지 않고 위치(`secretRef`)만 받는다. `env:` 저장소는 **서버 프로세스 환경변수**만 읽으므로(`.env` 파일을 직접 읽지 않음) `--env-file` 로 실행한다.

```bash
echo 'ECHO_SECRET_TEAM_ECHO__AWS_SEOUL_KUBECONFIG=local-dummy-value' >> .env   # ECHO_SECRET_<조직 키>__ 로 시작
uv run --env-file .env uvicorn app.main:app
# 로그인한 브라우저 세션 쿠키(echo_session)와 허용 Origin 으로 부른다. 조직 키 = slug 대문자·`-`→`_`
curl -s -X POST localhost:8000/api/v1/orgs/<조직 id>/environments -H 'content-type: application/json' \
  -H 'Origin: http://localhost:3000' -b 'echo_session=<세션 쿠키>' \
  -d '{"name":"aws-seoul","kind":"aws","config":{"region":"ap-northeast-2","clusterName":"echo-eks"},
       "secretRef":"env:ECHO_SECRET_TEAM_ECHO__AWS_SEOUL_KUBECONFIG"}'
# → 201, connectionStatus=CONNECTED, clusterInfo.simulated=true (가짜 어댑터)
```

원터치 배포 해 보기 — 배포는 **일꾼**이 진행한다. 일꾼이 없으면 요청은 `QUEUED` 에 남는다.

```bash
uv run --env-file .env python -m app.worker        # 별도 터미널. 일꾼 (SIGTERM·Ctrl+C 면 지금 실행만 끝내고 멈춤)
# 또는 컨테이너로: docker compose --profile worker up -d worker

curl -s -X POST localhost:8000/api/v1/orgs/<조직 id>/services -H 'content-type: application/json' \
  -H 'Origin: http://localhost:3000' -b 'echo_session=<세션 쿠키>' \
  -d '{"name":"shop-api","repoUrl":"https://github.com/team-echo/shop-api"}'
# → 201, url=https://shop-api-a1b2c3d4.apps.echo.test (끝 8자는 무작위), simulated=true (가짜 소스 저장소)
curl -s -X POST localhost:8000/api/v1/orgs/<조직 id>/services/<서비스 id>/deploy-requests -H 'content-type: application/json' \
  -H 'Origin: http://localhost:3000' -b 'echo_session=<세션 쿠키>' \
  -d '{"environmentId":"<환경 id>","branch":"main"}'
# → 202 QUEUED. GET /api/v1/orgs/<조직 id>/deploy-requests/<요청 id> 를 폴링하면 몇 초 뒤 SUCCEEDED (가짜 실행기 단계당 2초)
```

되돌리기·로그 데모 순서(일꾼을 띄운 채로) — 같은 환경에 두 번 배포한 뒤 첫 번째로 되돌린다.

```bash
# 1) main 배포(v1) → 2) release 배포(v2). 각각 SUCCEEDED 가 될 때까지 폴링
# 3) 이력: v1 은 rollbackable=true, v2 는 isCurrent=true
curl -s 'localhost:8000/api/v1/orgs/<조직 id>/services/<서비스 id>/deploy-requests?environmentId=<환경 id>&status=SUCCEEDED' \
  -b 'echo_session=<세션 쿠키>'
# 4) v1 으로 되돌리기 (개발자 이상 사람만. 에이전트 토큰은 403 AGENT_READ_ONLY)
curl -s -X POST localhost:8000/api/v1/orgs/<조직 id>/deploy-requests/<v1 요청 id>/rollback \
  -H 'Origin: http://localhost:3000' -b 'echo_session=<세션 쿠키>'
# → 202 QUEUED, trigger=ROLLBACK, rollbackOfRequestId=<v1 요청 id>, build.reused=true
#   다시 하면 409 ALREADY_CURRENT. 일부러 실패: -d '{"simulate":{"failAt":"VERIFYING"}}' → 현재 배포 유지
# 5) 로그 (뷰어·에이전트도 읽는다)
curl -s 'localhost:8000/api/v1/orgs/<조직 id>/deploy-requests/<요청 id>/logs?source=DEPLOY_JOB' -b 'echo_session=<세션 쿠키>'
curl -s 'localhost:8000/api/v1/orgs/<조직 id>/deploy-requests/<요청 id>/logs?source=BUILD&limit=3' -b 'echo_session=<세션 쿠키>'
# → simulated=true (가짜 실행기 로그). 다음 페이지는 &cursor=<nextCursor>
```

이전 데모 순서(일꾼을 띄운 채로) — aws 에 서비스 중(100%), gcp 에 같은 커밋을 예비(0%)로 올린 뒤 옮긴다.

```bash
# 0) aws-seoul 에 배포(서비스 중 100%) → gcp-seoul 에 같은 브랜치 배포(예비 0%, versionStatus=IN_SYNC)
# 1) 계획 만들기 (개발자 A). 201 PENDING_APPROVAL, steps 5개, expectedSeconds=17(합성 트래픽 50 rps 기준)
curl -s -X POST localhost:8000/api/v1/orgs/<조직 id>/services/<서비스 id>/migration-plans \
  -H 'Origin: http://localhost:3000' -H 'Content-Type: application/json' -b 'echo_session=<A 의 쿠키>' \
  -d '{"targetEnvironmentId": "<gcp 환경 id>"}'
# 2) 승인 (개발자 B. A 가 승인하면 403 SELF_APPROVAL_FORBIDDEN — 어드민이 PATCH /settings {"allowSelfApproval": true} 로 허용 가능)
curl -s -X POST localhost:8000/api/v1/orgs/<조직 id>/migration-plans/<계획 id>/approve \
  -H 'Origin: http://localhost:3000' -b 'echo_session=<B 의 쿠키>'
# 3) 약 17초(합성 트래픽 50 rps 기준) 동안 단건을 1초 간격으로 폴링: currentStep 0→4, 비율 (90,10) → (50,50) → (0,100), SUCCEEDED
#    비율 단계마다 progress 에 "도착 처리 14/50건 · 5xx 0/허용 1" 이 보인다. 트래픽이 없으면 단계마다 30초 뒤 트래픽 적음으로 넘어간다
curl -s localhost:8000/api/v1/orgs/<조직 id>/migration-plans/<계획 id> -b 'echo_session=<쿠키>'
curl -s localhost:8000/api/v1/orgs/<조직 id>/migration-plans/<계획 id>/logs -b 'echo_session=<쿠키>'
# 4) 서비스 상세: gcp SERVING(100) · aws STANDBY(0, IN_SYNC), activeMigrationPlanId=null
# 자동 롤백 시연: 1) 에 "simulate": {"failAtStep": 2, "reason": "ERROR_RATE"} → 단계 2 첫 읽기에서 5xx 허용 초과 → ROLLED_BACK, 비율 (100,0)
#   (진입점이 비율을 안 따르는 상황은 "TARGET_NOT_RECEIVING", 트래픽 적음 통과는 "LOW_TRAFFIC")
# 중단 시연: 실행 중 POST …/migration-plans/<계획 id>/abort → ROLLED_BACK(errorCode=ABORTED)
```

- 이전 중(`APPROVED`·`RUNNING`·`ROLLING_BACK`)에는 그 서비스의 배포 요청·되돌리기·삭제가 409 `MIGRATION_IN_PROGRESS` 다. 예비가 아직 새 버전을 따라가는 중이면 계획 만들기는 409 `TARGET_SYNCING`(서비스 상세의 `queuedRequestId`·`activeRequestId`·`lastSyncSeconds` 로 보인다).
- **실제 반영 지연은 Argo CD 동기화가 정한다.** 우리 쪽 예비 따라가기는 원본 성공과 같은 시각에 할 일을 넣어 대기가 거의 없다(`lastSyncSeconds`). 실제 배포 실행기가 붙으면 Argo CD 의 폴링(기본 3분) 대신 **Git 웹훅으로 바로 동기화**하도록 인프라 3 에 요청한다.
- 트래픽 나눠 보내기·지표·복제 확인은 가짜(`simulated: true`)다. 실제 Envoy(인프라 6)·OpenTelemetry(인프라 9)·복제(인프라 7)가 붙기 전 운영(`api.echoport.uk`)은 비율 칸만 바뀐다.

가짜 모드 규칙(기본값): 저장소 owner 가 `private-` 면 비공개, `denied-`·`missing-` 면 404, `flaky-` 면 502. 브랜치가 `missing-` 로 시작하면 422 `REF_NOT_FOUND`. `"simulate": {"failAt": "BUILDING"|"DEPLOYING"|"VERIFYING"}` 로 그 단계를 일부러 실패시킬 수 있다(가짜 모드에서만).

실제 GitHub App 은 `Echoport Deploy`(https://github.com/apps/echoport-deploy, 조직 소유, App ID `5574944`, Client ID `Iv23lipNlpKaIxNuvR17`, 11/2 생성)이고, 조직에는 echo-was 저장소에만 설치돼 있다. **App 설정·설치 범위 변경은 별도 승인 뒤**에 한다. 서버에 두는 비밀값은 App 개인키 하나이고, 설정에는 비밀 저장소 위치만 둔다.

```bash
SOURCE_CLIENT_MODE=github
GITHUB_APP_ID=<App ID>
GITHUB_APP_PRIVATE_KEY_REF=env:ECHO_SECRET_GITHUB_APP_PRIVATE_KEY   # 값(PEM)은 프로세스 환경변수로만
GITHUB_APP_INSTALL_URL=https://github.com/apps/<app>/installations/new
```

### 데모 배포 주소

도메인 `echoport.uk`(11/2 구매, Cloudflare Registrar, 1년만 쓰고 자동 갱신 꺼 둠). 배포 환경 값은 `.env.example` 의 "데모 배포 주소" 블록에 있다.

| 용도 | 주소 | 설정 |
|---|---|---|
| 프론트 | `https://app.echoport.uk` | `FRONTEND_BASE_URL`, `FRONTEND_ALLOWED_ORIGINS` |
| API | `https://api.echoport.uk` | 같은 사이트(`echoport.uk`)라 세션 쿠키가 붙는다 |
| 고객 앱 대표 주소 | `https://{서비스}-{무작위 8자}.echoport.uk` (예 `shop-a1b2c3d4.echoport.uk`) | `PUBLIC_BASE_DOMAIN=echoport.uk` |
| 고객 앱 환경별 주소 | 클라우드(환경 config `ingressDomain` 있음) `https://{서비스}-{무작위 8자}.{ingressDomain}` (예 `shop-a1b2c3d4.aws-tokyo.echoport.uk`) · 온프레(없음) `https://{서비스}-{환경}-{무작위 8자}.echoport.uk` (예 `shop-onprem-a1b2c3d4.echoport.uk`) | 같은 설정 + 환경 config. 8자는 서비스마다 하나(대표 주소와 같은 값). 클라우드는 `*.{ingressDomain}` 와일드카드 DNS·인증서 |
| GitHub App 콜백 | `https://api.echoport.uk/api/v1/auth/github/callback` | `GITHUB_OAUTH_REDIRECT_URI` + App 의 Callback URL |
| 세션 쿠키 이름 | `__Host-echo_session` | `SESSION_COOKIE_NAME=__Host-echo_session` (`SESSION_COOKIE_SECURE=true` 필수) |

- **고객 앱 주소는 도메인 바로 아래 한 단계**다. DNS 레코드·인증서는 `*.echoport.uk` 하나로 모든 고객 앱을 덮는다(Cloudflare 무료 인증서도 한 단계까지만 덮는다). `app`·`api` 는 따로 둔 레코드가 와일드카드보다 앞선다. 고객 앱 레이블에는 늘 `-{8자}` 가 붙으므로 `app`·`api`·`www` 와 겹치지 않는다(예약 이름 없음).
- **무작위 8자**는 서비스를 만들 때 한 번 정해 `services.host_suffix` 에 저장한다. 지운 서비스와 같은 이름으로 새 서비스를 만들어도 옛 주소를 이어받지 못한다(하위 도메인 탈취 방지). 레이블이 63자를 넘으면 8자 앞부분을 54자로 자른다.
- **서비스 이름은 조직 안에서만 유일**하다(주소 유일성은 8자가 맡는다). [specs/api-3-1-public-host-format.md](specs/api-3-1-public-host-format.md) 가 [API 6 SPEC](specs/api-6-github-login-roles.md) 의 "서비스 이름은 Echo 전체 유일"을 바꾼다.
- **세션 쿠키 이름**: 고객 앱이 같은 등록 도메인(`echoport.uk`) 아래에 있으므로, 고객 앱 JavaScript 가 `Domain=echoport.uk` 쿠키로 우리 세션 쿠키를 덮어쓰지 못하게 데모 배포는 `__Host-echo_session` 을 쓴다(`__Host-` 쿠키는 Secure·`Path=/`·Domain 없음일 때만 저장된다. 서버는 세션 쿠키에 Domain 을 붙이지 않는다). 로컬 기본값은 `echo_session` 그대로다.

### GitHub 로그인 설정

로그인 앱은 API 서버 3 의 GitHub App(`Echoport Deploy`)을 그대로 쓴다(사용자 인증). Callback URL 에는 `https://api.echoport.uk/api/v1/auth/github/callback` 과 `http://localhost:8000/api/v1/auth/github/callback` 이 들어 있다. **App 설정 변경은 별도 승인 뒤**에 한다. 서버에 두는 비밀값은 client secret 하나이고 설정에는 비밀 참조만 둔다.

```bash
GITHUB_APP_CLIENT_ID=<App 의 Client ID>
GITHUB_APP_CLIENT_SECRET_REF=env:ECHO_SECRET_GITHUB_APP_CLIENT_SECRET   # 값은 프로세스 환경변수로만
GITHUB_OAUTH_REDIRECT_URI=https://api.echoport.uk/api/v1/auth/github/callback
FRONTEND_BASE_URL=https://app.echoport.uk
FRONTEND_ALLOWED_ORIGINS=https://app.echoport.uk        # 비면 쿠키로 인증한 쓰기 요청이 모두 403
# LOGIN_ALLOWED_GITHUB_ORG=<GitHub 조직>               # 설정하면 그 조직 active 멤버만 로그인
```

프론트는 `/api/v1/auth/github/login?next=/원래/경로` 로 보내고, 돌아오면 `GET /api/v1/me` 로 사용자와 조직(역할)을 읽는다(이때 대기 초대가 적용된다). 요청은 `credentials: "include"` 로 보낸다.

### 데모 전날 점검 목록

1. **콜백 주소**: `GITHUB_OAUTH_REDIRECT_URI` 가 **이 API 호스트의** `/api/v1/auth/github/callback` 이고, GitHub App 설정의 Callback URL 목록에 **글자까지 같게**(스킴·호스트·포트·끝 `/` 없음) 들어 있다. 상태 쿠키가 API 호스트에 내려가므로 다른 호스트면 `OAUTH_STATE_MISMATCH` 가 난다.
2. **client secret 참조**: `GITHUB_APP_CLIENT_SECRET_REF=env:ECHO_SECRET_GITHUB_APP_CLIENT_SECRET` 이고 그 환경변수가 API 프로세스에 들어 있다(`--env-file` 또는 배포 비밀). 없으면 콜백이 `GITHUB_UNAVAILABLE`.
3. **프론트 주소**: `FRONTEND_BASE_URL` 이 실제 프론트 주소이고, `FRONTEND_ALLOWED_ORIGINS` 에 브라우저가 보내는 Origin(스킴·호스트·포트)이 정확히 있다. HTTPS 가 아니면 `SESSION_COOKIE_SECURE=false` 로 둔다(로컬만).
4. **허용 스위치**를 쓸 때: App 이 그 GitHub 조직에 설치돼 있고 **Organization permissions → Members: Read** 권한이 있다. 없으면 모두 `LOGIN_NOT_ALLOWED`.
5. **로그인을 열기 전에** 데모 팀원이 각자 한 번 로그인한 뒤 `demo_seed` 를 실행해 `team-echo` 조직·역할·에이전트 토큰을 만든다(아래). `team-echo` 는 예약 slug 라 API 로는 만들 수 없다.
6. 출력된 에이전트 토큰으로 배포 요청을 보내 403 `AGENT_READ_ONLY` 와 `audit_logs` 한 줄을 확인한다(데모 거부 장면).
7. 방금 한 조작이 `GET /api/v1/orgs/{orgId}/audit-logs` 맨 위에 나오는지 본다(NFR-08). 대시보드 최근 활동은 `?includeReads=false&size=3` 으로 AI 읽기 줄을 뺀다.

### 데모 준비 명령

```bash
uv run --env-file .env python -m app.scripts.demo_seed --org team-echo --admin <GitHub 아이디> --viewer <GitHub 아이디>
```

조직이 없으면 만들고 `--admin` 을 ADMIN, `--viewer` 를 VIEWER 로 맞춘 뒤 에이전트 토큰 하나를 발급해 원문을 표준 출력에 한 번 쓴다. 두 번 돌려도 조직·멤버는 늘지 않는다. 로그인한 적 없는 아이디나 남이 만든 같은 slug 조직이면 실패한다. 자세한 내용은 [docs/api/organizations.md](docs/api/organizations.md#데모-준비-명령).

### AI 러너 토큰

샌드박스 VM 의 러너(echo-ai-agent)가 WAS 를 부를 토큰이다. 새 환경 변수는 없다(마감 15분·임대 60초는 코드 상수). 배포된 API 와 같은 DB 설정으로 어드민이 한 번 돌린다.

```bash
uv run --env-file .env python -m app.scripts.issue_runner_token --name echo-sandbox-runner    # 발급
uv run --env-file .env python -m app.scripts.issue_runner_token --revoke echo-sandbox-runner  # 철회(바로 401)
```

운영 WAS 파드 안에서는 `python -m app.scripts.issue_runner_token --name echo-sandbox-runner` 다. 출력된 원문은 **GCP Secret Manager 비밀 `echo-runner-token` 에만** 넣고(VM 서비스 계정에 그 비밀 읽기만 준다), 채팅·로그·`.env` 에 남기지 않는다. DB 에는 SHA-256 만 있어 다시 볼 수 없다. 이름이 있으면(철회한 이름 포함) 발급이 실패하므로 교체는 새 이름으로 한다. 계약은 [docs/agent-runner.md](docs/agent-runner.md), C-AI-1 을 실제로 판정하려면 `SECURITY_GATE_ENABLED=true` 가 필요하다(기본 꺼짐, 켜면 다른 필수 통제도 판정한다).

자주 쓰는 명령

```bash
uv run ruff check . && uv run ruff format --check .   # 린트·포맷
uv run mypy app                                       # 타입 검사 (strict)
uv run pytest                                         # 테스트, 기본 병렬 (DB 가 없으면 통합 테스트는 이유를 출력하고 건너뜀)
uv run pytest -n 0                                    # 테스트를 직렬로 (파일 하나만 돌리거나 디버깅할 때)
uv run alembic check                                  # 모델과 DB 차이 확인
scripts/dump_schema.sh                                # .claude/rules/db-schema.sql 다시 뽑기
docker compose down                                   # DB 끄기 (데이터는 볼륨에 남음, 지우려면 -v)
```

환경변수(`.env`)

| 이름 | 예 | 설명 |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://echo:echo@localhost:55432/echo` | 앱·Alembic 이 쓰는 DB |
| `TEST_DATABASE_URL` | `postgresql+asyncpg://echo:echo@localhost:55432/echo_test` | 통합 테스트 DB. 이름이 `_test` 로 끝나야 한다(테스트가 스키마를 지우고 다시 만든다). 실제로는 작업 폴더마다 `echo_<폴더 해시 8자>_test`(병렬이면 워커마다 `echo_<폴더 해시 8자>_<gwN>_test`)를 쓰고, 없으면 테스트가 만든다 |
| `LOG_LEVEL` | `INFO` | `DEBUG`·`INFO`·`WARNING`·`ERROR` |
| `ENVIRONMENT_CHECK_TIMEOUT_SECONDS` | `5.0` | 환경 접속 확인 전체 제한 시간(초) |
| `ENVIRONMENT_ADAPTER_MODE` | `simulated` | `real` 이면 환경 접속 확인이 실제 클러스터(`/version`·노드 수)를 읽는다. 기본은 고정 값(`simulated: true`) |
| `ECHO_SECRET_<조직 키>__<NAME>` | (로컬 더미 값) | `secretRef = env:ECHO_SECRET_<조직 키>__<NAME>` 이 가리키는 환경 접속 비밀값(조직 키 = slug 대문자·`-`→`_`). 프로세스 환경변수로만 읽는다 |
| `PUBLIC_BASE_DOMAIN` | `apps.echo.test`(로컬) · `echoport.uk`(데모) | 퍼블릭 주소 도메인(125자 이하). 대표 `{서비스}-{무작위 8자}.{도메인}`, 환경별 `{서비스}-{같은 8자}.{환경 ingressDomain}`(클라우드) 또는 `{서비스}-{환경}-{같은 8자}.{도메인}` |
| `EXECUTOR_MODE` | `fake` | 빌드·배포·내리기 실행기. `real` 이면 GitHub Actions 빌드 + 쿠버네티스 직접 반영(필수: GitHub App, `SOURCE_CLIENT_MODE=github`, `HEALTH_CHECKER_MODE=http`). 운영 기본값은 실환경 확인(AC-9) 전까지 `fake` |
| `EXECUTOR_AWS_ROLE_ARN`·`BUILD_GITHUB_REPOSITORY`·`BUILD_GITHUB_WORKFLOW`·`BUILD_GITHUB_REF` | (비움) · `Softbank-Hackathon-2026-Team-Echo/echo-was` · `customer-build.yml` · `main` | 실제 실행기: aws 기본 역할, 고객 앱 빌드 워크플로 |
| `IMAGE_REGISTRY_MIRRORS` | (비움) | `<AR 접두>=<ECR 접두>,…`. aws 환경은 이미지 주소를 ECR 로 바꾼다(같은 digest) |
| `CLOUDFLARE_API_TOKEN_REF`·`CLOUDFLARE_ZONE_ID` | `env:ECHO_SECRET_CLOUDFLARE_DNS_TOKEN` · (존 id) | 온프레 앱 주소 DNS 레코드(DNS 편집 권한만). 토큰은 비밀 참조만 |
| `ONPREM_PULL_CHECK_SECONDS`·`ONPREM_PULL_MIN_REMAINING_SECONDS`·`DEPLOY_IMAGE_PULL_GRACE_SECONDS`·`KUBE_REQUEST_TIMEOUT_SECONDS`·`GOOGLE_METADATA_BASE_URL` | `300`·`1800`·`30`·`15`·`http://metadata.google.internal` | 온프레 이미지 받기 자격 갱신, `ErrImagePull` 유예, 창구 요청 제한 시간, 메타데이터 서버 |
| `HEALTH_CHECKER_MODE` | `fake` | `fake`(늘 통과) · `http`(환경별 주소 + 헬스 경로를 실제 GET) |
| `ENTRYPOINT_EXECUTOR_MODE`·`ENTRYPOINT_BASE_URL`·`ENTRYPOINT_TOKEN_REF` | `fake` · `http://<진입점 내부 IP>:8080` · `env:ECHO_SECRET_ENTRYPOINT_TOKEN` | `http` 면 트래픽 비율·대표 주소·지표 실행기가 고정 진입점(Envoy 가중치 API)을 부른다. 토큰은 비밀 참조만 ([docs/executors.md](docs/executors.md)) |
| `ENTRYPOINT_ENVIRONMENT_MAP`·`ENTRYPOINT_REQUEST_TIMEOUT_SECONDS` | (비움) · `5` | 환경 id → 진입점 환경 이름(비면 환경 종류 그대로), 요청 제한 시간 |
| `APP_SECRET_STORE_MODE`·`APP_SECRET_PROJECT_ID`·`APP_SECRET_NAME_PREFIX` | `fake` · (비움) · `echo-app-` | 고객 앱 환경 변수 보관함. `gcp` 면 GCP Secret Manager(프로젝트 필수). `fake` + `EXECUTOR_MODE=real` 이면 쓰기 API 409, 실행기는 앱 Secret 을 건드리지 않는다 ([docs/executors.md](docs/executors.md)) |
| `DATA_SYNC_CHECKER_MODE`·`DATA_SYNC_DATABASES`·`DATA_SYNC_PASSWORD_REF` | `fake` · (비움) · `env:ECHO_SECRET_DATA_SYNC_PASSWORD` | `postgres` 면 이전 계획 데이터 확인이 출발 PostgreSQL 의 `pg_stat_replication` 복제 지연을 읽고, 도착이 이미 쓰기 담당이면 지연 0(`TARGET_IS_WRITER`)으로 통과한다. 매핑은 `<환경 id 또는 종류>=<host>:<port>/<구독 이름>` ([docs/executors.md](docs/executors.md)) |
| `DATA_SYNC_USER`·`DATA_SYNC_DATABASE`·`DATA_SYNC_SSL_MODE`·`DATA_SYNC_TIMEOUT_SECONDS` | `echo_monitor` · `postgres` · `require` · `3` | `pg_monitor` 권한 계정, 접속 DB, TLS(`require`·`verify-full`·`disable`), 제한 시간 |
| `CUTOVER_RUNS_DATABASE`·`CUTOVER_RESULTS_NOT_BEFORE`·`CUTOVER_RESULT_CACHE_SECONDS` | `app` · (없음) · `5` | 이전 계획 상세의 컷오버 결과(`result`). `postgres` 모드에서 도착 환경 고객 DB 의 `echo_ops.cutover_runs` 를 읽는다. DB 이름이 비면 끈다 ([docs/api/migration-plans.md](docs/api/migration-plans.md)) |
| `SOURCE_CLIENT_MODE` | `fake` | `fake`(owner 이름 규칙) · `github`(GitHub App, 위 설정 자리) |
| `GITHUB_APP_ID`·`GITHUB_APP_PRIVATE_KEY_REF`·`GITHUB_APP_INSTALL_URL`·`GITHUB_API_BASE_URL` | | GitHub App 설정. 개인키는 `env:ECHO_SECRET_…` 참조만 |
| `GITHUB_APP_CLIENT_ID`·`GITHUB_APP_CLIENT_SECRET_REF`·`GITHUB_OAUTH_REDIRECT_URI`·`FRONTEND_BASE_URL` | | GitHub 로그인 설정. 하나라도 비면 로그인·콜백만 503 |
| `FRONTEND_ALLOWED_ORIGINS` | `https://app.example` | 쉼표 구분. 쿠키 인증 쓰기 요청 허용 Origin + CORS. 비면 쓰기 403 |
| `SESSION_COOKIE_NAME`·`SESSION_COOKIE_SECURE`·`SESSION_TTL_SECONDS` | `echo_session`(로컬) · `__Host-echo_session`(데모)·`true`·`604800` | 세션 쿠키 이름·Secure·수명(7일) |
| `LOGIN_ALLOWED_GITHUB_ORG` | | 설정하면 그 GitHub 조직 active 멤버만 로그인 |
| `RESERVED_ORG_SLUGS` | `team-echo` | API 로 만들 수 없는 조직 slug(쉼표 구분) |
| `AGENT_TOKEN_DEFAULT_DAYS`·`AGENT_TOKEN_MAX_DAYS`·`INVITATION_TTL_SECONDS` | `30`·`90`·`604800` | 에이전트 토큰 기간, 초대 유효 기간 |
| `BUILD_TIMEOUT_SECONDS`·`DEPLOY_TIMEOUT_SECONDS`·`TEARDOWN_TIMEOUT_SECONDS` | `900`·`300`·`300` | 단계별 제한 시간 |
| `HEALTH_CHECK_INTERVAL_SECONDS` | `5` | 헬스체크 재확인 간격(요청 하나 제한 5초) |
| `FAKE_EXECUTOR_STEP_SECONDS` | `2.0` | 가짜 실행기가 한 작업을 끝내는 시간 |
| `WORKER_POLL_INTERVAL_SECONDS`·`WORKER_LEASE_SECONDS`·`WORKER_CONCURRENCY` | `1.0`·`30`·`1` | 일꾼 폴링 간격·임대 시간·동시 루프 수. 일꾼은 여러 개 띄워도 된다 |
| `MIGRATION_REQUIRED_REQUESTS_DEFAULT`·`MIGRATION_MAX_WAIT_SECONDS_DEFAULT`·`MIGRATION_EXPECTED_RPS` | `20,50,50`·`30`·`50` | 이전 비율 단계 필요 건수(10·50·100%)·단계 최대 대기(5~300)·예상 rps(예상 시간·가짜 카운터) |
| `MIGRATION_COUNT_POLL_SECONDS`·`MIGRATION_COUNTS_STALE_SECONDS`·`MIGRATION_P95_REFERENCE_WINDOW_SECONDS` | `1`·`5`·`30` | 카운터 읽기 주기·마감 때 오래된 읽기 기준·p95 참고 창 |
| `MIGRATION_STEP_OVERHEAD_SECONDS`·`MIGRATION_PLAN_APPROVAL_TTL_SECONDS`·`MIGRATION_ROLLBACK_MAX_ATTEMPTS` | `2`·`86400`·`3` | 단계 여유 시간·승인 대기 만료·롤백 최대 시도 |
| `SECURITY_GATE_ENABLED`·`WAS_VERSION` | `false`·(없음) | 보안 게이트(이전 승인·일꾼 시작 판정, 실행 직전 TLS 시험. API·일꾼 같은 값)·판정 범위의 WAS 버전([docs/api/migration-plans.md](docs/api/migration-plans.md) "보안 게이트") |

`echo`/`echo` 는 로컬 compose 용 더미 계정이다. 실제 비밀값은 커밋하지 않는다.

## 인터페이스 요약

| 메서드 | 경로 | 응답 | 설명 |
|---|---|---|---|
| GET | `/healthz` | 204 | 프로세스 생존만 본다(DB 와 무관) |
| GET | `/readyz` | 204 / 503 | DB `SELECT 1` 이 2초 안에 성공하면 204, 아니면 503 |
| GET | `/api/v1/auth/github/login?next` · `/api/v1/auth/github/callback` | 302 | GitHub 로그인 시작·콜백(공개) |
| POST | `/api/v1/auth/logout` | 204 | 로그아웃(공개) |
| GET | `/api/v1/me` | 200 | 나 + 내 조직·역할(대기 초대 적용) |
| POST · GET | `/api/v1/orgs` | 201 · 200 | 조직 만들기(만든 사람 어드민) · 내 조직 목록 |
| GET | `/api/v1/orgs/{orgId}` | 200 | 조직 하나 |
| GET · PATCH · DELETE | `/api/v1/orgs/{orgId}/members[/{userId}]` | 200 · 200 · 204 | 멤버 목록 · 역할 변경(어드민) · 내보내기/탈퇴 |
| POST · GET · DELETE | `/api/v1/orgs/{orgId}/invitations[/{id}]` | 201 · 200 · 204 | 초대(어드민, GitHub 아이디 + 역할) |
| POST · GET · DELETE | `/api/v1/orgs/{orgId}/agent-tokens[/{id}]` | 201 · 200 · 204 | 에이전트 토큰(개발자, 원문은 발급 때 한 번) |
| POST | `/api/v1/orgs/{orgId}/environments` | 201 | 환경 등록 + 접속 확인(실패해도 201) |
| GET | `/api/v1/orgs/{orgId}/environments` | 200 | 목록(저장 값만, `page`·`size`·`kind`) |
| GET | `/api/v1/orgs/{orgId}/environments/{id}` | 200 | 단건 + 그 자리에서 읽은 클러스터 정보 |
| PATCH | `/api/v1/orgs/{orgId}/environments/{id}` | 200 | name·config·secretRef 수정(kind 변경 422) |
| DELETE | `/api/v1/orgs/{orgId}/environments/{id}` | 204 | 소프트 삭제(사용 중이면 409, 어드민) |
| POST | `/api/v1/orgs/{orgId}/environments/{id}/connection-check` | 200 | 접속 다시 확인 |
| GET | `/api/v1/environment-kinds` | 200 | 등록된 종류 + config JSON Schema(로그인만) |
| POST | `/api/v1/orgs/{orgId}/services` | 201 | 서비스 등록(저장소 접근 확인) |
| GET | `/api/v1/orgs/{orgId}/services` | 200 | 서비스 목록(`page`·`size`) |
| GET | `/api/v1/orgs/{orgId}/services/{id}` | 200 | 서비스 + 연결별 역할·버전 상태·주소 |
| PATCH | `/api/v1/orgs/{orgId}/services/{id}` | 200 | 설명·저장소·헬스·포트 수정(이름 변경 422) |
| DELETE | `/api/v1/orgs/{orgId}/services/{id}` | 202 | 모든 환경에서 내린 뒤 소프트 삭제(어드민) |
| POST | `/api/v1/orgs/{orgId}/services/{id}/deploy-requests` | 202 | 원터치 배포 접수(개발자, 환경 1개 + 브랜치·커밋) |
| GET | `/api/v1/orgs/{orgId}/services/{id}/deploy-requests` | 200 | 배포 이력(최신순, `environmentId`·`trigger`·`status`, `isCurrent`·`rollbackable`) |
| GET | `/api/v1/orgs/{orgId}/deploy-requests/{id}` | 200 | 배포 요청 진행 상황 |
| POST | `/api/v1/orgs/{orgId}/deploy-requests/{id}/rollback` | 202 | 이력에서 고른 성공 버전으로 되돌리기(개발자, 사람만) |
| GET | `/api/v1/orgs/{orgId}/deploy-requests/{id}/logs` | 200 | 배포 작업·빌드·실행 로그(`source`·`cursor`·`limit`) |
| PATCH | `/api/v1/orgs/{orgId}/settings` | 200 | 조직 설정(자기 승인 스위치 `allowSelfApproval`, 어드민·사람) |
| POST · GET | `/api/v1/orgs/{orgId}/services/{id}/migration-plans` | 201 · 200 | 이전 계획 만들기(개발자·사람) · 목록(최신순, `status`) |
| GET | `/api/v1/orgs/{orgId}/migration-plans/{id}` | 200 | 계획 단건(단계별 계획 값과 실제 결과) |
| POST | `/api/v1/orgs/{orgId}/migration-plans/{id}/approve·reject·cancel·abort` | 202·200·200·202 | 승인(= 실행 시작)·거절·취소·중단(개발자·사람) |
| GET | `/api/v1/orgs/{orgId}/migration-plans/{id}/logs` | 200 | 이전 실행 로그(`cursor`·`limit`) |

- 모든 JSON 응답은 `{"success", "code", "message", "data", "details"}` 봉투다. 봉투의 `code`·`message`·`details` 는 값이 없으면 빼고, **성공 응답의 `data` 안 칸은 항상 있고 값이 없으면 null** 이다. 오류도 같은 봉투다(`data` 없음).
- 다른 파트는 환경을 `id` 로만 가리킨다(`name` 은 바뀔 수 있다). 자세한 계약: [docs/api/environments.md](docs/api/environments.md).
- 권한: 조회는 뷰어(에이전트 토큰 가능), 등록·수정·배포·토큰은 개발자, 삭제·초대·역할 변경은 어드민. 남의 조직은 404, 역할이 모자라면 403(`audit_logs` 에 기록). 역할 표·검사 순서: [docs/api/auth.md](docs/api/auth.md).
- 모든 응답에 `X-Request-ID` 헤더가 붙는다. 요청에 형식이 맞는 `X-Request-ID` 가 있으면 그대로 이어 쓴다.
- 로그는 stdout 에 JSON 한 줄씩(`component=echo-api`, `request_id` 포함).

## 배포

- `Dockerfile`: uv 멀티스테이지, 비루트 UID 1001, 기본 명령 `uvicorn app.main:app --host 0.0.0.0 --port 8000`. 일꾼은 같은 이미지로 명령만 `python -m app.worker` 로 바꿔 띄운다(`compose.yaml` 의 `worker` 서비스, `--profile worker`). 일꾼은 HTTP 포트가 없다.
- **일꾼과 API 는 같은 이미지로 함께 올린다.** 일꾼만 옛 코드면 되돌리기(`trigger = ROLLBACK`) 요청을 처리하지 못하고, API 만 옛 코드면 배포 작업 로그가 빠진다. 마이그레이션(head `4cea1ec99f1b`)을 먼저 적용한다.
- **옛 일꾼(API 서버 8 전)이 `MIGRATION_PLAN` 할 일을 집으면** 모르는 종류를 내리기 처리기로 보내 조용히 DONE 으로 닫는다. 그러면 계획이 `APPROVED` 에 남고 그 서비스가 `MIGRATION_IN_PROGRESS` 로 막힌다. 이때는 **중단(abort)** 이 그 자리에서 `CANCELLED` 로 풀어 준다. 새 일꾼은 모르는 종류를 예외로 올린다.
- 마이그레이션은 **앱 시작 때 돌리지 않는다.** 같은 이미지로 `alembic upgrade head` 를 한 번 따로 실행한다. 클러스터에서 어떻게 실행할지는 인프라 파트와 정한다.
- 프로브: liveness `/healthz`, readiness `/readyz`.
- CI 워크플로는 아직 없다(팀 회의에서 정한다).

## 현재 상태와 한계

- 업무 API 는 로그인·조직·환경·서비스·배포 요청 API 다. **실제 GitHub 로그인은 GitHub App 설정 뒤에만 된다**(시험은 가짜 GitHub 를 의존성 교체로 쓴다). API 2·3 경로는 모두 `/api/v1/orgs/{orgId}` 아래로 옮겼다(깨지는 변경, 프론트 미연결).
- **알려진 제한 — GitHub App 설치가 조직에 묶이지 않는다.** App 이 설치된 비공개 저장소는 다른 Echo 조직도 서비스로 등록할 수 있다(해커톤 데모 범위에서 감수).
- **이전 계획의 트래픽·지표·복제 확인은 가짜다**(`simulated: true`). 비율 칸·로그·판정 흐름은 실제이고, 실제 Envoy(인프라 6, 시연용은 늦으면 지민)·OpenTelemetry(인프라 9)·복제(인프라 7)가 붙으면 실행기만 바꾼다. 실제 연결 확인 전에는 이슈 #8 을 `In Progress` 로 둔다. 판정 최소 표본 수는 없다(실제 지표 연결 때 다시 본다). 예비·주력 동시 배포, 단계별 사람 승인, 비율 목록 변경은 하지 않는다.
- **AI 사전 점검 API·러너 계약은 있지만 러너 쪽은 별도다.** 러너 스크립트는 echo-ai-agent 에 있고, 러너 토큰은 배포 뒤 발급한다. 패치 승인·거절·`APPLY_PATCH`, AI 결과의 `findings` 표 저장, `blockedAttempts` 와 감사 줄 맞추기, 계획 만들 때 최신 점검 필수화, 주기 만료(일꾼 루프)는 2차다. 실행 시간 초과는 누가 부를 때 저장한다([docs/agent-runner.md](docs/agent-runner.md)). 데이터 레지던시 기록은 러너 신고 그대로이고(WAS 가 재지 않는다) 정책은 서버 상수뿐이다. 조직별 정책 저장과 게이트 연동은 후속이다([docs/api/ai-residency.md](docs/api/ai-residency.md)).
- **감사 로그의 범위**: 실시간 알림·보관 기간·내보내기·외부 수집기는 없다. 배포 요청 표·이력 응답에는 요청자 칸이 없고, 누가 눌렀는지는 감사 로그로 읽는다(`targetId`). 롤아웃 수 분 동안 옛 파드가 처리한 작업은 성공 줄이 없을 수 있다. 운영 스키마는 forward-only 다([docs/api/audit-logs.md](docs/api/audit-logs.md)).
- **빌드·실행 로그는 기본(가짜 모드)에서 가짜 실행기가 만든 줄이다**(`simulated: true`). `EXECUTOR_MODE=real` 이면 Actions 잡 로그·앱 파드 로그를 즉석으로 읽는다(실행기 묶음은 API 프로세스당 하나). 가짜로 만든 기록을 실제 모드에서 읽으면 빈 페이지다. 로그 실시간 스트리밍·검색·보존 기간 정책은 없다.
- 조직 삭제 API·조직 안 프로젝트(2단)는 없다.
- 어댑터는 **가짜**다. 접속 확인·클러스터 정보는 고정 값이고 `simulated: true` 로 표시한다. 실제 EKS·GKE·k3s 접속은 클러스터가 생긴 뒤 별도 작업에서 같은 인터페이스·계약 검사로 바꾼다.
- **원터치 배포의 실제 실행기는 있지만 운영 기본값은 가짜다.** `EXECUTOR_MODE=real` 이면 GitHub Actions 빌드(워크플로 본문은 인프라 담당) + EKS·GKE·온프레 k3s 직접 반영이다(API 서버 3-2, GitOps 는 2단계). 저장소 밖 준비(빌드 워크플로·공유 인그레스·인증서·DNS·App 권한·AR 읽기 권한)와 실환경 확인(SPEC AC-9) 전까지 `fake` 로 둔다. 이 이슈(#3)는 실환경 확인 뒤 닫는다. 실제 실행기는 앱 네임스페이스 밖을 쓰지 않지만, GKE·EKS 신원 자체는 넓은 권한이라 이 제한은 코드가 지킨다.
- 대표 주소의 실제 헬스 확인은 실제 라우터 연결 뒤에 더한다(지금은 환경별 주소만 확인). 트래픽 전환은 이전 계획(API 서버 8)이 한다.
- 일꾼이 없으면 배포 요청은 `QUEUED` 에 남는다. `/readyz` 는 일꾼을 보지 않는다.
- 비밀값은 `env:` 저장소만 실제로 읽는다. `k8s:`·`aws-sm:`·`gcp-sm:` 은 형식만 받고 접속 확인은 `SECRET_STORE_UNSUPPORTED` 로 실패한다.
- 접속 확인은 등록·다시 확인·config/secretRef 수정 때만 한다. 자동 주기 확인은 없다(API 서버 5).
- 배포·배포 요청 상태 **값**만 DB 가 막는다. 상태 전환 규칙은 코드(조건부 UPDATE)가 지킨다.
- `environments` 를 Core·raw SQL 로 쓰면 설정 검사를 건너뛴다(규칙으로 금지, DB 는 막지 않음).
- 서비스별 트래픽 비율 합계는 DB 가 검사하지 않는다. 이전 계획은 실행기가 적용했다고 답했을 때만 두 연결을 한 트랜잭션에서 함께 적어 합계 100 을 지킨다.

## 문서

| 문서 | 내용 |
|---|---|
| [docs/schema.md](docs/schema.md) | 공통 모델 스키마 설명(ER·칸·행 예시·상태 흐름·설계 이유·새 환경 추가·조직 표) |
| [docs/api/auth.md](docs/api/auth.md) | 로그인 흐름·쿠키·Origin·권한 검사 순서·역할 표·거부 기록·오류 |
| [docs/api/organizations.md](docs/api/organizations.md) | 조직·멤버·초대·에이전트 토큰 API, 조직 열쇠 이름, 데모 준비 명령 |
| [docs/api/audit-logs.md](docs/api/audit-logs.md) | 감사 로그 조회·행동 이름표·결과 규칙·`component` 값·기록하지 않는 것·forward-only |
| [docs/api/environments.md](docs/api/environments.md) | 환경 API 계약(엔드포인트·스키마·오류 코드·secretRef 형식·null 규칙) |
| [docs/api/services.md](docs/api/services.md) | 서비스 API 계약(등록·수정·삭제, 역할·버전 상태, `simulated` 뜻) |
| [docs/api/deploy-requests.md](docs/api/deploy-requests.md) | 원터치 배포·이력·되돌리기·로그 API 계약(접수 규칙·진행 단계·실패 코드·로그 줄) |
| [docs/api/migration-plans.md](docs/api/migration-plans.md) | 이전 계획 API 계약(만들기 거절 순서·승인·지문·단계 실행·롤백·중단·로그·`simulate`) |
| [docs/api/prechecks.md](docs/api/prechecks.md) · [docs/agent-runner.md](docs/agent-runner.md) | AI 사전 점검 API(사람 쪽) · 러너 계약(토큰·임대·이벤트·`RunResult`·감사·발급 명령) |
| [docs/api/ai-residency.md](docs/api/ai-residency.md) | AI 데이터 레지던시 조직 요약 API(계산 표·민감 등급 판정·정책 위반·기본 정책) |
| [docs/executors.md](docs/executors.md) | 실행기 계약(멱등 키·호출 순서·실패 코드·로그 읽기·마스킹·가짜 구현) |
| [CLAUDE.md](CLAUDE.md) · [AGENTS.md](AGENTS.md) | AI 도구용 작업 규칙 |
| [.claude/rules/](.claude/rules/) | 용어 사전·DB 스키마(DDL)·마이그레이션·백엔드·Git 규칙 |
| [specs/](specs/) | 태스크별 SPEC |
