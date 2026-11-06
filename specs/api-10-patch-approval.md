# SPEC — AI 사전 점검 패치 승인·거절 API (echo-was, API 서버 10)

- 상태: **확정(Confirmed)** · 지민 결정 B-1(11/5 15:20) · 결정 ①~⑥ 모두 추천 A, PD-1~14 기본값(아래 "PD-7 바뀜" 하나만 다름)
- 기준: echo-was main `41705ed9e659187d3f508c0f91bd5781451291c6`(#76 병합 뒤). 승인 표·실행 종류·결정 enum은 리비전 `201cf5419f4d`에 이미 있다 → **마이그레이션 0**
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-ai-agent/issues/6 의 서버 쪽 (Project #4)
- 근거: echo-ai-agent 계약 부록 A v1 2.1·3.1·3.3·4·5·6절, `docs/api/prechecks.md`, `docs/agent-runner.md`, 조사 `facts.md`, 결정 `decisions.md`
- 범위 밖(별도 PR): 러너 쪽 `APPLY_PATCH` 가져가기(claim 종류 확장)·반영 결과 받기(`ApplyPatchResult`)·`patchHash` 재계산 검사(PD-8·9·14 → S3), 화면(echo-frontend S4), 러너 규칙 패치(echo-ai-agent S2), 배포 키·Secret, 운영 고객 저장소 PR(P1), `CHANGE_PROPOSAL`

## 1. 목표

사람(개발자 이상)이 사전 점검 결과의 `PROPOSED` 패치를 승인하거나 거절한다. 결정은 `agent_patch_approvals`에 한 번만 남고(지우지 않음), 감사에 USER 줄이 남는다. 패치 상태는 **저장하지 않고 계산**한다: 결과 JSON의 검증 상태(BLOCKED·PROPOSED) + 결정 행 + 반영 실행 상태.

승인하면 계약 4절대로 반영 실행(`APPLY_PATCH`)을 만들 수 있지만, **스위치 `PATCH_APPLY_ENABLED`의 기본값은 꺼짐**이다. 꺼져 있으면 실행을 만들지 않고 패치는 `APPROVED`·`apply: null`("승인됨 · 반영 대기")로 보인다. 러너가 반영 실행을 가져가는 길(S3)은 [api-10-2-apply-patch-run.md](api-10-2-apply-patch-run.md)로 WAS 쪽에 생겼다. 다만 **러너가 `capabilities`에 `APPLY_PATCH`를 넣는 버전이 배포되기 전에** 켜면 여전히 실행이 `QUEUED`로 15분 머문 뒤 `TIMED_OUT`이 되고 그동안 그 서비스의 새 사전 점검이 409다 — 그래서 기본 꺼짐은 그대로이고, 켜는 것은 러너 배포 확인 뒤 별도 PR이다.

## 2. 확인된 사실(기준 커밋)

| 무엇 | 위치 |
|---|---|
| 승인 표 `agent_patch_approvals(run_id, patch_id≤64, patch_hash, checks_digest, policy_version≤32, decision, decided_by_user_id, reason, decided_at, apply_run_id)`, `(run_id, patch_id)` unique, 해시 형식 CHECK 2개, CHECK `apply_run_only_when_approved` | `app/models/agent_patch_approval.py`, 리비전 `201cf5419f4d` |
| `AgentRunKind.APPLY_PATCH`, `agent_runs` CHECK `apply_patch_has_parent`, `parent_run_id` 자기 참조 FK, 서비스당 열린 실행 하나(`uq_agent_runs_service_id_open`, 종류 무관) | `app/enums.py`, `app/models/agent_run.py` |
| `AgentPatchDecision{APPROVED, REJECTED}`, `AgentPatchStatus{PROPOSED, BLOCKED}`, `AgentRunOrigin{RULE, AI}` | `app/enums.py` |
| 러너 결과 패치 `RunPatchRequest`(`extra=forbid`), 결과 저장 때 상태 재계산 `_with_recalculated_status` | `app/schemas/runner.py`, `app/services/run_result.py` |
| 상세 응답 `PrecheckPatchResponse.from_stored`(status·blockReason·verification) | `app/schemas/precheck.py` |
| 자기 승인 `SelfApprovalForbiddenError`(`AuditedForbiddenError`, 403 + 거부 감사 줄), 조직 `allow_self_approval` | `app/core/exceptions.py`, `migration_plan_service.py` 549·646행 |
| `HumanDeveloperAccess`(에이전트 403 `AGENT_READ_ONLY`) | `app/auth/guards.py`, 사전 점검 만들기·취소가 씀 |
| 사람 쓰기 경로 표 `WRITE_ROUTE_ACTIONS` 31개, 시험 `len(...) == 31` + 감사 걷기 시험 `ledger.seen == set(WRITE_ROUTE_ACTIONS.values())` | `app/audit/actions.py`, `tests/integration/test_audit_logs.py` 146·668행 |
| 역할 표 시험 | `tests/integration/test_authorization.py` 162~165행 |
| 시연 조직 `echo-demo`는 `allowSelfApproval: true` | 11/5 리허설 기록 |

## 3. 사람 쪽 경로 (`/api/v1/orgs/{orgId}`, `ApiResponse`, camelCase)

| 메서드·경로 | 본문 | 성공 | 검사기 | 감사 |
|---|---|---|---|---|
| `POST /prechecks/{precheckId}/patches/{patchId}/approve` | `{patchHash, reason?}` | 200 `PrecheckPatch`(갱신된 패치 하나) | `HumanDeveloperAccess` | `AGENT_PATCH_APPROVED` |
| `POST /prechecks/{precheckId}/patches/{patchId}/reject` | `{patchHash, reason?}` | 200 `PrecheckPatch` | `HumanDeveloperAccess` | `AGENT_PATCH_REJECTED` |

- `patchHash`: 화면이 본 값(`^sha256:[0-9a-f]{64}$`, 형식이 틀리면 422 `VALIDATION_ERROR`). `reason`: 선택, 1~2,000자(PD-2). 본문은 `extra=forbid`.
- 응답은 `GET /prechecks/{id}`의 `patches[]` 원소와 **같은 모양**(4절)이다. 화면은 그 원소를 바꿔 끼우면 된다.

거절 순서(PD-4, 위에서부터 처음 맞는 것):

| 순서 | 경우 | 응답 |
|---|---|---|
| 1 | 실행 없음·다른 조직·`kind ≠ PRECHECK` | 404 `PRECHECK_NOT_FOUND` |
| 2 | 결과에 그 `patchId` 없음(결과가 없거나 `patches`가 빈 경우 포함) | 404 `PATCH_NOT_FOUND` |
| 3 | 부모 실행이 `SUCCEEDED` 아님 | 409 `RUN_NOT_SUCCEEDED` |
| 4 | 결과 JSON의 패치 상태가 `BLOCKED`(승인만. 거절은 허용) | 409 `PATCH_BLOCKED`(`details.blockReason`) |
| 5 | 이미 결정됨 — **같은 결정이면 멱등 200**(새 행·감사 없음, 현재 패치 그대로), 다른 결정이면 409 | 409 `PATCH_ALREADY_DECIDED`(`details.decision`) |
| 6 | 본문 `patchHash` ≠ 저장 값(`details.reason=PATCH_HASH_MISMATCH`), 또는 부모 `finished_at`이 지금보다 24시간 넘게 전(PD-5, `details.reason=PRECHECK_EXPIRED`) | 409 `PATCH_STALE` |
| 7 | 승인자 = 부모 실행 요청자(`requested_by_user_id`)이고 조직 `allow_self_approval = false`(승인만) | 403 `SELF_APPROVAL_FORBIDDEN` + 거부 감사 줄(기존 예외 그대로) |
| 8 | 스위치 켜짐 + 승인 + 그 서비스에 열린 실행이 있음(만료는 `expire_overdue_runs`로 먼저 정리) | 409 `PRECHECK_IN_PROGRESS`(기존 예외, `details.runId`) |
| — | 에이전트 토큰 | 403 `AGENT_READ_ONLY` |
| — | 뷰어 | 403 `ROLE_FORBIDDEN` |

동시 요청: 결정 행 INSERT의 `(run_id, patch_id)` unique 위반(IntegrityError, savepoint)은 다시 읽어 5번 규칙을 적용한다(같은 결정이면 200, 다르면 409). 어느 경우든 결정 행과 USER 감사 줄은 하나다.

처리(한 트랜잭션, 부모 실행 행을 `FOR UPDATE`로 잠근 뒤):
1. 지문: `patchHash` = 저장 값, `checksDigest` = `sha256:` + `checks`의 정렬 JSON(`sort_keys`, `separators=(",", ":")`, `ensure_ascii=False`) 해시, `policyVersion` = 상수 `PATCH_POLICY_VERSION = "patch-policy-2026.11.05"`(PD-3).
2. (승인 + 스위치 켜짐만) `agent_runs` 행: `kind=APPLY_PATCH`, `parent_run_id`=부모, `organization_id`·`service_id`·`source/target_environment_id`=부모와 같음, `requested_by_user_id`=승인자, `deadline_at`=지금+15분(`RUN_DEADLINE`), `input_snapshot`(PD-6), `input_hash=compute_input_hash(snapshot)`. 열린 실행 unique 위반은 기존 `_insert`처럼 409 `PRECHECK_IN_PROGRESS`.
3. `agent_patch_approvals` 행: `decision`, `decided_by_user_id`, `reason`, `decided_at`=지금, `apply_run_id`=2의 id(없으면 null).
4. 감사(5절).

반영 실행 입력 `inputSnapshot`(PD-6, 스위치 켜짐일 때만 만들어짐):

```json
{"schemaVersion": 1, "parentRunId": "<부모 id>",
 "patch": {"id": "p1", "patchHash": "sha256:…", "files": ["scenarios/demo-app-0ff0d44/Dockerfile"], "diff": "--- a/…", "origin": "RULE"},
 "repo": "Softbank-Hackathon-2026-Team-Echo/echo-ai-agent", "branch": "applied/<applyRunId>"}
```

`branch`에 실행 id가 들어가므로 실행 id를 먼저 정한 뒤(`uuid4()`를 직접 넣음) 사본을 만든다.

## 4. `PrecheckPatch` 응답 모양 (상세 `patches[]`와 승인·거절 응답 공통)

```text
{id, patchHash, files[], diff, reason, touchesProtectedFiles, checks[],
 origin,                                   # 새: RULE | AI
 status,                                   # 넓힘: 아래 표
 blockReason, verification: {passed, total},
 approval: {decision, decidedByUserId, decidedAt, reason} | null,   # 새
 apply: {runId, status, failureCode, finishedAt} | null}            # 새
```

상태 계산(④A, 저장하지 않음 — 순수 함수 하나로 두고 단위 시험):

| 결과 JSON 상태 | 결정 행 | 반영 실행 | `status` |
|---|---|---|---|
| 아무거나 | `REJECTED` | — | `REJECTED`(맨 먼저 본다) |
| `BLOCKED` | 없음 | — | `BLOCKED` |
| `PROPOSED` | 없음 | — | `PROPOSED` |
| `PROPOSED` | `APPROVED` | 없음(스위치 꺼짐) 또는 `QUEUED`·`CLAIMED`·`RUNNING` | `APPROVED` |
| `PROPOSED` | `APPROVED` | `SUCCEEDED` | `APPLIED` |
| `PROPOSED` | `APPROVED` | `FAILED`·`TIMED_OUT`·`CANCELLED` | `APPLY_FAILED` |

- `AgentPatchStatus`에 `REJECTED`·`APPROVED`·`APPLIED`·`APPLY_FAILED`를 더한다(JSON 안 값·응답용, DB CHECK 없음). 결과 저장 때 재계산(`run_result.py`)은 지금처럼 `PROPOSED`·`BLOCKED`만 쓴다.
- `origin`: 저장된 패치에 없으면 `AI`(PD-11).
- `apply.status`는 반영 실행의 `AgentRunStatus`. `appliedCommitSha`·`branch`·`simulated`는 S3([api-10-2-apply-patch-run.md](api-10-2-apply-patch-run.md) 4절)에서 더했다.
- 조회: 부모 실행 + 결정 행(`run_id`로 한 번) + 반영 실행(`apply_run_id` 목록으로 한 번). N+1 없음. `GET`은 여전히 쓰지 않는다.
- 반영 실행은 사람 쪽 `GET /prechecks/{id}`로 직접 열리지 않는다(PRECHECK 아님 → 404 유지, PD-10).

## 5. 감사 (⑤A)

| 이름 | 행위자 | 언제 | 결과 | 대상·details |
|---|---|---|---|---|
| `AGENT_PATCH_APPROVED` (새) | USER | 승인 저장(멱등 200은 기록 안 함) | SUCCEEDED | 부모 실행(`run_target(run)`) · `{patchId, patchHash, applyRunId}`(applyRunId는 null 가능) |
| `AGENT_PATCH_REJECTED` (새) | USER | 거절 저장 | SUCCEEDED | 부모 실행 · `{patchId, patchHash}` |
| (기존 거부 줄) | USER | 자기 승인 금지 | DENIED | `SelfApprovalForbiddenError` 그대로 |

- `WRITE_ROUTE_ACTIONS` 31 → **33**(사람 경로 2개). `RUNNER_ROUTE_ACTIONS` 변경 없음. 감사 걷기 시험이 새 경로 둘을 실제로 부르게 고친다.
- 러너 쪽 `AGENT_RUN_CLAIMED`·`FINISHED`에 `kind` 추가(PD-12 일부)는 S3.

## 6. 러너 계약 변경 — `origin` 칸 하나만 (⑥A)

- `RunPatchRequest`에 선택 칸 `origin: AgentRunOrigin`(기본 `AI`) 하나를 더한다. 그 밖의 모르는 칸은 지금처럼 422. 저장 결과 JSON의 패치에 `origin`이 실린다(빠지면 `"AI"`로 저장).
- 패치 `patchHash` 재계산 검사(PD-14)·`claim` 종류 확장(PD-8)·반영 결과 모양(PD-9)은 이번에 바꾸지 않는다 — 러너(S2)와 합의한 계약 변경은 `origin` 하나다.
- 이미 저장된 결과(origin 없음)는 응답에서 `AI`로 보인다.

## 7. 데이터·설정

- **마이그레이션 0**(PD-1). alembic 리비전을 만들지 않는다. 마이그레이션이 정말 필요하다고 판단되면 구현을 멈추고 보고한다.
- 설정 `patch_apply_enabled: bool = False`(환경 변수 `PATCH_APPLY_ENABLED`, `.env.example`에 주석과 함께 `false`). **PD-7 바뀜**: 결정 표 초안은 기본 true였으나 지민 결정 B-1로 기본 false.
- 상수 `PATCH_POLICY_VERSION`(32자 이하).
- 새 오류 코드(`app/core/exceptions.py` 파일 끝, PD-13): `PATCH_NOT_FOUND`(404), `RUN_NOT_SUCCEEDED`(409), `PATCH_BLOCKED`(409), `PATCH_ALREADY_DECIDED`(409), `PATCH_STALE`(409).
- 경로는 `app/routers/precheck_router.py`에 더한다(새 라우터 없음). 서비스는 새 파일 `app/services/precheck_patch_service.py`, 저장소 새 파일 `app/repositories/agent_patch_approval_repository.py`.

## 8. 수용 기준(AC)

| # | 기준 | 시험(가칭) |
|---|---|---|
| AC-1 | **승인 끝까지**: `SUCCEEDED` 사전 점검 결과에 PROPOSED 패치 1개 → 개발자 승인(`patchHash` 일치) → 200, 패치 `status=APPROVED`·`approval{decision APPROVED, decidedByUserId, decidedAt, reason}`·`apply=null`, `GET` 상세의 같은 패치도 같음, 결정 행 1개(checks_digest·policy_version 채움, apply_run_id null), 열린 실행 0, `AGENT_PATCH_APPROVED` 줄 1. 같은 승인 다시 → 200·행·감사 그대로. 스위치를 켠 설정으로 승인 → `APPLY_PATCH` 실행 1개(`QUEUED`, `parent_run_id`, inputSnapshot의 patch·`branch=applied/<id>`), `apply.runId`·`apply.status=QUEUED`, 감사 `applyRunId`; 기존 `claim(["PRECHECK"])`은 그 실행을 주지 않음. 상태 계산 6줄(4절 표)은 단위 시험 | `test_patch_approve_round_trip`, `test_patch_approve_with_apply_enabled`, `test_patch_status_*`(단위) |
| AC-2 | **거절**: 거절 → 200 `status=REJECTED`, 반영 실행 없음, `AGENT_PATCH_REJECTED` 줄 1; 같은 거절 다시 200(멱등), 그 뒤 승인 409 `PATCH_ALREADY_DECIDED`; BLOCKED 패치도 거절은 됨 | `test_patch_reject_*` |
| AC-3 | **거부 순서**: 없는 실행 404 `PRECHECK_NOT_FOUND`, 없는 패치 404 `PATCH_NOT_FOUND`, 끝나지 않은 부모 409 `RUN_NOT_SUCCEEDED`, BLOCKED 승인 409 `PATCH_BLOCKED`, 해시 다름 409 `PATCH_STALE`(`PATCH_HASH_MISMATCH`), 24시간 지남 409 `PATCH_STALE`(`PRECHECK_EXPIRED`), 동시 승인·거절 2건 → 하나만 200·하나 409, 결정 행·감사 줄 각 1 | `test_patch_decision_rejections`, `test_patch_decision_concurrent` |
| AC-4 | **권한**: 자기 승인 스위치 꺼짐 403 `SELF_APPROVAL_FORBIDDEN` + 거부 감사 줄(행 없음), 켜짐 200; 다른 개발자는 스위치와 무관하게 200; 뷰어 403, 에이전트 토큰 403 `AGENT_READ_ONLY`; 역할 표(`test_authorization.py`)에 2줄 `human_only=True` | `test_patch_self_approval_*`, 역할 표 |
| AC-5 | **계약·감사·문서**: 러너 결과 패치에 `origin` 빠지면 저장·응답 `AI`, `RULE`은 그대로, 다른 값 422, 그 밖의 모르는 칸은 여전히 422; `WRITE_ROUTE_ACTIONS == 33`, 감사 걷기 시험 통과; `docs/api/prechecks.md`(경로·거부 순서·상태 표·스위치)·`docs/agent-runner.md`(`origin` 칸, "승인 없음" 문장 고침)·`docs/api/audit-logs.md`(이름 2개) 갱신; ruff·format·mypy app·전체 pytest(건너뜀 0)·`alembic heads` 하나·새 리비전 0 | `test_runner_patch_origin_*`, 기존 걷기 시험 |

## 9. 위임한 세부사항(워커가 정하고 보고)

- 서비스·저장소 함수 이름, 응답 조립 위치(`PrecheckView`에 결정·반영 실행 맵을 더하는 방식 권장).
- `PATCH_POLICY_VERSION` 상수 위치, 시험 파일 나누기(`tests/integration/test_precheck_patches.py` 권장).
- 스위치를 시험에서 켜는 방법(설정 의존성 덮어쓰기 등 기존 시험 방식).

## 10. 운영 영향

- 스위치 기본 꺼짐: 승인·거절은 기록만, 실행·서비스 잠금 영향 0.
- 스위치 켜면 반영 실행이 열려 있는 동안(최대 15분) 그 서비스의 새 사전 점검·다음 승인은 409 `PRECHECK_IN_PROGRESS`. WAS 쪽 S3는 생겼다([api-10-2](api-10-2-apply-patch-run.md)). `APPLY_PATCH`를 가져가는 러너가 배포되기 전에는 켜지 않는다.
