# Echo Control API (echo-was)

Team Echo 멀티 클라우드 관리 플랫폼의 **관리 평면 API 서버**다. AWS·GCP·온프레 클러스터를 환경으로 등록하고, 서비스를 여러 환경에 배포·이전하며, 그 상태를 한곳에서 보여 준다. 지금은 골격·공통 모델(환경·서비스·배포)과 환경 API(등록·조회·수정·삭제·접속 확인, 가짜 어댑터)까지 있다.

- **스택**: Python 3.13+, FastAPI, Pydantic v2, SQLAlchemy 2.0(async), PostgreSQL(asyncpg), Alembic, uv
- **배경·환경변수·실행·세팅**: [README.md](README.md) 참조 (여기서 중복하지 않는다)
- **원장**: 본선 필수 기능 정리 `final_features_v6.md`(F1~F17). 기능 번호는 이 원장을 따른다.

---

## 규칙 문서 (반드시 따른다)

작업 전 아래 문서를 우선한다. **충돌 시 우선순위: 용어 사전 > 코드 규칙 > 일반 관례.**

| 문서 | 언제 본다 |
|---|---|
| [.claude/rules/term-dictionary.md](.claude/rules/term-dictionary.md) | 도메인 용어·엔티티·필드명·Enum 코드를 정할 때 — **표준 영문 용어/식별자의 단일 출처**. 동음이의어(Service·Deployment·Environment·Config) 구분 포함 |
| [.claude/rules/db-schema.sql](.claude/rules/db-schema.sql) | 테이블·컬럼·타입·제약·인덱스 등 **물리 DB 스키마(DDL)의 단일 출처**. 직접 고치지 않고 `scripts/dump_schema.sh` 로 뽑는다. 모델 변경은 [.claude/logs/db-schema-changelog.md](.claude/logs/db-schema-changelog.md) 에 자동 기록된다 |
| [.claude/rules/db-migration.md](.claude/rules/db-migration.md) | 스키마를 바꿀 때 — Alembic revision 생성·검토·downgrade 규칙, `alembic check` 가 못 보는 것(CHECK·부분 인덱스·트리거) |
| [.claude/rules/backend-conventions.md](.claude/rules/backend-conventions.md) | 백엔드 코드(레이어·네이밍·비동기·예외·로깅·설정·테스트·린터)를 작성할 때. §2.5 에 이 저장소의 DB 규칙 |
| [.claude/rules/git-conventions.md](.claude/rules/git-conventions.md) | 커밋 메시지·브랜치명을 정할 때 — Conventional Commits 규칙 |

## 설계 문서

| 문서 | 언제 본다 |
|---|---|
| [docs/schema.md](docs/schema.md) | 공통 모델(ER·칸 설명·행 예시·상태 흐름·설계 이유·새 환경 추가 방법)을 다룰 때. **스키마 설명의 원본** |
| [docs/api/environments.md](docs/api/environments.md) | 환경 API 계약(엔드포인트·오류 코드·secretRef 형식·null 규칙)을 바꾸거나 쓸 때 |

---

## 구조

```text
app/
├─ main.py            # API 진입점 (FastAPI). /healthz, /readyz, 라우터 등록, 모듈 로드 때 discover()
├─ routers/           # environment_router, environment_kind_router (/api/v1, 권한 검사 자리 authorize_request)
├─ services/          # EnvironmentService(트랜잭션) · EnvironmentConnectionService(접속 확인, DB 없음)
├─ repositories/      # EnvironmentRepository
├─ clients/           # secret_client: secretRef → SecretValue (env 저장소만 실제 조회)
├─ adapters/          # 환경 종류별 어댑터: 설정 양식 + 동작 클래스(이번에는 가짜)
│   ├─ base.py        #   EnvironmentConfig, EnvironmentAdapter, ClusterInfo, 등록 데코레이터 2개
│   ├─ registry.py    #   discover()(양식·동작 짝 검사), validate_environment_config(by_alias), get_environment_adapter()
│   └─ aws.py · gcp.py · onprem.py   # 종류마다 파일 하나
├─ models/            # SQLAlchemy: services, environments, service_environments, deployments
├─ schemas/           # API 요청·응답 (공통 봉투 ApiResponse[T], 환경 스키마)
├─ core/              # config, database, exceptions, exception_handlers, logging, middleware, secrets
├─ dependencies.py    # Depends 주입 함수 (세션 팩토리·서비스·어댑터 조회·비밀값 조회기)
└─ enums.py           # DeploymentStatus · EnvironmentConnectionStatus · ConnectionErrorCode (용어 사전 §3)
alembic/              # 마이그레이션
scripts/              # dump_schema.sh
tests/                # 단위 테스트 · adapter_contract.py(어댑터 공통 계약) · integration/ (실제 PostgreSQL)
```

