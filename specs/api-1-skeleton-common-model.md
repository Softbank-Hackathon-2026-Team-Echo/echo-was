# API 서버 1: FastAPI 골격 + 공통 모델 SPEC

- 상태: Confirmed
- 확인일: 2026-11-01
- Depth: Deep
- Ambiguity: Low (≈ 15% / 목표 ≤ 20%)
- High-impact unresolved decisions: None
- Interview rounds: 7
- 노션 태스크: https://app.notion.com/p/40a8bee9ada4800d89aef250c0ae96a6
- 저장소: `echo-was` (https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was, private) · 브랜치 `feat/api-skeleton`
- 지시서: `/Users/jimin/Documents/projects/2026-softbank/reference/task-api-1.md`

## 1. 목표

Team Echo 멀티 클라우드 관리 평면 API 서버의 첫 골격을 만든다. 그 위에 환경·서비스·배포 공통 모델을 올린다. 서비스 하나가 여러 환경(AWS·GCP·온프레)에 동시에 있을 수 있어야 한다(이전 중 A/B 동시 가동). 새 환경 종류를 더할 때 DB 표를 고치지 않고 설정 양식 파일 하나만 더하면 되게 한다(NFR-15). 심사용 설계 문서에 쓸 스키마 설명(`docs/schema.md`)을 함께 만든다.

## 2. 현재 맥락과 확인된 사실

- 저장소: `README.md`, `.gitignore`만 있다. `main` = `a9b0193`. `origin`은 GitHub private 저장소다. `.deep-interview/`는 `.git/info/exclude`에 있다.
- 원장 `final_features_v6.md`
  - F2(110~124줄): 공통 모델(환경·서비스·배포)과 환경별 어댑터. 서비스 하나가 여러 환경에 동시에 있을 수 있게 설계. 설계 문서에 스키마 예시 한 화면. 데모 증거는 "환경 목록에 AWS·GCP·온프레가 같은 열로 보인다. 새 환경은 어댑터 하나만 추가".
  - NFR-15: 새 환경 추가 = 어댑터 1개 추가, 공통 모델 변경 0.
  - F1: 원터치 배포(소스 → 빌드 → 배포), 되돌리기는 이미지와 설정을 함께.
  - F3: 환경 카드 = 클라우드·버전·트래픽 비율·위치.
- 예선 템플릿 `reference/iris-was-pr1/` (예선 iris-was 커밋 `2e1cc10`)
  - `app/core/{config,database,exceptions,exception_handlers,logging,middleware}.py`, `app/schemas/response.py`, `app/models/base.py`(제약 naming convention), `app/enums.py`, `app/main.py`(`/healthz` 204, `/readyz` DB `SELECT 1` 2초 제한 → 204/503)
  - `alembic/env.py`(async), `alembic.ini`, `Dockerfile`(uv 멀티스테이지, UID 1001), `pyproject.toml`(Python ≥ 3.13, ruff line 100, mypy strict, pytest `asyncio_mode=auto`), `tests/test_{response,error_handling,logging,entrypoints}.py`
  - `.claude/settings.json`: `git add`·`git commit` 전 `uv run ruff check .` → `uv run mypy app` → `uv run pytest`(종료 코드 0 또는 5 허용), `git push` 전 ruff. 모델 편집 뒤 `db_schema_changelog.py` 훅.
  - `.claude/rules/term-dictionary.md` 표기 규칙: 테이블 `snake_case` 복수형, 시각 `*_at` `timestamptz`(UTC), 내부 Enum 코드 `UPPER_SNAKE`, `image_digest`는 `sha256:...`만, tag 필드 없음.
  - `.claude/rules/backend-conventions.md` §2.5: 소프트 삭제 믹스인(`is_deleted`, `deleted_at`) + `created_at`·`updated_at` 믹스인. Enum은 `app/enums.py`의 `StrEnum` 한곳.
- M5: `uv` 있음(`~/.local/bin/uv`), Docker 실행 중, PostgreSQL 미설치. m5claude 작업 환경 PATH에서 `uv`·`git`·`python3`가 잡힌다.
- 지시서와 달라진 사실(11/1): ① 브랜치는 `feat/project-skeleton` 대신 `feat/api-skeleton`. 앞서 연 Orca 워크트리가 `feat/project-skeleton`을 잡고 있어 같은 이름을 쓸 수 없다. ② 지민 지시로 GitHub private 저장소를 만들고 `main`을 올렸다. 그래서 `origin`이 있다. push는 여전히 구현 작업에서 하지 않는다. 나머지 지시서 결정은 그대로다.

## 3. 범위

