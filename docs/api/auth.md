# 로그인·세션·권한 검사 (API 서버 6)

사람은 **GitHub 로그인**으로 들어오고, AI 에이전트는 조직에서 발급한 **에이전트 토큰**으로 호출한다. 근거: SPEC [specs/api-6-github-login-roles.md](../../specs/api-6-github-login-roles.md), 원장 F4·F5·F16, NFR-07. 조직·멤버·초대·토큰 API 는 [organizations.md](organizations.md).

- 공통 봉투·camelCase·null 규칙·`X-Request-ID` 는 [environments.md](environments.md#공통-규칙)와 같다.
- **개발용·비상용 로그인은 없다.** 로그인 경로는 GitHub 하나다. 시험은 가짜 GitHub 를 의존성 교체로만 넣는다(설정으로 켤 수 없다).
- 실제 GitHub App 을 만들고 설정하기 전에는 실제 로그인을 할 수 없다(로그인·콜백은 503 `LOGIN_NOT_CONFIGURED`). 나머지 API·일꾼·alembic 은 로그인 설정 없이도 뜬다.

## 엔드포인트 (공개 경로)

| 메서드 · 경로 | 성공 | 설명 |
|---|---|---|
| `GET /api/v1/auth/github/login?next=/path` | 302 GitHub | 로그인 시작. 상태 쿠키를 내려 주고 GitHub 인증 화면으로 보낸다 |
| `GET /api/v1/auth/github/callback?code&state` | 302 프론트 | 세션 쿠키를 내려 주고 `{frontendBaseUrl}{next}` 로. 실패는 `{frontendBaseUrl}/login?error=<코드>` |
| `POST /api/v1/auth/logout` | 204 | 세션 폐기 + 쿠키 삭제. 세션이 없어도 204 |
| `GET /api/v1/me` | 200 | 사람만. `{user, organizations[{id, slug, name, personal, role, createdAt}]}`. 부를 때 대기 초대를 적용한다 |

`/healthz`·`/livez`·`/readyz`·위 세 auth 경로·`/docs`·`/openapi.json` 밖의 모든 경로는 로그인이 필요하다(없으면 401 `UNAUTHENTICATED`).

## 로그인 흐름

```text
브라우저 ──GET /auth/github/login?next=/services──▶ API
         ◀── 302 github.com/login/oauth/authorize?client_id&redirect_uri&state
             + Set-Cookie echo_oauth_state (HttpOnly, SameSite=Lax, 10분, Path=/api/v1/auth/github)
브라우저 ──GitHub 승인──▶ GitHub ──302──▶ API /auth/github/callback?code&state
API: state == 쿠키 값(상수 시간 비교, 10분 이내)?
     code → 사용자 토큰(메모리만) → GET /user → (허용 조직 설정 시) GET /user/memberships/orgs/{org}
     트랜잭션: 사용자 넣기·갱신 → (처음이면) 개인 조직 + ADMIN → 대기 초대 적용 → 세션
         ◀── 302 {frontendBaseUrl}/services + Set-Cookie echo_session, 상태 쿠키 삭제
```

- `next` 는 `/` 로 시작하는 상대 경로만 받는다. `//host`·`/\host`·스킴·제어 문자가 섞이면 `/` 로 바꾼다. 그래서 로그인 뒤에는 언제나 우리 프론트로만 간다.
- 상태 쿠키는 이 API 호스트에 내려간다. 그래서 `GITHUB_OAUTH_REDIRECT_URI` 는 **같은 호스트의** `/api/v1/auth/github/callback` 이어야 한다.
- GitHub 사용자 토큰은 메모리에서만 쓰고 저장하지 않는다.
- 같은 사람의 콜백 두 개가 동시에 와도 사용자·개인 조직은 하나다(`ON CONFLICT DO NOTHING`).
- 처음 로그인하면 개인 조직이 생긴다. slug 는 GitHub 아이디 소문자이고, 겹치거나 예약이면 `-2`, `-3` …, 그래도 안 되면 `user-{GitHub 번호}` 다.

### 콜백 실패 (`/login?error=<코드>`)

| 코드 | 상황 |
|---|---|
| `OAUTH_DENIED` | 사용자가 GitHub 화면에서 거절(`error=access_denied` 등), 또는 GitHub 가 code 를 받지 않음(만료·재사용) |
| `OAUTH_STATE_MISMATCH` | `state` 가 없거나 상태 쿠키와 다름, 상태 쿠키 없음·10분 지남 |
| `GITHUB_UNAVAILABLE` | GitHub 장애·시간 초과·한도 초과 |
| `LOGIN_NOT_ALLOWED` | 허용 스위치(`LOGIN_ALLOWED_GITHUB_ORG`)가 있고 그 GitHub 조직의 active 멤버가 아님(멤버십 확인 403·404 포함) |
| `LOGIN_NOT_CONFIGURED` | 로그인 설정이 비어 있음(프론트 주소도 없으면 503 JSON) |

실패하면 사용자·세션을 만들지 않는다. 상태 쿠키는 어느 경우든 지운다.

## 세션 쿠키

| 속성 | 값 |
|---|---|
| 이름 | `echo_session` (`SESSION_COOKIE_NAME`). 데모 배포는 `__Host-echo_session`: 고객 앱(`*.echoport.uk`)이 같은 등록 도메인 아래라 `Domain=` 쿠키로 덮어쓰지 못하게 한다(API 서버 3-1, README "데모 배포 주소") |
| Domain | 붙이지 않는다(API 호스트 전용). `__Host-` 이름의 조건(Secure·`Path=/`·Domain 없음)을 만족한다 |
| 값 | 무작위 32바이트(base64url). DB `sessions` 에는 SHA-256 만 둔다 |
| 속성 | `HttpOnly`, `SameSite=Lax`, `Path=/`, `Secure`(`SESSION_COOKIE_SECURE`, 기본 true), `Max-Age=604800`(7일) |
| 만료 | 로그인 후 7일 고정(연장 없음). 로그아웃하면 즉시 무효 |

- 요청마다 쿠키 해시로 세션을 찾고 `revoked_at IS NULL AND expires_at > now` 면 그 사람이다. 아니면 401 `UNAUTHENTICATED` 이고 응답에 쿠키 삭제 `Set-Cookie` 를 싣는다.
- 같은 사람이 두 브라우저에서 로그인하면 세션 두 개다.
- 프론트는 `fetch(..., {credentials: "include"})` 로 부른다. CORS 는 `FRONTEND_ALLOWED_ORIGINS` + `allow_credentials` 다.

## 변경 요청 Origin 확인

쿠키로 인증한 **GET·HEAD·OPTIONS 밖의 모든 요청**(조직 밖 `POST /orgs` 포함)은 `Origin` 헤더가 `FRONTEND_ALLOWED_ORIGINS` 에 **정확히** 있어야 한다. 없거나 다르면 403 `ORIGIN_NOT_ALLOWED`(거부 기록 남음). SameSite=Lax 에 더한 CSRF 둘째 겹이다. 에이전트 토큰(Bearer) 요청은 Origin 을 보지 않는다.

> `FRONTEND_ALLOWED_ORIGINS` 가 비어 있으면 쿠키로 인증한 쓰기 요청이 모두 403 이다.

## 에이전트 토큰 인증

- `Authorization: Bearer echo_agt_…` 가 있으면 쿠키는 보지 않는다. 해시로 찾고 폐기·만료·모르는 토큰이면 401 `UNAUTHENTICATED`.
- 에이전트는 **토큰의 조직 경로의 GET·HEAD 만** 한다. 그 밖(POST·PATCH·DELETE)은 403 `AGENT_READ_ONLY`. 사람만 쓰는 경로(멤버·초대·토큰·`/me`·`GET/POST /orgs`)도 403 `AGENT_READ_ONLY`. 다른 조직 경로는 404 `ORGANIZATION_NOT_FOUND`. `GET /api/v1/environment-kinds` 는 읽을 수 있다.
- 마지막 사용 시각(`lastUsedAt`)은 1분에 한 번만 적는다.

## 권한 검사 순서 (모든 요청)

1. 인증: Bearer → 에이전트, 아니면 세션 쿠키 → 사람, 둘 다 없으면 익명. 공개 경로 밖에서 익명이면 401.
2. `/api/v1/orgs/{orgId}/…`: 사람은 그 조직 멤버가 아니면(조직이 없어도, `orgId` 가 uuid 형식이 아니어도) 404 `ORGANIZATION_NOT_FOUND`. 에이전트는 토큰의 조직과 다르면 같은 404.
3. 경로의 최소 역할(아래 표)보다 낮으면 403 `ROLE_FORBIDDEN`. 사람만 경로·쓰기 경로에 에이전트면 403 `AGENT_READ_ONLY`.
4. 쿠키로 인증한 변경 요청은 Origin 확인.
5. 조직 안 리소스(서비스·환경·배포 요청)는 경로의 조직 것이 아니면 각 리소스의 404(`SERVICE_NOT_FOUND` 등).

검사는 라우터 의존성으로 **본문 검증·리소스 조회보다 먼저** 한다. 그래서 권한 없는 쓰기는 본문이 틀려도, 대상 id 가 없어도 403 이다(본문이 JSON 으로 읽히지 않을 때만 프레임워크가 먼저 422 를 준다).

### 역할 (`ADMIN` > `DEVELOPER` > `VIEWER`)

원칙: 되돌릴 수 있는 일(배포·롤백·이전 실행·AI 수정 운영 반영 승인·에이전트 토큰)은 **개발자**, 삭제·사람 관리는 **어드민**.

| 경로 (`/api/v1/orgs/{orgId}` 아래) | 최소 역할 | 에이전트 |
|---|---|---|
| `GET ` (조직 하나) · `GET /environments…` · `GET /services…` · `GET /services/{id}/deploy-requests` · `GET /deploy-requests/{id}` | 뷰어 | 읽기 가능 |
| `GET /deploy-requests/{id}/logs` (배포 작업·빌드·실행 로그, API 서버 4) | 뷰어 | 읽기 가능 |
| `GET /services/{id}/migration-plans` · `GET /migration-plans/{id}` · `GET /migration-plans/{id}/logs` (이전 계획, API 서버 8) | 뷰어 | 읽기 가능 |
| `GET /audit-logs` (감사 로그, API 서버 7) | 뷰어 | 읽기 가능 |
| `GET /security/exceptions` · `GET /security/exceptions/{id}` (기한부 예외, 보안 3) | 뷰어 | 읽기 가능 |
| `GET /members` | 뷰어(사람만) | 403 |
| `POST /environments` · `PATCH /environments/{id}` · `POST /environments/{id}/connection-check` | 개발자 | 403 |
| `POST /services` · `PATCH /services/{id}` · `POST /services/{id}/deploy-requests` | 개발자 | 403 |
| `POST /deploy-requests/{id}/rollback` (되돌리기, API 서버 4. 배포 요청 접수와 같은 검사기) | 개발자 | 403 |
| `POST·GET /agent-tokens` · `DELETE /agent-tokens/{id}` | 개발자(사람만) | 403 |
| `POST /services/{id}/migration-plans` · `POST /migration-plans/{id}/approve·reject·cancel·abort` (이전 계획 만들기·승인·거절·취소·중단, API 서버 8) | 개발자(사람만) | 403 |
| `POST /security/exceptions` · `POST /security/exceptions/{id}/approve·revoke` (기한부 예외 요청·승인·철회, 보안 3) | 개발자(사람만) | 403 |
| `DELETE /environments/{id}` · `DELETE /services/{id}` | 어드민 | 403 |
| `PATCH /members/{userId}` · `POST·GET /invitations` · `DELETE /invitations/{id}` | 어드민(사람만) | 403 |
| `PATCH /settings` (자기 승인 스위치 `allowSelfApproval`, API 서버 8) | 어드민(사람만) | 403 |
| `DELETE /members/{userId}` | 어드민(본인 탈퇴는 누구나) | 403 |

조직 밖: `GET /api/v1/me`·`GET·POST /api/v1/orgs` 는 로그인한 사람, `GET /api/v1/environment-kinds` 는 로그인한 누구나(에이전트 포함).

### 다른 태스크가 쓰는 검사기

롤백(API 서버 4)·이전(API 서버 8)·AI 반영(AI 파트) 엔드포인트는 같은 검사기를 건다(`app/auth/guards.py`).

```python
from app.auth.guards import HumanDeveloperAccess   # = require_org_role(DEVELOPER, human_only=True)

@router.post("/{service_id}/rollbacks")
async def create_rollback(access: HumanDeveloperAccess, ...) -> ...:
    ...  # access.organization_id, access.user
```

`require_org_role(min_role, human_only=False)` 를 직접 써도 된다. `/api/v1/orgs/{org_id}` 아래 라우트에 검사기가 빠지면 `tests/integration/test_authorization.py::test_every_route_has_guards` 가 실패한다.

## 거부 기록 (`audit_logs`)

403(`ROLE_FORBIDDEN`·`AGENT_READ_ONLY`·`CANNOT_CHANGE_OWN_ROLE`·`ORIGIN_NOT_ALLOWED`·`SELF_APPROVAL_FORBIDDEN`)마다 한 줄을 요청과 **따로 커밋**한다(요청은 실패해도 기록은 남는다). 401·404 는 기록하지 않는다. 행은 지우거나 고칠 수 없다(DELETE·UPDATE 금지 트리거). 성공 줄·끝 줄·에이전트 읽기 줄과 조회 API 는 [audit-logs.md](audit-logs.md).

| 칸 | 값 |
|---|---|
| `organization_id` | 경로의 조직, 없으면 에이전트 토큰의 조직, 둘 다 없으면 NULL |
| `actor_type` · `actor_user_id` · `actor_agent_token_id` | `USER`(사람) 또는 `AGENT`(토큰) 중 하나 |
| `action` | 쓰기 경로면 행동 이름(예 `DEPLOY_REQUESTED`, [audit-logs.md](audit-logs.md)). 표에 없는 경로와 API 서버 6 때 쌓인 줄은 메서드 + 경로 틀 |
| `route` | 메서드 + 경로 틀. 예 `POST /api/v1/orgs/{org_id}/services/{service_id}/deploy-requests` |
| `path` · `reason` · `result` · `request_id` · `created_at` | 실제 경로 · 오류 코드 · `DENIED` · `X-Request-ID` · 시각 |

## 오류 코드

| HTTP | code | 상황 |
|---|---|---|
| 401 | `UNAUTHENTICATED` | 로그인 안 함, 세션 만료·로그아웃, 토큰 폐기·만료·모름 |
| 403 | `ORIGIN_NOT_ALLOWED` | 쿠키 인증 변경 요청의 Origin 이 허용 목록에 없음 |
| 403 | `ROLE_FORBIDDEN` | 역할이 모자람(서비스가 잠금 안에서 다시 본 결과 포함) |
| 403 | `AGENT_READ_ONLY` | 에이전트의 쓰기·사람만 경로 |
| 403 | `SELF_APPROVAL_FORBIDDEN` | 이전 계획을 만든 사람의 승인·거절, 또는 기한부 예외를 요청한 사람의 승인인데 조직이 자기 승인을 막아 둠(API 서버 8, 보안 3) |
| 404 | `ORGANIZATION_NOT_FOUND` | 조직이 없거나 멤버가 아님(구분하지 않음) |
| 503 | `LOGIN_NOT_CONFIGURED` | 로그인 설정이 비어 있음(로그인 시작) |

## 설정

| 이름 | 기본 | 설명 |
|---|---|---|
| `GITHUB_APP_CLIENT_ID` | — | GitHub App 의 Client ID(API 서버 3 의 App 재사용) |
| `GITHUB_APP_CLIENT_SECRET_REF` | — | client secret 의 **비밀 참조**(`env:ECHO_SECRET_GITHUB_APP_CLIENT_SECRET`). 값은 프로세스 환경변수로만 |
| `GITHUB_OAUTH_REDIRECT_URI` | — | 이 API 호스트의 콜백 공개 주소. App 콜백 목록과 글자까지 같아야 한다 |
| `FRONTEND_BASE_URL` | — | 로그인 뒤 돌아갈 프론트 주소(끝 `/` 는 뗀다) |
| `FRONTEND_ALLOWED_ORIGINS` | 빈 목록 | 쉼표 구분. 쿠키 인증 쓰기 허용 Origin + CORS |
| `GITHUB_OAUTH_BASE_URL` | `https://github.com` | 인증 화면·code 교환 주소 |
| `SESSION_COOKIE_NAME` · `SESSION_TTL_SECONDS` · `SESSION_COOKIE_SECURE` | `echo_session` · `604800` · `true` | 세션 쿠키 |
| `OAUTH_STATE_TTL_SECONDS` | `600` | 상태 쿠키 수명 |
| `LOGIN_ALLOWED_GITHUB_ORG` | — | 설정하면 그 GitHub 조직의 active 멤버만 로그인. App 이 그 조직에 설치돼 있고 Members 읽기 권한이 있어야 한다 |
| `RESERVED_ORG_SLUGS` | `team-echo` | 사람이 API 로 만들 수 없는 조직 slug(쉼표 구분) |
| `AGENT_TOKEN_DEFAULT_DAYS` · `AGENT_TOKEN_MAX_DAYS` | `30` · `90` | 에이전트 토큰 기본·최대 유효 기간 |
| `INVITATION_TTL_SECONDS` | `604800` | 초대 유효 기간(7일) |

로그인 넷(`CLIENT_ID`·`CLIENT_SECRET_REF`·`REDIRECT_URI`·`FRONTEND_BASE_URL`) 중 하나라도 비면 로그인·콜백만 503 이다.