- 레이어는 `routers/`(API) → `services/`(유스케이스, `session.begin()` 블록으로 트랜잭션) → `repositories/`(DB) 순서다. 외부 시스템 호출은 `clients/` 와 `adapters/` 의 동작 코드가 맡는다.
- 세션은 `get_session_factory()`(`expire_on_commit=False`)로 만들고 요청 의존성은 트랜잭션을 열지 않은 세션을 넘긴다. 시험은 `app.dependency_overrides` 로 세션 팩토리·설정·어댑터 조회를 바꾼다.
- **import 방향**: `app.models` → `app.adapters` 만 허용한다. `app.adapters` 는 `app.models` 를 import 하지 않는다(테스트가 확인).
- Worker 진입점은 아직 없다. 배포 실행이 생기는 API 서버 3 에서 정한다.

---

## 핵심 원칙 (요약)

코드를 쓰기 전 위 문서를 보되, 자주 어기는 핵심만 추린다.

### 네이밍·식별자
- 필드·변수·컬럼은 **`snake_case`**. camelCase 금지(프론트 경계에서만 alias 변환).
- 레이어는 접미사로 드러낸다: `_router` / `Service` / `Client` / `Repository` / Model·Schema.
- 약어는 클래스명에서 첫 글자만 대문자(`HttpClient`, `AwsEnvironmentConfig`), 필드는 전부 소문자.
- 동사로 반환 형태를 고정: `get_*`(1건/없으면 예외) · `find_*`(nullable) · `search_*`(다건) · `check_*`/`validate_*`.

### 레이어 경계
- 흐름: `Router → Service → Repository → DB` / Service → Client·Adapter(외부).
- Router는 얇게(로직 없음, Service 호출 + Schema 변환만). Repository는 Model만 반환. Model은 Schema를 모른다.
- 모든 JSON 응답은 공통 봉투 **`ApiResponse[T]`** 로 감싼다. 에러도 같은 봉투(`success=False`, 도메인 예외의 `code`, 검증 실패 시 `details`)로 예외 핸들러가 만든다. 요청 ID 는 본문이 아니라 `X-Request-ID` 헤더로 준다(앞단이 준 값이 형식에 맞으면 이어 쓴다).

### 공통 모델 (이 저장소에서 자주 어기는 것)
- **서비스 하나는 여러 환경에 동시에 있을 수 있다.** 서비스↔환경은 `service_environments` 연결 행으로만 잇는다.
- **새 환경 종류 = `app/adapters/<kind>.py` 파일 하나**(설정 양식 `@register_environment_kind` + 동작 클래스 `@register_environment_adapter`). 모델·마이그레이션·`registry.py`·라우터·서비스를 고치지 않는다(NFR-15). `kind` 를 Enum·DB CHECK 로 만들지 않는다. 동작 클래스는 `tests/adapter_contract.py` 의 공통 계약을 통과해야 한다.
- `config` 키는 DB snake_case, API camelCase. API 경계는 `validate_environment_config(kind, config, by_alias=True)`, DB 쪽은 기본값(snake_case)으로 검사한다.
- **API 는 비밀값을 받지 않는다**(`secretRef` 위치만). 비밀값은 `SecretValue` 로만 다루고, 환경변수 직접 읽기는 `app/clients/secret_client.py` 한 곳만 허용한다(`ECHO_SECRET_` 접두어).
- **환경은 Session 객체로만 쓴다.** `session.add(env)` / `env.config = {...새 dict...}`. 제자리 수정(`env.config["x"] = ...`)은 저장되지 않고, `insert(Environment)`·`update(Environment)` 는 거부되며, Core·raw SQL 쓰기는 금지(검사를 건너뛴다).
- **DB 에 비밀값을 넣지 않는다.** 환경은 `secret_ref`(보관 위치)만, `config_snapshot` 에도 비밀 참조만.
- **배포 행은 지우지 않는다**(트리거가 막음). 재시도·되돌리기는 새 행이고, 되돌리기는 `rollback_of_id` + 원본의 이미지·설정 복사.
- 현재 배포는 `current_deployment_id` 값을 넣어 바꾼다(`current_deployment` 관계는 읽기 전용). 같은 연결의 배포만 가리킬 수 있다(복합 FK).
- `traffic_weight` 는 "마지막으로 프록시에 적용한 값"이다. 합계 100% 는 DB 가 아니라 이전 엔진이 맞춘다.