### 포함
- 골격: 프로젝트 설정, 공통 응답·예외·로깅·요청 ID, `/healthz`·`/readyz`, Settings, Alembic, Docker, docker compose(로컬 DB), 규칙 문서·훅·PR 템플릿·write-pr 스킬, `CLAUDE.md`·`AGENTS.md`·`README.md`
- 공통 모델: 표 4개(`services`, `environments`, `service_environments`, `deployments`), 믹스인, Enum, 첫 Alembic revision, `.claude/rules/db-schema.sql`, 용어 사전
- 환경 설정 양식: 종류별 설정 Pydantic 모델(aws·gcp·onprem), 자동 등록 레지스트리, 저장 전 검사
- 문서: `docs/schema.md`(스키마 설명 원본), `docs/PR.md`(gitignore, PR 본문 초안)
- 테스트: 공통 코드 단위 테스트, 설정 양식 테스트, DB 통합 테스트(제약·시나리오·마이그레이션 왕복)

### 제외
- 환경 등록·조회 API, 어댑터 동작 코드(배포·상태 조회), 비밀값 조회와 `secret_ref` 형식 → API 서버 2
- 빌드 표, 배포 실행·상태 전환 코드, Worker 진입점 → API 서버 3
- 배포 이력·로그 API(4), 상태 조회 API(5), 역할·권한(6), 감사 로그(7), 이전 계획(8)
- GitHub Actions CI 워크플로(PR·CI 사용은 11/1 19:00 팀 회의에서 정한다)
- 노션 설계 문서 반영과 팀 공유(구현 뒤 M1이 따로 한다)
- 예선(Iris) 도메인 코드·문서·`vendor/` wheel

## 4. 구성 요소

| 구성 요소 | 상태 | 비고 |
|---|---|---|
| 골격 | active | 예선 틀을 옮기고 이름만 본선용으로 |
| 공통 모델 | active | 표 4개 + 첫 revision |
| 환경 설정 양식 | active | 어댑터의 "설정 검사" 부분만. 동작 코드는 API 2 |
| 문서 | active | `docs/schema.md`, 용어 사전, README, CLAUDE.md |

## 5. 흐름

```mermaid
erDiagram
    services ||--o{ service_environments : has
    environments ||--o{ service_environments : runs_on
    service_environments ||--o{ deployments : history
    service_environments }o--o| deployments : current
    deployments |o--o{ deployments : rollback_of
```

```mermaid
stateDiagram-v2
    [*] --> PENDING : 배포 행 생성
    PENDING --> IN_PROGRESS : 어댑터 시작
    PENDING --> CANCELLED : 시작 전 취소
    IN_PROGRESS --> SUCCEEDED : 헬스체크 통과 / 연결 행 current 갱신
    IN_PROGRESS --> FAILED : 오류·시간 초과
    SUCCEEDED --> [*]
    FAILED --> [*]
    CANCELLED --> [*]
```

재시도와 되돌리기는 상태를 되감지 않고 새 배포 행을 만든다. 되돌리기 행은 `rollback_of_id`로 원본을 가리키고, 원본의 `image_repository`·`image_digest`·`config_snapshot`을 복사한다. 상태 전환 코드는 API 3에서 쓴다. 이번에는 상태 값과 허용 전환을 용어 사전에 적고 DB CHECK로 값만 막는다.

연결 점검(체크포인트, 지민 확인 11/1): 빌드 → 배포(image), 배포 성공 → 연결 행(current), 되돌리기(rollback_of), 이전 엔진 → 연결 행(traffic), 환경 카드(F3) 모두 MATCH. 환경 행 → 비밀값 조회(`secret_ref` 형식)는 API 2로 미룸.

## 6. 기능 요구사항

