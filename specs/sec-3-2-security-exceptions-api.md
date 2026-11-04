# 보안·거버넌스 3-2: 기한부 예외 요청·승인·철회 API SPEC

- 상태: Draft (2026-11-04, 김형래)
- 작성일: 2026-11-04
- Depth: Standard (새 API 5개, DB 변경 없음 — 표는 #60 `security_exceptions`)
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/27 (보안·거버넌스 3, "예외는 사유·승인자·만료가 있을 때만")
- 근거: 보안 1 SPEC `specs/sec-1-security-controls.md` §5.3(시연 예외)·§6.4(예외 규칙) / 규칙표 `app/services/security_rules.py`(`_is_valid_exception`·`scope_keys`) / PR #60 리뷰(Saccharine1211) ② 7일 상한과 시계 차이 / 보안 5 SPEC `specs/sec-5-migration-gate.md` §5(켜기 전 준비물)
- 저장소: `echo-was` · 브랜치 `feat/sec-3-security-exceptions-api` (처음엔 PR #61 위에 쌓음 → #61 이 11/5 병합돼 기준을 `main` 으로 바꿈) · PR로 올린다

## 1. 목표

사람이 기한부 예외를 요청·승인·철회하고 조회하는 API 를 만든다. 게이트를 켰을 때 시연의 C-DB-1·C-DST-1(확인 수단이 없는 통제)을 예외로 통과시키고, C-AI-1 대비책(러너가 금요일까지 안 될 때)도 같은 API 로 만든다.

## 2. 확인된 사실 (11/4)

- `security_exceptions` 표(#60, 운영 적용)가 있다. DB 가 막는 것: 사유 공백, 승인·철회 칸 짝, `expires_at > created_at`, **`expires_at <= created_at + 7일`**, digest 형식.
- `created_at` 은 서버 기본값 `now()` 다. 앱이 `now + 7일` 로 만료를 계산하면 DB 시각이 앱 시각보다 늦을 때 CHECK 위반(500)이 날 수 있다(#60 리뷰 ②).
- 판정(`_is_valid_exception`)은 요청·승인이 사람이고, 승인자가 있고, 7일 이하이고, 범위가 맞을 때만 받는다. 자기 승인은 조직 스위치(`allow_self_approval`)를 본다. 게이트(#61)는 승인됐고 철회되지 않은 것만 읽는다.
- 범위 비교는 통제가 정한 칸(`scope_keys`)만 하고, 판정 대상의 값이 없으면 맞지 않는다(조직 전체로 넓힐 수 없음).
- (11/4 기준) `audit_logs` 는 거부(DENIED)만 받았다. 11/5 #66 으로 행동 줄(USER·SYSTEM·AGENT)이 생겼다.

## 3. 범위

### 3.1 이번에 한다
- API 5개: 만들기·목록·단건·승인·철회(`app/routers/security_exception_router.py`, `SecurityExceptionService`)
- 저장소: `SecurityExceptionRepository` 에 저장·조회·목록·개수·조건부 승인·철회 더함(게이트용 `search_for_judgement` 그대로)
- 상태 `SecurityExceptionStatus`(응답용), 오류 `SECURITY_EXCEPTION_NOT_FOUND`·`SECURITY_EXCEPTION_NOT_PENDING`·`SECURITY_EXCEPTION_CLOSED`, 422 여러 칸(`RequestFieldsInvalidError`)
- 문서: `docs/api/security-exceptions.md`(김도영 님 화면용 계약), `docs/api/auth.md` 역할 표, 용어 사전 §3, CLAUDE.md
- 시험: 통합 8개 + 역할 표 5줄

### 3.2 다음 PR
- 매트릭스 API(#59)가 예외를 읽어 칸에 "예외 적용 중"(`hasException`) 표시
- 만료 감사 줄(만료 시각을 지난 예외를 일꾼이 기록할지)
- 예외 화면(김도영 님): 목록·요청·승인 버튼

### 3.3 하지 않는다
- 예외 고치기(PATCH)·지우기. 바꾸려면 철회하고 새로 만든다.
- 에이전트 예외(검사기가 막는다). 통제별로 예외를 막는 목록(모든 통제에 만들 수 있고, 판정이 FAIL·UNKNOWN 에만 쓴다)

## 4. 결정

| ID | 결정 | 출처 |
|---|---|---|
| D-1 | **만든 시각을 앱 시계로 직접 넣는다**(`created_at = updated_at = clock.now()`). 만료 검사(`expiresAt - now <= 7일`)와 DB CHECK 가 같은 만든 시각을 본다. 정확히 7일은 받는다 | #60 리뷰 ② |
| D-2 | 범위 칸은 **통제가 정한 것만, 모두**. 빠지면 `REQUIRED_FOR_CONTROL`, 더 있으면 `NOT_ALLOWED_FOR_CONTROL`(422, 여러 칸 한 번에). 정한 칸이 없는 통제(C-SEC-1·C-CI-1)는 조직 전체 | 규칙표 `scope_keys`, findings 와 같은 방식 |
| D-3 | 서비스·환경은 이 조직의 살아 있는 것(404 `SERVICE_NOT_FOUND`·`ENVIRONMENT_NOT_FOUND`). digest·설정 커밋·WAS 버전은 형식만 본다 | 조직 격리 |
| D-4 | 승인: `PENDING` 만, 요청한 사람의 승인은 조직 스위치가 켜졌을 때만(403 `SELF_APPROVAL_FORBIDDEN`, 거부 기록). 이전 계획 승인과 같은 스위치·같은 오류 | 보안 1 §6.4, 규칙표 |
| D-5 | 철회: 철회 전·만료 전이면 승인 전이어도 된다. 철회 칸만 채우고 지우지 않는다 | #60 모델 |
| D-6 | 승인·철회는 조건부 UPDATE(`approved_by IS NULL AND revoked_at IS NULL AND expires_at > now` 등). 0행이면 409 | 상태 변경 규칙 |
| D-7 | 상태는 저장하지 않고 조회 때 정한다: REVOKED > EXPIRED > ACTIVE > PENDING. 목록의 상태 조건도 같은 규칙(SQL) | 만료는 시간이 지나면 바뀜 |
| D-8 | 조회는 뷰어(에이전트 포함), 쓰기는 개발자 이상 사람 | 보안 1 §6.4 "AGENT는 예외를 만들 수 없다" |
| AD-1 | ~~감사 행은 #32 로 미룬다~~ → **(11/5) #66(API 서버 7 감사 로그)이 병합돼 요청·승인·철회를 같은 트랜잭션에서 감사 줄로 남긴다**(`SECURITY_EXCEPTION_*`, 대상 `security_exception`). 만료는 줄을 남기지 않는다(조회 때 정함) | #66 |

## 5. Acceptance criteria

| AC | 확인 | 시험 |
|---|---|---|
| AC-1 | 정확히 7일 만료를 받고, 만든 시각이 앱 시계이며 DB CHECK 를 통과한다 | `test_create_exception_up_to_exactly_seven_days_uses_app_clock` |
| AC-2 | 7일 + 1초·지금 이하·시간대 없음·빈 사유는 422 | `test_create_exception_rejects_expiry_past_seven_days_or_not_in_future` |
| AC-3 | 통제가 정한 범위 칸만(빠짐·더함 422, 조직 전체 통제, digest 범위) | `test_create_exception_requires_exactly_the_control_scope` |
| AC-4 | 모르는 서비스·환경 404 | `test_create_exception_for_unknown_service_or_environment_is_not_found` |
| AC-5 | 다른 개발자 승인 → ACTIVE, 자기 승인은 스위치를 따름(403 + 거부 기록), 두 번 승인 409 | `test_approve_by_other_developer_and_self_approval_follows_switch` |
| AC-6 | 철회(승인 전·후), 다시 철회 409, 철회·만료된 것 승인 409, 만료 상태 표시 | `test_expired_or_revoked_exception_cannot_be_approved_or_revoked_again` |
| AC-7 | 목록 조건(상태·통제·서비스·환경)·순서·조직 격리 | `test_search_exceptions_filters_and_stays_in_organization` |
| AC-8 | API 로 만든 C-DB-1·C-DST-1 예외로 게이트가 승인을 통과시킨다 | `test_api_exceptions_let_the_security_gate_pass_db_and_destination` |
| AC-9 | 역할: 조회 뷰어, 쓰기 개발자 사람, 에이전트 쓰기 403 | `test_role_table`(5줄), `test_every_route_has_guards` |

## 6. 리스크

- **R-a 쌓은 PR.** #61 이 먼저 병합되어야 한다(같은 저장소 파일을 만든다). #61 리뷰에서 그 파일이 바뀌면 이 브랜치를 다시 맞춘다.
- **R-b 매트릭스 표시.** #59 가 이 표를 아직 읽지 않아, 게이트는 예외를 쓰지만 매트릭스 칸은 "예외 적용 중"을 아직 안 보인다(다음 PR).
- **R-c 만료는 감사 줄이 없다.** 만료는 시간이 지나 조회 때 정하는 상태라 따로 기록하지 않는다(보안 1 §6.4 "만료가 감사에 남는다"는 다음에 일꾼이 만료를 기록하게 할지 정한다).
