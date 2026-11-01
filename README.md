# echo-was — Echo Control API

Team Echo 멀티 클라우드 관리 플랫폼의 API 서버(관리 평면)다.

## 시스템 내 위치

```text
[웹 콘솔(프론트)] ──HTTP──▶ [echo-was: 관리 평면 API] ──▶ PostgreSQL (환경·서비스·배포 요청·빌드·할 일)
                                     │                        ▲
                                     │                        │ 할 일 폴링(SKIP LOCKED + 임대)
                                     │                 [echo-was 일꾼: python -m app.worker]
                                     │                        │
                                     │                        └─(실행기, 지금은 가짜)──▶ 빌드 · 배포 · 대표 주소 · 헬스체크 · 내리기
                                     ├─(GitHub App)──▶ GitHub (저장소 접근·커밋 고정·읽기 토큰)
                                     └─(어댑터, API 서버 2~)──▶ AWS EKS · GCP GKE · 온프레 k3s
                                                                  ▲
                                         [고정 도메인 + L7 프록시(Envoy)] ── 트래픽 비율 적용 (이전 엔진)
```

- 사용자는 웹 콘솔에서 클러스터를 **환경**으로 등록하고, **서비스**(Git 저장소)를 등록한 뒤 **원터치 배포**(브랜치·커밋 → 빌드 → 배포 → 헬스체크)로 환경에 올리고, 클라우드 사이를 옮긴다.
- 이 서버는 그 기록(무엇이 어디에 어떤 버전으로 몇 % 돌고 있나)의 주인이다. 실제 클러스터 조작은 환경 종류별 어댑터와 실행기가 한다. **지금 어댑터와 실행기는 가짜(simulated)다** — 네트워크 호출 없이 정해진 결과를 돌려주고 응답에 `simulated: true` 를 단다.

## 구성

| 구성 요소 | 상태 | 설명 |
|---|---|---|
| API 골격 | 있음 | 공통 응답 봉투·예외·JSON 로깅·요청 ID·`/healthz`·`/readyz` |
| 공통 모델 | 있음 | `services`·`environments`·`service_environments`·`deployments` + `builds`·`deploy_requests`·`jobs`, 마이그레이션 3개 |
| 환경 설정 양식 | 있음 | `aws`·`gcp`·`onprem` 설정 검사(저장 전). 새 종류는 파일 하나 |
| 환경 API | 있음 | 등록·목록·단건·수정·삭제·다시 확인·종류 목록 ([docs/api/environments.md](docs/api/environments.md)) |
| 어댑터 동작 | 있음(가짜) | 접속 확인·클러스터 정보 읽기. 실제 EKS·GKE·k3s 접속은 클러스터가 생긴 뒤 별도 작업 |
| 비밀값 조회기 | 있음 | `secretRef` 형식 4종. `env:` 만 실제 조회, 나머지는 형식만 |
| 서비스 API | 있음 | 등록·목록·단건·수정·삭제(모든 환경에서 내린 뒤) ([docs/api/services.md](docs/api/services.md)) |
| 원터치 배포 API | 있음 | 접수·목록·단건. 서비스 중·예비, 예비 자동 따라가기, 실행 1 + 대기 1 ([docs/api/deploy-requests.md](docs/api/deploy-requests.md)) |
| 일꾼 | 있음 | `python -m app.worker`. 할 일 표 + 임대 + 멱등 키로 복구 |
| 실행기(빌드·배포·대표 주소·헬스·내리기) | 있음(가짜) | 실제 빌드·배포·라우터·내리기는 인프라 3·5 이후 실행기만 교체 ([docs/executors.md](docs/executors.md)). HTTP 헬스체커는 실제 구현 있음 |
| 소스 저장소 클라이언트 | 있음 | 가짜(기본) + 실제 GitHub App(공개·비공개 저장소). 실제 App 생성·설치는 별도 승인 |

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
서비스 등록   POST /services (repoUrl 접근 확인, 대표 주소 {name}.{도메인})
   │
배포 요청     POST /services/{id}/deploy-requests (환경 1개 + 브랜치·커밋 → 40자 SHA 고정, 202 QUEUED)
   │          └ 그 환경과 첫 요청이면 연결 생성(주소 {service}.{env}.{도메인}). 같은 연결의 대기 요청은 밀려남