### 골격
- `FR-1` Python 3.13, uv. 의존성: fastapi, uvicorn, pydantic-settings, sqlalchemy[asyncio] 2.x, asyncpg, alembic. dev: ruff, mypy, pytest, pytest-asyncio, httpx. `uv.lock`을 커밋한다. ruff·mypy(strict)·pytest 설정은 템플릿과 같다.
- `FR-2` 템플릿의 공통 코드를 옮긴다: `ApiResponse[T]`·`ErrorDetail`·`Page[T]`, `AppError` 계층과 예외 핸들러, JSON 한 줄 로깅, `RequestContextMiddleware`(`X-Request-ID` 받기·생성·응답 헤더), `Settings`(`DATABASE_URL`, `LOG_LEVEL`). 로그 서비스 이름은 `echo-api`, 앱 제목은 `Echo Control API`. Iris·AnyDeploy·CodeBuild·GitOps 언급은 남기지 않는다.
- `FR-3` `GET /healthz`는 프로세스 생존만 보고 204. `GET /readyz`는 DB `SELECT 1`을 2초 안에 성공하면 204, 실패·시간 초과면 503(템플릿과 같다).
- `FR-4` Alembic async 템플릿. 제약 naming convention, URL은 Settings에서 주입, 앱 시작 시 마이그레이션을 실행하지 않는다.
- `FR-5` `Dockerfile`(uv 멀티스테이지, 비루트 UID 1001)과 `compose.yaml`. compose는 `db` 서비스를 둔다: `postgres:17`, 호스트 포트 `55432`, named volume, 개발용 고정 계정 `echo`/`echo`, DB `echo`, `healthcheck`(`pg_isready`). 통합 테스트용 DB `echo_test`는 컨테이너 초기화 스크립트나 테스트 픽스처가 만든다. `.env.example`은 `DATABASE_URL=postgresql+asyncpg://echo:echo@localhost:55432/echo`, `TEST_DATABASE_URL=postgresql+asyncpg://echo:echo@localhost:55432/echo_test`, `LOG_LEVEL=INFO`. 이 계정은 개발용 더미이며 실제 비밀값이 아니다.
- `FR-6` `.claude/`: `settings.json` 훅(템플릿 그대로), `hooks/db_schema_changelog.py`, `logs/db-schema-changelog.md`(빈 기록에서 시작), `skills/write-pr/`, `rules/git-conventions.md`·`backend-conventions.md`·`db-migration.md`(예선 도메인 예시만 본선 것으로 바꿈), `rules/term-dictionary.md`(새로), `rules/db-schema.sql`(새로). `.github/PULL_REQUEST_TEMPLATE.md`(템플릿 + `노션 태스크:` 줄).
- `FR-7` `CLAUDE.md`: 템플릿의 틀(규칙 문서 표, 우선순위 "용어 사전 > 코드 규칙 > 일반 관례", 핵심 원칙, 도구)을 유지하고 서비스 설명·구조·도메인 절은 본선 기준으로 새로 쓴다. `AGENTS.md`는 "이 저장소의 규칙은 `CLAUDE.md`와 `.claude/rules/`를 따른다" 한 단락만 둔다(Codex 조사도 같은 규칙을 읽게).
- `FR-8` `README.md` 목차: 시스템 내 위치 → 구성 → 핵심 흐름 → 기술 스택 → 디렉터리 구조 → 빠른 시작 → 인터페이스 요약 → 배포 → 현재 상태/한계 → 문서. 빠른 시작은 `uv sync` → `cp .env.example .env` → `docker compose up -d --wait db` → `uv run alembic upgrade head` → `uv run uvicorn app.main:app` 다섯 단계 이내(`--wait`로 DB가 접속을 받을 때까지 기다린다).
- `FR-9` `.gitignore`에 `.env`, `.venv/`, `docs/PR.md`, `.deep-interview/`, 캐시 폴더를 둔다.

### 공통 모델
- `FR-10` `app/models/base.py`: `Base`(naming convention), `TimestampMixin`(`created_at`, `updated_at`: `timestamptz` NOT NULL, 서버 기본값 `now()`, `updated_at`은 ORM `onupdate=func.now()`로 갱신 — Core·raw SQL 수정 경로는 갱신하지 않는다고 규칙 문서에 적는다), `SoftDeleteMixin`(`is_deleted` boolean NOT NULL 기본 false, `deleted_at` `timestamptz` NULL, `CHECK (is_deleted = (deleted_at IS NOT NULL))`). PK는 `uuid`.
- `FR-11` 표 정의(8절 데이터 계약)를 SQLAlchemy 모델과 첫 Alembic revision으로 만든다.
  - 순환 FK: `service_environments`의 current 복합 FK는 모델에서 `ForeignKeyConstraint(..., use_alter=True, name=...)`로 선언한다. revision은 두 표를 만든 뒤 `op.create_foreign_key`로 추가하고, downgrade는 이 FK를 먼저 지운 다음 표를 지운다(직접 작성).
  - ORM 관계: `ServiceEnvironment.current_deployment`는 `viewonly=True` relationship(읽기 전용)으로 두고, 현재 배포 변경은 `current_deployment_id` 값을 직접 넣는다. ORM이 `service_environments.id`(PK)를 복합 FK 때문에 덮어쓰지 않음을 테스트로 확인한다.
  - `alembic/env.py`는 `compare_server_default=True`를 켠다. `alembic check`는 CHECK 제약·부분 인덱스 조건을 비교하지 못하므로, 통합 테스트가 `upgrade head` 뒤 `pg_catalog`에서 CHECK·인덱스(조건 포함)·FK·트리거 정의를 읽어 기대 목록과 비교한다.
- `FR-11a` `deployments`는 DB가 지우기를 막는다: `BEFORE DELETE` 행 트리거가 예외를 낸다(revision에 직접 작성). `downgrade`의 표 삭제와 테스트 정리(`TRUNCATE`)는 행 트리거와 무관하다.
- `FR-12` `app/enums.py`에 `DeploymentStatus(StrEnum)`: `PENDING`, `IN_PROGRESS`, `SUCCEEDED`, `FAILED`, `CANCELLED`. 환경 종류는 Enum으로 두지 않는다(레지스트리 키).
- `FR-13` `.claude/rules/db-schema.sql`은 첫 revision 적용 뒤 `docker compose exec db pg_dump --schema-only --no-owner --no-privileges`로 뽑아 정리한 결과다(뽑는 명령을 `scripts/dump_schema.sh`로 둔다). `.claude/logs/db-schema-changelog.md`는 훅이 남긴 줄만 두고 직접 고치지 않는다.