### 공통 안전 규칙
- I/O는 전부 `async def` + `await`. 동기 블로킹 호출은 `asyncio.to_thread`로 감싼다. async 세션에서 관계는 `selectinload` 로 미리 읽는다.
- 시각은 항상 **timezone-aware UTC**(`datetime.now(UTC)`). `utcnow()` 금지.
- 스키마 변경은 **모델 → Alembic revision → db-schema.sql(`scripts/dump_schema.sh`)** 한 세트. DB에 직접 DDL 금지, 적용된 revision 수정 금지.
- 마이그레이션은 **앱 시작 시 실행하지 않는다**(lifespan·startup 금지). 로컬은 `uv run alembic upgrade head`. 배포 환경의 실행 방식은 인프라 파트와 정한다.
- 시크릿은 `BaseSettings`로 주입. `os.getenv` 직접 호출·하드코딩 금지. 로그·예외에 시크릿·설정 값 노출 금지(설정 오류는 `invalid_fields` 경로만).
- 예외는 `AppError` 계층(`app/core/exceptions.py`)으로 raise, HTTP 매핑은 `exception_handlers.py` 한곳에서. 식별자는 메시지가 아니라 `fields`로. 빈 `except`·예외 뭉개기 금지.
- 로깅은 구조화형 JSON 한 줄: message 는 고정 문구, 값은 `extra`(`action`·도메인 ID). `component`(`echo-api`)·`request_id` 는 자동으로 붙는다. `service` 필드는 쓰지 않는다(사용자 앱 `service_id` 와 혼동). `print` 금지.
- 모든 함수 시그니처에 타입 힌트. 신문법(`str | None`, `list[T]`) 사용.
- 주석·docstring에 규칙 문서 위치(`backend-conventions §3` 등)를 인용하지 않는다.

---

## 도구

- **패키지·실행**: `uv`. 명령은 `uv run <cmd>` — 린트 `uv run ruff check .` / 포맷 `uv run ruff format .` / 타입 `uv run mypy app` / 테스트 `uv run pytest` / API `uv run --env-file .env uvicorn app.main:app`(`ECHO_SECRET_*` 를 프로세스 환경변수로 넣으려고 `--env-file`) / 마이그레이션 `uv run alembic upgrade head`.
- **로컬 DB**: `docker compose up -d --wait db`(PostgreSQL 17, 호스트 포트 55432, 개발용 더미 계정). 접속 정보는 `.env`(gitignore, `.env.example` 복사).
- **테스트**: `pytest` + `pytest-asyncio`. 함수명 `test_{대상}_{시나리오}_{기대}`. `tests/integration/` 은 `TEST_DATABASE_URL`(`echo_test`)에 붙고, DB 가 없으면 이유를 출력하고 건너뛴다. 같은 테스트 DB 를 쓰는 pytest 는 advisory lock 으로 한 번에 하나씩 돈다.
- **마이그레이션 검증**: `upgrade head` → `downgrade base`(또는 `-1`) → `upgrade head` → `alembic check`. CHECK·부분 인덱스·트리거는 `tests/integration/test_migrations.py` 가 `pg_catalog` 로 비교한다.
- **품질 게이트**: Claude Code PreToolUse 훅(`.claude/settings.json`)이 `git add`·`git commit` 전 `ruff`·`mypy`·`pytest`, `git push` 전 `ruff`를 돌려 실패 시 차단한다(180초 제한). `pyproject.toml`이 없으면 건너뛴다. 훅의 `if` 조건은 heredoc·여러 줄 명령에서는 보수적으로 맞춰져 그 명령에도 돈다 — 파일 편집은 Edit/Write 도구로 한다.
- **스키마 동기화**: `app/models/*.py` 변경 시 PostToolUse 훅(`.claude/hooks/db_schema_changelog.py`)이 변경을 `.claude/logs/db-schema-changelog.md`에 기록하고 revision 생성과 `db-schema.sql` 동기화를 알린다.
- **PR 본문**: `write-pr` 스킬(`.claude/skills/write-pr/`)이 `.github/PULL_REQUEST_TEMPLATE.md` 구조(맨 위 `노션 태스크:` 줄)로 `docs/PR.md`(gitignore)를 작성한다.