일꾼          QUEUED → BUILDING(같은 커밋 빌드 재사용) → DEPLOYING(배포 기록) → VERIFYING(환경별 주소 헬스체크)
   │
성공 처리     현재 배포 교체 · 서비스 중 연결이 없으면 대표 주소를 이 연결로(비율 100) · 아니면 예비(비율 0)
   │          └ 서비스 중 연결이 새 버전을 받으면 예비 연결마다 STANDBY_SYNC 요청(같은 빌드)
실패          배포 기록이 생긴 뒤 실패면 되돌리기 → 직전 버전 유지(revertStatus)
```

## 기술 스택

Python 3.13 · FastAPI · Pydantic v2 · SQLAlchemy 2.0(async) · PostgreSQL 17(asyncpg) · Alembic · uv · ruff · mypy(strict) · pytest(+pytest-asyncio)

## 디렉터리 구조

```text
app/
├─ main.py         # FastAPI 앱, /healthz·/readyz, 라우터 등록, 환경 종류 discover()
├─ routers/        # environment_router · environment_kind_router · service_router · deploy_request_router (/api/v1)
├─ services/       # Environment*Service · ServiceService · DeployRequestService(접수)
│                  #   DeployRequestProcessor · ServiceTeardownProcessor(일꾼 단계) · service_view(역할·버전 상태)
├─ repositories/   # Environment·Service·ServiceEnvironment·DeployRequest·Build·Deployment·Job Repository
├─ clients/        # secret_client · source_repository(인터페이스 + 가짜) · github_source_repository(GitHub App)
├─ executors/      # 실행기 인터페이스(base) · 가짜(fake) · HTTP 헬스체커
├─ worker/         # 일꾼 (python -m app.worker): 할 일 폴링·임대·종료 신호
├─ adapters/       # 환경 종류별 설정 양식 + 동작 클래스 (aws.py · gcp.py · onprem.py) + 레지스트리
├─ models/         # SQLAlchemy 모델 (표 7개)
├─ schemas/        # 공통 응답 봉투, 환경·서비스·배포 요청 API 요청·응답
├─ core/           # 설정·DB 엔진·예외·로깅·요청 ID 미들웨어·비밀값 타입·시계·저장소 주소
├─ dependencies.py # Depends 주입 함수(세션·서비스·어댑터·소스 클라이언트·시계·권한 자리)
└─ enums.py        # 배포·배포 요청·빌드·할 일·서비스 상태와 코드
alembic/           # 마이그레이션 (공통 모델 표, 환경 접속 상태 칸, 원터치 배포 표·칸)
scripts/           # dump_schema.sh (db-schema.sql 생성)
tests/             # 단위 테스트, adapter_contract.py·executor_contract.py(공통 계약), integration/ (실제 PostgreSQL)
docs/schema.md     # 스키마 설명 원본
docs/api/          # API 계약 문서 (environments · services · deploy-requests)
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

> **인터넷에 열지 않는다.** 환경 API 에는 아직 인증이 없다(권한 검사 자리만 있음, API 서버 6 에서 GitHub 로그인 + 역할). `127.0.0.1` 기본 바인딩 그대로 팀 내부에서만 실행한다.

로컬에서 환경 비밀값 넣기 — API 는 비밀값을 받지 않고 위치(`secretRef`)만 받는다. `env:` 저장소는 **서버 프로세스 환경변수**만 읽으므로(`.env` 파일을 직접 읽지 않음) `--env-file` 로 실행한다.

```bash
echo 'ECHO_SECRET_AWS_SEOUL_KUBECONFIG=local-dummy-value' >> .env   # 이름은 ECHO_SECRET_ 로 시작
uv run --env-file .env uvicorn app.main:app
curl -s -X POST localhost:8000/api/v1/environments -H 'content-type: application/json' \
  -d '{"name":"aws-seoul","kind":"aws","config":{"region":"ap-northeast-2","clusterName":"echo-eks"},
       "secretRef":"env:ECHO_SECRET_AWS_SEOUL_KUBECONFIG"}'
# → 201, connectionStatus=CONNECTED, clusterInfo.simulated=true (가짜 어댑터)
```