### 환경 설정 양식 (어댑터의 설정 검사 부분)
- `FR-20` `app/adapters/` 패키지. `base.py`에 `EnvironmentConfig`(Pydantic, `extra="forbid"`, `frozen=True`) 기반 클래스, 종류를 등록하는 데코레이터 `@register_environment_kind("<kind>")`, `display_location()` 추상 메서드(F3 "위치" 칸용)를 둔다. 종류 이름은 데코레이터 인자(클래스 변수)로만 두고 config JSON 안에는 넣지 않는다.
- `FR-21` 종류별 파일 하나씩: `aws.py`(`aws`: `region`, `cluster_name`), `gcp.py`(`gcp`: `project_id`, `region`, `cluster_name`), `onprem.py`(`onprem`: `location`, `kube_context`). 각 필드는 빈 문자열을 거부하고, `region`·`project_id`는 각 클라우드의 표기 형식 정규식으로 검사한다.
- `FR-22` 레지스트리 `app/adapters/registry.py`의 `discover(package_name="app.adapters")`가 패키지 안 모듈을 불러와 등록한다. 처음 조회할 때 한 번 실행한다. 새 종류 추가는 **파일 하나 추가**로 끝나고 레지스트리·모델·마이그레이션을 고치지 않는다. import 방향은 `app.models` → `app.adapters`만 허용한다(adapters는 models를 import하지 않는다).
- `FR-23` `validate_environment_config(kind, config) -> EnvironmentConfig`. 모르는 종류는 `UnknownEnvironmentKindError`(`code="UNKNOWN_ENVIRONMENT_KIND"`, 422), 양식 위반은 `InvalidEnvironmentConfigError`(`code="ENVIRONMENT_CONFIG_INVALID"`, 422). 둘 다 `InvalidInputError`를 상속한다. 위반 정보는 `fields`의 `invalid_fields`(필드 경로 목록, 예 `["region", "extra_key"]`)에만 담고 입력 값은 담지 않는다(로그에 설정 값이 섞이지 않게). 응답 `details`로 보여 줄지는 환경 등록 API(API 2)에서 정한다.
- `FR-24` 저장 전 검사 강제 범위:
  - Session flush(객체 추가·속성 대입)로 `environments`를 넣거나 `kind`·`config`를 바꾸면 매퍼 이벤트가 `validate_environment_config`를 부른다. 통과한 설정은 `model_dump(mode="json")` 값으로 바꿔 저장한다.
  - ORM 문장 경로(`insert(Environment)`·`update(Environment)` 실행)는 `do_orm_execute` 이벤트가 `InvalidInputError`로 거부한다. 환경 저장은 객체 경로만 쓴다.
  - Core·raw SQL로 `environments`를 쓰는 것은 규칙 문서(backend-conventions)에서 금지한다. DB는 막지 않는다(수용한 위험).
  - `config`는 변경 추적 dict(MutableDict)를 쓰지 않는다. 바꿀 때는 새 dict를 다시 대입한다. 제자리 수정(`env.config["region"] = ...`)은 저장되지 않음을 테스트로 고정한다.
  - 읽기: `Environment.parsed_config() -> EnvironmentConfig`가 저장된 dict를 해당 종류의 양식 객체로 되돌린다.

### 문서
- `FR-30` `docs/schema.md`(스키마 설명 원본, 한국어): ① Mermaid ER ② 표별 칸 설명(타입·NULL·제약·뜻) ③ 실제 행 예시: shop-api가 AWS 90%·GCP 10%로 같은 이미지(digest)를 돌리는 순간(환경 3행 포함) ④ 배포 상태 흐름 Mermaid ⑤ 설계 이유: JSON `config` + 설정 양식(NFR-15), `secret_ref`만 저장(DB에 비밀값 0), 배포 행 삭제 없음, 복합 FK로 "현재 배포는 같은 연결의 배포만", `traffic_weight`는 "마지막으로 적용한 값"이고 실제 분배는 앞단 프록시 ⑥ 새 환경 추가 방법(Azure 예: 파일 하나, 표 변경 0) ⑦ 파트별 확인 요청: 프론트·인프라/이전 엔진·보안·AI 파트가 읽거나 쓰는 칸 ⑧ 후속 표 목록(빌드, 이전 계획, 감사 로그, 사용자·역할, 정책·finding 등)과 담당 태스크.
- `FR-31` `.claude/rules/term-dictionary.md`(새로): 원장 용어 근거. 핵심(환경·서비스·서비스-환경 연결·배포·어댑터·설정 양식·secret_ref·트래픽 비율·이미지 digest)과 후속(빌드·이전 계획·정책·finding·감사 로그·에이전트·샌드박스·역할) 용어, 표기 규칙(템플릿 규칙 유지), 상태 값과 허용 전환, `config_snapshot`에는 비밀값을 넣지 않고 비밀 참조만 넣는다는 규칙.
- `FR-32` `docs/PR.md`(gitignore): write-pr 스킬로 PR 본문 초안. `노션 태스크:` 링크, 채운 완료 기준, 측정값(P4) 포함.

