# AI 러너 API(레인 A): 사전 점검 실행 만들기 · 러너 가져가기 · 결과 제출 — SPEC (확정)

- 저장소 경로: `echo-was/specs/api-runner-prechecks.md`
- 상태: **확정(confirmed)** — 지민 11/5 03:05 결정 ①A ②A ③A, AI 기본값 AD-1~21 채택(아래 부록, AD-20은 기준 main에 맞춰 고침)
- 기준: echo-was main `552e80b0e0a3b21c613214cddbe586d2f9556a14`(#64 기한부 예외·#68 환경 좌표 병합 뒤, 11/5 03:10) · echo-ai-agent main `7d521e4` · echo-frontend main `b2b2c96`. 마이그레이션 head `286d9ea982d6`(하나)
- GitHub 이슈: echo-ai-agent #4 https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-ai-agent/issues/4 (레인 A, Project #4)
- 근거: echo-ai-agent `specs/ai-1-appendix-a-was-runner-contract.md`(**v1 승인 11/4, 김형래** — "echo-was 쪽은 Saccharine1211 리뷰 뒤 병합") · echo-was `specs/sec-ai-base-schema.md` 3.2절 "다음 PR" · 보안 5 게이트 `_precheck_finding`
- Depth: Standard (새 라우터 2·서비스 2·검사기 2·발급 명령 1, **마이그레이션 없음**)

## 1. 목표

사람이 `POST prechecks`로 실행을 만들고 → 러너가 가져가 → 결과를 제출해 `agent_runs` 행이 `SUCCEEDED`(+ `simulated=false`)가 되면, 보안 게이트가 그 행을 **C-AI-1 PASS 증거**로 읽는다. 모델 없이 "환경 설정 차이 비교 + 경고 1건"만 내는 러프 러너(echo-ai-agent, 별도 PR)로도 이 흐름이 끝까지 돌게 범위를 좁힌다.

## 2. 확인된 사실 (main `552e80b` 기준)

- 게이트(`app/services/security_gate.py` 215~227·251~270행)는 `service_id`·도착 연결의 `environment_id`·`kind=PRECHECK`·`status=SUCCEEDED`·`finished_at > now−24h`인 가장 늦은 행 하나를 읽어 `result=PASS` 증거를 만든다. **결과 JSON 내용은 보지 않는다.** `run.simulated=true`면 `mode=SIMULATED` → 규칙표가 `EVIDENCE_SIMULATED`로 버린다(`security_rules.py` 643행)
- 표·제약은 이미 있다: `agent_runners`(이름·`token_hash`·`revoked_at`), `agent_runs`(CHECK 11개·부분 unique 2개), `agent_run_events`(`(run_id,seq)` unique, message 200자), `agent_patch_approvals`. 리비전 `201cf5419f4d`. 쓰는 코드는 `find_latest_succeeded_precheck`(`app/repositories/agent_run_repository.py` 17행) 하나뿐
- 인증: `Authorization` 헤더가 있으면 에이전트로만 보고 `echo_agt_` 아니면 401(`app/auth/guards.py` 88~103행) → 러너·실행 토큰은 지금 조직 경로에서 401. 검사기 자동 수집 시험이 `AUTH_GUARDS`(`guards.py` 63행) 등록을 요구한다(`test_authorization.py` `test_every_route_has_guards`)
- 감사(#64 병합 뒤): `app/audit/actions.py`의 `WRITE_ROUTE_ACTIONS`는 **27개**(API 서버 7의 24 + 기한부 예외 3). `AuditAction` 사람 묶음 끝은 `SECURITY_EXCEPTION_REVOKED`(39행), `AuditTargetType` 끝은 `SECURITY_EXCEPTION`(60행), `AuditComponent`는 api·deploy-worker·migration-worker·teardown-worker(66~69행, `ai-runner` 없음). 시험: `tests/integration/test_audit_logs.py` 144행 `len(WRITE_ROUTE_ACTIONS) == 27`·147행 주석·599행 "27개를 모두 지났다", `tests/test_audit_recorder.py` 129~131행 `27`. `docs/api/audit-logs.md` 81행 "조직 안 쓰기 27개 경로(기한부 예외 3개 포함)"
- SYSTEM + `component` 줄은 표 변경 없이 쓸 수 있다(`audit_log.py` 37~44행, `recorder.py` 50~52행). 쓰기 경로 시험은 "모든 쓰기 경로 − 로그아웃 = `WRITE_ROUTE_ACTIONS`" + 걷기 시험이 모든 이름에 USER 줄을 기대한다
- 라우터 등록(#64 뒤): `app/main.py` import 21~34행(`security_exception_router`·`security_router`·`service_router`… 알파벳 순), `include_router` 72~84행, 끝 줄 `security_exception_router`
- 역할 표(#64 뒤): `docs/api/auth.md` 역할 표 97~105행에 기한부 예외 줄(뷰어 읽기 98행, 개발자(사람만) 쓰기 105행), 오류 표 143~146행. `tests/integration/test_authorization.py` `ROLE_TABLE`(94행~)에 `_org("GET"/"POST", "/security/exceptions…")` 5줄(155~159행) 뒤 `PATCH /settings`(161행)
- 러너(echo-ai-agent)에는 WAS 클라이언트가 0줄이다. 이벤트 모양(`events.py`)은 계약 그대로다. 샌드박스 VM은 443으로 `api.echoport.uk`에 닿는다(ai-1 SPEC 178행)
- 계약과 저장소의 빈칸·차이(이 SPEC이 정하고 형래님께 계약 문구 수정을 요청): ⓐ `RunResult`에 `simulated` 칸 없음, 121행은 "실제 모델이면 false" → D-1 ⓑ `CLAIMED→RUNNING`을 만드는 호출 없음 → AD-2 ⓒ 이벤트 message 2KB(계약) vs 200자(DB) → AD-12 ⓓ GET 상세 화면 편의 칸 → AD-19 ⓔ 프론트 4-5와 패치·단계 모양 차이(프론트 몫)

## 3. 범위

### 3.1 이번에 한다 (결정 ②A)
1. 사람 쪽 4개: `POST /orgs/{org_id}/prechecks` · `GET …/prechecks/{id}` · `GET …/prechecks/{id}/logs` · `POST …/prechecks/{id}/cancel`
2. 러너 쪽 4개: `POST /runner/runs:claim` · `…/{id}/heartbeat` · `…/{id}/events` · `…/{id}/result`
3. 검사기 `require_runner`·`require_run_token`, 토큰 `echo_rnr_`·`echo_run_` 만들기·해시
4. 발급 명령 `python -m app.scripts.issue_runner_token`(만들기만 — 운영 실행은 병합·배포 뒤 M1)
5. "부를 때 만료"(임대·마감 지난 실행 → `TIMED_OUT`)
6. 감사: 사람 2개(USER) + 러너 3개(SYSTEM `ai-runner`)
7. 문서: `docs/api/prechecks.md`(사람 쪽), `docs/agent-runner.md`(러너 쪽), `docs/api/auth.md`·`docs/api/audit-logs.md`·용어 사전·`CLAUDE.md`·`README.md` 설정 표
8. `mask_secrets()`에 Echo 토큰 3종 패턴

### 3.2 하지 않는다 (2차 후보)
- `PRECHECK` 밖 종류: `APPLY_PATCH`(승인한 패치 반영)·`CHANGE_PROPOSAL`(P1). claim은 `PRECHECK`만 준다
- 패치 승인·거절 API(`agent_patch_approvals` 쓰기), 승인 → `APPLY_PATCH` 실행 만들기
- AI findings를 `findings` 표에 넣기(매트릭스·게이트 영향), `blockedAttempts`와 감사 줄 맞추기
- PLANNED 계획 만들 때 최신 사전 점검 필수화(계약 8절 2번 — 게이트/계획 쪽 이슈)
- 일꾼 루프의 주기 만료, 러너 토큰 API 발급, 화면 전환(프론트 몫)
- 이 저장소 밖: 러프 러너 스크립트(11절, echo-ai-agent 별도 PR)
- **마이그레이션**(새 표·칸·인덱스·리비전). 필요해 보이면 구현을 멈추고 보고한다

## 4. 결정

| ID | 결정 | 출처 |
|---|---|---|
| D-1 | `simulated` = **결과 값을 지어냈는지**. `RunResult`에 필수 칸 **`simulated: bool`**을 더한다. 실제 러너가 실제 `inputSnapshot`으로 계산했으면(러프 러너의 환경 설정 비교 포함) 모델이 없어도 `false`, 가짜 모드면 `true`. `model: null` 허용, 응답 `aiReviewed = (model != null)`로 "AI 검토 없음 · 규칙 비교만"을 구분한다. 계약 121행 문구 수정은 형래님께 요청(12절) | 지민 11/5 03:05 ①A |
| D-2 | 계약 API 중 패치 승인·거절만 빼고 8개 경로(사람 4·러너 4). `APPLY_PATCH` 제외 | 지민 11/5 03:05 ②A |
| D-3 | 러프 러너는 echo-ai-agent PR(형래 리뷰), 샌드박스 VM에서 실행. 이 PR은 WAS만 | 지민 11/5 03:05 ③A |
| D-4 | 새 표·칸·리비전 없음 | 기반 스키마 |
| D-5 | 감사 행위자: 러너 행동은 SYSTEM + `component="ai-runner"`, 이 신원은 가져가기·결과 제출만(`RUNNER` 행위자 만들지 않음) | 계약 8절 1번 |
| D-6 | 마감 15분·임대 60초·결과 크기(diff 200KB·패치 10·findings 100) | 계약 8절 3번 |
| D-7 | 가져가기는 `FOR UPDATE SKIP LOCKED` + 조건부 UPDATE | 계약 83행, `job_repository.claim_next` |
| D-8 | AI `origin` finding의 PASS는 422. 패치 상태는 WAS가 재계산 | 계약 110~119행 |
| AD-1~21 | 부록 "AI 기본값" 표 그대로 | AI-default(지민 채택 11/5 03:05) |

## 5. API

공통: 경로 접두어 `/api/v1`, 응답 `ApiResponse[T]` 봉투, 본문·응답 camelCase.

### 5.1 사람 쪽 (조직 경로, 기존 검사기)

| 메서드·경로 | 검사기 | 요청 | 응답 | 실패 |
|---|---|---|---|---|
| `POST /orgs/{org_id}/prechecks` | `HumanDeveloperAccess` | `{serviceId: uuid, targetEnvironmentId: uuid}` | 201 `PrecheckDetail`(status `QUEUED`) | 404 `SERVICE_NOT_FOUND`·`ENVIRONMENT_NOT_FOUND` · 409 `SERVICE_DELETING` · 409 `SOURCE_NOT_SINGLE` · 422 `VALIDATION_ERROR`(`targetEnvironmentId`/`SAME_ENVIRONMENT`) · 409 `PRECHECK_IN_PROGRESS`(`details.runId`) |
| `GET /orgs/{org_id}/prechecks/{precheck_id}` | `ViewerAccess`(에이전트 읽기 가능) | — | 200 `PrecheckDetail` | 404 `PRECHECK_NOT_FOUND`(다른 조직·`PRECHECK` 아닌 실행 포함) |
| `GET /orgs/{org_id}/prechecks/{precheck_id}/logs?after=<seq>&size=` | `ViewerAccess` | `after` 기본 0, `size` 기본 200·최대 500 | 200 `{items:[{seq,at,step,level,message,data}], nextAfter}` | 404 `PRECHECK_NOT_FOUND` |
| `POST /orgs/{org_id}/prechecks/{precheck_id}/cancel` | `HumanDeveloperAccess` | `{}` | 200 `PrecheckDetail`(status `CANCELLED`) | 404 · 409 `RUN_NOT_ACTIVE`(이미 끝남) |

`PrecheckDetail`:
```text
{id, serviceId, sourceEnvironmentId, targetEnvironmentId, status, failureCode,
 simulated, aiReviewed, requestedByUserId,
 createdAt, claimedAt, startedAt, finishedAt, deadlineAt, leaseUntil,
 inputHash, sandboxId,                              # sandboxId = result.sandbox.id
 steps: [{name: AgentRunStep, firstAt, lastAt, level}],   # 이벤트에서 계산(가장 나쁜 level)
 model: {id, effort, promptVersion, refusal} | null,
 configDiff[], featureGaps[], findings[],
 patches: [{id, patchHash, files[], diff, reason, touchesProtectedFiles, checks[],
            status: PROPOSED|BLOCKED, blockReason, verification: {passed, total}}],
 blockedAttempts: [{action, target, at, reason, auditLogId: null, confirmed: false}]}
```
- 결과가 없는 상태(QUEUED·CLAIMED·RUNNING·TIMED_OUT·CANCELLED)면 결과 칸은 빈 배열·null
- 목록 `GET /prechecks`는 이번에 만들지 않는다(화면은 상세 id로 부른다)

### 5.2 러너 쪽 (조직 밖 경로, 새 검사기)

| 메서드·경로 | 검사기 | 요청 | 응답 | 실패 |
|---|---|---|---|---|
| `POST /runner/runs:claim` | `require_runner`(`Bearer echo_rnr_…`) | `{runnerId: str, capabilities: ["PRECHECK"]}` | 200 `{run: {id, kind, orgId, serviceId, sourceEnvironmentId, targetEnvironmentId, inputSnapshot, inputHash, scenarioRef, deadlineAt, leaseUntil}, runToken: "echo_run_…"}` · 없으면 200 `{run: null, runToken: null}` | 401 `RUNNER_TOKEN_INVALID`(없음·형식·해시 없음·철회) · 403 `RUNNER_FORBIDDEN`(쿠키·에이전트 토큰) · 422(`runnerId`가 토큰 이름과 다름) |
| `POST /runner/runs/{run_id}/heartbeat` | `require_run_token`(`Bearer echo_run_…`) | `{}` | 200 `{status, leaseUntil}`(CLAIMED면 RUNNING으로) | 401 · 403 · 409 `RUN_NOT_ACTIVE`(끝남·임대/마감 지남 → 이때 TIMED_OUT으로 바꿈) |
| `POST /runner/runs/{run_id}/events` | `require_run_token` | `{events: [{seq≥1, at, step, level, message≤200자, data?}]}` 1~100개 | 200 `{acceptedUpTo}` | 401 · 403 · 409 `RUN_NOT_ACTIVE` · 422 `RESULT_INVALID`(형식·크기) |
| `POST /runner/runs/{run_id}/result` | `require_run_token` | `RunResult`(5.3) | 200 `{status}` | 401 · 403 · 409 `RESULT_ALREADY_SUBMITTED`(끝난 실행) · 409 `RUN_NOT_ACTIVE`(임대·마감 지남 → TIMED_OUT) · 422 `RESULT_INVALID` |

- 실행 토큰 검사: 접두어 `echo_run_` → `hash_token` → `agent_runs.run_token_hash`로 행 찾기 → **경로 `run_id`와 같아야** 함(다르면 401)
- 러너 토큰 검사: 접두어 `echo_rnr_` → `agent_runners.token_hash` + `revoked_at IS NULL`
- 이벤트·하트비트는 임대를 `now+60초`로 늘린다(마감은 넘지 않음). 결과 제출 전 마지막 확인: `lease_until ≥ now AND deadline_at > now`

### 5.3 `RunResult` (계약 3.3절 + D-1)

```json
{
  "status": "SUCCEEDED",
  "failureCode": null,
  "simulated": false,
  "sandbox": {"id": "echo-ws-<runId>", "startedAt": "…", "finishedAt": "…", "network": "none", "deletedAt": "…"},
  "model": null,
  "configDiff": [{"key": "region", "source": "ap-northeast-1", "target": "asia-northeast3", "origin": "RULE", "severity": "INFO"}],
  "featureGaps": [],
  "findings": [{"control": "config-diff", "environmentId": "<target>", "result": "WARN", "title": "…", "reason": "…", "origin": "RULE", "suggestedFix": null}],
  "patches": [],
  "blockedAttempts": []
}
```
- `simulated` 필수(빠지면 422). `sandbox`는 null 허용(러너가 샌드박스 밖이면). `model` null 허용(D-1)
- `origin`: `RULE`·`AI`. `severity`: `INFO`·`WARN`·`FAIL`. finding `result`: `PASS`·`WARN`·`FAIL`·`UNKNOWN`(AI면 PASS 금지). RULE finding의 PASS는 받는다(게이트는 결과 내용을 쓰지 않음)
- 저장: `status`·`failure_code`·`result`(정규화한 JSON, `mask_secrets` 뒤)·`model_id`·`prompt_version`·`simulated`·`finished_at`. `result`에서 `status`·`failureCode`·`simulated`는 칸으로만 두고 JSON에서 뺀다

## 6. 데이터

- **새 표·칸·인덱스·리비전 없음.** `alembic heads` 하나(`286d9ea982d6`), `alembic check` 차이 0(`test_alembic_check_reports_no_differences`), `db-schema.sql` 변경 없음
- 러너 신원·토큰: `agent_runners`(발급 명령이 행을 만든다, `token_hash` = SHA-256 16진)
- 실행 토큰: `agent_runs.run_token_hash`(claim 때 채움, 끝나도 지우지 않음 — AD-4)
- 이벤트: `agent_run_events`(`INSERT … ON CONFLICT (run_id, seq) DO NOTHING`)
- 코드에서만 쓰는 값: 실패 코드 `DEADLINE`(러너가 냄)·`LEASE_EXPIRED`·`DEADLINE_EXCEEDED`(WAS가 냄) — DB는 자유 문자열 32자라 enum은 코드 상수(`AgentRunFailureCode`, `app/enums.py` AI 실행 묶음 끝)
- 감사 이름(코드): `AuditAction.PRECHECK_REQUESTED`·`PRECHECK_CANCELLED`·`AGENT_RUN_CLAIMED`·`AGENT_RUN_FINISHED`·`AGENT_RUN_TIMED_OUT`, `AuditTargetType.AGENT_RUN="agent_run"`, `AuditComponent.AI_RUNNER="ai-runner"`

## 7. 상태 전이

| 지금 | 사건 | 다음 | 조건부 UPDATE의 WHERE | 쓰는 칸 | 감사 |
|---|---|---|---|---|---|
| — | 사람 `POST prechecks` | QUEUED | (INSERT, 열린 실행 unique) | `deadline_at=now+15m`, `input_*`, `source/target` | USER `PRECHECK_REQUESTED` |
| QUEUED | 러너 claim | CLAIMED | `status='QUEUED' AND deadline_at>now` (SKIP LOCKED로 집은 행) | `claimed_by_runner_id`, `run_token_hash`, `claimed_at`, `lease_until=now+60s` | SYSTEM `AGENT_RUN_CLAIMED` |
| CLAIMED | 첫 heartbeat·events | RUNNING | `status='CLAIMED' AND lease_until>=now AND deadline_at>now` | `started_at`, `lease_until` | — |
| RUNNING | heartbeat·events | RUNNING | `status='RUNNING' AND lease_until>=now AND deadline_at>now` | `lease_until=min(now+60s, deadline_at)` | — |
| CLAIMED·RUNNING | 러너 result | SUCCEEDED·FAILED | `status IN (CLAIMED,RUNNING) AND lease_until>=now AND deadline_at>now` | `result`, `failure_code`, `model_id`, `prompt_version`, `simulated`, `finished_at` | SYSTEM `AGENT_RUN_FINISHED` `{status, failureCode?, simulated, aiReviewed}` |
| QUEUED·CLAIMED·RUNNING | 부를 때 만료(임대 지남 또는 마감 지남) | TIMED_OUT | `status IN (열린) AND (deadline_at<=now OR (status<>'QUEUED' AND lease_until<now))` | `failure_code`, `finished_at` | SYSTEM `AGENT_RUN_TIMED_OUT` |
| QUEUED·CLAIMED·RUNNING | 사람 cancel | CANCELLED | `status IN (열린)` | `finished_at` | USER `PRECHECK_CANCELLED` |
| 끝 상태 | 무엇이든 | (그대로) | — | — | — |

- 만료를 부르는 곳: `POST prechecks`(그 서비스의 열린 실행), `claim`(전체, 한 번에 최대 100행), 실행 토큰 호출(그 실행), `cancel`(그 실행). 만료 줄과 원래 응답은 같은 트랜잭션. `GET`은 쓰지 않는다
- `POST prechecks`는 서비스 행을 잠그고(`get_live_by_id(lock=True)`) 만료 → 열린 실행 확인 → INSERT 순서. 부분 unique 위반은 `PRECHECK_IN_PROGRESS`로 바꾼다(경합 대비)
- 자동 재시도 없음(계약 47행)

## 8. 시험 AC

| AC | 확인 | 시험(가칭) |
|---|---|---|
| AC-1 | **끝까지 한 번**: 개발자 POST → 러너 claim → events 3개 → result(SUCCEEDED, simulated=false, model=null) → 상세에 configDiff·finding 1건·`aiReviewed=false`. 그 뒤 게이트를 켠 계획 승인에서 C-AI-1이 막지 않고, `gate_log_data.findingIds`에 실행 id가 있다 | `test_precheck_round_trip_satisfies_ai_1_gate` |
| AC-2 | 같은 흐름에서 `simulated=true`로 내면 C-AI-1이 `EVIDENCE_SIMULATED`로 막는다 | `test_simulated_precheck_does_not_satisfy_gate` |
| AC-3 | 24시간 지나면 증거가 아니다(시계 주입) · 다른 도착 환경 실행은 증거가 아니다 | `test_precheck_evidence_expires_after_24h`, `test_precheck_for_other_target_is_ignored` |
| AC-4 | 만들기 거절: 서비스 없음·삭제 중·출발 하나 아님·같은 환경·다른 조직 환경·열린 실행 있음(409 + runId) | `test_create_precheck_rejects_*` |
| AC-5 | `inputSnapshot`에 비밀값·`secretRef`·변수 값이 없고, 같은 상태면 같은 `inputHash` | `test_input_snapshot_has_no_secrets_and_stable_hash` |
| AC-6 | 가져가기: 실행 없으면 `{run:null}` · 두 러너 동시 claim에 하나만 받음 · capabilities에 PRECHECK 없으면 null · 마감 지난 QUEUED는 TIMED_OUT · 철회된 러너 401 · runnerId 불일치 422 | `test_claim_*` |
| AC-7 | 실행 토큰: 다른 실행 id에 쓰면 401 · 결과 뒤 heartbeat·events 409 `RUN_NOT_ACTIVE` · 두 번째 result 409 `RESULT_ALREADY_SUBMITTED` · 임대 지난 뒤 result 409 + TIMED_OUT 저장 | `test_run_token_*` |
| AC-8 | 이벤트: 같은 seq 두 번 무시·`acceptedUpTo` · 101개·message 201자·모르는 step 422 · 첫 이벤트가 RUNNING을 만든다 · logs `after` 페이지 | `test_events_*`, `test_logs_page_after_seq` |
| AC-9 | 결과 검사: 모르는 칸 422 · `simulated` 빠짐 422 · AI finding PASS 422 · 크기 상한 422 · SUCCEEDED+failureCode 422 · FAILED는 failureCode 필수 · 토큰 모양 문자열이 저장 전에 `***` · 패치 상태 재계산(보호 파일·check 실패 → BLOCKED) | `test_result_validation_*`(단위), `test_result_is_masked_before_storage` |
| AC-10 | 취소: 열린 실행 CANCELLED · 끝난 실행 409 · 취소 뒤 실행 토큰 409 | `test_cancel_precheck_*` |
| AC-11 | 권한: 역할 표에 8개 경로(사람 4는 `_org(...)` — GET 2개 `V`, POST 2개 `D, human_only=True`; 러너 4는 `RUNNER_ONLY = {anonymous:401, outsider·viewer·developer·admin·agent: 403}`) · 검사기 자동 수집 통과 · 러너 토큰으로 조직 경로 401 | `test_role_table`(표 추가), `test_every_route_has_guards`, `test_runner_token_cannot_call_org_routes` |
| AC-12 | 감사: 사람 2개 경로가 USER 줄 1개씩(`WRITE_ROUTE_ACTIONS` **27 → 29**, `test_audit_logs.py` 144·147·599행과 `test_audit_recorder.py` 129~131행, `docs/api/audit-logs.md` 81행 개수 함께) · claim·result·만료가 SYSTEM `ai-runner` 줄 · heartbeat·events는 줄 없음 · "쓰기 경로 = 사람 표 ∪ 러너 표 ∪ 로그아웃" | `test_every_write_route_has_an_action_name`(수정), `test_runner_actions_are_system_ai_runner` |
| AC-13 | 발급 명령: 원문 한 번 출력·DB엔 해시만·같은 이름 두 번 실패·`--revoke` 뒤 claim 401 | `test_issue_runner_token_*` |
| AC-14 | 마이그레이션 차이 없음(`alembic heads` 하나 `286d9ea982d6`, 새 리비전 0) · ruff·format·mypy app·pytest(DB, 건너뜀 0) 통과 | `test_alembic_check_reports_no_differences`(기존) |

## 9. 운영 설정

- **새 환경 변수 없음**(마감·임대는 상수, AD-10)
- C-AI-1을 실제로 보려면 운영 API·일꾼에 `SECURITY_GATE_ENABLED=true`(기존 설정, 기본 false). 켜면 C-DB-1·C-DST-1 등 다른 필수 통제도 판정하므로 기한부 예외(#64, 병합됨)가 함께 있어야 시연 이전이 통과한다 — 켜는 시점은 이 PR 밖(12절)
- 러너 토큰: 병합·배포 뒤 WAS 파드에서 한 번 `python -m app.scripts.issue_runner_token --name echo-sandbox-runner` → 출력 원문을 GCP Secret Manager 비밀 **`echo-runner-token`**에 넣고 VM SA에 그 비밀 읽기만(AI-1 AD-4 방식). 원문은 채팅·로그에 남기지 않는다
- 인그레스: `/api/v1/runner/*`는 기존 `api.echoport.uk` 경로 그대로(추가 설정 없음, 추론 — 배포 뒤 VM에서 `claim` 401 응답으로 확인)

## 10. 호환성

- 마이그레이션 0 → 롤링 배포 중 옛 파드와 새 파드가 섞여도 표가 같다. 옛 파드는 새 경로를 404로 답할 뿐(`replicas: 1`이라 짧음)
- 기존 행: `agent_runs`는 운영에 행이 없다(쓰는 코드가 없었음 — 추론, 병합 전 `select count(*)`로 확인). 게이트의 C-AI-1 판정 코드는 바꾸지 않는다
- 프론트: `PRECHECK_API_READY=false` 그대로면 화면 영향 없음. 전환 때 단계 이름·패치 모양 대응은 프론트 몫(계약 170행)
- 에이전트 토큰·쿠키 인증 경로는 바뀌지 않는다(`authenticate`의 `echo_agt_` 분기 그대로, 러너 검사기는 별도 함수)
- `mask_secrets` 패턴 추가는 기존 로그 응답에도 적용된다(가리는 것이 늘기만 함)

## 11. 이 저장소 밖 — 러프 러너 스크립트가 할 일 (결정 ③A, echo-ai-agent 별도 PR)

이 PR은 만들지 않는다. WAS가 맞춰야 할 러너 쪽 동작만 적는다. 모델 호출 없이 성립한다.

1. 설정: `ECHO_WAS_URL`(예 `https://api.echoport.uk`), `ECHO_RUNNER_TOKEN`(Secret Manager `echo-runner-token` → 프로세스 환경 변수만), `ECHO_RUNNER_NAME=echo-sandbox-runner`
2. `POST /api/v1/runner/runs:claim {runnerId, capabilities:["PRECHECK"]}` — `run:null`이면 5초 쉬고 다시(`--once`면 끝)
3. 받은 `runToken`으로 이벤트를 보낸다(`events.py` `RunEventLog` 재사용): `SANDBOX_CREATE` "sandbox created" → `INPUT_LOAD` "input snapshot loaded" → `CONFIG_DIFF` "config diff computed" → `SANDBOX_DELETE`
4. 규칙 비교: `source.config`와 `target.config`의 키 합집합을 돌며 값이 다르면 `configDiff {key, source, target, origin:"RULE", severity:"INFO"}`; `variableKeys` 차이는 `WARN`; 이미지 digest 다름·도착 digest 없음은 `INFO`
5. 경고 1건: 차이가 하나라도 있으면 `findings` 1개 `{control:"config-diff", environmentId:target, result:"WARN", …, origin:"RULE"}`. 차이가 0이면 `result:"PASS"`, origin RULE
6. `POST …/result {status:"SUCCEEDED", failureCode:null, simulated:false, sandbox, model:null, configDiff, featureGaps:[], findings, patches:[], blockedAttempts:[]}`. 예외가 나면 `FAILED` + `failureCode:"RUNNER_ERROR"`, 마감 1분 전이면 `DEADLINE`
7. 가짜 모드(`--fake`)는 입력을 보지 않고 고정 값을 내며 반드시 `simulated:true` — C-AI-1 증거가 되지 않는다

## 12. 이 PR 밖에서 M1이 할 일 (구현 뒤)

1. 형래님께 계약 문구 변경 알림(echo-ai-agent #4 댓글 + PR 리뷰 요청): `RunResult.simulated`·`model:null`(D-1), `CLAIMED→RUNNING`은 첫 하트비트·이벤트(AD-2), 이벤트 message 200자(AD-12), GET 상세 추가 칸(AD-19)
2. 병합·배포 뒤 러너 토큰 발급 → Secret Manager `echo-runner-token` → VM에서 러너 `--once` 실행 → 상세 GET으로 SUCCEEDED·`simulated=false` 확인
3. 게이트 켜기 리허설(별도 결정): 기한부 예외(C-DB-1·C-DST-1) 승인 → `SECURITY_GATE_ENABLED=true` → 이전 계획 승인 → 실행 로그 `SECURITY_GATE_PASSED`의 `findingIds`에 실행 id
4. 프론트(김도영)에 응답 모양·단계 대응표 공유

## 13. 규모 어림

| 묶음 | 파일 | 어림 줄 수 |
|---|---|---|
| 새 파일 | `app/routers/precheck_router.py` · `app/routers/runner_router.py` · `app/services/precheck_service.py`(만들기·조회·취소·입력 스냅숏) · `app/services/agent_run_service.py`(가져가기·하트비트·이벤트·결과·만료) · `app/services/run_result.py`(검사·패치 재계산, 순수) · `app/schemas/precheck.py` · `app/schemas/runner.py` · `app/auth/runner_guards.py` · `app/repositories/agent_runner_repository.py` · `app/repositories/agent_run_event_repository.py` · `app/scripts/issue_runner_token.py` · `docs/api/prechecks.md` · `docs/agent-runner.md` | ~1,300 |
| 고치는 파일 | `app/auth/tokens.py` · `app/repositories/agent_run_repository.py` · `app/core/exceptions.py` · `app/audit/actions.py` · `app/enums.py` · `app/core/log_masking.py` · `app/main.py` · `app/dependencies.py` · `docs/api/auth.md` · `docs/api/audit-logs.md` · `.claude/rules/term-dictionary.md` · `CLAUDE.md` · `README.md` | ~250 |
| 시험 | 새 `tests/integration/test_prechecks_api.py` · `test_runner_api.py` · `test_precheck_gate.py` · `test_issue_runner_token.py` · `tests/test_run_result.py`(단위) + 고침 `test_authorization.py` · `test_audit_logs.py` · `tests/test_audit_recorder.py` · `tests/test_log_masking.py` | ~1,100 |

- 합계: 새 파일 ~18개 + 고침 ~17개, 약 2,600줄, 새 시험 ~40개. M5 구현 2.5~3.5시간

## 14. 기준 main에 맞춘 배치 (#64·#68 병합 뒤)

#64(기한부 예외)는 이미 main에 있으므로 충돌은 없다. 대신 아래 자리에 이어 쓴다.

| 파일 | 넣을 자리 |
|---|---|
| `app/audit/actions.py` | `AuditAction` 사람 묶음 끝(`SECURITY_EXCEPTION_REVOKED` 뒤)에 `PRECHECK_REQUESTED`·`PRECHECK_CANCELLED`, 서버(SYSTEM) 묶음에 `AGENT_RUN_CLAIMED`·`AGENT_RUN_FINISHED`·`AGENT_RUN_TIMED_OUT`, `AuditTargetType` 끝 `AGENT_RUN`, `AuditComponent` 끝 `AI_RUNNER`, `WRITE_ROUTE_ACTIONS` 끝 2줄(→ 29), 새 표 `RUNNER_ROUTE_ACTIONS` |
| `tests/integration/test_audit_logs.py` · `tests/test_audit_recorder.py` | 개수 27 → 29(상수 그대로 고침, `len(WRITE_ROUTE_ACTIONS)`로 바꾸지 않음), 걷기 시험에 사전 점검 2개 |
| `docs/api/audit-logs.md` | 81행 "27개 경로" → 29, 행동 표 + SYSTEM `ai-runner` 줄 |
| `app/main.py` | import 목록 알파벳 순(`precheck_router`는 `organization_router` 뒤, `runner_router`는 `precheck_router` 뒤), `include_router` 끝에 두 줄 |
| `tests/integration/test_authorization.py` | `ROLE_TABLE`의 기한부 예외 5줄 뒤·`PATCH /settings` 앞에 사람 4줄, 러너 4줄은 `RUNNER_ONLY` |
| `docs/api/auth.md` | 역할 표에 사전 점검 줄(뷰어 읽기·개발자(사람만) 쓰기), 러너 경로 절, 오류 표 맨 아래에 새 코드 |
| `app/core/exceptions.py` · `app/dependencies.py` | 파일 끝(AD-21) |

## 15. 위임한 세부사항 (워커가 정하고 보고)

- 서비스·저장소 클래스 이름, 파일 나눔, 스키마 클래스 구조, 로그 줄 문구, 페이지 크기 상수
- `steps[]` 계산 세부(이벤트가 없을 때 빈 배열)
- 만료 UPDATE 한 번 행 수 상한(100 기준)

## 부록. AI 기본값 (AD-1~21, 지민 채택 11/5 03:05)

| ID | 정한 것 |
|---|---|
| AD-1 | **새 표·칸·마이그레이션 없음.** 러너 신원·토큰은 `agent_runners`, 실행 토큰은 `agent_runs.run_token_hash`, 이벤트는 `agent_run_events` |
| AD-2 | `CLAIMED → RUNNING`은 **첫 하트비트 또는 첫 이벤트**가 만든다(`started_at`). 결과는 CLAIMED·RUNNING 모두에서 받는다 |
| AD-3 | 시간 초과는 **일꾼 루프 없이 "부를 때 만료"**: `POST prechecks`(그 서비스)·`claim`(전체)·실행 토큰 호출(그 실행)·`cancel` 때 조건부 UPDATE로 `TIMED_OUT`. `failure_code` = `LEASE_EXPIRED`·`DEADLINE_EXCEEDED`. `GET`은 쓰지 않고 저장된 상태 + `leaseUntil`만 보여 준다 |
| AD-4 | 실행 토큰 "무효"는 **해시를 지우지 않고 상태로 판정**: 끝난 실행에 하트비트·이벤트 → 409 `RUN_NOT_ACTIVE`, 결과 → 409 `RESULT_ALREADY_SUBMITTED`. 형식·해시 불일치·경로의 실행 ID와 다름 → 401 `RUNNER_TOKEN_INVALID` |
| AD-5 | 검사기 2개 `require_runner`(러너 토큰)·`require_run_token`(실행 토큰)을 `AUTH_GUARDS`에 등록. `Authorization` 헤더도 쿠키도 없음 → 401 `RUNNER_TOKEN_INVALID`, **쿠키(사람)·`Bearer echo_agt_…` → 403 `RUNNER_FORBIDDEN`**, 접두어가 틀리거나(예: claim에 `echo_run_`) 해시가 없음 → 401. `RUNNER_FORBIDDEN`은 일반 `ForbiddenError`(거부 감사 줄 안 남김) |
| AD-6 | 러너 쓰기 경로는 `WRITE_ROUTE_ACTIONS`에 넣지 않고 **별도 표 `RUNNER_ROUTE_ACTIONS`**(claim → `AGENT_RUN_CLAIMED`, result → `AGENT_RUN_FINISHED`, heartbeat·events는 기록 안 함). 시간 초과 → `AGENT_RUN_TIMED_OUT`. 모두 SYSTEM `component="ai-runner"`, 조직 = 실행의 조직, 대상 `agent_run`. 시험은 "쓰기 경로 = 사람 표 ∪ 러너 표 ∪ 로그아웃"으로 고친다 |
| AD-7 | 사람 쪽 감사 이름 `PRECHECK_REQUESTED`·`PRECHECK_CANCELLED`(USER, 대상 `agent_run`, details `{serviceId, targetEnvironmentId}`) |
| AD-8 | 출발 환경은 이전 계획과 같은 `_single_source`(트래픽 100% 연결 하나 + 현재 배포) → 아니면 409 `SOURCE_NOT_SINGLE`. 도착 = 출발이면 422(사유 `SAME_ENVIRONMENT`). 도착은 아직 연결 안 된 환경도 받는다. 열린 실행이 있으면 409 `PRECHECK_IN_PROGRESS`(`details.runId`) |
| AD-9 | `inputSnapshot` v1 = `{schemaVersion:1, service:{id,name,containerPort,healthCheckPath}, source:{environmentId,kind,config,imageDigest,variableKeys[]}, target:{…, imageDigest:null 가능}, findings:[{controlId,result,source,environmentId,collectedAt}] ≤50}`. `config`에서 `secretRef`는 빼고, 직렬화 문자열에 `mask_secrets()`. `inputHash` = `sha256:` + 정렬 JSON 해시. `scenarioRef` = null(저장소 고정 커밋은 2차) |
| AD-10 | 마감 15분·임대 60초는 **코드 상수**(설정 아님) |
| AD-11 | `RunResult` 검사: Pydantic `extra="forbid"`(camelCase), 상한 findings 100·patches 10·diff 200KB·configDiff 200·featureGaps 100·blockedAttempts 50·문자열 2,000자·본문 1MB. SUCCEEDED면 `failureCode` null, FAILED면 필수(`^[A-Z][A-Z0-9_]{0,31}$`). `origin=AI` + PASS → 422 `RESULT_INVALID`. 저장 전 직렬화 문자열 `mask_secrets()` |
| AD-12 | 이벤트 message는 **200자**(DB 칸 기준), 넘으면 422. 한 번에 100개, 같은 seq는 무시, `acceptedUpTo` = 그 실행의 가장 큰 seq. 계약 111행 "2KB"는 200자로 고치자고 형래님께 알림 |
| AD-13 | `patches`는 받아서 **WAS가 상태 재계산**(BLOCKED·PROPOSED, `blockReason`)해 결과에 저장. 승인·거절 API는 2차 |
| AD-14 | AI `findings`는 `agent_runs.result`에만 두고 **`findings` 표에는 넣지 않는다**(2차) |
| AD-15 | `blockedAttempts`는 저장하되 각 줄 `auditLogId: null, confirmed: false`("확인 안 됨"). 감사 줄 맞추기는 2차 |
| AD-16 | `claim` 본문 `runnerId`는 토큰의 러너 `name`과 같아야 함(다르면 422). `capabilities`에 `PRECHECK` 없으면 `{run:null}`. 철회된 러너(`revoked_at`) → 401. `last_seen_at`은 60초 간격으로 갱신 |
| AD-17 | 발급 명령 `python -m app.scripts.issue_runner_token --name <이름>`(있으면 실패) · `--revoke <이름>`. 원문은 표준 출력 한 번, 로그 0. 감사 줄 없음(데모 시드와 같음) |
| AD-18 | `mask_secrets()`에 `echo_(agt|rnr|run)_[A-Za-z0-9_-]{20,}` 패턴 추가 |
| AD-19 | `GET 상세`는 계약 v1 모양(단계 이름 `SANDBOX_CREATE…`, 패치 `files[]`·unified `diff`) + 화면 편의 칸 `sandboxId`·`steps[]`(이벤트에서 계산)·`patches[].verification{passed,total}`(checks에서 계산)·`simulated`·`aiReviewed`. 단계 이름 대응은 프론트 몫(계약 170행) |
| AD-20 | 브랜치는 **#64 병합 뒤 main `552e80b`**에서 딴다. 감사 개수는 **27 → 29**(사람 쓰기 경로 2개 추가) |
| AD-21 | 새 오류 코드 7개(`RUNNER_TOKEN_INVALID`·`RUNNER_FORBIDDEN`·`RUN_NOT_ACTIVE`·`RESULT_ALREADY_SUBMITTED`·`RESULT_INVALID`·`PRECHECK_IN_PROGRESS`·`PRECHECK_NOT_FOUND`)는 `app/core/exceptions.py` **파일 끝**, 서비스 팩토리는 `dependencies.py` **파일 끝**에 둔다 |
