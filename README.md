# echo-was — Echo Control API

Team Echo 멀티 클라우드 관리 플랫폼의 API 서버(관리 평면)다.

## 시스템 내 위치

```text
[웹 콘솔(프론트)] ──HTTP──▶ [echo-was: 관리 평면 API] ──▶ PostgreSQL (환경·서비스·배포 기록)
                                     │
                                     └─(어댑터, API 서버 2~)──▶ AWS EKS · GCP GKE · 온프레 k3s
                                                                  ▲
                                         [고정 도메인 + L7 프록시(Envoy)] ── 트래픽 비율 적용 (이전 엔진)
```

- 사용자는 웹 콘솔에서 클러스터를 **환경**으로 등록하고, **서비스**를 여러 환경에 **배포**하고, 클라우드 사이를 옮긴다.
- 이 서버는 그 기록(무엇이 어디에 어떤 버전으로 몇 % 돌고 있나)의 주인이다. 실제 클러스터 조작은 환경 종류별 어댑터가 한다(다음 태스크).

## 구성

| 구성 요소 | 상태 | 설명 |
|---|---|---|
| API 골격 | 있음 | 공통 응답 봉투·예외·JSON 로깅·요청 ID·`/healthz`·`/readyz` |
| 공통 모델 | 있음 | `services`·`environments`·`service_environments`·`deployments` + 첫 마이그레이션 |
| 환경 설정 양식 | 있음 | `aws`·`gcp`·`onprem` 설정 검사(저장 전). 새 종류는 파일 하나 |
| 환경 등록 API·어댑터 동작 | 다음 | API 서버 2 |
| 원터치 배포·빌드·Worker | 다음 | API 서버 3 |

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

## 기술 스택

Python 3.13 · FastAPI · Pydantic v2 · SQLAlchemy 2.0(async) · PostgreSQL 17(asyncpg) · Alembic · uv · ruff · mypy(strict) · pytest(+pytest-asyncio)

## 디렉터리 구조

```text
app/
├─ main.py         # FastAPI 앱, /healthz·/readyz
├─ adapters/       # 환경 종류별 설정 양식 (aws.py · gcp.py · onprem.py) + 레지스트리
├─ models/         # SQLAlchemy 모델 (표 4개)
├─ schemas/        # 공통 응답 봉투
├─ core/           # 설정·DB 엔진·예외·로깅·요청 ID 미들웨어
└─ enums.py        # DeploymentStatus
alembic/           # 마이그레이션 (첫 revision: 공통 모델 표)
scripts/           # dump_schema.sh (db-schema.sql 생성)
tests/             # 단위 테스트, integration/ (실제 PostgreSQL)
docs/schema.md     # 스키마 설명 원본
.claude/           # 규칙 문서·훅·write-pr 스킬
```

## 빠른 시작

준비물: [uv](https://docs.astral.sh/uv/), Docker. 저장소 루트에서:

```bash
uv sync                                  # 1. 의존성 설치 (.venv)
cp .env.example .env                     # 2. 로컬 설정 (개발용 더미 계정)
docker compose up -d --wait db           # 3. PostgreSQL 17 (localhost:55432), 접속 가능할 때까지 대기
uv run alembic upgrade head              # 4. 표 만들기
uv run uvicorn app.main:app              # 5. API 실행 → http://127.0.0.1:8000/readyz 가 204
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

`echo`/`echo` 는 로컬 compose 용 더미 계정이다. 실제 비밀값은 커밋하지 않는다.

## 인터페이스 요약

| 메서드 | 경로 | 응답 | 설명 |
|---|---|---|---|
| GET | `/healthz` | 204 | 프로세스 생존만 본다(DB 와 무관) |
| GET | `/readyz` | 204 / 503 | DB `SELECT 1` 이 2초 안에 성공하면 204, 아니면 503 |

- 모든 JSON 응답은 `{"success", "code", "message", "data", "details"}` 봉투이고 null 칸은 뺀다. 오류도 같은 봉투다.
- 모든 응답에 `X-Request-ID` 헤더가 붙는다. 요청에 형식이 맞는 `X-Request-ID` 가 있으면 그대로 이어 쓴다.
- 로그는 stdout 에 JSON 한 줄씩(`component=echo-api`, `request_id` 포함).
- 업무 API(환경 등록 등)는 API 서버 2 부터 더한다.

## 배포

- `Dockerfile`: uv 멀티스테이지, 비루트 UID 1001, 기본 명령 `uvicorn app.main:app --host 0.0.0.0 --port 8000`.
- 마이그레이션은 **앱 시작 때 돌리지 않는다.** 같은 이미지로 `alembic upgrade head` 를 한 번 따로 실행한다. 클러스터에서 어떻게 실행할지는 인프라 파트와 정한다.
- 프로브: liveness `/healthz`, readiness `/readyz`.
- CI 워크플로는 아직 없다(팀 회의에서 정한다).

## 현재 상태와 한계

- 업무 API 가 없다. 이번 범위는 골격·공통 모델·설정 양식·문서다.
- 어댑터는 설정 검사만 한다. 클러스터 배포·상태 조회 코드는 API 서버 2 에서 더한다. `secret_ref` 의 형식과 비밀 조회도 API 서버 2 에서 정한다.
- 배포 상태 **값**만 DB 가 막는다. 상태 전환 규칙(PENDING → IN_PROGRESS → …)은 API 서버 3 이 코드로 지킨다.
- `environments` 를 Core·raw SQL 로 쓰면 설정 검사를 건너뛴다(규칙으로 금지, DB 는 막지 않음).
- 서비스별 트래픽 비율 합계와, 살아 있는 연결이 있는 서비스·환경의 삭제 규칙은 DB 가 검사하지 않는다(각각 API 서버 8, API 서버 2·3).

## 문서

| 문서 | 내용 |
|---|---|
| [docs/schema.md](docs/schema.md) | 공통 모델 스키마 설명(ER·칸·행 예시·상태 흐름·설계 이유·새 환경 추가) |
| [CLAUDE.md](CLAUDE.md) · [AGENTS.md](AGENTS.md) | AI 도구용 작업 규칙 |
| [.claude/rules/](.claude/rules/) | 용어 사전·DB 스키마(DDL)·마이그레이션·백엔드·Git 규칙 |
| [specs/](specs/) | 태스크별 SPEC |
