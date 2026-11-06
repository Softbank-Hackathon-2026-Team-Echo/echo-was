# SPEC — 배포 실패 AI 진단 → 승인 → 반영 → 같은 환경 재배포 (echo-was, 이슈 #98)

- 상태: **확정(Confirmed)** · 이슈 [#98](https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/98) (요청 김도영 — 프론트, 담당 김지민). 칸 이름은 이슈 본문 그대로(프론트가 그 이름으로 붙인다)
- 기준: echo-was main `30aca36`(#96 첫 배포 전 변수, #97 실패 상세 `failure` 칸)
- 마이그레이션 **1개**: `9d8e1f2a3b4c` (down `31bc0563b24b`) — `agent_runs.deploy_request_id`(NULL 허용, FK RESTRICT) + CHECK `diagnosis_has_deploy_request` + 부분 unique `uq_agent_runs_deploy_request_id_diagnosis`. 더하기만 한다. heads 1
- 받는 쪽: echo-frontend #91(`DIAGNOSIS_API_READY`), echo-ai-agent #22(러너 진단 — 4절 형식에 맞춘다)
- API 문서: [docs/api/deploy-diagnosis.md](../docs/api/deploy-diagnosis.md)

## 1. 목표

시나리오 3(배포 실패 → AI 진단 → 승인 → 재배포)을 화면에서 실제 응답으로 보인다. 러너 진단이 붙기 전에는 WAS 가 **가짜 진단(`simulated: true`)** 을 돌려주고, 러너가 붙으면 스위치 하나로 실제 진단으로 바꾼다. 응답 모양은 같다.

## 2. 계약 (이슈 #98 대비)

| # | 이슈 요청 | 구현 | 차이 |
|---|---|---|---|
| 1 | `POST /orgs/{orgId}/deploy-requests/{id}/diagnosis` 본문 없음, 개발자 이상, 201 `PrecheckDetail`(`CHANGE_PROPOSAL`), 이미 있으면 200, 409 `DEPLOY_NOT_FAILED` | 그대로 | 선택 본문 `{simulate: {result}}` 를 더 받는다(없어도 된다) |
| 2 | 배포 상세 `diagnosis: {runId, status}` | 그대로(진단 전 null) | — |
| 3 | `GET /prechecks/{runId}` 에 `diagnosis{result, category, cause, fixable, unfixableReason, evidence[], deployRequestId, environmentId, sourceCommitSha}`, patches origin AI | 그대로 | `evidence[]` 원소는 `{source, line, path, text}` 4칸이 늘 있다(없으면 null) |
| 4 | `patches[].apply{…, appliedCommitSha, branch, pullRequestUrl, redeployRequestId}` | 그대로 + 기존 `finishedAt`·`simulated` | 가칭 칸 `redeployRejectedCode` 를 더했다(재배포 접수 거절 코드, 없으면 null). `pullRequestUrl` 은 지금 늘 null |
| 5 | `CHANGE_PROPOSAL` 은 C-AI-1 증거가 아니다 | 그대로 + AI 레지던시 요약에서도 뺀다 | — |

## 3. 동작

### 3-1. 만들기

거절 순서: simulate 스위치(422 `SIMULATE_NOT_ALLOWED`) → 배포 요청(404) → 이미 있음(200, 같은 실행·감사 줄 없음) → `FAILED` 아님(409 `DEPLOY_NOT_FAILED`) → 서비스 삭제 중(409) → 열린 실행(409 `PRECHECK_IN_PROGRESS`, 러너 실행만 — 가짜 진단은 끝난 채로 넣어 열린 실행 규칙에 걸리지 않는다).

- 실행: `kind = CHANGE_PROPOSAL`, `target_environment_id` = 실패한 환경, `source_environment_id = NULL`, `deploy_request_id` = 실패한 요청, 마감 15분, 요청자 = 누른 사람.
- 입력 사본(v1): `service`·`deployRequest{id, environmentId, trigger, repo, refType, refValue, commitSha, errorCode, failedAt}`·`environment{id, kind}`·`variableKeys`·`failure`(#97 배포 상세 `failure` 칸과 같은 camelCase 값). 비밀값·비밀 참조·변수 값 없음, 전체 `mask_secrets`.
- 동시 요청: 부분 unique 위반이면 먼저 들어간 실행을 200 으로 돌려준다.

### 3-2. 스위치

| 설정 | 기본 | 뜻 |
|---|---|---|
| `AI_DIAGNOSIS_RUNNER_ENABLED` | false | 꺼짐: 본문 없는 요청도 가짜 진단(`DIAGNOSED`). 켜짐: 러너가 가져갈 `QUEUED` 실행 |
| `AI_DIAGNOSIS_SIMULATE_ENABLED` | true | 본문 `simulate` 허용 여부 |

운영 동작: 병합 즉시 운영에서 진단 버튼이 가짜 진단을 돌려준다(`simulated: true` 배지). 러너 진단은 echo-ai-agent #22 가 `capabilities` 에 `CHANGE_PROPOSAL` 을 넣어 배포된 뒤 `deploy/base.yaml` 에서 `AI_DIAGNOSIS_RUNNER_ENABLED=true` 로 켠다(별도 PR).

### 3-3. 가짜 진단

러너 결과 본문과 같은 모양(`simulated_runner_payload`)을 만들어 러너 결과와 같은 정규화(`normalize_diagnosis_result`)를 거친 뒤 `SUCCEEDED`·`simulated = true`·`model = null` 로 바로 끝낸다. 이벤트 4줄(INPUT_LOAD·ANALYZE·PATCH_PROPOSE·PATCH_VERIFY)을 넣어 `steps` 가 보이게 한다.

- `DIAGNOSED`: `app/static/index.html` 120번째 줄 괄호, `CODE`, 근거 2줄, AI 패치 1개(검사 3개 PASS).
- `INSUFFICIENT_EVIDENCE`: 패치 없음 + `unfixableReason`.

### 3-4. 승인 → 반영 → 재배포

- 승인·거절은 사전 점검 규칙 그대로(부모 실행이 `PRECHECK` 또는 `CHANGE_PROPOSAL`). 자기 승인 금지도 그대로다.
- 반영 실행 입력 사본: 사전 점검 반영 + `repo` = 서비스 저장소, `diagnosis{deployRequestId, environmentId, sourceCommitSha}`.
- 가짜 진단의 패치: `PATCH_APPLY_ENABLED` 와 상관없이 반영 실행을 `SUCCEEDED`·`simulated = true`(지어낸 `appliedCommitSha`, `branch = applied/<id>`)로 넣는다. 승인 응답 안에서 재배포까지 잇는다.
- 러너 진단의 패치: `PATCH_APPLY_ENABLED` 가 켜져 있어야 반영 실행(`QUEUED`)이 생긴다. 러너가 `ApplyPatchResult(SUCCEEDED)` 를 내면 결과 요청 안에서 재배포를 잇는다.
- 재배포(`redeploy_after_apply`): 반영 실행이 `SUCCEEDED` 이고 부모가 진단이며 아직 연결이 없을 때 한 번만. 기존 배포 접수 규칙(`create_deploy_request` 두 번째 단계 = `create_redeploy_in_transaction`: 서비스 삭제 중·환경 접속·이전 진행 중 거절, 대기 요청 SUPERSEDED)으로 같은 환경·`ref = COMMIT <실패한 commitSha>`(이미 고정된 커밋이라 GitHub ref 해석 없음)·요청자 = 승인자, 감사 `details.diagnosisApplyRunId`. 성공하면 반영 실행 `result.redeployRequestId`, 접수 거절(AppError)이면 `result.redeployRejectedCode` 를 남긴다(결과·승인 응답은 성공 그대로).
- 동시성(검증 반영): **한 트랜잭션**으로 반영 실행 행 `SELECT … FOR UPDATE` → 연결 확인 → 접수 → `result` 연결을 한다. 같은 반영을 두 요청이 함께 이어도(같은 패치 동시 승인, 러너 결과와 승인 경합) 뒤 요청은 앞 커밋을 기다렸다가 이미 연결된 값을 읽어 그대로 돌려준다 — 재배포는 정확히 하나, 두 응답 모두 살아 있는(QUEUED) 그 요청 id. 잠금 순서: 반영 실행 → 환경 → 서비스 → 연결 → 요청 → 할 일(끝난 반영 실행 행을 서비스 잠금 뒤에 잡는 경로는 없다).
- 거절 격리(검증 반영): 접수는 savepoint(`begin_nested`) 안에서 한다. 거절이면 savepoint 만 되돌리고 같은 트랜잭션에서 `redeployRejectedCode` 를 쓴다(접수 전에 필요한 값을 미리 빼 두어 만료 객체를 읽지 않는다). 그 밖의 예외는 `INTERNAL_ERROR` 로 남긴다.

### 3-5. 러너 결과 (CHANGE_PROPOSAL)

결과 본문은 `RunResult` 이고 선택 칸 `diagnosis` 를 쓴다(진단 전용 검사는 서버가 실행 종류로 `DiagnosisRunResultRequest` 를 골라 더한다). 사전 점검 실행이 `diagnosis` 를 보내면(null 이어도) 422 `RESULT_INVALID`(`details: [{field: diagnosis, reason: DIAGNOSIS_NOT_ALLOWED}]`). 검사: DIAGNOSED ⇒ category·cause 필수 / 그 밖 ⇒ fixable false / fixable false ⇒ unfixableReason 필수 / patches 는 fixable 진단에만 / evidence ≤ 20, line ≥ 1, FILE ⇒ path. 위반·모르는 칸 422 `RESULT_INVALID`. `diagnosis` 가 없으면 근거 부족으로 저장(`FAILED` 면 사유에 failureCode). OpenAPI 결과 본문 `oneOf` 는 기존 2모양(RunResult·ApplyPatchResult) 그대로이고 RunResult 에 선택 칸 `diagnosis` 가 더해진다 — 진단 결과를 3번째 모양으로 두면 diagnosis 없는 본문이 RunResult·DiagnosisRunResult 에 함께 맞아 oneOf 가 겹친다(검증 반영).

### 3-6. C-AI-1 · 레지던시

게이트 증거는 `kind = PRECHECK` 만 읽는다(변경 없음, 시험으로 고정). `RESIDENCY_EXCLUDED_KINDS = {CHANGE_PROPOSAL}` 을 **조회 조건에서 먼저 빼고(LIMIT 전)** 5,000건 상한을 건다(`search_finished_for_residency(excluded_kinds=…)`) — 진단이 상한을 먹어 기존 사전 점검을 가리지 않는다(검증 반영). `summarize_residency` 의 같은 제외는 순수 함수 방어로 남긴다.

## 4. 러너 연동 요구 형식 (echo-ai-agent #22)

[docs/api/deploy-diagnosis.md 「러너 계약」](../docs/api/deploy-diagnosis.md#러너-계약-change_proposal--echo-ai-agent-22-가-맞출-기준) 이 기준이다. 요약:

1. `:claim` 의 `capabilities` 에 `CHANGE_PROPOSAL` 을 넣는다.
2. `inputSnapshot.failure`(#97 칸)·`deployRequest.repo`·`commitSha` 로 원인을 찾는다.
3. 결과는 `RunResult` + `diagnosis{result, category, cause, fixable, unfixableReason, evidence[]}`. 패치는 `origin: "AI"`, 검사 PASS.
4. 반영 실행(APPLY_PATCH)은 `repo`(서비스 저장소)·`diagnosis.sourceCommitSha` 에서 패치를 적용·검증한다. push·PR 을 하게 되면 `ApplyPatchResult` 에 칸을 더하는 별도 합의가 필요하다(지금은 `pullRequestUrl` null, 재배포는 원래 커밋).

## 5. 감사

USER `DIAGNOSIS_REQUESTED`(쓰기 경로 35개 중 하나, `WRITE_ROUTE_ACTIONS`). details `{serviceId, deployRequestId, environmentId, simulated}`.

## 6. 시험

- 단위: `tests/test_diagnosis_result.py`(결과 검사·가림·근거 부족·가짜 진단이 러너 스키마 통과), `tests/test_ai_residency.py::test_change_proposal_runs_are_not_counted`.
- 통합: `tests/integration/test_failed_deploy_diagnosis.py` — 실패 배포 → 진단 201 → 배포 상세 diagnosis → 가짜 결과 → 중복 200 → 승인 → 가짜 반영 → redeployRequestId → 재배포 성공 / 근거 부족 / 409 `DEPLOY_NOT_FAILED`·404·422 / simulate 스위치 / C-AI-1·레지던시 미산입 / 러너 경로(입력 failure·결과 diagnosis·APPLY_PATCH 입력·재배포) / 진단 없는 러너 결과 / (검증 반영) 같은 패치 동시 승인 → 재배포 1개·두 응답 같은 QUEUED id / 실제 접수 함수 거절(이전 계획 APPROVED·서비스 DELETING) → 승인 200·`redeployRejectedCode`·재배포 행 0 / 레지던시 상한 1 경계(옛 PRECHECK 1 + 최신 진단 1 → total 1·truncated false) / 사전 점검 결과의 `diagnosis` 칸 422.
- 기존 시험 갱신: 마이그레이션 카탈로그·head·down/up, 감사 쓰기 경로 35개, OpenAPI 결과 본문 2모양(RunResult 에 선택 `diagnosis`), `apply` 칸 3개.

## 7. 알려진 한계 (지금 하지 않음)

- 재배포는 원래 실패 커밋이다. 러너가 고친 커밋을 push 하지 않아서다 — 가짜 진단 시연에서 실패 원인이 실제 코드라면 재배포도 다시 실패할 수 있다.
- 배포 요청 하나에 진단은 하나다. 러너 진단이 `TIMED_OUT`·`CANCELLED` 로 끝나도 같은 요청에서 다시 만들 수 없다(200 으로 그 실행).
- 러너 실행이 열려 있는 동안 그 서비스의 사전 점검·다른 승인은 409 다(사전 점검과 같은 서비스당 하나 규칙).