## 7. 상태와 예외

- 정상: 모델·마이그레이션·설정 양식·문서가 일치하고 품질 검사 통과.
- `environments` 저장 시 종류를 모르거나 양식이 틀리면 DB 쓰기 전에 422 계열 `AppError`.
- DB 제약 위반(중복 연결, 범위 밖 비율, digest 형식, 상태 값, 다른 연결의 배포를 current로 지정)은 `IntegrityError`로 막힌다. HTTP 변환은 API 태스크에서 한다.
- `/readyz`는 DB가 없거나 2초를 넘기면 503, `/healthz`는 DB와 상관없이 204.
- 소프트 삭제된 행(`is_deleted = true`)은 이름·연결 유일성 검사에서 빠진다(같은 이름으로 다시 만들 수 있다). 부분 unique index 조건은 `WHERE is_deleted = false` 하나로 통일한다.
- 살아 있는 연결이 있는 서비스·환경, 또는 current 배포가 있는 연결을 소프트 삭제할 때의 처리(거부·함께 삭제)는 이번에 정하지 않는다. 삭제 기능을 만드는 API 태스크(환경은 API 2, 서비스·연결은 API 3)에서 정한다. 이번에는 DB가 막지 않는다.
- `deployments` 행 DELETE는 트리거가 예외로 막는다.
- ORM 문장 경로로 `environments`를 쓰면 `InvalidInputError`로 거부한다(FR-24).
- DB 없이 `pytest`를 돌리면 DB 통합 테스트는 이유를 출력하고 건너뛴다. 커밋 전 훅은 이 상태로도 통과한다. AC-1 판정은 DB를 띄운 상태의 결과로 한다. 테스트 픽스처는 DB 접속을 최대 10초 재시도한다.
- 커밋 전 훅은 180초 제한이다. 넘으면 커밋이 막힌 것으로 보고 원인(느린 테스트)을 고친다.
- 경계: `traffic_weight` 0과 100은 허용, -1·101은 거부. 서비스별 비율 합계는 DB가 검사하지 않는다(API 8 이전 엔진 담당).

## 8. 데이터 계약

공통: PK `id uuid`. `created_at`·`updated_at`(TimestampMixin). 소프트 삭제(SoftDeleteMixin)는 `services`·`environments`·`service_environments`에만. 이름 칸은 소문자·숫자·하이픈 1~63자(Kubernetes 이름 규칙, CHECK). FK 삭제 동작은 모두 RESTRICT.

### `services`
| 칸 | 타입 | 제약 |
|---|---|---|
| `id` | uuid | PK |
| `name` | varchar(63) | NOT NULL, 이름 형식 CHECK, 살아 있는 행 중 유일(부분 unique index) |
| `description` | text | NULL |

### `environments`
| 칸 | 타입 | 제약 |
|---|---|---|
| `id` | uuid | PK |
| `name` | varchar(63) | NOT NULL, 이름 형식 CHECK, 살아 있는 행 중 유일 |
| `kind` | varchar(32) | NOT NULL. 허용 값은 DB가 아니라 설정 양식 레지스트리가 검사 |
| `config` | jsonb | NOT NULL, 기본 `{}`, `jsonb_typeof(config) = 'object'` CHECK |
| `secret_ref` | varchar(255) | NULL. 비밀 저장소의 보관 위치만. 값은 저장하지 않는다 |

### `service_environments` (서비스-환경 연결)
| 칸 | 타입 | 제약 |
|---|---|---|
| `id` | uuid | PK |
| `service_id` | uuid | NOT NULL, FK `services.id` |
| `environment_id` | uuid | NOT NULL, FK `environments.id` |
| `current_deployment_id` | uuid | NULL. 복합 FK `(current_deployment_id, id)` → `deployments(id, service_environment_id)` |
| `traffic_weight` | smallint | NOT NULL, 기본 0, `0 ≤ 값 ≤ 100` CHECK. 마지막으로 프록시에 적용한 값 |
| `traffic_applied_at` | timestamptz | NULL. 비율을 적용한 시각 |
| (유일성) | | `(service_id, environment_id)` 살아 있는 행 중 유일 |