원터치 배포 해 보기 — 배포는 **일꾼**이 진행한다. 일꾼이 없으면 요청은 `QUEUED` 에 남는다.

```bash
uv run --env-file .env python -m app.worker        # 별도 터미널. 일꾼 (SIGTERM·Ctrl+C 면 지금 실행만 끝내고 멈춤)
# 또는 컨테이너로: docker compose --profile worker up -d worker

curl -s -X POST localhost:8000/api/v1/services -H 'content-type: application/json' \
  -d '{"name":"shop-api","repoUrl":"https://github.com/team-echo/shop-api"}'
# → 201, url=https://shop-api.apps.echo.test, simulated=true (가짜 소스 저장소)
curl -s -X POST localhost:8000/api/v1/services/<서비스 id>/deploy-requests -H 'content-type: application/json' \
  -d '{"environmentId":"<환경 id>","branch":"main"}'
# → 202 QUEUED. GET /api/v1/deploy-requests/<요청 id> 를 폴링하면 몇 초 뒤 SUCCEEDED (가짜 실행기 단계당 2초)
```

가짜 모드 규칙(기본값): 저장소 owner 가 `private-` 면 비공개, `denied-`·`missing-` 면 404, `flaky-` 면 502. 브랜치가 `missing-` 로 시작하면 422 `REF_NOT_FOUND`. `"simulate": {"failAt": "BUILDING"|"DEPLOYING"|"VERIFYING"}` 로 그 단계를 일부러 실패시킬 수 있다(가짜 모드에서만).

실제 GitHub App(비공개 저장소) 설정 자리 — **App 생성·조직 설치는 별도 승인 뒤**에 한다. 서버에 두는 비밀값은 App 개인키 하나이고, 설정에는 비밀 저장소 위치만 둔다.

```bash
SOURCE_CLIENT_MODE=github
GITHUB_APP_ID=<App ID>
GITHUB_APP_PRIVATE_KEY_REF=env:ECHO_SECRET_GITHUB_APP_PRIVATE_KEY   # 값(PEM)은 프로세스 환경변수로만
GITHUB_APP_INSTALL_URL=https://github.com/apps/<app>/installations/new
```

자주 쓰는 명령

```bash
uv run ruff check . && uv run ruff format --check .   # 린트·포맷
uv run mypy app                                       # 타입 검사 (strict)
uv run pytest                                         # 테스트 (DB 가 없으면 통합 테스트는 이유를 출력하고 건너뜀)
uv run alembic check                                  # 모델과 DB 차이 확인
scripts/dump_schema.sh                                # .claude/rules/db-schema.sql 다시 뽑기
docker compose down                                   # DB 끄기 (데이터는 볼륨에 남음, 지우려면 -v)
```

환경변수(`.env`)

| 이름 | 예 | 설명 |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://echo:echo@localhost:55432/echo` | 앱·Alembic 이 쓰는 DB |
| `TEST_DATABASE_URL` | `postgresql+asyncpg://echo:echo@localhost:55432/echo_test` | 통합 테스트 DB. 이름이 `_test` 로 끝나야 한다(테스트가 스키마를 지우고 다시 만든다). 없으면 테스트가 만든다 |
| `LOG_LEVEL` | `INFO` | `DEBUG`·`INFO`·`WARNING`·`ERROR` |
| `ENVIRONMENT_CHECK_TIMEOUT_SECONDS` | `5.0` | 환경 접속 확인 전체 제한 시간(초) |
| `ECHO_SECRET_<NAME>` | (로컬 더미 값) | `secretRef = env:ECHO_SECRET_<NAME>` 이 가리키는 환경 접속 비밀값. 프로세스 환경변수로만 읽는다 |
| `PUBLIC_BASE_DOMAIN` | `apps.echo.test` | 퍼블릭 주소 도메인(125자 이하). 대표 `{서비스}.{도메인}`, 환경별 `{서비스}.{환경}.{도메인}` |
| `EXECUTOR_MODE` | `fake` | 빌드·배포·대표 주소·내리기 실행기. 지금은 `fake` 만 받는다(`real` 은 인프라 3·5 이후) |
| `HEALTH_CHECKER_MODE` | `fake` | `fake`(늘 통과) · `http`(환경별 주소 + 헬스 경로를 실제 GET) |
| `SOURCE_CLIENT_MODE` | `fake` | `fake`(owner 이름 규칙) · `github`(GitHub App, 위 설정 자리) |
| `GITHUB_APP_ID`·`GITHUB_APP_PRIVATE_KEY_REF`·`GITHUB_APP_INSTALL_URL`·`GITHUB_API_BASE_URL` | | GitHub App 설정. 개인키는 `env:ECHO_SECRET_…` 참조만 |
| `BUILD_TIMEOUT_SECONDS`·`DEPLOY_TIMEOUT_SECONDS`·`TEARDOWN_TIMEOUT_SECONDS` | `900`·`300`·`300` | 단계별 제한 시간 |
| `HEALTH_CHECK_INTERVAL_SECONDS` | `5` | 헬스체크 재확인 간격(요청 하나 제한 5초) |
| `FAKE_EXECUTOR_STEP_SECONDS` | `2.0` | 가짜 실행기가 한 작업을 끝내는 시간 |
| `WORKER_POLL_INTERVAL_SECONDS`·`WORKER_LEASE_SECONDS`·`WORKER_CONCURRENCY` | `1.0`·`30`·`1` | 일꾼 폴링 간격·임대 시간·동시 루프 수. 일꾼은 여러 개 띄워도 된다 |

