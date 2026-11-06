# SPEC — AI 패치 반영 실행을 러너가 가져가고 결과를 받는다 (echo-was, API 서버 10-2)

- 상태: **확정(Confirmed)** · 지민 결정 11/6 10:5x "P2 지금 진행". 세부 규칙은 API 서버 10 결정표의 PD-8·12·14 그대로, PD-9(결과 모양)는 **계약 2.1에 없는 `patchHash` 칸을 빼고** 따른다(아래 3절 — 지시 "계약 칸을 더하지 않는다", 러너 쪽 병행 구현과 같은 5칸)
- 기준: echo-was main `61fe915`(#84 병합 뒤). 표·칸은 리비전 `201cf5419f4d`에 이미 있다 → **마이그레이션 0**, `alembic heads` = `31bc0563b24b` 하나
- 앞 작업: [api-10-patch-approval.md](api-10-patch-approval.md) "범위 밖(별도 PR)"의 S3
- 근거(정본): echo-ai-agent `specs/ai-1-appendix-a-was-runner-contract.md` v1 — 2.1절(`APPLY_PATCH` 결과 `{appliedCommitSha, branch}`, "승인 때의 `patchHash`와 다시 계산한 해시가 같을 때만"), 3.2절(`:claim`·`result`), 4절(반영 대상은 echo-ai-agent 시나리오 사본 브랜치뿐, 운영 저장소·운영 환경에는 쓰지 않음)
- 짝: 러너 쪽(echo-ai-agent, 같은 계약을 보고 따로 진행). 러너 쪽 PR이 병합·배포되기 전에는 스위치를 켜도 반영 실행을 가져갈 러너가 없다
- 범위 밖: 반영 성공 뒤 새 배포로 잇기(샌드박스에 레지스트리·운영 쓰기 권한을 주지 않는 설계), 운영 고객 저장소 PR(P1), 반영 실행 취소 API, 반영 실행 이벤트 화면, `CHANGE_PROPOSAL`, 운영 ConfigMap(`deploy/base.yaml`) 스위치 변경

## 1. 목표

승인된 패치의 반영 실행(`APPLY_PATCH`)을 러너가 `:claim`으로 가져가고, 결과(`ApplyPatchResult`)를 내면 WAS가 **승인 지문과 대조한 뒤에만** 저장한다. 사람은 부모 사전 점검 상세의 `patches[].apply`로 반영 상태(`QUEUED`→`CLAIMED`→`RUNNING`→`SUCCEEDED`·`FAILED`·`TIMED_OUT`)와 결과 요약(커밋 SHA·브랜치·`simulated`)을 본다.

러너 자격은 그대로다: 러너 토큰(가져가기만) + 실행 토큰(그 실행 하나의 heartbeat·events·result). 새 권한·새 토큰 없음.

## 2. 가져가기 `POST /api/v1/runner/runs:claim` (PD-8)

- `capabilities`에 든 종류만 준다: `PRECHECK`·`APPLY_PATCH`(대소문자 그대로). `CHANGE_PROPOSAL`·모르는 값은 무시한다.
- 고른 종류 중 **오래된 순**(`created_at`, `id`) 하나. 우선순위는 따로 없다(계약에도 없음). `FOR UPDATE SKIP LOCKED` + 조건부 UPDATE는 그대로.
- 지금 러너(`["PRECHECK"]`)는 영향 0: 반영 실행을 받지 않는다.
- 응답 모양은 그대로다(새 칸 없음). `kind = "APPLY_PATCH"`, 반영에 필요한 값은 모두 `inputSnapshot`(API 서버 10 PD-6)에 있다:

```json
{"run": {"id": "<반영 실행 id>", "kind": "APPLY_PATCH", "orgId": "…", "serviceId": "…",
         "sourceEnvironmentId": "…", "targetEnvironmentId": "…",
         "inputSnapshot": {"schemaVersion": 1, "parentRunId": "<부모 사전 점검 id>",
                           "patch": {"id": "p1", "patchHash": "sha256:…", "files": ["…"], "diff": "--- a/…", "origin": "RULE"},
                           "repo": "Softbank-Hackathon-2026-Team-Echo/echo-ai-agent", "branch": "applied/<반영 실행 id>"},
         "inputHash": "sha256:…", "scenarioRef": null, "deadlineAt": "…", "leaseUntil": "…"},
 "runToken": "echo_run_…"}
```

- 감사 `AGENT_RUN_CLAIMED` details에 `kind`를 더한다: `{runnerName, kind}`(PD-12).

## 3. 결과 `POST /api/v1/runner/runs/{id}/result` (PD-9)

본문 모양은 **실행 종류로 고른다**: `APPLY_PATCH`면 `ApplyPatchResult`, 그 밖은 지금의 `RunResult`. 다른 모양을 보내면 422 `RESULT_INVALID`.

```json
{"status": "SUCCEEDED", "failureCode": null, "simulated": false,
 "appliedCommitSha": "<40자 소문자 hex 또는 null>", "branch": "applied/<반영 실행 id>"}
```

계약 2.1의 두 칸 + 모든 결과의 공통 칸(`status`·`failureCode`·`simulated`)뿐이다. `patchHash`를 보내면 모르는 칸(422)이다.

| 칸 | 규칙 |
|---|---|
| `status` | `SUCCEEDED`·`FAILED` |
| `failureCode` | `SUCCEEDED`면 null, `FAILED`면 필수(`^[A-Z][A-Z0-9_]{0,31}$`) |
| `simulated` | 필수 bool. 러너 가짜 모드면 true(화면 배지) |
| `appliedCommitSha` | `^[0-9a-f]{40}$` 또는 null. 러너가 커밋하지 못한 사본(git 없음)에서 반영·검증만 했으면 성공이어도 null. `FAILED`면 null |
| `branch` | 1~255자. `SUCCEEDED`면 필수이고 입력 사본 `branch`와 같아야 함. `FAILED`면 아무 값·null(대조 안 함) |
| 그 밖 | 모르는 칸·snake_case → 422(`extra=forbid`) |

WAS 지문 검사 — **`SUCCEEDED` 결과만**(위에서부터, 모두 422 `RESULT_INVALID`, 저장 없음, 실행은 열린 채 — 마감 안에 다시 낼 수 있다). 실패 결과는 대조하지 않는다(러너가 해시 불일치를 `FAILED`로 알리는 길을 막지 않으려고):

| 순서 | 경우 | `details` |
|---|---|---|
| 1 | 반영 실행을 만든 승인 행이 없음(정상 흐름엔 없음) | `[{field: "status", reason: "APPROVAL_NOT_FOUND"}]` |
| 2 | 러너에게 준 입력 사본 `patch.patchHash` ≠ 승인 행 `patch_hash` | `[{field: "inputSnapshot.patch.patchHash", reason: "PATCH_HASH_MISMATCH"}]` |
| 3 | WAS가 입력 사본 `patch.diff`로 **다시 계산한** 해시 ≠ 승인 행(가려진 diff 등) | `[{field: "inputSnapshot.patch.diff", reason: "PATCH_HASH_MISMATCH"}]` |
| 4 | `branch` ≠ 입력 사본 `branch` | `[{field: "branch", reason: "BRANCH_MISMATCH"}]` |

- 끝난 실행·임대 지남·두 번째 제출은 지금 규칙 그대로(409 `RESULT_ALREADY_SUBMITTED`·`RUN_NOT_ACTIVE`, 만료 저장). 상태 검사가 지문 검사보다 먼저다.
- 저장: `status`·`failure_code`·`simulated`는 표의 칸, `result = {appliedCommitSha, branch}`. `model_id`·`prompt_version`·`processing`은 null(이 결과에 칸이 없다 → 레지던시 요약에서 "기록 없음").
- 감사 `AGENT_RUN_FINISHED`: `{kind: "APPLY_PATCH", status, failureCode?, simulated, aiReviewed: false, appliedCommitSha}`. 사전 점검 결과의 줄에도 `kind: "PRECHECK"`를 더한다(PD-12).
- OpenAPI: 요청 본문은 `oneOf [RunResult, ApplyPatchResult]`.

### 3.1 패치 해시(PD-14)

- 정의: `patchHash = "sha256:" + sha256(diff 문자열의 UTF-8 바이트).hexdigest()` — echo-ai-agent #18(규칙 패치 제안)과 같은 정의.
- **사전 점검 결과를 받을 때도** WAS가 `diff`로 다시 계산해 다르면 422 `RESULT_INVALID`(`details[0].field = "patches.<i>"`). 지금 운영 러너는 패치를 내지 않아 영향 0. 검사는 비밀값 가리기 전의 원문 diff로 한다.
- 가리기(`mask_secrets`)로 저장 diff가 바뀐 패치는 승인돼도 반영 성공을 받을 수 없다(3절 3번). 러너는 입력 사본 diff를 다시 계산해 다르면 `FAILED`·`failureCode: "PATCH_HASH_MISMATCH"`로 낸다(계약 2.1 "같을 때만").

## 4. 사람 쪽 `patches[].apply` (상태 계산은 API 서버 10 4절 표 그대로)

```text
apply: {runId, status, failureCode, finishedAt,
        appliedCommitSha,   # 새: 결과의 커밋 SHA. 결과 전·실패면 null
        branch,             # 새: 결과의 브랜치. 결과 전이면 null
        simulated} | null   # 새: 반영 실행 표의 값(결과 전 false)
```

| 결과 JSON 상태 | 결정 | 반영 실행 | 패치 `status` |
|---|---|---|---|
| 아무거나 | `REJECTED` | — | `REJECTED` |
| `BLOCKED` | 없음 | — | `BLOCKED` |
| `PROPOSED` | 없음 | — | `PROPOSED` |
| `PROPOSED` | `APPROVED` | 없음(스위치 꺼짐)·`QUEUED`·`CLAIMED`·`RUNNING` | `APPROVED` |
| `PROPOSED` | `APPROVED` | `SUCCEEDED` | `APPLIED` |
| `PROPOSED` | `APPROVED` | `FAILED`·`TIMED_OUT`·`CANCELLED` | `APPLY_FAILED` |

- "사람이 이어서 할 일"(`apply.nextStep` 등)은 계약에 없어 **더하지 않는다**. 화면은 `APPLIED`면 "검증된 패치가 `branch`에 커밋됨 — 고객 저장소 반영은 사람이" 문구를 고정으로 쓴다(프론트 몫).

## 5. 스위치 `PATCH_APPLY_ENABLED`

- 기본값 **꺼짐 그대로**, 운영 `deploy/base.yaml` 변경 없음. 꺼져 있으면 반영 실행을 만들지 않으므로 이번 변경으로 운영 동작이 바뀌는 것은 PD-14 검사(패치를 내는 러너만)와 감사 details의 `kind` 한 칸뿐이다.
- 켜기 조건: **러너가 `capabilities`에 `APPLY_PATCH`를 넣는 버전이 VM에 배포된 뒤**. 그 전에 켜면 여전히 반영 실행이 `QUEUED`로 15분 머문 뒤 `TIMED_OUT`(패치 `APPLY_FAILED`)이고 그동안 그 서비스의 새 사전 점검이 409다. 켜는 것은 별도 `deploy/base.yaml` PR.

## 6. 수용 기준(AC)

| # | 기준 | 시험 |
|---|---|---|
| AC-1 | 스위치 켬 → 승인 → `["PRECHECK"]` 러너는 null → `["PRECHECK","APPLY_PATCH"]` 러너가 `APPLY_PATCH`(입력 사본 = PD-6) → heartbeat(`RUNNING`) → 결과 200 → 상세 패치 `APPLIED`·`apply{appliedCommitSha, branch, simulated}` → 감사 CLAIMED·FINISHED(`kind`, `appliedCommitSha`) → 두 번째 결과 409, 새 사전 점검 201. 커밋 없는 성공(`appliedCommitSha: null`)도 `APPLIED`, 실패 결과 `APPLY_FAILED`, 임대 지남 `TIMED_OUT`, 가짜 모드 `simulated: true` | `test_apply_patch_round_trip`·`_success_without_commit_`·`_failed_`·`_simulated_`·`_lease_expiry_` |
| AC-2 | 지문(성공 결과만): 입력 사본 해시가 승인과 다름 422(실행 열린 채, 실패로 내면 200), 입력 diff 재계산 다름 422·실패 200, 브랜치 다름 422·실패 200, 사전 점검 결과의 해시가 diff와 다르면 422 | `test_apply_patch_snapshot_hash_*`·`_server_recomputes_*`·`_branch_mismatch_*`·`test_precheck_result_patch_hash_must_be_hash_of_diff` |
| AC-3 | 본문은 종류로 고름: 반영 실행에 RunResult·성공인데 branch 없음·모르는 칸(`checks`·`patchHash`)·SHA 형식·실패 코드 없음 422, 사전 점검에 ApplyPatchResult 422 | `test_result_body_is_chosen_by_run_kind`·`test_precheck_run_does_not_accept_apply_result_body` |
| AC-4 | 종류 필터: `PRECHECK`만·빈 값·`CHANGE_PROPOSAL`·소문자는 반영 실행 안 줌, `APPLY_PATCH`만이면 반영만 주고 사전 점검은 안 줌, 두 번 안 줌 | `test_claim_*` |
| AC-5 | 스위치 꺼짐: 승인은 `APPROVED`·`apply: null`, 반영 실행 0, 반영 러너도 null | `test_switch_off_*` |
| AC-6 | 권한·문서: 에이전트 토큰·사람 쿠키 403 `RUNNER_FORBIDDEN`, 러너 토큰 401, 반영 실행 토큰으로 부모 결과 401; OpenAPI `oneOf` 2개·`$ref` 없음; 단위(해시 정의·형식·정규화); ruff·format·mypy app·전체 pytest(건너뜀 0)·`alembic heads` 하나 | `test_apply_run_token_*`·`test_openapi_result_body_*`·`tests/test_run_result.py` |

## 7. 계약 보완 필요(러너 쪽과 맞출 것, 이번에 칸을 더하지 않음)

1. 계약 2.1은 결과를 `{appliedCommitSha, branch}`로만 적었다. 공통 칸 `status·failureCode·simulated`를 더한 5칸이 실제 모양이다(러너 쪽과 같음) → 계약 문서에 `ApplyPatchResult` 전체 모양과 `appliedCommitSha` null 허용(커밋 없는 성공)을 적어야 한다. 결정 PD-9의 `patchHash` 칸은 계약에 없어 넣지 않았다 — WAS는 입력 사본으로 대조한다.
2. 검증 결과(빌드·보안 스캔 `checks[]`), 산출물 요약, 사람이 이어서 할 일(`nextStep`)은 계약에 칸이 없다.
3. 대상 이미지 digest가 반영 입력 사본에 없다(부모 사전 점검 입력 사본에만 있다).
4. 이벤트 `step`에 반영 단계(예: `PATCH_APPLY`·`COMMIT`)가 없다 — 지금은 기존 값 중 하나를 써야 한다.
5. 반영 결과에 `processing`(레지던시) 칸이 없다 → 반영 실행은 레지던시 요약에서 "기록 없음".
6. 계약 4절은 "배포 키로 echo-ai-agent `applied/<runId>` 브랜치에 커밋"이다. 러너 쪽은 push 없는 로컬 커밋 + 샌드박스 검증(빌드·Trivy·healthz)으로 `SUCCEEDED`를 낸다 → `branch`는 원격에 없을 수 있고 `appliedCommitSha`는 로컬 커밋이다. 이 해석을 계약에 적어야 한다.
7. 실패 코드 목록(러너 쪽 `PATCH_HASH_MISMATCH`·`IMAGE_SCAN_FAILED` 등)이 계약 6절에 없다. WAS는 형식(`^[A-Z][A-Z0-9_]{0,31}$`)만 본다.