### `deployments` (지우지 않는 이력)
| 칸 | 타입 | 제약 |
|---|---|---|
| `id` | uuid | PK, `UNIQUE (id, service_environment_id)` |
| `service_environment_id` | uuid | NOT NULL, FK `service_environments.id`, 인덱스 `(service_environment_id, created_at)` |
| `image_repository` | varchar(512) | NOT NULL. 저장소 URI, tag 없음 |
| `image_digest` | varchar(71) | NOT NULL, `^sha256:[0-9a-f]{64}$` CHECK |
| `config_snapshot` | jsonb | NOT NULL, 기본 `{}`, object CHECK. 비밀값 없이 참조만 |
| `status` | varchar(16) | NOT NULL, 기본 `PENDING`, 5개 값 CHECK |
| `rollback_of_id` | uuid | NULL, FK `deployments.id`, `rollback_of_id <> id` CHECK |
| `started_at`, `finished_at` | timestamptz | NULL |

### 설정 양식 계약
| kind | 필드 | `display_location()` |
|---|---|---|
| `aws` | `region`(예 `ap-northeast-2`), `cluster_name` | `region` |
| `gcp` | `project_id`(예 `echo-hack-123`), `region`(예 `asia-northeast3`), `cluster_name` | `region` |
| `onprem` | `location`(예 `team-laptop`), `kube_context` | `location` |

## 9. 호환성·배포·복구

- 새 저장소라 기존 데이터·호환성 문제는 없다.
- 첫 revision은 `upgrade head` → `downgrade base` → `upgrade head` 왕복이 성공해야 한다. 이후 칸 추가는 revision 하나로 끝나는 구조를 유지한다(사전 부검 P1).
- 앱 시작 시 마이그레이션을 실행하지 않는다. 배포 환경의 마이그레이션 방식은 인프라 파트와 정한다(이번 범위 밖).
- 브랜치 `feat/api-skeleton`에서 작업하고 로컬 `main`에 합치기 전에 멈춘다. push·원격 작업은 하지 않는다(M1이 지민 확인 뒤 한다).

## 10. Acceptance criteria

- `AC-1` Given 깨끗한 체크아웃, When `uv sync --frozen` 뒤 `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy app`, `uv run pytest`를 실행, Then 모두 통과한다. DB 통합 테스트는 `docker compose up -d --wait db` 상태에서 실제로 실행돼 통과한다(건너뜀 0건을 결과에 기록).
- `AC-2` Given 빈 DB, When `alembic upgrade head` → `downgrade base` → `upgrade head` → `alembic check`, Then 모두 성공하고 `alembic check`는 차이가 없다고 보고한다(`compare_server_default=True`). 그리고 `pg_catalog` 비교 테스트가 CHECK 제약, 부분 인덱스 조건, 복합 FK, 삭제 금지 트리거가 기대 목록과 같음을 확인한다.
- `AC-3` Given 앱과 DB 실행, Then `GET /healthz` 204, `GET /readyz` 204. Given DB 중지, Then `/readyz` 503, `/healthz` 204.
- `AC-4` Given shop-api, 환경 3개(aws-seoul·gcp-seoul·onprem-k3s), 같은 digest의 성공 배포 2개, 연결 2개(90·10), When 살아 있는 연결을 조회, Then 2행이 나오고 두 current 배포의 digest가 같다.
- `AC-5` 같은 서비스×환경의 살아 있는 연결을 하나 더 넣으면 거부된다. 기존 연결을 소프트 삭제하면 다시 만들 수 있다. 서비스·환경 이름도 같은 규칙이다.
- `AC-6` 연결 A의 `current_deployment_id`에 연결 B의 배포를 넣으면 DB가 거부한다. 연결 A의 배포를 넣으면 허용되고, 이때 ORM이 `service_environments.id`를 바꾸지 않는다.
- `AC-7` DB가 거부한다: `traffic_weight` -1·101, `image_digest` `sha256:xyz`, `status` `DONE`, `config` `[]`, `rollback_of_id = id`, 이름 `Shop_API`, `is_deleted = true`인데 `deleted_at` NULL, `deployments` 행 DELETE. 허용한다: `traffic_weight` 0·100.
- `AC-8` Session으로 `kind="aws"`인데 `region`이 없는 환경을 저장하면 flush 때 DB 쓰기 전에 `InvalidEnvironmentConfigError`(`ENVIRONMENT_CONFIG_INVALID`, `invalid_fields`에 `region`, 값은 없음)가 난다. 모르는 `kind="azure"`는 `UnknownEnvironmentKindError`, 양식에 없는 키도 거부한다. `insert(Environment)`·`update(Environment)` 실행은 `InvalidInputError`로 거부된다.
- `AC-9` (NFR-15) 테스트가 `tmp_path`에 `azure.py` 양식 파일 하나를 만들고 `discover()`로 그 패키지를 등록하면(테스트 뒤 레지스트리 원복), `app/models/`·`alembic/versions/`·`registry.py`를 고치지 않고 `kind="azure"` 환경을 저장할 수 있다. 다시 읽은 `config` dict가 저장 전 값과 같고, `parsed_config()`가 그 Azure 양식 클래스의 객체를 돌려준다.
- `AC-10` 모든 요청 로그는 JSON 한 줄이고 `request_id`를 담는다. 응답 헤더에 `X-Request-ID`가 있다.
- `AC-11` `docs/schema.md`에 FR-30의 ①~⑧이 모두 있고, `db-schema.sql`의 표·칸 이름이 모델과 같다.
- `AC-12` 커밋은 `feat/api-skeleton`에만 있고 `<type>: <설명>` 형식이다. 로컬 `main`은 `a9b0193` 그대로다. 원격 작업(push·fetch·remote 변경)이 없다. `.env`가 커밋되지 않았고 커밋된 파일에 실제 비밀값이 없다.
- `AC-13` (P4) 통과 기준: DB를 띄운 상태에서 커밋 전 검사(`ruff` + `mypy` + `pytest`) 1회가 180초(훅 제한) 안에 끝난다. 측정·보고: 실제 소요 시간과, README 빠른 시작을 새 폴더 체크아웃에서 그대로 따라 해 `/readyz` 204까지 걸린 시간(Docker 이미지 첫 다운로드 제외, 목표 5분)을 `docs/PR.md`에 적는다. 목표를 넘으면 원인을 적는다.
- `AC-14` (측정·보고) 화면 없는 실행에서 `.claude/settings.json`의 커밋 전 훅이 실제로 돌았는지(훅 출력 또는 차단 메시지)를 보고한다. 돌지 않았다면 같은 검사를 직접 실행한 결과를 남긴다.

