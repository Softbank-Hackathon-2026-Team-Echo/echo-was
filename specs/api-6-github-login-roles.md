# API 서버 6: GitHub 로그인 + 조직·역할 권한 SPEC

- 상태: Confirmed
- 확인일: 2026-11-02
- Depth: Deep
- Ambiguity: Low (≈ 12% / 목표 ≤ 20%)
- High-impact unresolved decisions: None
- Interview rounds: 10 (R1~R10, 상태 파일 `.deep-interview/api-6-github-login-roles.state.md`) + fresh-eyes 검토 10건 반영
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/6 (Project #4, `part:API 서버`, `phase:1차`, In Progress, 11/2 완료 기준 수정)
- 저장소: `echo-was` · 브랜치 `feat/auth-organizations` — API 서버 3(`feat/one-touch-deploy` `4326d5d`)을 합친 `main`에서 만든다.
- 체크포인트: `~/archive/softbank-hackathon-2026/api-6/api6-checkpoint.html` (M1, 지민 확인 11/2: A1·C0·D1·E1, 15A)

## 1. 목표

Echo는 여러 고객의 앱을 배포해 주는 서비스다. 고객 회사 단위의 **조직**을 두고, 사람은 GitHub로 로그인해 조직의 멤버로서 역할(뷰어 < 개발자 < 어드민)만큼 일한다. AI 에이전트는 조직에서 발급한 토큰으로 그 조직을 읽기만 한다. 권한이 없어 막힌 요청은 기록한다.

- 처음 로그인하면 개인 조직이 자동으로 생기고 본인이 어드민이다(D13). 누구나 조직을 더 만들 수 있고 만든 사람이 어드민이다(D12).
- 어드민은 GitHub 아이디와 역할을 정해 초대한다. 그 사람이 로그인하는 순간 멤버가 된다(D7).
- 서비스·환경·배포는 조직에 속하고 다른 조직에는 보이지 않는다. API 2·3의 경로는 `/api/v1/orgs/{orgId}/…` 아래로 옮긴다(D11).

GitHub 이슈 완료 기준(원문, 11/2 수정):
- 사람은 GitHub 로그인으로 들어온다. 고객 회사 단위의 조직을 만들면 만든 사람이 그 조직의 어드민이 되고, 어드민이 GitHub 아이디와 역할(뷰어 < 개발자 < 어드민)을 정해 초대하거나 역할을 바꾼다. 서비스·환경·배포는 조직에 속하고 다른 조직에는 보이지 않는다.
- 개발자는 배포·롤백·이전 실행과 AI 수정의 운영 반영 승인, 에이전트 토큰 발급·폐기를 한다. 서비스·환경 삭제와 초대·역할 변경은 어드민만 한다. 권한 없는 계정의 이전 요청이 거부되고 기록된다 (NFR-07).
- AI 에이전트는 사람 로그인 대신 조직에서 발급한 API 토큰으로 호출하고 그 조직을 읽기만 한다. 운영 변경 시도는 거부되고 기록된다 (F16).

롤백(API 4)·이전(API 8)·AI 반영(AI 파트) 엔드포인트는 아직 없다. 이번에는 그 엔드포인트들이 붙일 권한 검사기("개발자 이상", "사람만")와 거부 기록을 만들고, 지금 있는 쓰기 경로(배포 요청 등)로 거부·기록을 증명한다. 실제 GitHub App 생성 전까지 실제 로그인은 시연할 수 없으므로(D9), 실제 앱 연결 확인은 이슈 완료 판정에 남긴다.

## 2. 현재 맥락과 확인된 사실 (API 3 브랜치 `feat/one-touch-deploy` = `4326d5d` 기준, 합친 뒤의 `main`과 같다고 가정)

- `app/dependencies.py`: `authorize_request()`는 아무것도 검사하지 않는 자리 표시. 라우터 4개가 `dependencies=[Depends(authorize_request)]`로 건다.
- 라우터: `environment_router`(`/api/v1/environments`: POST·GET 목록·GET·PATCH·DELETE·`POST /{id}/connection-check`), `environment_kind_router`(`/api/v1/environment-kinds` GET), `service_router`(`/api/v1/services`: POST·GET 목록·GET·PATCH·DELETE·`POST|GET /{id}/deploy-requests`), `deploy_request_router`(`/api/v1/deploy-requests/{id}` GET).
- `app/core/exceptions.py`: `AppError`(code·status_code), `UnauthorizedError`(401 `UNAUTHORIZED`), `ForbiddenError`(403 `FORBIDDEN`), `NotFoundError`, `ConflictError`, `InvalidInputError`, `ExternalError`(502). 응답은 `ApiResponse` 봉투, camelCase, 오류 `details[{field, reason, value?}]`.
- `app/core/config.py`: `Settings(BaseSettings, env_file=.env)`. GitHub App 설정 `github_app_id`, `github_app_private_key_ref`(비밀 참조), `github_app_install_url`, `github_api_base_url`, `github_request_timeout_seconds`. 비밀 참조는 `app/core/secrets.py`의 `parse_secret_ref`(`env:` 이름은 `ECHO_SECRET_`로 시작), 값 조회는 `EnvVarSecretClient`.
- `app/core/logging.py`: 키 이름에 `password`·`secret`·`token`·`authorization`이 들어간 로그 칸을 가린다. `app/core/middleware.py`: 요청 ID.
- `app/core/clock.py`: 주입 시계(`Clock`, `SystemClock`). API 3 코드는 DB `now()` 대신 주입 시계를 쓴다.
- 모델 믹스인 `UuidPrimaryKeyMixin`·`TimestampMixin`·`SoftDeleteMixin`(`app/models/base.py`). 이름 유일성: `uq_services_name_live`(서비스 이름, 살아 있는 행 전체), `uq_environments_name_live`(환경 이름, 살아 있는 행 전체).
- API 3이 만든 공용 삭제 금지 트리거 함수 `prevent_append_only_delete()`(표 이름을 메시지에 넣음).
- 시험: `tests/integration/conftest.py`의 `TABLES`(jobs, deploy_requests, builds, deployments, service_environments, environments, services)를 테스트마다 TRUNCATE. `tests/integration/test_migrations.py`가 CHECK·부분 unique·트리거를 비교. `tests/github_fake.py`(API 3의 가짜 GitHub). `alembic check`는 CHECK·부분 인덱스·트리거를 비교하지 않는다.
- 품질 게이트: `uv run ruff check .`, `uv run ruff format`, `uv run mypy app`, `uv run pytest`(훅이 `git add`·`commit` 전에 실행).
- GitHub 사실: 사용자 인증은 `https://github.com/login/oauth/authorize` → 콜백 `code` → `POST https://github.com/login/oauth/access_token` → `GET /user`. `state` 값은 위조 방지로 "강력 권장". GitHub App은 콜백 주소를 최대 10개 둘 수 있다. GitHub App 사용자 토큰은 기본 8시간 만료. 조직 초대는 7일 뒤 만료(GitHub 조직 기준).
- 해커톤 규칙: 목업은 밝힌다. 저장소에 시크릿 0건.

## 3. 범위

### 포함
- GitHub 로그인(GitHub App 사용자 인증), 쿠키 세션, 로그아웃, `GET /api/v1/me`
- 조직: 만들기·내 목록·하나 보기, 처음 로그인 때 개인 조직 자동 생성
- 멤버: 목록·역할 변경·내보내기/탈퇴. 초대: 보내기·목록·취소, 로그인(과 `GET /me`) 때 적용
- 에이전트 토큰: 발급·목록·폐기, Bearer 인증, 읽기 전용
- 권한 검사기: 로그인 확인, 조직 멤버 확인, 최소 역할, 사람만, 에이전트 읽기 전용, 변경 요청 Origin 확인. 지금 있는 모든 라우터에 적용
- 거부 기록 표 `audit_logs`(최소, 403만)
- API 2·3 표에 `organization_id` 칸, 경로를 `/api/v1/orgs/{orgId}/…`로 이동, 환경 이름 조직 안 유일, 환경 열쇠 이름의 조직 묶기
- 로그인 허용 스위치(기본 모든 GitHub 계정, 설정 시 GitHub 조직 멤버만)
- 데모 준비 명령(P3), README 데모 전날 점검 목록(P2)
- 문서: `docs/api/auth.md`, `docs/api/organizations.md`, `docs/api/environments.md`·`services.md`·`deploy-requests.md` 경로 갱신, `docs/schema.md`, 용어 사전(역할·조직·초대·에이전트 토큰 줄), `README.md`
- 테스트: 단위 + DB 통합(역할 × 경로 표, 조직 격리, 로그인 흐름, 초대, 토큰, 거부 기록, 동시성) + 마이그레이션 왕복

### 제외
- 조직 안 프로젝트(2단) → 2차 후보. 조직 삭제 API(AD-5)
- 결재 흐름(이전 계획 승인 버튼, 자기 승인 규칙) → API 서버 8. 롤백 → API 서버 4. AI 반영 → AI 파트. 이번에는 그들이 쓸 검사기만
- 감사 로그 조회 API·다른 행위 기록 → API 서버 7
- 개발용·비상용 로그인(D9), 비밀번호·SSO 로그인
- 고객이 클라우드 열쇠를 화면에서 올리는 기능 → API 2 후속(별도 이슈)
- GitHub App 실제 생성·조직 설치(별도 승인), push·`main` 병합(M1)
- 노션·Slack·이슈 쓰기(M1)
- 알려진 제한(README에 적음): GitHub App 설치가 조직에 묶이지 않는다. App이 설치된 비공개 저장소는 다른 Echo 조직도 서비스로 등록할 수 있다(해커톤 데모 범위에서 감수, AD-26).

## 4. 구성 요소

| 구성 요소 | 상태 | 비고 |
|---|---|---|
| 인증(로그인·세션·토큰) | active | `app/auth/` 또는 `services/auth_*` |
| 조직·멤버·초대 API | active | routers → services → repositories |
| 권한 검사기 | active | FastAPI 의존성, 모든 라우터 |
| 거부 기록 | active | `audit_logs` 최소 |
| API 2·3 조직 범위 | active(수정) | 표 칸·경로·이름 유일성·열쇠 묶기 |
| GitHub 사용자 클라이언트 | active | 인터페이스 + 실제(httpx) + 시험용 가짜(의존성 교체로만) |
| 데모 준비 명령 | active | `python -m app.scripts.demo_seed` |

## 5. 흐름

### 5.1 로그인 (`GET /api/v1/auth/github/login?next=/path`)
0. 로그인 설정(`github_app_client_id`·`github_app_client_secret_ref`·`github_oauth_redirect_uri`·`frontend_base_url`)이 하나라도 없으면 503 `LOGIN_NOT_CONFIGURED`(콜백은 `…/login?error=LOGIN_NOT_CONFIGURED`). 나머지 API·일꾼·alembic은 이 설정 없이도 뜬다.
1. `next`는 `/`로 시작하는 상대 경로만 받는다(기본 `/`, `//`·`\`·스킴 포함은 `/`로 바꾼다).
2. 무작위 `state`(32바이트)를 만들어 httpOnly 쿠키 `echo_oauth_state`(SameSite=Lax, 10분, `next` 포함, 서명 또는 서버 저장은 구현 선택)로 내려 주고, `https://github.com/login/oauth/authorize?client_id=…&redirect_uri=…&state=…`로 302. 상태 쿠키는 이 API 호스트에 내려가므로 `github_oauth_redirect_uri`는 **같은 호스트의** `/api/v1/auth/github/callback`이어야 한다(README 점검 목록).

### 5.2 콜백 (`GET /api/v1/auth/github/callback?code&state`)
1. `error=access_denied`면 프론트 `{frontend_base_url}/login?error=OAUTH_DENIED`로 302. `state`가 없거나 쿠키 값과 다르면(상수 시간 비교) `…?error=OAUTH_STATE_MISMATCH`. 상태 쿠키는 어느 경우든 지운다.
2. GitHub 사용자 클라이언트(트랜잭션 밖): `code` 교환 → 사용자 토큰(메모리에서만, 저장 안 함) → `GET /user`(`id`, `login`, `name`, `avatar_url`). GitHub 장애·시간 초과는 `…?error=GITHUB_UNAVAILABLE`.
3. 로그인 허용 스위치(`login_allowed_github_org`)가 있으면 같은 토큰으로 `GET /user/memberships/orgs/{org}`가 `state=active`일 때만 통과. 404·403(App이 그 조직에 설치되지 않았거나 Members 읽기 권한이 없음 포함)은 `…?error=LOGIN_NOT_ALLOWED`로 보내고 이유를 로그에 남긴다.
4. 트랜잭션: 사용자는 `INSERT … ON CONFLICT (github_id) DO NOTHING RETURNING` 뒤 다시 읽는다(`github_login`·`name`·`avatar_url`은 매번 갱신). **이번에 새로 넣었을 때만** 개인 조직을 만든다(5.4). 같은 사람의 콜백 두 개가 동시에 와도 사용자·개인 조직은 하나다. 대기 초대를 적용한다(5.6). 세션을 만든다(5.3). 중복 키 오류 뒤 같은 트랜잭션에서 다시 읽지 않는다(PostgreSQL은 오류 뒤 트랜잭션을 멈춘다) — `ON CONFLICT` 또는 SAVEPOINT(`begin_nested`)로 처리한다.
5. 세션 쿠키를 내려 주고 `{frontend_base_url}{next}`로 302.

### 5.3 세션
- 쿠키 `echo_session`: 무작위 32바이트, `HttpOnly`, `SameSite=Lax`, `Path=/`, `Secure`(설정 `session_cookie_secure`, 기본 true), `Max-Age` = 7일. DB `sessions`에는 값의 SHA-256만 둔다.
- 요청마다: 쿠키 해시로 세션을 찾고 `revoked_at IS NULL AND expires_at > now`면 그 사용자. 아니면 401 `UNAUTHENTICATED`(응답에 쿠키 삭제 `Set-Cookie`를 싣는다 — 예외 처리기가 헤더를 실을 수 있게 고친다). 만료 연장은 하지 않는다.
- 쿠키로 인증된 GET·HEAD·OPTIONS 외 **모든** 요청(조직 밖 `POST /orgs` 포함, 공개 경로 제외)은 `Origin` 헤더가 `frontend_allowed_origins`에 정확히 있어야 한다. 아니면 403 `ORIGIN_NOT_ALLOWED`(D1 CSRF 둘째 겹). 이 확인은 인증 의존성에서 한다. CORS는 같은 목록 + `allow_credentials`.
- `POST /api/v1/auth/logout`: 세션 `revoked_at`, 쿠키 삭제, 204. 세션이 없어도 204.

### 5.4 조직
- `slug`: `^[a-z0-9](?:-?[a-z0-9]){0,38}$`(1~39자, 소문자·숫자·하이픈, 하이픈 연속·처음·끝 금지), 전체 유일, 만든 뒤 못 바꿈. `name`: 1~100자.
- `POST /api/v1/orgs {slug, name}`: 로그인한 사람(에이전트 불가). 조직 + 만든 사람 ADMIN 멤버를 한 트랜잭션에. 겹치면 409 `ORGANIZATION_SLUG_CONFLICT`. 예약 slug(설정 `reserved_org_slugs`, 기본 `["team-echo"]`)는 409 `ORGANIZATION_SLUG_RESERVED` — 데모 준비 명령만 만든다(AD-25).
- 개인 조직(D13): `slug` = GitHub 아이디 소문자. 쓰이고 있거나 예약이면 `-2`, `-3`…. 39자를 넘으면 앞을 줄이고, 줄인 끝의 `-`는 뗀다. 그래도 규칙 밖이면 `user-{github_id}`. 시도는 `ON CONFLICT DO NOTHING` 또는 SAVEPOINT로 한다. `name` = GitHub 아이디, `personal = true`. 다른 조직과 똑같이 초대할 수 있다.
- `GET /api/v1/orgs`: 내가 멤버인 조직(역할 포함). `GET /api/v1/orgs/{orgId}`: 멤버(뷰어 이상, 에이전트는 자기 조직).

### 5.5 멤버
- `GET …/members`: 멤버(사람만). 사용자 `id`, `githubLogin`, `name`, `avatarUrl`, `role`, `joinedAt`.
- `PATCH …/members/{userId} {role}`: 어드민. 자기 역할은 못 바꾼다(403 `CANNOT_CHANGE_OWN_ROLE`).
- `DELETE …/members/{userId}`: 어드민은 다른 멤버를 내보낸다. 누구나 자기 자신은 탈퇴할 수 있다. 결과 어드민이 0명이 되면 409 `LAST_ADMIN`.
- 멤버 변경(PATCH·DELETE)은 조직 행을 `FOR NO KEY UPDATE`(`with_for_update(key_share=True)`)로 잠근 뒤, **잠금 안에서 행위자의 멤버 행과 역할을 다시 읽는다**(어드민이 아니면 403 `ROLE_FORBIDDEN` + 기록). 변경 뒤 어드민 수가 1 미만이면 409 `LAST_ADMIN`(PATCH·DELETE 모두). 두 어드민이 동시에 서로를 내려도(PATCH든 DELETE든) 어드민 ≥ 1. 잠금 순서 문서(`CLAUDE.md`, `docs/schema.md`, `.claude/rules/term-dictionary.md`)에 "조직(멤버 변경만) →"을 맨 앞에 더한다(`FOR NO KEY UPDATE`는 서비스·환경 등록의 FK 검사 `FOR KEY SHARE`와 부딪히지 않는다).

### 5.6 초대 (D7)
- `POST …/invitations {githubLogin, role}`: 어드민. GitHub 사용자 클라이언트 `GET /users/{login}`(공개 API)으로 숫자 번호를 얻는다. 없으면 422 `GITHUB_USER_NOT_FOUND`, 장애는 502 `GITHUB_UNAVAILABLE`. 그 번호의 사용자가 이미 멤버면 409 `ALREADY_MEMBER`. 같은 조직·같은 번호의 대기 초대가 있으면 그것을 `REVOKED`로 닫고 새로 만든다(부분 unique: 조직 + `github_id` WHERE PENDING). `expires_at = now + 7일`.
- 적용: 로그인 콜백과 `GET /api/v1/me`에서, 그 사람 `github_id`의 `PENDING` 초대 중 만료 안 된 것을 멤버로 추가하고 `ACCEPTED`(`accepted_at`). 이미 멤버면 역할은 바꾸지 않고 `ACCEPTED`. 만료된 것은 `EXPIRED`로 닫는다. (이미 로그인해 있던 사람도 프론트가 `/me`를 부르면 바로 들어온다.) 두 요청이 동시에 와도 한 번만 적용된다: `UPDATE invitations SET status='ACCEPTED' … WHERE id=:id AND status='PENDING'`(0행이면 건너뜀) + 멤버 `INSERT … ON CONFLICT (organization_id, user_id) DO NOTHING`.
- `GET …/invitations`: 어드민. `DELETE …/invitations/{id}`: 어드민, PENDING만 `REVOKED`(아니면 409 `INVITATION_NOT_PENDING`).

### 5.7 에이전트 토큰
- `POST …/agent-tokens {name, expiresInDays}`: 개발자 이상, 사람만. `expiresInDays` 1~90, 기본 30. 응답에 원문 `token`을 **이때 한 번만** 싣는다. 원문 형식 `echo_agt_` + base64url 32바이트. DB는 SHA-256과 끝 4자리만.
- `GET …/agent-tokens`: 개발자 이상, 사람만. 원문 없이 `id`, `name`, `last4`, `createdBy`, `createdAt`, `expiresAt`, `revokedAt`, `lastUsedAt`.
- `DELETE …/agent-tokens/{id}`: 개발자 이상, 사람만. `revoked_at`, 204(이미 폐기여도 204).
- 인증: `Authorization: Bearer echo_agt_…`가 있으면 쿠키는 보지 않는다. 해시로 찾고 폐기·만료면 401. 에이전트는 자기 조직 경로의 GET·HEAD만, 그 밖은 403 `AGENT_READ_ONLY` + 기록. 사람만 경로(멤버·초대·토큰·`/me`·`/orgs` 목록·조직 만들기)도 403 `AGENT_READ_ONLY`.

### 5.8 권한 검사 (모든 요청)
1. 인증: Bearer → 에이전트, 아니면 세션 쿠키 → 사람, 둘 다 없으면 익명.
2. 공개 경로(`/healthz`, `/readyz`, `/api/v1/auth/github/login`, `/api/v1/auth/github/callback`, `/api/v1/auth/logout`) 밖에서 익명이면 401 `UNAUTHENTICATED`.
3. `/api/v1/orgs/{orgId}/…`: 사람은 그 조직 멤버가 아니면(조직이 없어도) 404 `ORGANIZATION_NOT_FOUND`. 에이전트는 토큰의 조직과 다르면 같은 404.
4. 경로마다 정한 최소 역할(§8.2)보다 낮으면 403 `ROLE_FORBIDDEN` + 기록. 사람만 경로에 에이전트면 403 `AGENT_READ_ONLY` + 기록.
5. 쿠키로 인증된 변경 요청은 Origin 확인(5.3).
6. 조직 안 리소스(서비스·환경·배포 요청)는 경로의 조직과 리소스의 `organization_id`가 다르면 각 리소스의 404(`SERVICE_NOT_FOUND` 등). 배포 요청 접수에서 환경이 서비스와 다른 조직이면 404 `ENVIRONMENT_NOT_FOUND`.
- 검사기는 앞으로 API 4·8·AI 파트가 쓸 수 있게 공개한다: `require_org_role(min_role, human_only=False)`.
- **검사 순서**: 검사는 라우터 의존성으로 본문 검증·리소스 조회보다 먼저 한다. 그래서 권한 없는 쓰기는 본문이 틀려도, 대상 id가 없어도 403이다. 경로 변수는 코드에서 `{org_id}`로 쓴다. `org_id`가 uuid 형식이 아니면 404 `ORGANIZATION_NOT_FOUND`(422가 아니다).
- **DB 세션**: 검사기는 요청 공용 세션(`get_session`)을 쓰지 않는다. `Depends(get_session_factory)`로 받은 팩토리로 짧은 세션(`async with factory() as s, s.begin()`)을 열어 세션·토큰·멤버를 읽고 닫는다. 공용 세션에서 자동 트랜잭션이 열려 서비스의 `session.begin()`이 실패하는 것을 막기 위해서다. 행위자·경로 틀·조직 id·팩토리는 `request.state`에 둔다.
- **예외 클래스**: 기존 코드는 바꾸지 않고 하위 클래스를 더한다: `UnauthenticatedError(UnauthorizedError)` 코드 `UNAUTHENTICATED`, `RoleForbiddenError`·`AgentReadOnlyError`·`OriginNotAllowedError`·`CannotChangeOwnRoleError`(`ForbiddenError`), `OrganizationNotFoundError` 등(`NotFoundError`), `LoginNotConfiguredError`(503).

### 5.9 거부 기록
- 403(`ROLE_FORBIDDEN`·`AGENT_READ_ONLY`·`CANNOT_CHANGE_OWN_ROLE`·`ORIGIN_NOT_ALLOWED`)마다 `audit_logs`에 한 줄: `organization_id`(경로의 조직, 없으면 에이전트 토큰의 조직, 둘 다 없으면 NULL), `actor_type`(USER·AGENT), `actor_user_id`·`actor_agent_token_id`, `action`(메서드 + 경로 틀, 예 `POST /api/v1/orgs/{org_id}/services/{service_id}/deploy-requests`), `path`(실제 경로), `reason`(오류 코드), `result = DENIED`, `request_id`, `created_at`.
- 요청 트랜잭션과 따로 커밋한다(요청은 실패해도 기록은 남는다). 403은 검사기에서도, 서비스(자기 역할 변경, 잠금 안 재확인)에서도 난다. 그래서 위 403 예외 전용 처리기(또는 한 곳의 `deny()` 도우미)가 `request.state`의 팩토리로 새 세션을 열어 기록을 커밋한 뒤 응답한다. 처리기가 `get_session_factory()`를 직접 부르지 않으므로 시험의 의존성 교체가 그대로 적용된다. 401·404는 기록하지 않는다. DELETE 금지 트리거(`prevent_append_only_delete()`, API 3 소유 함수를 그대로 씀).

### 5.10 API 2·3 조직 범위 (D10, D11)
- `services.organization_id`, `environments.organization_id` NOT NULL FK. 서비스 등록·환경 등록은 경로의 조직으로 만든다. 목록·조회·수정·삭제는 그 조직 것만.
- 경로: `/api/v1/environments…` → `/api/v1/orgs/{orgId}/environments…`, `/api/v1/services…` → `/api/v1/orgs/{orgId}/services…`, `/api/v1/deploy-requests/{id}` → `/api/v1/orgs/{orgId}/deploy-requests/{id}`. `/api/v1/environment-kinds`는 그대로(로그인만 필요).
- 이름: 서비스 이름은 Echo 전체 유일 유지(퍼블릭 주소 `{서비스}.{도메인}`). 환경 이름은 조직 안 유일(`uq_environments_org_name_live`).
- 환경 열쇠 이름 묶기(AD-4): 조직 열쇠 키 = slug의 `-`를 `_`로 바꾸고 대문자(`team-echo` → `TEAM_ECHO`). `env:` 비밀 참조는 `ECHO_SECRET_{키}__`(밑줄 두 개)로 시작해야 한다. 예 `env:ECHO_SECRET_TEAM_ECHO__AWS_SEOUL_KUBECONFIG`. 아니면 422 `SECRET_REF_NOT_IN_ORGANIZATION`. slug에 하이픈 연속이 없어 키에 `__`가 없으므로 `a-corp`와 `a-corp-x`가 서로의 열쇠를 가리킬 수 없다. `env:` 외 저장소(형식만 검사하는 것)는 이번에 바꾸지 않는다.
- 일꾼(`python -m app.worker`)은 인증과 무관하고 바뀌지 않는다.
- **바꿀 곳(빠뜨리기 쉬운 것)**:
  - 저장소: API용 `get_live_by_id`·`search_live`·`count_live`·`find_by_id`류는 `organization_id`를 필수 인자로 받는다. 일꾼이 쓰는 조회는 따로 두고 조직 조건 없이 그대로 둔다.
  - 환경 이름: `find_live_by_name`은 조직 범위. `environment_service.py`의 `NAME_UNIQUE_INDEX = "uq_environments_name_live"`를 새 인덱스 이름으로 바꾼다(안 바꾸면 동시 중복 등록이 409 대신 500).
  - 서비스 이름: `find_live_by_name`과 `uq_services_name_live`는 전역 그대로.
  - 배포 요청: `deploy_requests`에는 조직 칸이 없다. 단건·목록 조회는 `JOIN services ON … AND services.organization_id = :org`로 거른다.
  - 배포 요청 접수: 잠금 없는 1단계 읽기와 `FOR SHARE` 2단계 읽기 모두 환경을 조직 조건으로 읽는다(AC-11).

## 6. 기능 요구사항

- `FR-1` GitHub 로그인으로만 들어온다. 개발용·비상용 로그인은 없다(D9). 시험은 의존성 교체로 가짜 GitHub를 쓴다(설정으로 켤 수 없음).
- `FR-2` 세션은 httpOnly 쿠키, DB에는 해시만, 7일 만료, 로그아웃 즉시 무효(D1, AD-10).
- `FR-3` 처음 로그인하면 개인 조직이 생기고 본인이 어드민이다(D13). 누구나 조직을 만들 수 있다(D12).
- `FR-4` 역할은 조직마다 하나(뷰어 < 개발자 < 어드민). 한 사람이 여러 조직에 속한다(AD-1).
- `FR-5` 초대는 GitHub 아이디 + 역할, 로그인·`/me` 때 적용, 7일 만료(D7).
- `FR-6` 권한은 §8.2 표대로다(D4). 자기 역할 변경 금지, 마지막 어드민 보호(AD-2).
- `FR-7` 다른 조직의 리소스는 404로 숨긴다(AD-14).
- `FR-8` 에이전트 토큰은 조직 하나에 묶이고 그 조직 읽기만 한다. 원문은 발급 때 한 번만(AD-3, AD-15).
- `FR-9` 403은 `audit_logs`에 남는다(D5, AD-6).
- `FR-10` 로그인 허용 스위치(D2): 기본 모든 GitHub 계정, 설정하면 GitHub 조직 멤버만.
- `FR-11` API 2·3 리소스는 조직에 속하고 경로가 `/api/v1/orgs/{orgId}` 아래다(D10, D11).
- `FR-12` 환경 열쇠 이름은 조직 키로 묶인다(AD-4).
- `FR-13` 세션 쿠키 값, 에이전트 토큰 원문(발급 응답 제외), GitHub 사용자 토큰, GitHub App client secret은 로그·응답·DB·예외 메시지에 0회 나온다(P4).

## 7. 상태와 예외

### 7.1 초대 상태 (`InvitationStatus`): `PENDING` → `ACCEPTED` | `EXPIRED` | `REVOKED`
- CHECK: `ACCEPTED ⇔ accepted_at IS NOT NULL`, `status` 값. 부분 unique (`organization_id`, `github_id`) WHERE `status = 'PENDING'`.
### 7.2 역할 (`OrganizationRole`): `ADMIN` · `DEVELOPER` · `VIEWER` (순서 VIEWER < DEVELOPER < ADMIN)
### 7.3 오류 코드
`UNAUTHENTICATED`(401), `ORIGIN_NOT_ALLOWED`·`ROLE_FORBIDDEN`·`AGENT_READ_ONLY`·`CANNOT_CHANGE_OWN_ROLE`(403), `ORGANIZATION_NOT_FOUND`·`MEMBER_NOT_FOUND`·`INVITATION_NOT_FOUND`·`AGENT_TOKEN_NOT_FOUND`(404), `ORGANIZATION_SLUG_CONFLICT`·`ORGANIZATION_SLUG_RESERVED`·`ALREADY_MEMBER`·`LAST_ADMIN`·`INVITATION_NOT_PENDING`(409), `GITHUB_USER_NOT_FOUND`·`SECRET_REF_NOT_IN_ORGANIZATION`·`VALIDATION_ERROR`(422), `GITHUB_UNAVAILABLE`(502, GitHub 403·429 한도 초과 포함), `LOGIN_NOT_CONFIGURED`(503). 로그인 콜백 오류는 프론트 주소의 `error` 쿼리: `OAUTH_DENIED`, `OAUTH_STATE_MISMATCH`, `GITHUB_UNAVAILABLE`, `LOGIN_NOT_ALLOWED`, `LOGIN_NOT_CONFIGURED`.
### 7.4 경계 조건
- 같은 사람이 두 브라우저에서 로그인하면 세션 두 개.
- 탈퇴·내보내진 사람의 세션은 살아 있지만 그 조직 경로는 404.
- 에이전트 토큰을 발급한 개발자가 조직을 떠나도 토큰은 남는다(조직의 개발자가 폐기).

## 8. 계약

### 8.1 새 엔드포인트 (`/api/v1`, `ApiResponse` 봉투·camelCase)

| 메서드 · 경로 | 누가 | 성공 |
|---|---|---|
| `GET /auth/github/login?next` | 누구나 | 302 GitHub |
| `GET /auth/github/callback` | 누구나 | 302 프론트 + 세션 쿠키 |
| `POST /auth/logout` | 누구나 | 204 |
| `GET /me` | 사람 | 200 `{user, organizations[{id, slug, name, personal, role}]}` |
| `POST /orgs` · `GET /orgs` | 사람 | 201 · 200 |
| `GET /orgs/{orgId}` | 뷰어 이상(에이전트 가능) | 200 |
| `GET /orgs/{orgId}/members` | 뷰어 이상, 사람 | 200 Page |
| `PATCH /orgs/{orgId}/members/{userId}` | 어드민 | 200 |
| `DELETE /orgs/{orgId}/members/{userId}` | 어드민(본인은 누구나) | 204 |
| `POST · GET /orgs/{orgId}/invitations`, `DELETE …/{invitationId}` | 어드민 | 201 · 200 · 204 |
| `POST · GET /orgs/{orgId}/agent-tokens`, `DELETE …/{tokenId}` | 개발자 이상, 사람 | 201(원문 한 번) · 200 · 204 |

### 8.2 옮긴 API 2·3 경로의 최소 역할

| 경로 (`/api/v1/orgs/{orgId}` 아래) | 최소 역할 |
|---|---|
| `GET /environments`, `GET /environments/{id}` | 뷰어(에이전트 가능) |
| `POST /environments`, `PATCH /environments/{id}`, `POST /environments/{id}/connection-check` | 개발자 |
| `DELETE /environments/{id}` | 어드민 |
| `GET /services`, `GET /services/{id}`, `GET /services/{id}/deploy-requests`, `GET /deploy-requests/{id}` | 뷰어(에이전트 가능) |
| `POST /services`, `PATCH /services/{id}`, `POST /services/{id}/deploy-requests` | 개발자 |
| `DELETE /services/{id}` | 어드민 |
| `GET /api/v1/environment-kinds`(조직 밖) | 로그인한 누구나 |

### 8.3 데이터 (모델 → revision 1개 → `db-schema.sql`)
- 마이그레이션은 `services`·`environments` 행이 0개라고 가정한다. 있으면 분명한 메시지로 실패한다(AD-13). 기존 표에 더한 칸은 `sort_order=APPENDED_COLUMN_SORT_ORDER`.
- `users`: `id`, `github_id` bigint unique, `github_login` varchar(39), `name` NULL, `avatar_url` NULL, 타임스탬프.
- `organizations`: `id`, `slug` varchar(39) unique(CHECK 형식), `name` varchar(100), `personal` bool, `created_by_user_id` FK, 타임스탬프.
- `organization_members`: `id`, `organization_id` FK, `user_id` FK, `role`(CHECK), 타임스탬프. unique (`organization_id`, `user_id`). 인덱스 (`user_id`).
- `invitations`: `id`, `organization_id` FK, `github_id` bigint, `github_login`, `role`, `status`, `invited_by_user_id` FK, `expires_at`, `accepted_at` NULL, 타임스탬프. §7.1 CHECK·부분 unique.
- `sessions`: `id`, `user_id` FK, `token_hash` char(64) unique, `expires_at`, `revoked_at` NULL, `created_at`.
- `agent_tokens`: `id`, `organization_id` FK, `name` varchar(100), `token_hash` char(64) unique, `last4` char(4), `created_by_user_id` FK, `expires_at`, `revoked_at` NULL, `last_used_at` NULL, `created_at`.
- `audit_logs`: §5.9 칸. 인덱스 (`organization_id`, `created_at`). DELETE 금지 트리거.
- `services` + `organization_id` NOT NULL FK. `environments` + `organization_id` NOT NULL FK, `uq_environments_name_live` → `uq_environments_org_name_live`(`organization_id`, `name`) WHERE 살아 있음.
- downgrade: 새 표·칸을 지우고 전역 `uq_environments_name_live`를 다시 만든다(조직끼리 같은 환경 이름이 있으면 실패한다고 docstring에 적음). `prevent_append_only_delete()` 함수는 API 3 소유라 지우지 않고 `audit_logs` 트리거만 지운다.
- 시험 준비(경로 이동과 인증 때문에 기존 시험이 대부분 바뀐다):
  - `tests/integration/conftest.py` `TABLES`에 새 표 7개.
  - 공용 픽스처 `member_client(role)`: 사용자·조직·멤버·세션 행을 DB에 직접 넣고, 세션 쿠키 + 허용 Origin 헤더를 실은 클라이언트. 시험 설정은 `session_cookie_secure=False`(httpx는 `http://test`에 Secure 쿠키를 돌려보내지 않는다) 또는 base_url `https://test`. 에이전트용 `agent_client`.
  - `factories.py`: `make_service`·`make_environment`가 조직을 받고, `secret_ref_for`는 `ECHO_SECRET_{조직 키}__…`를 만든다.
  - 경로 상수가 있는 시험을 `/api/v1/orgs/{org_id}/…` 함수로: `deploy_harness.py`(SERVICES·DEPLOY_REQUESTS), `test_environment_api.py`(BASE, 비밀 환경변수 이름), `test_service_api.py`, `test_service_delete.py`, `test_deploy_concurrency.py`(DELETE 경로), `test_deploy_flow.py`, `test_deploy_recovery.py`, `test_secret_exposure.py`, `test_environment_config_guard.py`, `tests/test_entrypoints.py`(OpenAPI 경로 단언) 등 경로·`factories`를 쓰는 모든 시험.
  - `test_migrations.py`: 표 목록·`EXPECTED_CONSTRAINTS`·인덱스·트리거(`uq_environments_name_live` 교체, `audit_logs` 트리거, 새 CHECK·부분 unique) 갱신. `test_migration_downgrade_one_removes_connection_columns_and_upgrade_restores`는 연결 칸 이전 revision으로 내린 뒤 행을 넣고 API 3 head(`d95d675cb256`)까지만 올려 확인한 다음, 행을 지우고 head로 올린다. 행이 있으면 upgrade가 "environments has 1 rows"처럼 분명히 실패하는 시험을 더한다.

### 8.4 설정 추가 (`Settings`)
`github_app_client_id`(None), `github_app_client_secret_ref`(None, 있으면 `parse_secret_ref`로 검증), `github_oauth_base_url`(기본 `https://github.com`), `github_oauth_redirect_uri`(None, 이 API 호스트의 콜백 공개 주소), `frontend_base_url`(None, 끝 `/` 정규화), `frontend_allowed_origins`(기본 빈 목록, 환경변수는 쉼표 구분 — `NoDecode` + validator), `session_cookie_name`(`echo_session`), `session_ttl_seconds`(604800), `session_cookie_secure`(true), `oauth_state_ttl_seconds`(600), `login_allowed_github_org`(None = 모두), `reserved_org_slugs`(기본 `team-echo`, 쉼표 구분), `agent_token_default_days`(30), `agent_token_max_days`(90), `invitation_ttl_seconds`(604800). 모든 새 설정에 기본값이 있어 `Settings()`·alembic·일꾼·단위 시험이 그대로 뜬다. 로그인 설정이 비면 5.1-0. `frontend_allowed_origins`가 비면 쿠키 인증 쓰기가 모두 403이므로 README에 적는다. `.env.example`에 키와 예시.

### 8.5 GitHub 사용자 클라이언트 (`app/clients/github_user.py`)
- 인터페이스: `exchange_code(code, redirect_uri) -> 사용자 토큰(메모리)`, `get_user(token) -> GithubUser(id, login, name, avatar_url)`, `is_active_org_member(token, org) -> bool`, `find_user(login) -> GithubUser | None`.
- 실제 구현은 httpx(제한 시간 10초). 시험은 `httpx.MockTransport`로만. 가짜 구현은 시험 폴더에만 두고 앱 설정으로는 고를 수 없다(FR-1).

### 8.6 데모 준비 명령 (P3)
`python -m app.scripts.demo_seed --org team-echo --admin <GitHub 아이디> --viewer <GitHub 아이디>`: 이미 로그인한 적 있는 사용자만 쓴다(없으면 분명히 실패). 조직이 없으면 만들고(예약 slug도 이 명령은 만들 수 있다) 어드민 멤버, 뷰어를 멤버로 추가, 에이전트 토큰 하나를 발급해 원문을 표준 출력에 한 번 쓴다. 두 번 돌려도 조직·멤버는 늘지 않는다. 조직이 이미 있는데 만든 사람이 `--admin`이 아니면 실패한다(남의 조직에 팀을 넣지 않음).

## 9. 호환성·배포·복구
- API 2·3 경로가 모두 바뀐다(깨지는 변경). 프론트는 아직 붙이지 않았다 → echo-frontend 이슈로 알린다(M1, 승인 뒤). 문서·예시를 모두 새 경로로.
- 환경 열쇠 이름 예시가 `ECHO_SECRET_<조직 키>__…`로 바뀐다. README·`.env.example`·`docs/api/environments.md` 예시를 바꾼다.
- 롤백(코드): revision `downgrade`로 새 표·칸을 지운다(시험으로 왕복 확인).
- 실제 로그인은 GitHub App 생성 뒤(D8, D9). 그 전까지 데모·프론트 개발은 실제 로그인을 쓸 수 없다.

## 10. Acceptance criteria

시험은 가짜 GitHub(의존성 교체), 주입 시계로 결정적으로 돌린다(실제 네트워크 0).

- `AC-1` 로그인: `/auth/github/login` → 302(`client_id`·`redirect_uri`·`state`) + 상태 쿠키 → 콜백 → 사용자 생성, 개인 조직(`slug` = 아이디 소문자, `personal`, 본인 ADMIN), 세션 쿠키(HttpOnly·SameSite=Lax·Max-Age 7일, 설정에 따른 Secure) → 302 `frontend_base_url + next`. 두 번째 로그인은 개인 조직을 더 만들지 않고 아이디·이름을 갱신한다. 같은 사람의 콜백 두 개를 동시에 보내도 사용자·개인 조직은 하나, 500 없음. 로그인 설정이 비면 503 `LOGIN_NOT_CONFIGURED`(다른 API는 정상).
- `AC-2` 콜백 실패: `state` 없음·다름 → `OAUTH_STATE_MISMATCH`, `access_denied` → `OAUTH_DENIED`, GitHub 장애 → `GITHUB_UNAVAILABLE`, 사용자·세션 생성 0. `next`에 외부 주소를 넣어도 우리 프론트로만 간다.
- `AC-3` 허용 스위치: 조직 설정 + 비멤버 → `LOGIN_NOT_ALLOWED`, 멤버 → 통과. 설정 없음 → 누구나.
- `AC-4` 세션: `/me` 200(조직·역할 목록), 시계를 7일 넘기면 401, 로그아웃 뒤 같은 쿠키 401. DB 덤프에 쿠키 원문 0회.
- `AC-5` Origin: 쿠키 인증 POST에 허용 밖 Origin → 403 `ORIGIN_NOT_ALLOWED` + 기록, 허용 Origin → 통과. Bearer 요청은 Origin을 보지 않는다.
- `AC-6` 조직: 만들기 201(만든 사람 ADMIN), slug 규칙 위반 422(하이픈 연속 포함), 겹침 409, `GET /orgs`는 내 조직만. 예약 slug(`team-echo`) 409 `ORGANIZATION_SLUG_RESERVED`. 개인 조직 slug가 겹치거나 예약이면 `-2`, 39자 아이디 + 겹침이어도 규칙에 맞는 slug.
- `AC-7` 초대: 어드민 초대 → PENDING(7일) → 그 사람 로그인 → 정한 역할로 멤버, ACCEPTED. 이미 로그인해 있던 사람은 `/me` 호출로 적용. `/me`를 동시에 두 번 불러도 멤버 1줄, 500 없음. 만료(시계) → 적용 안 됨, EXPIRED. 취소 → REVOKED. 재초대는 앞 초대를 REVOKED. 이미 멤버 409. 없는 GitHub 아이디 422. 개발자의 초대 시도 403 + 기록.
- `AC-8` 멤버: 어드민이 남의 역할 변경 200, 자기 역할 403 `CANNOT_CHANGE_OWN_ROLE` + 기록, 탈퇴 204, 마지막 어드민 탈퇴 409. 두 어드민이 동시에 서로를 내보내도(DELETE), 서로를 뷰어로 내려도(PATCH) 어드민 ≥ 1(통합 시험). 이때 진 쪽 요청은 403 `ROLE_FORBIDDEN` 또는 409 `LAST_ADMIN`.
- `AC-9` 역할 × 경로 표: §8.1·§8.2의 모든 경로를 익명·비멤버·뷰어·개발자·어드민·에이전트로 불러 기대 상태(401·404·403·2xx)와 일치한다(표 기반 시험).
- `AC-10` 조직 격리(P1): 앱에 등록된 모든 `/api/v1/orgs/{org_id}` 경로를 다른 조직 멤버로 불러 404. 공개 목록 밖 모든 경로를 익명으로 불러 401. 조직 경로인데 검사기가 빠진 경로가 생기면 이 시험이 실패한다. 판정: 앱의 route 목록을 자동으로 모아 `/api/v1/orgs/{org_id}` 아래 모든 route의 의존성에 `require_org_role`이, 공개 목록 밖 모든 route에 인증 의존성이 있는지 본다. `org_id`가 uuid가 아니면 404.
- `AC-11` 교차 조직: A 조직 서비스에 B 조직 환경으로 배포 요청 → 404 `ENVIRONMENT_NOT_FOUND`. B 경로로 A의 배포 요청 조회 → 404. 두 조직에 같은 환경 이름 가능, 서비스 이름은 전체에서 409.
- `AC-12` 에이전트 토큰: 발급 응답에만 원문, 목록에는 `last4`만. 그 조직 GET 200, 다른 조직 404, POST(배포 요청) 403 `AGENT_READ_ONLY` + 기록(actor AGENT) — **데모 거부 장면**. 폐기·만료 → 401. 멤버·초대·토큰 경로 → 403.
- `AC-13` 거부 기록: 위 403마다 `audit_logs` 한 줄(조직, 행위자, 경로 틀, 이유, 요청 ID), 요청이 실패해도 남음. 401·404는 0줄. `DELETE FROM audit_logs`는 트리거가 거부.
- `AC-14` 환경 열쇠: `team-echo` 조직에서 `env:ECHO_SECRET_TEAM_ECHO__X` 201, `env:ECHO_SECRET_X`·`env:ECHO_SECRET_A_CORP_X__Y`(a-corp 조직에서) 422 `SECRET_REF_NOT_IN_ORGANIZATION`.
- `AC-15` 비밀값(P4): 세션 쿠키 값, 에이전트 토큰 원문, 가짜 GitHub 사용자 토큰, client secret 시험 값이 로그(caplog)·응답(발급 응답 제외)·DB 모든 표 텍스트에 0회.
- `AC-16` 실제 GitHub 사용자 클라이언트: MockTransport로 코드 교환·`/user`·조직 멤버십(200 active·200 pending·403·404 → 통과·불허·불허·불허)·`/users/{login}`(200·404·403·429)·5xx·시간 초과를 시험해 §7.3 코드로 바뀌는지 확인. 실제 네트워크 0.
- `AC-17` 데모 준비 명령(P3): 빈 DB + 로그인한 사용자 2명 → 조직·멤버·토큰 생성, 다시 돌려도 늘지 않음, 로그인 안 한 아이디는 분명히 실패. 출력된 토큰으로 이전 대신 배포 요청 POST가 403 + 기록.
- `AC-18` 스키마: revision 왕복, `alembic check` 0, `dump_schema.sh` 일치, `test_migrations.py` 새 CHECK·부분 unique·트리거 비교 통과, 서비스·환경 행이 있으면 upgrade가 분명한 메시지로 실패.
- `AC-19` 문서: `docs/api/auth.md`(로그인 흐름·쿠키·Origin·오류), `docs/api/organizations.md`, API 2·3 문서 새 경로·역할, `docs/schema.md`, 용어 사전(역할·조직·초대·에이전트 토큰, 옛 "운영자·승인자" 줄 교체), README(GitHub App 로그인 설정, **데모 전날 점검 목록(P2)**: 콜백 주소 = API 호스트의 `/api/v1/auth/github/callback`이고 App 콜백 목록과 글자까지 같음, client secret 참조, `frontend_base_url`·`frontend_allowed_origins`, 허용 스위치를 쓸 때 App의 그 조직 설치 + Members 읽기 권한, 로그인 열기 전 `demo_seed` 실행. 데모 준비 명령, 알려진 제한(GitHub App 설치가 조직에 묶이지 않음)).
- `AC-20` 품질: ruff·format·`mypy app`·pytest 통과, 통합 시험 건너뜀 0.

## 11. 핵심 엔티티

| 이름 | 뜻 | 관계 |
|---|---|---|
| 사용자 `users` | GitHub로 로그인한 사람 | 멤버 N, 세션 N |
| 조직 `organizations` | 고객 회사(개인 조직 포함) | 멤버 N, 초대 N, 토큰 N, 서비스 N, 환경 N |
| 조직 멤버 `organization_members` | 사람 × 조직 × 역할 | 사용자 1, 조직 1 |
| 초대 `invitations` | GitHub 번호 × 역할 대기 | 조직 1 |
| 세션 `sessions` | 브라우저 로그인 하나 | 사용자 1 |
| 에이전트 토큰 `agent_tokens` | AI 에이전트 출입증 | 조직 1 |
| 거부 기록 `audit_logs` | 막힌 요청 한 건 | 조직 0..1 |

## 12. 리스크 (사전 부검, 지민 확인 D1)

| 리스크 | 처리 | 연결 |
|---|---|---|
| P1 남의 회사 자료가 보임 | 요구사항화: 공통 검사기 + 경로 자동 수집 격리 시험 | FR-7, AC-9, AC-10, AC-11 |
| P2 데모 날 로그인 실패 | 요구사항화: 콜백·프론트 주소 설정, 로그인 통합 시험, README 점검 목록 | AC-1~4, AC-19 |
| P3 거부 장면이 안 나옴 | 요구사항화: 데모 준비 명령 + 거부 기록 확인 | AC-12, AC-17 |
| P4 토큰 유출 | 요구사항화: 해시 저장, 만료·폐기, 0회 노출 시험 | FR-13, AC-15 |

## 13. 결정 기록

| ID | 결정 | 근거 | 출처 |
|---|---|---|---|
| P0 | Echo = 여러 고객의 앱을 배포하는 서비스. 역할은 고객 조직 기능. Echo 자체 다중 리전은 별개 | 지민 정정 | 사용자 R6 |
| D1 | 세션 = httpOnly 쿠키 + 서버 세션 표, SameSite=Lax + 변경 요청 Origin 확인 | XSS에 세션 값 노출 없음, 즉시 무효화 | 사용자 R1 2A |
| D2 | 로그인 허용 스위치, 기본 모든 GitHub 계정 | #11 결과가 달라도 설정만 바꿈 | 사용자 R1 4A |
| D4 | 되돌릴 수 있는 일 = 개발자(배포·롤백·이전·AI 반영 승인·토큰), 삭제·사람 관리 = 어드민 | Railway·Render 역할 분리 | 사용자 R3 |
| D5 | 403을 `audit_logs`에 최소로 기록, 조회는 API 7 | #6 완료 기준 "기록된다" | AI-default, 지민 확인 C0 |
| D7 | 초대 = GitHub 아이디 + 역할, 로그인 순간 적용, 7일 만료, 숫자 번호 저장 | 수락 화면 불필요, GitHub 조직 초대와 같은 기간 | 사용자 R4 8A |
| D8 | 로그인 앱 = API 3의 GitHub App 재사용 | 앱·비밀 한 곳, 콜백 10개 | 사용자 R4 9A |
| D9 | GitHub 로그인만, 개발·비상 로그인 없음 | 공격 경로 최소 | 사용자 R4 10B |
| D10 | 조직 1단(조직 안에 서비스·환경), 프로젝트는 2차 | 11/7 일정 | 사용자 R7 |
| D11 | 조직은 경로 `/api/v1/orgs/{orgId}`로 | GitHub API 방식, 검사 누락 방지 | 사용자 R8 13A |
| D12 | 로그인한 누구나 조직 생성 | 4A와 짝, 회원 가입 | 사용자 R8 14A |
| D13 | 처음 로그인 때 개인 조직 자동 생성 | Render·Supabase 방식, 규칙 하나 유지 | 사용자 R10 15A |
| AD-1~16 | 체크포인트 목록 그대로(여러 조직, 자기 역할·마지막 어드민, 토큰 조직 묶기·읽기만, 열쇠 이름 묶기, 조직 삭제 제외, 403 기록, 경로 이동 시점, 이름 유일 범위, 조직 id·slug 고정, 세션 규칙, 초대 규칙, state·콜백, 마이그레이션 실패, 남의 조직 404, 토큰 규칙, 데모 거부 장면) | 체크포인트 근거 | AI-default, 지민 확인 C0 |
| AD-17 | `state`는 httpOnly 상태 쿠키와 비교(10분) | 서버 표 없이 위조 방지 | AI-default(SPEC 작성) |
| AD-18 | 초대 적용 시점에 `GET /me` 추가 | 이미 로그인한 사람이 재로그인 없이 들어옴(D7 뜻 유지) | AI-default(SPEC 작성) |
| AD-19 | Bearer가 있으면 쿠키 무시, 에이전트는 멤버·초대·토큰·`/me`·조직 목록 불가 | 사람 정보·출입증 관리는 사람만 | AI-default(SPEC 작성) |
| AD-20 | 열쇠 키 구분자 `__`, slug에 하이픈 연속 금지 | `a-corp`/`a-corp-x` 접두어 겹침 방지(AD-4 구현 세부) | AI-default(SPEC 작성) |
| AD-21 | 가짜 GitHub는 시험의 의존성 교체로만, 설정으로 못 켬 | D9(개발용 로그인 없음) 유지 | AI-default(SPEC 작성) |
| AD-22 | 데모 준비 명령은 로그인한 적 있는 사용자만 | GitHub 번호를 실제 로그인으로 얻음, 네트워크 없는 시험 | AI-default(SPEC 작성) |
| AD-23 | 401·404는 기록하지 않음, 거부 기록은 요청과 따로 커밋 | 익명·비멤버 소음 제외, 실패해도 기록 | AI-default(SPEC 작성) |
| AD-24 | 세션 만료 연장 없음(로그인 후 7일 고정) | 단순, 데모 기간 충분 | AI-default(SPEC 작성) |
| AD-25 | 예약 slug(`team-echo`)는 데모 준비 명령만 만든다. 명령은 남이 만든 같은 이름 조직에 팀을 넣지 않는다 | 외부인이 먼저 `team-echo` 조직을 만들어 운영 열쇠 `ECHO_SECRET_TEAM_ECHO__…`를 쓰는 것을 막음(검토 지적, 최소 방어) | AI-default(fresh-eyes) |
| AD-26 | GitHub App 설치를 조직에 묶는 일은 이번 범위 밖, README에 알려진 제한으로 | 데모 범위, 조직별 허용 저장소는 생성 규칙(D12)까지 건드림 | AI-default(fresh-eyes) |
| AD-27 | 검사기는 짧은 별도 DB 세션, 거부 기록은 403 처리기가 `request.state`의 팩토리로 따로 커밋 | 공용 세션 자동 트랜잭션 충돌·시험 우회 방지 | AI-default(fresh-eyes) |
| AD-28 | 로그인 설정은 모두 선택값, 비면 503 `LOGIN_NOT_CONFIGURED` | GitHub App 생성 전에도 API·일꾼·alembic이 뜸 | AI-default(fresh-eyes) |
| AD-29 | 멤버 변경은 조직 `FOR NO KEY UPDATE` 잠금 안에서 행위자 역할 재확인 + PATCH·DELETE 모두 마지막 어드민 검사 | 동시 상호 강등으로 어드민 0명 방지 | AI-default(fresh-eyes) |
| AD-30 | 첫 로그인·초대 적용·개인 slug는 `ON CONFLICT`/SAVEPOINT로 | 중복 키 뒤 트랜잭션 중단(500) 방지 | AI-default(fresh-eyes) |

## 14. 구현 계획으로 위임한 low-impact 세부사항
- 모듈·클래스 이름, 검사기 구현 방식(의존성·미들웨어 조합), 상태 쿠키 서명 방식
- `last_used_at` 갱신 빈도, 목록 정렬·페이지 기본값(API 2와 같게)
- 오류 코드별 고정 문장
- OpenAPI 문서(`/docs`) 공개 여부(공개 목록에 넣을지)
- 데모 준비 명령의 출력 형식
