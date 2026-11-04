# API 서버 7: 감사 로그 저장 · 조회 API SPEC

- 상태: Confirmed
- 확인일: 2026-11-04
- Depth: Deep
- Ambiguity: Low (≈ 10% / 목표 ≤ 20%)
- High-impact unresolved decisions: None
- Interview rounds: R1(Q1~Q4 묶음: 전부 추천) + 체크포인트 R2(A1 C0 D1: 전부 추천) + fresh-eyes 검토 반영(10건) · 상태 파일 `.deep-interview/api-7-audit-logs.state.md`
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/7 (Project #4, `part:API 서버`, `phase:1차`, In Progress)
- 저장소: `echo-was` · 브랜치 `feat/audit-logs` (SPEC 작성 기준 `main` = `a936eff`)
- 그림: `~/archive/softbank-hackathon-2026/api-7/api7-r1-background.html`, `api7-checkpoint.html` (M1 archive, 저장소 밖)

## 1. 목표

조직 안에서 일어난 **모든 변경 행동**과 **AI 에이전트의 행동**을 누가·언제·무엇을·결과로 `audit_logs`에 남기고, 조직별로 조회한다.

- 사람(USER)이 누른 쓰기 22개 경로 + 초대 적용(가입)은 그 작업과 **같은 트랜잭션**에서 한 줄을 쓴다.
- 배포 요청·이전 계획·서비스 삭제가 **끝 상태로 바뀌는 모든 곳**(일꾼, 그리고 사람 요청 안의 자동 종료)에서 SYSTEM 한 줄을 같은 트랜잭션에 쓴다(같은 대상 id).
- 403 거부는 지금처럼 따로 커밋하되 행동 이름을 붙인다. AI 에이전트 토큰의 성공한 읽기(GET·HEAD 2xx)도 따로 한 줄 쓴다.
- 조회: `GET /api/v1/orgs/{orgId}/audit-logs` (프론트 계약 4-8 모양).

GitHub 이슈 완료 기준(원문): **이전·롤백·승인·권한 변경·에이전트 행동을 누가·언제·무엇을·결과로 100% 기록한다. 행위자 유형에 AI Agent. 데모 직후 방금 한 조작이 조회된다 (NFR-08).** 이 SPEC으로 문구 변경 없이 채운다.

## 2. 현재 맥락과 확인된 사실 (`main` = `a936eff`, 스냅샷 `31333b8` 대비 보안 수집기 파일만 다름)

- `app/models/audit_log.py` `AuditLog`(`UuidPrimaryKeyMixin`, `CreatedAtMixin`): `organization_id` FK NULL(RESTRICT), `actor_type` String(16) (CHECK `actor_type_valid`: `USER`·`AGENT`), `actor_user_id`·`actor_agent_token_id` FK NULL(RESTRICT), `action` String(300) NOT NULL(지금은 `METHOD 경로 틀`), `path` Text NOT NULL, `reason` String(64) NOT NULL, `result` String(16) NOT NULL default `DENIED` (CHECK `result_valid`: `DENIED`), `request_id` String(128) NULL. CHECK `actor_consistency`. 색인 `ix_audit_logs_organization_id_created_at`. 트리거 `trg_audit_logs_prevent_delete`(공용 함수 `prevent_append_only_delete()`, 시험 `test_audit_logs_cannot_be_deleted`가 메시지 `audit_logs rows are append-only` 단언).
- 이름 규칙: `app/models/base.py` naming convention `ck_%(table_name)s_%(constraint_name)s` — 모델에는 짧은 이름(`name="target_consistency"`), 마이그레이션에는 `op.f("ck_audit_logs_target_consistency")`(선례 `20261102_1750_33ad047a493c`의 drop/create_check_constraint). 나중에 더한 칸은 `sort_order=APPENDED_COLUMN_SORT_ORDER`(base.py:27)가 있어야 `test_model_metadata_matches_migrated_schema`(칸 순서 비교)가 통과한다. JSONB 기본값 선례: `deployment.py`의 `default=dict, server_default=text("'{}'::jsonb")`.
- `alembic/env.py`의 `alembic check`는 타입·server_default만 비교하고 CHECK 본문·트리거는 비교하지 않는다. 실제 방어선은 `tests/integration/test_migrations.py`의 `EXPECTED_CONSTRAINTS`·`EXPECTED_INDEXES`·`EXPECTED_TRIGGERS`·`HEAD_REVISION = "eee7a6c8229f"`와 모델-마이그레이션 카탈로그 비교 시험이다.
- `app/auth/audit.py` `record_denial`: `request.state`의 `session_factory`·`actor`·`organization_id`·`clock`·`request_id`로 따로 커밋, `created_at = clock.now()`. `SQLAlchemyError`면 로그만.
- `app/core/middleware.py` `RequestContextMiddleware`는 **순수 ASGI**(BaseHTTPMiddleware 아님): `send_with_request_id`에서 응답 상태를 잡고, `scope["state"]`(= `request.state`)에 검사기가 둔 행위자·팩토리를 `await self.app(...)` 뒤에도 읽을 수 있다. FastAPI `yield` 의존성은 응답 상태를 볼 수 없다.
- `app/auth/guards.py`: `require_org_role(min_role, human_only)`가 짧은 별도 세션으로 검사하고 `request.state`를 채운다. `require_login` 경로(예 `/environment-kinds`)는 조직 id를 에이전트 토큰에서 채운다(guards.py:124-125).
- 쓰기 경로 22개(라우터 POST·PATCH·DELETE, 로그아웃 제외): organization_router(`POST /orgs`(OrganizationAccess 아님, `Human`), `PATCH /{org_id}/settings`, `PATCH·DELETE /{org_id}/members/{user_id}`), invitation_router(POST, DELETE), agent_token_router(POST, DELETE), environment_router(POST, PATCH, DELETE, `POST /{environment_id}/connection-check`), service_router(POST, PATCH, DELETE(202), `POST /{service_id}/deploy-requests`), deploy_request_router(`POST /{deploy_request_id}/rollback`), migration_plan_router(`POST /services/{service_id}/migration-plans`, `/migration-plans/{plan_id}/approve|reject|cancel|abort`). 거절에는 요청 본문이 없다.
- 서비스 트랜잭션 모양(기록 위치 결정에 씀):
  - `migration_plan_service.approve_plan`·`reject_plan`·`cancel_plan`: 둘째 블록이 `EXPIRED`(승인은 `STALE`도)를 **커밋한 뒤** 블록 밖 `_raise_rejection`으로 409를 낸다. 성공 분기는 `_approve`·`_close_pending`의 만료 아닌 분기.
  - `create_plan`: 같은 서비스의 다른 대기 계획을 `_expire_pending_for_service`로 EXPIRED 처리하고 커밋.
  - `abort_plan`: 이미 중단 요청이면 바뀌는 것 없이 200(멱등). `_recover_missing_job`이 APPROVED→CANCELLED.
  - `deploy_support.enqueue_deploy_request`: 대기 요청을 CANCELLED(`SUPERSEDED`)로 밀어냄(배포·되돌리기 접수 안에서).
  - `service_service.delete_service`: 대기 배포 요청을 취소, 이미 DELETING이면 바뀌는 것 없이 202. `create_service`는 트랜잭션 밖 `check_access` 뒤 `_insert_service` 블록을 이름 충돌 때마다 다시 시도.
  - `environment_service.create_environment`: 첫 블록에서 행 커밋 뒤 둘째 블록에서 접속 확인. `_check_and_record`: 읽기 블록 → 외부 호출 → 쓰기 블록(결과가 낡았으면 `is_current=False`로 버림, 그 블록에서 `EnvironmentNotFound` 가능).
  - `deploy_request_service.create_deploy_request`·`create_rollback`: 첫 블록 → 외부 호출 → 둘째 블록에서 행 생성.
  - `ServiceTeardownProcessor`도 배포 요청을 취소한다(배포 일꾼이 아님).
- 초대 적용: `invitation_service.apply_pending_invitations(session, user, clock)`는 `auth_service.get_me`(`/me`)와 `_sign_in`(OAuth 콜백) **두 곳**에서 같은 트랜잭션으로 불린다. 첫 로그인은 개인 조직도 만든다.
- 에이전트 토큰 발급 모듈 함수 `agent_token_service.issue_agent_token`은 `app/scripts/demo_seed.py:92`도 쓴다.
- 이름 길이: 서비스·환경 이름 최대 63(`RESOURCE_NAME_MAX_LENGTH`), 조직·토큰 이름 최대 100. 가장 긴 이전 라벨은 63+3+63+3+63 = 195자.
- 시험 시계: 하네스 `FakeClock`은 `advance`/`tick_on_read` 전에는 멈춰 있다 → 한 시험의 줄이 같은 `created_at`을 가질 수 있다. `id`는 uuid4라 정렬 보조로 쓸 수 없다.
- `deploy_requests`에는 요청자 칸이 없다. `migration_plans`에는 `created_by`·`approved_by`·`rejected_by`·`cancelled_by`·`aborted_by_user_id`가 있다.
- 목록 응답 `Page[T]`(`items,total,page,size`, page 0부터, size 기본 20 최대 100), 봉투 `ApiResponse`, camelCase.
- 표 전체를 세는 기존 시험(바뀌어야 함): `test_demo_seed.py:99-106`(정확히 1줄), `test_rollback.py:305-314`(reason 정렬 — NULL이면 TypeError, action 집합), `test_authorization.py:270-299, 420-424, 504-521`(`_audit_rows`, `== ['AGENT_READ_ONLY']*5`, `rows[0]`), `test_organizations.py:44-46, 315, 342`(`_audit_reasons`), `test_migration_plans.py:261`(reasons 목록), `:784`(`count == 0`). `tests/integration/conftest.py` 표 비우기 목록에 `audit_logs`.
- 실시간(SSE) 채널은 echo-was에 없다. 보안 규칙 `C-AUD-1`은 이 표를 출처 이름으로만 든다.
- 열린 PR: #59·#53(형래, `alembic/`·`audit` 파일 없음). #59가 병합되면서 쓰기 경로를 더하면 FR-2 시험이 그 경로의 이름을 요구한다(병합 순서에 따라 한쪽이 맞춤).

### 앞 태스크에서 넘겨받은 결정

- API 6 D5: 403 거부를 `audit_logs`에 최소 기록, 조회와 다른 행위 기록은 API 7. 표 이름 유지.
- API 4: 되돌리기 요청자 기록은 API 7.
- 권한 원칙: 조회 = 뷰어 이상(에이전트 포함), 남의 조직 404.
- main 병합 = 운영 배포 → 마이그레이션은 더하기·넓히기만.

## 3. 범위

### 포함

- `audit_logs` 칸·규칙 확장 마이그레이션 + 모델 + `db-schema.sql`
- 행동 이름표(코드 상수 한 곳)와 공용 기록 함수
- 쓰기 22개 + 초대 적용의 USER 기록, 끝 상태 SYSTEM 기록, 거부 행동 이름, 에이전트 읽기 기록
- 조회 API, 문서, 시험(기존 시험 갱신 포함)

### 제외

- 실시간 알림(`audit.created`) — 실시간 채널이 없다
- AI 러너·사전 점검 API의 기록(SYSTEM `ai-runner`, `runId`) — 그 API가 없다. 기록 함수만 재사용 가능하게 둔다
- 배포 요청 표·배포 이력 응답의 요청자 칸
- 사람의 읽기, 로그인·로그아웃, 첫 로그인의 개인 조직 자동 생성, 데모 시드 스크립트(`demo_seed`)의 기록
- 거절 사유 입력(요청 본문 추가)
- 보관 기간·내보내기·외부 수집기(보안 12 몫), 이미 쌓인 거부 줄의 값 고치기

## 4. 구성 요소

| 구성 요소 | 파일(제안) |
|---|---|
| 행동 이름표 | `app/audit/actions.py` — `AuditAction` StrEnum + `WRITE_ROUTE_ACTIONS: dict[tuple[method, route 틀], AuditAction]` |
| 기록 함수 | `app/audit/recorder.py` — `AuditRecorder(session, clock)`, 값 객체 `RequestContext(route, path, request_id)`·`AuditTarget(type, id, label)`, 자르기 헬퍼 |
| 모델·마이그레이션 | `app/models/audit_log.py`, `alembic/versions/<새 revision>.py`(`down_revision = "eee7a6c8229f"`), `db-schema.sql` |
| 거부 기록 | `app/auth/audit.py` 수정(행동 이름·`route` 채움) |
| 에이전트 읽기 | `app/core/middleware.py` `RequestContextMiddleware`에 응답 뒤 기록 |
| 조회 | `app/routers/audit_log_router.py`, `app/schemas/audit_log.py`, `AuditLogRepository.search` |
| 문서 | `docs/api/audit-logs.md`(새), `docs/api/auth.md`·`docs/schema.md`·`.claude/rules/term-dictionary.md`·`README.md`·`CLAUDE.md` 갱신 |

## 5. 흐름

### 5.0 기록 함수 계약

- `AuditRecorder`는 `OrganizationAccess`가 아니라 **명시적인 값**을 받는다: `organization_id`, 행위자(`actor_type` + `actor_user_id` 또는 `actor_agent_token_id` 또는 `component`), `action`, `result`, `target: AuditTarget | None`, `details: dict`, `reason: str | None`, `ctx: RequestContext | None`.
- 모든 경로가 `created_at = clock.now()`(서비스·일꾼이 이미 가진 시계)를 쓴다. DB 기본값 `now()`에 기대지 않는다.
- 문자열은 칸 길이로 자른다(`action` 300, `route` 300, `target_label` 200, `component` 32, `reason` 64, `request_id` 128). 자르기 때문에 작업이 실패하지 않는다(P3).
- 기록은 `session.add`만 한다(커밋은 호출한 블록이). 거부·에이전트 읽기만 자기 세션으로 커밋한다.
- `RequestContext`는 라우터가 `Request`에서 만든다(`route` = `"{METHOD} {scope['route'].path}"`). 서비스에 넘기는 모양은 위임(§14). 시험이 서비스를 직접 부를 때는 `None`이어도 된다.

### 5.1 사람의 쓰기 (USER) — 기록 위치

행 상태를 실제로 바꾸고 커밋되는 블록의 **성공 분기에만** 둔다. 409 등으로 끝나는 분기에는 USER 줄이 없다.

| 경로 | 기록 위치 |
|---|---|
| 조직 만들기 | 조직 행을 flush해 id가 생긴 뒤 같은 블록. 행위자 = `Human` 사용자 |
| 설정·역할 변경·내보내기·초대·토큰 발급·폐기 | 각 서비스의 변경 블록. 토큰 발급은 **서비스 메서드**(`AgentTokenService.issue`)에서 기록하고 모듈 함수 `issue_agent_token`(demo_seed 공용)에서는 기록하지 않는다 |
| 환경 등록 | 행을 만드는 첫 블록. 뒤따르는 접속 확인은 따로 기록하지 않는다 |
| 환경 수정·삭제 | 변경 블록 |
| 접속 다시 확인 | `_check_and_record`의 쓰기 블록. 결과가 낡아 버려져도(`is_current=False`) 확인 행동은 일어났으므로 기록하고 `details.discarded = true` |
| 서비스 등록 | `_insert_service` 블록(시도마다). 이름 충돌로 되감긴 시도의 줄은 함께 사라지고 커밋된 시도만 남는다 |
| 서비스 수정 | 변경 블록 |
| 서비스 삭제 | 상태를 DELETING으로 바꾸는 블록. 이미 DELETING이면(아무것도 안 바뀜) 기록하지 않는다 |
| 배포 요청 · 되돌리기 | 요청 행을 만드는 둘째 블록 |
| 이전 계획 만들기 | 계획 행을 만드는 블록 |
| 승인 | `_approve` 분기(EXPIRED·STALE 분기 아님) |
| 거절 · 취소 | `_close_pending`의 만료 아닌 분기 |
| 중단 | 중단 요청을 실제로 저장한 경우만. 이미 중단 요청이면(멱등 200) 기록하지 않는다 |

**아무것도 바뀌지 않은 성공(멱등 재요청)은 기록하지 않는다**(AD-19). 결과는 `SUCCEEDED`, `reason` NULL.

### 5.2 초대 적용 (MEMBER_JOINED)

`apply_pending_invitations`가 멤버 행을 실제로 만든 초대마다(경쟁으로 0행이면 없음) 같은 트랜잭션에 한 줄: 행위자 = 가입한 USER, 조직 = 초대한 조직, 대상 = `user`(label = GitHub 아이디), `details = {role, invitationId}`. `/me`와 OAuth 콜백 **두 경로 모두** 같은 함수 안에서 기록되므로 자동으로 덮인다. 함수에 선택 인수 `ctx`를 더하고, 없으면 `route`·`path` NULL.

### 5.3 끝 상태 (SYSTEM)

배포 요청·이전 계획·서비스 삭제가 끝 상태로 바뀌는 **모든 곳**에서, 그 상태를 저장하는 같은 블록에 한 줄. 끝 상태는 되감지 않으므로 대상마다 끝 줄은 정확히 1개다.

| 끝 상태를 만드는 곳 | `action` | `component` |
|---|---|---|
| 배포 일꾼 성공·실패(`_succeed`·`_fail`) | `DEPLOY_FINISHED`(trigger `ROLLBACK`이면 `ROLLBACK_FINISHED`, `STANDBY_SYNC`는 `DEPLOY_FINISHED` + `details.trigger`) | `deploy-worker` |
| 접수 때 대기 요청 밀어내기(`enqueue_deploy_request`, CANCELLED·`SUPERSEDED`) | 위와 같음 | `api` |
| 서비스 삭제 접수의 배포 요청 취소 / 삭제 일꾼의 배포 요청 취소(CANCELLED·`SERVICE_DELETED`) | 위와 같음 | `api` / `teardown-worker` |
| 이전 일꾼의 모든 끝(SUCCEEDED·ROLLED_BACK·FAILED·STALE 등) | `MIGRATION_FINISHED` | `migration-worker` |
| 승인·거절·취소 시도 때 만료(EXPIRED)·승인 때 낡음(STALE), `create_plan`의 다른 대기 계획 만료, `abort`의 `_recover_missing_job`(CANCELLED) | `MIGRATION_FINISHED` | `api` |
| 서비스 삭제 완료(`ServiceTeardownProcessor._finish`) | `SERVICE_DELETED` | `teardown-worker` |

- `component = api`(AD-20): 사람 요청을 처리하다 서버 규칙으로 **자동으로** 끝낸 것. 누른 사람의 행동이 아니므로 USER로 쓰지 않는다. 승인 시도가 EXPIRED로 끝나면 USER 승인 줄은 없고 SYSTEM `api` 줄만 있다.
- 결과: 끝 상태가 `SUCCEEDED`면 `SUCCEEDED`, 그 밖은 `FAILED`. `details = {status, cancelReason?, errorCode?, simulated?, trigger?}`(상태 값은 `DeployRequestStatus`·`MigrationPlanStatus` 그대로. `ROLLBACK_FAILED`는 로그 코드이지 상태가 아니다).
- 대상: `deploy_request`(label = "서비스 · 환경 · 커밋 7자"), `migration_plan`(label = "서비스 · 출발 → 도착"), `service`(label = 서비스 이름). 사람 줄과 같은 대상 id. 끝 상태 전환 지점이 여러 곳이므로 공용 헬퍼로 모으는 것을 권한다(위임 §14).

### 5.4 거부 (DENIED)

`record_denial`은 그대로 따로 커밋하되: `action` = `WRITE_ROUTE_ACTIONS[(method, route 틀)]`(없으면 지금처럼 `"{METHOD} {route 틀}"`), `route` = `"{METHOD} {route 틀}"`, 대상 칸 비움.

### 5.5 에이전트 읽기 (RESOURCE_READ)

`RequestContextMiddleware.__call__`에서 `await self.app(...)`이 **정상으로 끝난 뒤**(예외·500 경로 제외): `scope["state"]`의 `actor`가 `AgentActor`이고 메서드가 GET·HEAD이며 잡힌 상태가 200~299이면, `state.session_factory`로 짧은 세션을 열어 한 줄(`log_context` 안). 조직 = `state.organization_id`(없으면 토큰의 조직). `action = RESOURCE_READ`, `route`, `path`, `result = SUCCEEDED`, `created_at = state.clock.now()`(없으면 `datetime.now(UTC)`). `Exception`을 모두 잡아 로그만 남기고 다시 던지지 않는다. httpx ASGITransport는 미들웨어가 끝날 때까지 기다리므로 시험에서 바로 보인다. 조회 API 자체를 에이전트가 읽어도 같은 규칙으로 남긴다.

### 5.6 조회

`GET /api/v1/orgs/{orgId}/audit-logs` (`ViewerAccess`) → 그 조직 행만, 정렬 `created_at DESC, seq DESC`(`seq` = 새 순번 칸, AD-21).

## 6. 기능 요구사항

- `FR-1` 행동 이름표 `AuditAction`:
  - USER: `ORGANIZATION_CREATED`, `ORGANIZATION_SETTINGS_CHANGED`, `MEMBER_ROLE_CHANGED`, `MEMBER_REMOVED`, `MEMBER_JOINED`, `INVITATION_CREATED`, `INVITATION_REVOKED`, `AGENT_TOKEN_ISSUED`, `AGENT_TOKEN_REVOKED`, `ENVIRONMENT_CREATED`, `ENVIRONMENT_UPDATED`, `ENVIRONMENT_DELETED`, `ENVIRONMENT_CONNECTION_CHECKED`, `SERVICE_CREATED`, `SERVICE_UPDATED`, `SERVICE_DELETE_REQUESTED`, `DEPLOY_REQUESTED`, `ROLLBACK_REQUESTED`, `MIGRATION_PLAN_CREATED`, `MIGRATION_PLAN_APPROVED`, `MIGRATION_PLAN_REJECTED`, `MIGRATION_PLAN_CANCELLED`, `MIGRATION_ABORT_REQUESTED`
  - SYSTEM: `DEPLOY_FINISHED`, `ROLLBACK_FINISHED`, `MIGRATION_FINISHED`, `SERVICE_DELETED`
  - AGENT: `RESOURCE_READ`
- `FR-2` `WRITE_ROUTE_ACTIONS`는 22개 쓰기 경로 각각을 USER 이름 하나에 대응시킨다. 시험이 `iter_api_routes`로 앱의 모든 POST·PUT·PATCH·DELETE 경로를 모아 `POST /api/v1/auth/logout`만 빼고 전부 표에 있는지, 표의 모든 항목이 실제 경로인지 단언한다(D1).
- `FR-3` 시험이 22개 경로 각각을 실제로 한 번 성공시키고(상태를 바꾸는 요청) 그 이름의 USER·`SUCCEEDED` 줄이 정확히 1개 생겼는지 확인한다. 멱등 재요청(이미 DELETING인 서비스 삭제, 두 번째 중단)은 0줄을 확인한다.
- `FR-4` USER 줄 `details`(비밀값 0): 설정 `{allowSelfApproval: {from, to}}`, 역할 변경 `{role: {from, to}}`, 내보내기 `{role}`, 초대 `{githubLogin, role}`, 토큰 `{name}`(원문·해시 없음), 환경 등록 `{kind}`, 환경·서비스 수정 `{changedFields: [...]}`(값 없음), 접속 확인 `{connectionStatus, discarded?}`, 배포 `{environmentId, ref, commitSha}`, 되돌리기 `{rollbackOfId, environmentId}`, 이전 만들기 `{sourceEnvironmentId, targetEnvironmentId}`, 승인 `{selfApproved}`. 그 밖은 `{}`.
- `FR-5` 대상 `target_type`: `organization`·`user`·`invitation`·`agent_token`·`environment`·`service`·`deploy_request`·`migration_plan`(멤버 역할 변경·내보내기도 `user`). `target_label`은 기록할 때 사람이 읽는 이름을 복사해 둔다.
- `FR-6` 끝 상태 SYSTEM 줄(5.3).
- `FR-7` 거부 줄에 행동 이름과 `route`(5.4).
- `FR-8` 에이전트 읽기 줄(5.5).
- `FR-9` 조회 API 질의: `actorType`(USER·AGENT·SYSTEM), `result`(SUCCEEDED·FAILED·DENIED), `action`(문자열 하나), `targetType`, `targetId`(uuid), `from`·`to`(ISO 시각, `from ≤ created_at < to`), `q`(1~100자, `action`·`target_label`·사용자 `github_login`·토큰 이름에 대소문자 무시 부분 일치), `includeReads`(bool, 기본 true; false면 `RESOURCE_READ` 제외, P1), `page`, `size`(1~100, 기본 20). 잘못된 값은 422.
- `FR-10` 응답 항목(camelCase): `id`, `at`, `actor {type, userId, githubLogin, agentTokenId, agentTokenName, component}`, `action`(문자열 — 옛 거부 줄의 경로 틀 문자열도 그대로), `route`, `path`, `target {type, id, label}`(없으면 `null`), `result`, `reason`, `details`(기본 `{}`), `requestId`. 사람은 `users`, 토큰은 `agent_tokens`를 붙여 읽는다(폐기된 토큰도 이름 표시).
- `FR-11` 고치기 금지: `BEFORE UPDATE` 트리거(새 함수 `prevent_append_only_update()`, 메시지 `audit_logs rows are append-only and cannot be updated`, ERRCODE `restrict_violation`). 지우기 금지는 그대로.
- `FR-12` 문서: `docs/api/audit-logs.md`(엔드포인트·질의·응답 예·행동 이름표·결과 규칙·`component` 값·기록하지 않는 것·forward-only), `auth.md` 거부 기록 절, `schema.md` 표 설명·ER·후속 표 행, 용어 사전 감사 로그 항목, README 기능 표·데모 절, CLAUDE.md의 “403만 기록” 문장.
- `FR-13` 기존 시험 갱신: §2의 표 전체를 세는 시험들은 헬퍼·질의를 `result = 'DENIED'`로 거르거나 새 전체 집합을 의도적으로 단언한다. 거부 시험의 `action` 단언은 새 이름으로 바꾼다. `test_migration_plans.py:784`의 `count == 0`은 거부 줄 0으로 바꾼다. 거부 동작 자체를 바꾸는 수정은 하지 않는다.

## 7. 상태와 예외

- 결과 `AuditResult`: `SUCCEEDED`·`FAILED`·`DENIED`. 행위자 `ActorType`: `USER`·`AGENT`·`SYSTEM`. `component` 값: `api`·`deploy-worker`·`migration-worker`·`teardown-worker`(앞으로 `ai-runner`).
- 새 오류 코드 없음. 남의 조직 404, 질의 오류 422(`VALIDATION_ERROR`). `from ≥ to` → 422. 조직 NULL 거부 줄은 어떤 조직 조회에도 안 나온다.

## 8. 계약

### 8.1 엔드포인트

| 메서드 · 경로 | 권한 | 성공 | 실패 |
|---|---|---|---|
| `GET /api/v1/orgs/{orgId}/audit-logs` | 뷰어 이상 사람, 에이전트 토큰 | 200 `ApiResponse[Page[AuditLogResponse]]` | 401, 404, 422 |

### 8.2 데이터 (모델 → 새 revision → `db-schema.sql`)

- 새 칸(add_column 순서 = 모델 정의 순서, 모델은 모두 `sort_order=APPENDED_COLUMN_SORT_ORDER`): `seq` BIGINT `GENERATED BY DEFAULT AS IDENTITY` NOT NULL(기존 행도 채워짐), `component` String(32) NULL, `route` String(300) NULL, `target_type` String(32) NULL, `target_id` UUID NULL(FK 없음), `target_label` String(200) NULL, `details` JSONB NOT NULL `default=dict, server_default=text("'{}'::jsonb")`.
- 넓히기: `path`·`reason` NULL 허용. CHECK는 drop 후 create(`op.f` 전체 이름, 모델은 짧은 이름):
  - `result_valid`: `result IN ('SUCCEEDED', 'FAILED', 'DENIED')`
  - `actor_type_valid`: `actor_type IN ('USER', 'AGENT', 'SYSTEM')`
  - `actor_consistency`: `(actor_type = 'USER' AND actor_user_id IS NOT NULL AND actor_agent_token_id IS NULL AND component IS NULL) OR (actor_type = 'AGENT' AND actor_agent_token_id IS NOT NULL AND actor_user_id IS NULL AND component IS NULL) OR (actor_type = 'SYSTEM' AND actor_user_id IS NULL AND actor_agent_token_id IS NULL AND component IS NOT NULL)`
  - 새 `target_consistency`: `((target_type IS NULL) = (target_id IS NULL)) AND (target_label IS NULL OR target_type IS NOT NULL)`
  - 모델 `CheckConstraint` 문자열과 마이그레이션 문자열은 같은 식을 쓴다 — pg 정규화 뒤 카탈로그 비교 시험이 같아야 한다.
- `result`의 `server_default 'DENIED'` 유지(옛 파드 INSERT 호환).
- 새 색인 `ix_audit_logs_organization_id_target_id` (`organization_id`, `target_id`). 기존 `(organization_id, created_at)` 유지.
- 새 트리거 `trg_audit_logs_prevent_update` + 함수 `prevent_append_only_update()`(이 revision 소유).
- `test_migrations.py`: `HEAD_REVISION`, `EXPECTED_CONSTRAINTS`(바뀐 3개 + 새 1개), `EXPECTED_INDEXES`, `EXPECTED_TRIGGERS` 갱신. `alembic check` 변경 없음, `scripts/dump_schema.sh` 결과 커밋.
- downgrade: 새 칸·색인·트리거·함수 제거, CHECK 원래대로, `path`·`reason` NOT NULL. **빈 표(또는 옛 모양 행만)일 때의 왕복 시험용**이다. 새 값 행이 생긴 운영에서는 UPDATE·DELETE가 막혀 downgrade할 수 없다고 docstring에 적는다.

### 8.3 다른 파트 계약

- 프론트(계약 4-8): 제안 모양 + `actor.userId`·`agentTokenId`·`component`, `route`, `path`, `details`. 대시보드는 `includeReads=false&size=3`.
- AI 파트: 러너 API가 생기면 `AuditRecorder`로 SYSTEM `component="ai-runner"` 줄을 쓴다. 실행 id는 대상(`target_type` + `target_id`)으로 잇는다.

## 9. 호환성·배포·복구

- **롤링 배포 중 옛 파드**: 옛 파드는 거부 줄만 옛 칸으로 INSERT → 새 CHECK·NULL 허용·`details`·`seq` 기본값을 모두 통과. 옛 파드는 이 표를 읽지 않는다. 시험: head에서 옛 모양 INSERT 성공(P4).
- **새 파드가 옛 행을 읽음**: 옛 거부 줄은 `route`·대상 NULL, `action`이 경로 틀 문자열 → 응답 `action: str` 그대로. 조회 시험에 옛 모양 행 포함.
- 롤아웃 수 분 동안 옛 파드 처리분은 성공 줄이 없다(감수). 진행 중이던 배포·이전이 새 일꾼에서 끝나면 SYSTEM 줄만 있을 수 있다(감수).
- **forward-only**: 운영에서 되돌릴 때는 코드만 되돌리고 revision 파일은 남기며 downgrade하지 않는다. 옛 코드는 넓어진 스키마에서 거부 줄만 쓰므로 그대로 동작한다. 문서(`docs/api/audit-logs.md`)에 적는다.

## 10. Acceptance criteria

- `AC-1` 이름표 완전성: `FR-2` 시험 통과.
- `AC-2` USER 기록: `FR-3` 시험 — 22개 경로마다 USER·`SUCCEEDED` 줄 1개, 행위자·조직·대상·`route`·`request_id`·`details`가 맞음, 비밀값(토큰 원문 `echo_agt_`) 0. 멱등 재요청 0줄.
- `AC-3` 같은 트랜잭션·성공 분기: 409로 끝나는 쓰기(이전 진행 중 배포 요청 `MIGRATION_IN_PROGRESS`, 마지막 어드민 내보내기)는 USER 줄 0. 만료된 계획 승인 → 409 + USER 승인 줄 0 + SYSTEM `api` `MIGRATION_FINISHED`(`FAILED`, `details.status = EXPIRED`) 1. 기록 함수를 예외로 바꾸면 작업 행도 저장되지 않음.
- `AC-4` 두 줄: 이전 계획 만들기 → 다른 사람 승인 → 일꾼 끝(가짜 실행기) → 같은 `targetId`로 거르면 시간 순으로 `MIGRATION_PLAN_CREATED`·`MIGRATION_PLAN_APPROVED`(USER)·`MIGRATION_FINISHED`(SYSTEM `migration-worker`, `SUCCEEDED`). 단계 사이에 FakeClock을 앞당긴다. `simulate` 실패 → `FAILED` + `details.status = ROLLED_BACK`. 배포 성공·실패, 되돌리기, 서비스 삭제도 각각 끝 줄 1개.
- `AC-5` 끝 상태 망라(P2): §5.3 표의 행마다 시험 1개 이상 — 배포 SUCCEEDED·FAILED, `SUPERSEDED` 취소, 서비스 삭제로 인한 취소, 이전 일꾼 끝(SUCCEEDED·ROLLED_BACK·STALE), 승인·거절·취소 시도 때 EXPIRED, 승인 때 STALE, `create_plan`의 다른 대기 계획 만료. 대상마다 끝 줄 정확히 1개.
- `AC-6` 초대 적용: 초대 → 그 사람 `/me` → `MEMBER_JOINED` 1줄. OAuth 콜백 경로로 가입해도 1줄. `/me` 동시 2회 → 1줄, 500 없음. `demo_seed` 실행은 USER·SYSTEM 줄 0(기존 거부 1줄 시험 유지, 에이전트 읽기 줄은 `result`·`action`으로 거름).
- `AC-7` 거부: 에이전트 토큰 배포 요청 → 403 `AGENT_READ_ONLY` + `action = DEPLOY_REQUESTED`, `route = POST /api/v1/orgs/{org_id}/services/{service_id}/deploy-requests`, `DENIED`, AGENT. 기존 거부 시험은 `FR-13` 범위의 거르기·단언 변경만으로 통과.
- `AC-8` 에이전트 읽기: 에이전트 GET 서비스 상세 200 → `RESOURCE_READ` 1줄. 에이전트 `/environment-kinds`(조직 경로 아님) 200 → 토큰 조직으로 1줄. 404 읽기 0줄. 사람 GET 0줄. 기록 저장 실패 주입 → 응답 200 그대로.
- `AC-9` 조회: 뷰어 200, 에이전트 200, 남의 조직 404, 비로그인 401. 최신순(`created_at`이 같아도 `seq`로 나중 줄이 위). 거르기 각각 1개 이상 시험(`includeReads=false` 포함). `size=101`·`from ≥ to` 422. 옛 모양 거부 줄도 200. 조직 NULL 줄 안 나옴. 조작 직후 조회하면 맨 위에 그 줄.
- `AC-10` `UPDATE audit_logs …` → 트리거 오류, `DELETE` → 기존 오류.
- `AC-11` 마이그레이션: upgrade → downgrade -1 → upgrade 왕복(빈 표), `alembic check` 변경 없음, `db-schema.sql` 일치, 카탈로그·칸 순서 시험 통과, 옛 모양 INSERT 성공(P4), 새 CHECK 위반(SYSTEM인데 component 없음, USER인데 component 있음, 대상 종류만 있고 id 없음) 거부.
- `AC-12` 자르기(P3): 기록 함수 단위 시험 — 칸보다 긴 `target_label`·`action`·`route`·`component`·`reason`을 넣어도 예외 없이 잘린 값이 저장됨. API로는 가장 긴 이전 라벨(195자)이 그대로 저장됨.
- `AC-13` 품질: `ruff check`, `ruff format --check`, `mypy app`, 전체 `pytest`(DB 포함, 건너뜀 0), OpenAPI에 새 경로.
- `AC-14` 운영 확인(병합 뒤, M1 담당): 운영 `/openapi.json`에 경로, 운영 DB `alembic_version` = 새 revision, 지민 로그인 세션으로 조작 하나 → 바로 조회에 나옴.

## 11. 핵심 엔티티

| 엔티티 | 한 행의 뜻 | 관계 |
|---|---|---|
| `audit_logs` | 행동 한 건(성공·실패·거부·에이전트 읽기) | 조직 0..1, 사용자 0..1, 에이전트 토큰 0..1, 대상(종류+id, FK 없음) 0..1 |

## 12. 리스크 (사전 부검, 사용자 채택 P1~P4)

| 실패 | 막는 것 |
|---|---|
| P1 대시보드 최근 활동이 AI 읽기 줄로 덮임 | `includeReads=false`(FR-9), 프론트 알림 |
| P2 이전은 끝났는데 결과 줄 없음 | 끝 상태 지점 망라(§5.3) + AC-5 |
| P3 긴 이름 때문에 작업 자체 500 | 자르기(§5.0) + AC-12 |
| P4 배포 중 옛 파드 거부 기록이 새 규칙에 막힘 | 넓히기만 + 옛 모양 INSERT 시험(AC-11) |
| 감수: 롤아웃 수 분 동안 옛 파드 처리분 성공 줄 없음 | §9 |

## 13. 결정 기록

| ID | 결정 | 근거 | 출처 |
|---|---|---|---|
| D1 | 조직 안 쓰기 22개 전부 + 끝 상태 + 에이전트, 이름표 완전성 시험 | 100% 증명, 프론트 계약 4-8 | 사용자 R1 Q1=A |
| D2 | 작업과 같은 트랜잭션에서 한 줄, 이름 붙은 행동 | 기록 누락 0, 사람이 읽는 대상 | 사용자 R1 Q2=A |
| D3 | 오래 걸리는 일은 USER 접수 + SYSTEM 끝 두 줄, SYSTEM 추가, 고치지 않음 | “결과” 칸, append-only | 사용자 R1 Q3=A |
| D4 | 에이전트 성공 읽기도 기록, 사람 읽기는 안 함 | “에이전트 행동” | 사용자 R1 Q4=A |
| AD-1~10 | R1 기본값 | — | AI-default, 사용자 거부 없음 |
| AD-11~18 | 체크포인트 기본값 | — | AI-default, 사용자 C0 |
| P1~P4 | 사전 부검 요구사항 | — | 사용자 D1 |
| AD-19 | 아무것도 안 바뀐 멱등 성공은 기록하지 않음(접속 확인은 결과를 버려도 기록) | AD-5(바뀐 것 없으면 기록 없음)와 같은 뜻 | AI-default (검토 반영), 거부권 |
| AD-20 | 사람 요청 안에서 서버 규칙으로 자동 종료된 것(만료·낡음·밀어내기·삭제 취소)은 SYSTEM `component = api` | D3의 끝 줄을 빠짐없이 남기되 누른 사람 행동과 구분 | AI-default (검토 반영), 거부권 |
| AD-21 | 정렬 보조 순번 칸 `seq`(identity) 추가, 정렬 `created_at DESC, seq DESC` | 같은 시각 줄의 순서 고정(uuid는 무작위) | AI-default (검토 반영), 거부권 |
| AD-22 | 거절 사유는 받지 않음(본문 없음) — FR-4에서 뺌 | 라우터에 본문 없음, 범위 밖 | AI-default (검토 반영) |
| AD-23 | 운영은 forward-only(되돌릴 때 revision 파일 유지, downgrade 안 함) | UPDATE·DELETE 금지라 downgrade 불가 | AI-default (검토 반영) |
| AD-24 | 기록 함수는 명시 값을 받음, 데모 시드는 기록 안 함, 초대 적용은 두 경로 모두 함수 안에서 기록 | 호출자 모양이 서로 다름 | AI-default (검토 반영) |

## 14. 구현 계획으로 위임한 low-impact 세부사항

- `RequestContext`를 서비스에 넘기는 모양(인수, contextvar 중 택1). 시험 하네스의 서비스 직접 호출이 깨지지 않게.
- 끝 상태 전환 지점을 공용 헬퍼로 모으는 방법.
- `q` 검색 구현(ILIKE + 사용자·토큰 조인).
- 라벨 문자열의 정확한 모양(§5.3 예시를 따름).
- FR-3 시험을 경로별 매개변수 시험으로 만들지 묶을지.