## 11. 핵심 엔티티

| 이름(코드) | 우리말 | 관계 |
|---|---|---|
| `Service` / `services` | 서비스 | 연결 N개 |
| `Environment` / `environments` | 환경 | 연결 N개, `kind` + `config` + `secret_ref` |
| `ServiceEnvironment` / `service_environments` | 서비스-환경 연결 | 서비스 1, 환경 1, 배포 N(이력), 현재 배포 0..1 |
| `Deployment` / `deployments` | 배포 | 연결 1, 되돌리기 원본 0..1 |
| `EnvironmentConfig` | 설정 양식 | 종류별 Pydantic 모델, 레지스트리로 찾음 |

## 12. 리스크 (사전 부검)

| 리스크 | 처리 | 연결 |
|---|---|---|
| P1 다른 파트가 필요한 칸이 없어 후반에 표를 크게 고침 | 요구사항화: `docs/schema.md` ⑦ 파트별 확인 요청 + 구현 뒤 노션 공유(M1). 칸 추가가 revision 하나로 끝나는 구조 | FR-30, AC-2, AC-11 |
| P2 환경 카드의 트래픽 %가 실제 프록시 값과 다름 | 요구사항화: `traffic_applied_at` 저장, 문서에 "마지막 적용 값" 명시 | 8절, FR-30 ⑤ |
| P3 JSON 설정 오타로 현장 배포 실패 | 요구사항화: 저장 전 설정 양식 검사 필수(ORM 수준) + 테스트 | FR-20~24, AC-8, AC-9 |
| P4 예선 틀의 훅·규칙이 무거워 팀원이 더딤 | AC: 검사 시간 측정, README 빠른 시작 5분 목표 | AC-13 |
| `config_snapshot`에 실수로 비밀값을 넣음 | 수용 + 규칙 문서화(DB로 막을 수 없음). API 3에서 저장 경로 검사 | FR-31 |
| Core·raw SQL로 `environments`를 써서 설정 검사를 건너뜀 | 수용 + 규칙 문서화(backend-conventions). ORM 문장 경로는 코드로 거부 | FR-24, AC-8 |

## 13. 결정 기록