`echo`/`echo` 는 로컬 compose 용 더미 계정이다. 실제 비밀값은 커밋하지 않는다.

## 인터페이스 요약

| 메서드 | 경로 | 응답 | 설명 |
|---|---|---|---|
| GET | `/healthz` | 204 | 프로세스 생존만 본다(DB 와 무관) |
| GET | `/readyz` | 204 / 503 | DB `SELECT 1` 이 2초 안에 성공하면 204, 아니면 503 |
| POST | `/api/v1/environments` | 201 | 환경 등록 + 접속 확인(실패해도 201) |
| GET | `/api/v1/environments` | 200 | 목록(저장 값만, `page`·`size`·`kind`) |
| GET | `/api/v1/environments/{id}` | 200 | 단건 + 그 자리에서 읽은 클러스터 정보 |
| PATCH | `/api/v1/environments/{id}` | 200 | name·config·secretRef 수정(kind 변경 422) |
| DELETE | `/api/v1/environments/{id}` | 204 | 소프트 삭제(사용 중이면 409) |
| POST | `/api/v1/environments/{id}/connection-check` | 200 | 접속 다시 확인 |
| GET | `/api/v1/environment-kinds` | 200 | 등록된 종류 + config JSON Schema |
| POST | `/api/v1/services` | 201 | 서비스 등록(저장소 접근 확인) |
| GET | `/api/v1/services` | 200 | 서비스 목록(`page`·`size`) |
| GET | `/api/v1/services/{id}` | 200 | 서비스 + 연결별 역할·버전 상태·주소 |
| PATCH | `/api/v1/services/{id}` | 200 | 설명·저장소·헬스·포트 수정(이름 변경 422) |
| DELETE | `/api/v1/services/{id}` | 202 | 모든 환경에서 내린 뒤 소프트 삭제 |
| POST | `/api/v1/services/{id}/deploy-requests` | 202 | 원터치 배포 접수(환경 1개 + 브랜치·커밋) |
| GET | `/api/v1/services/{id}/deploy-requests` | 200 | 배포 요청 목록(최신순, `environmentId`) |
| GET | `/api/v1/deploy-requests/{id}` | 200 | 배포 요청 진행 상황 |

- 모든 JSON 응답은 `{"success", "code", "message", "data", "details"}` 봉투다. 봉투의 `code`·`message`·`details` 는 값이 없으면 빼고, **성공 응답의 `data` 안 칸은 항상 있고 값이 없으면 null** 이다. 오류도 같은 봉투다(`data` 없음).
- 다른 파트는 환경을 `id` 로만 가리킨다(`name` 은 바뀔 수 있다). 자세한 계약: [docs/api/environments.md](docs/api/environments.md).
- 모든 응답에 `X-Request-ID` 헤더가 붙는다. 요청에 형식이 맞는 `X-Request-ID` 가 있으면 그대로 이어 쓴다.
- 로그는 stdout 에 JSON 한 줄씩(`component=echo-api`, `request_id` 포함).