| ID | 결정 | 근거 | 출처 |
|---|---|---|---|
| D-0 | 스택·하지 말 것은 지시서대로. 예외 두 가지: 브랜치 `feat/api-skeleton`, GitHub `origin` 있음(push는 M1) | 지시서 "결정 완료" + 11/1 변경(2절) | 사용자 |
| D-1 | 연결 행 = 현재 배포 + 트래픽 비율(마지막 적용 값) | 환경 카드·이전 A/B를 한 행에서 읽음 | 사용자 |
| D-2 | `kind` 문자열 + `config` jsonb 한 칸, 형식은 설정 양식이 검사 | NFR-15: 새 환경에 마이그레이션 0 | 사용자 |
| D-3 | 접속 정보는 `secret_ref`만, 값은 비밀 저장소 | DB가 털려도 키 없음 | 사용자 |
| D-4 | 배포 행 = 올리기만(이미지 digest + 설정 스냅샷 + 대상 + 상태), 빌드는 API 3 별도 표 | 같은 이미지를 여러 환경에, 되돌리기 = 예전 값으로 새 행 | 사용자 |
| D-5 | 스키마 설명은 `docs/schema.md` 원본, 구현 뒤 노션으로 옮김 | 코드와 같은 커밋에서 고침 | 사용자 |
| D-6 | 사전 부검 P1~P4 모두 채택 | 12절 | 사용자 |
| D-7 | 체크포인트 그림(표 4개·상태 흐름·연결 점검) 확정 | 11/1 확인 | 사용자 |
| D-8 | 설정 양식·검사는 API 1, 동작 코드는 API 2 | NFR-15를 이번 테스트로 증명 | 사용자 |
| D-9 | 상태 값 `UPPER_SNAKE` 5개, 취소는 PENDING에서만 | 예선 용어 규칙, 올리는 중 취소는 중간 상태를 남김 | AI-default |
| D-10 | Worker 진입점은 만들지 않음 | 실행할 작업이 API 3에서 생김 | AI-default |
| D-11 | 배포 행은 소프트 삭제 없음(지우지 않는 이력) | 되돌리기·감사의 근거 | AI-default |
| D-12 | 상태·숫자 범위는 varchar/smallint + CHECK, PG ENUM 안 씀 | 값 추가가 ALTER TYPE보다 쉬움(db-migration 규칙) | AI-default |
| D-13 | 복합 FK로 current 배포를 같은 연결로 제한 | 앱 실수를 DB가 막음, 심사용 스키마 디테일 | AI-default |
| D-14 | 트래픽 합계 100% 검사는 DB가 아니라 API 8 | 전환 중 일시적 합계 불일치를 허용해야 함 | AI-default |
| D-15 | 이름 칸은 Kubernetes 이름 규칙(소문자·숫자·하이픈 1~63자) | 이후 클러스터 리소스 이름으로 씀 | AI-default |
| D-16 | 로컬 DB는 compose `postgres:17`, 호스트 포트 55432 | PostgreSQL 미설치, 기본 포트 충돌 피함 | AI-default |
| D-17 | CI 워크플로는 이번에 넣지 않음 | PR·CI 사용은 팀 회의 결정 | AI-default |
| D-18 | `AGENTS.md`는 `CLAUDE.md`를 가리키는 한 단락 | Codex 조사도 같은 규칙을 읽게 | AI-default |
| D-19 | current 복합 FK는 `use_alter` + revision에서 따로 추가, ORM 관계는 읽기 전용 | 순환 FK의 생성·삭제 순서 문제와 PK 덮어쓰기 방지 | AI-default (검토 반영) |
| D-20 | 저장 전 검사는 Session flush 경로에서 강제, ORM 문장 경로는 거부, Core·raw SQL은 규칙으로 금지 | 매퍼 이벤트가 닿지 않는 경로를 명시 | AI-default (검토 반영) |
| D-21 | 배포 행 DELETE는 트리거로 막음 | "지우지 않는 이력"을 DB가 보장 | AI-default (검토 반영) |
| D-22 | 소프트 삭제 일관성 CHECK, 부분 unique 조건은 `is_deleted = false` | 두 칸이 어긋나 유일성이 흔들리는 것 방지 | AI-default (검토 반영) |
| D-23 | 부모 소프트 삭제 시 자식 처리 규칙은 삭제 API 태스크(API 2·3)로 미룸 | 이번에는 삭제 기능이 없음 | AI-default (검토 반영) |
| D-24 | 설정 위반은 `invalid_fields`(경로만)로 로그에, 응답 노출은 API 2에서 결정 | 설정 값이 로그에 섞이지 않게 | AI-default (검토 반영) |
| D-25 | `db-schema.sql`은 `pg_dump --schema-only`로 생성, 스키마 일치는 `pg_catalog` 비교 테스트로 확인 | `alembic check`가 CHECK·부분 인덱스를 비교하지 못함 | AI-default (검토 반영) |

## 14. 구현 계획으로 위임한 low-impact 세부사항

- UUID 생성 위치(서버 기본값 `gen_random_uuid()` 또는 Python `uuid4`), 인덱스 이름, 모듈 내부 구조
- 설정 양식 정규식의 정확한 모양(예시 값이 통과하고 빈 값·대문자 오타가 거부되면 된다)
- DB 통합 테스트의 픽스처 구조(테스트 DB `echo_test`를 만드는 방법, 표 정리 방식)
- compose에 API 서비스까지 둘지(빠른 시작은 uvicorn 직접 실행 기준)
- 커밋을 몇 개로 나눌지(골격 / 모델 / 설정 양식 / 문서 단위 권장)