## 배포

- `Dockerfile`: uv 멀티스테이지, 비루트 UID 1001, 기본 명령 `uvicorn app.main:app --host 0.0.0.0 --port 8000`. 일꾼은 같은 이미지로 명령만 `python -m app.worker` 로 바꿔 띄운다(`compose.yaml` 의 `worker` 서비스, `--profile worker`). 일꾼은 HTTP 포트가 없다.
- 마이그레이션은 **앱 시작 때 돌리지 않는다.** 같은 이미지로 `alembic upgrade head` 를 한 번 따로 실행한다. 클러스터에서 어떻게 실행할지는 인프라 파트와 정한다.
- 프로브: liveness `/healthz`, readiness `/readyz`.
- CI 워크플로는 아직 없다(팀 회의에서 정한다).

## 현재 상태와 한계

- 업무 API 는 환경·서비스·배포 요청 API 다. 인증이 없어 인터넷에 열지 않는다(API 서버 6).
- 어댑터는 **가짜**다. 접속 확인·클러스터 정보는 고정 값이고 `simulated: true` 로 표시한다. 실제 EKS·GKE·k3s 접속은 클러스터가 생긴 뒤 별도 작업에서 같은 인터페이스·계약 검사로 바꾼다.
- **원터치 배포의 실행은 가짜다.** 빌드·배포·대표 주소 연결·되돌리기·내리기는 가짜 실행기가 흉내 내고 결과에 `simulated: true` 를 저장한다. 흐름·상태·DB 기록·계약은 실제다. 실제 실행기(인프라 3 Argo CD 파이프라인·인프라 5 고정 진입점 프록시)가 준비되면 실행기만 바꾼다. 이 이슈(#3)의 완료 기준은 실제 배포까지라 실제 실행기 연결 전에는 이슈를 닫지 않는다.
- 대표 주소의 실제 헬스 확인은 실제 라우터 연결 뒤에 더한다(지금은 환경별 주소만 확인). 트래픽 전환·이전은 API 서버 8, 사용자 되돌리기·배포 로그는 API 서버 4.
- 일꾼이 없으면 배포 요청은 `QUEUED` 에 남는다. `/readyz` 는 일꾼을 보지 않는다.
- 비밀값은 `env:` 저장소만 실제로 읽는다. `k8s:`·`aws-sm:`·`gcp-sm:` 은 형식만 받고 접속 확인은 `SECRET_STORE_UNSUPPORTED` 로 실패한다.
- 접속 확인은 등록·다시 확인·config/secretRef 수정 때만 한다. 자동 주기 확인은 없다(API 서버 5).
- 배포·배포 요청 상태 **값**만 DB 가 막는다. 상태 전환 규칙은 코드(조건부 UPDATE)가 지킨다.
- `environments` 를 Core·raw SQL 로 쓰면 설정 검사를 건너뛴다(규칙으로 금지, DB 는 막지 않음).
- 서비스별 트래픽 비율 합계는 DB 가 검사하지 않는다(API 서버 8).

## 문서

| 문서 | 내용 |
|---|---|
| [docs/schema.md](docs/schema.md) | 공통 모델 스키마 설명(ER·칸·행 예시·상태 흐름·설계 이유·새 환경 추가) |
| [docs/api/environments.md](docs/api/environments.md) | 환경 API 계약(엔드포인트·스키마·오류 코드·secretRef 형식·null 규칙) |
| [docs/api/services.md](docs/api/services.md) | 서비스 API 계약(등록·수정·삭제, 역할·버전 상태, `simulated` 뜻) |
| [docs/api/deploy-requests.md](docs/api/deploy-requests.md) | 원터치 배포 API 계약(접수 규칙·진행 단계·실패 코드) |
| [docs/executors.md](docs/executors.md) | 실행기 계약(멱등 키·호출 순서·실패 코드·가짜 구현) |
| [CLAUDE.md](CLAUDE.md) · [AGENTS.md](AGENTS.md) | AI 도구용 작업 규칙 |
| [.claude/rules/](.claude/rules/) | 용어 사전·DB 스키마(DDL)·마이그레이션·백엔드·Git 규칙 |
| [specs/](specs/) | 태스크별 SPEC |
