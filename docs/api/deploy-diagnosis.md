# 배포 실패 AI 진단 API — 진단 시작 · 결과 · 승인 뒤 반영 · 재배포 (이슈 #98)

실패한 배포 요청 하나에서 **AI 진단**(`agent_runs.kind = CHANGE_PROPOSAL`)을 만들고, 원인·근거·수정 패치를 사람이 보고 승인하면 반영 실행(`APPLY_PATCH`)을 거쳐 **같은 환경에 재배포 요청**을 자동으로 만든다. 시나리오 3(배포 실패 → AI 진단 → 승인 → 재배포). 근거: SPEC [specs/api-98-failed-deploy-diagnosis.md](../../specs/api-98-failed-deploy-diagnosis.md), 요청 이슈 echo-was #98(칸 이름은 이슈 그대로), 러너 쪽 echo-ai-agent #22.

진단 실행은 사전 점검과 같은 `agent_runs` 행이고 상세·이벤트·승인은 사전 점검 API([prechecks.md](prechecks.md))를 그대로 쓴다. 모든 경로는 `/api/v1/orgs/{orgId}` 아래, 응답은 공통 봉투 `ApiResponse`, camelCase 다.

## 엔드포인트

| 메서드 · 경로 | 성공 | 주요 실패 | 역할 |
|---|---|---|---|
| `POST /deploy-requests/{id}/diagnosis` (본문 없음 또는 `{simulate?}`) | 201 `PrecheckDetail`(`kind: CHANGE_PROPOSAL`). 이미 있으면 **200 같은 실행** | 404 `DEPLOY_REQUEST_NOT_FOUND`, 409 `DEPLOY_NOT_FAILED`·`SERVICE_DELETING`·`PRECHECK_IN_PROGRESS`(러너 실행만), 422 `VALIDATION_ERROR`·`SIMULATE_NOT_ALLOWED` | 개발자(사람) |
| `GET /deploy-requests/{id}` | `diagnosis: {runId, status} \| null` 칸이 더해진다 | | 뷰어 |
| `GET /prechecks/{runId}` · `/logs` | `PrecheckDetail` + `diagnosis` 칸, 진행은 `steps`·`/logs` | 404 `PRECHECK_NOT_FOUND` | 뷰어 |
| `POST /prechecks/{runId}/patches/{patchId}/approve`·`reject` | `PrecheckPatch`, `apply` 에 칸 4개가 더해진다 | [prechecks.md](prechecks.md#패치-승인거절) 와 같다 | 개발자(사람) |
| `POST /prechecks/{runId}/cancel` | 러너가 안 가져간 진단을 풀어 준다 | 409 `RUN_NOT_ACTIVE` | 개발자(사람) |

거절 순서(만들기): `simulate` 스위치(422) → 배포 요청(404) → **이미 있음(200)** → 실패 아님(409 `DEPLOY_NOT_FAILED`, `FAILED` 가 아닌 모든 상태 — 진행 중·성공·취소) → 서비스 삭제 중(409) → 열린 실행(409 `PRECHECK_IN_PROGRESS`, 러너 실행일 때만). 배포 요청 하나에 진단은 하나(`uq_agent_runs_deploy_request_id_diagnosis`)라 동시에 눌러도 실행은 하나이고 늦은 쪽은 200 이다.

## 가짜 진단 `simulate` (러너 진단이 붙기 전 기본)

| 스위치(환경 변수) | 기본 | 뜻 |
|---|---|---|
| `AI_DIAGNOSIS_RUNNER_ENABLED` | `false` | 꺼져 있으면 **본문 없는 요청도 가짜 진단**(`simulate.result = DIAGNOSED`)이다. 켜면 본문 없는 요청이 러너가 가져갈 `QUEUED` 실행을 만든다. CHANGE_PROPOSAL 을 가져가는 러너(echo-ai-agent #22)가 배포된 뒤에 켠다 — 먼저 켜면 15분 `QUEUED` 뒤 `TIMED_OUT`, 그동안 그 서비스의 새 사전 점검이 409 |
| `AI_DIAGNOSIS_SIMULATE_ENABLED` | `true` | 본문 `simulate` 를 받는가. 끄면 `simulate` 본문은 422 `SIMULATE_NOT_ALLOWED` |

본문: `{"simulate": {"result": "DIAGNOSED" | "INSUFFICIENT_EVIDENCE"}}`(기본 `DIAGNOSED`, 다른 칸 422).

- `DIAGNOSED`: 원인 "app/static/index.html 120번째 줄의 괄호가 닫히지 않았습니다.", 분류 `CODE`, 근거 2줄(LOG 42줄, FILE `app/static/index.html` 120줄), AI 패치 1개(`fix-index-html-paren`, 검사 BUILD·TRIVY·HEALTHZ PASS).
- `INSUFFICIENT_EVIDENCE`: 패치 없이 `fixable: false` + `unfixableReason`.
- 실행은 만들 때 바로 `SUCCEEDED`·**`simulated: true`**·`aiReviewed: false` 로 끝나고 `steps` 4개(INPUT_LOAD·ANALYZE·PATCH_PROPOSE·PATCH_VERIFY)가 보인다. 러너 결과와 같은 검사·정규화를 거친다.
- 가짜 진단의 패치를 승인하면 러너 없이 **가짜 반영**(`apply.status: SUCCEEDED`, `apply.simulated: true`, `appliedCommitSha` 는 지어낸 40자)으로 끝나고 곧바로 재배포 요청을 만든다(`PATCH_APPLY_ENABLED` 와 상관없음).

```bash
# 실패한 배포 요청에서 (러너 스위치 꺼짐 = 기본이면 본문 없이도 가짜 진단)
curl -X POST "$API/api/v1/orgs/$ORG/deploy-requests/$DR/diagnosis" -H "Cookie: …"
# 근거 부족 모양을 보고 싶을 때
curl -X POST "$API/api/v1/orgs/$ORG/deploy-requests/$DR/diagnosis" -H "Cookie: …" \
  -H 'Content-Type: application/json' -d '{"simulate":{"result":"INSUFFICIENT_EVIDENCE"}}'
```

## 배포 상세 `diagnosis`

```json
"diagnosis": {"runId": "uuid", "status": "SUCCEEDED"}
```

진단 전이면 null. `status` 는 진단 실행의 상태(`QUEUED`·`CLAIMED`·`RUNNING`·`SUCCEEDED`·`FAILED`·`TIMED_OUT`·`CANCELLED`). OpenAPI 에서는 선택 칸이지만 응답에는 늘 실린다. 재배포 요청에는 없다(null).

## 진단 결과 `PrecheckDetail.diagnosis`

`kind = CHANGE_PROPOSAL` 일 때만 객체, 다른 종류는 null.

```json
{
  "kind": "CHANGE_PROPOSAL",
  "status": "SUCCEEDED",
  "simulated": true,
  "targetEnvironmentId": "uuid",
  "diagnosis": {
    "result": "DIAGNOSED",
    "category": "CODE",
    "cause": "app/static/index.html 120번째 줄의 괄호가 닫히지 않았습니다.",
    "fixable": true,
    "unfixableReason": null,
    "evidence": [
      {"source": "LOG", "line": 42, "path": null, "text": "SyntaxError: Unexpected token ';'"},
      {"source": "FILE", "line": 120, "path": "app/static/index.html", "text": "      renderStatus(data;"}
    ],
    "deployRequestId": "uuid",
    "environmentId": "uuid",
    "sourceCommitSha": "40자"
  },
  "patches": [{"id": "fix-index-html-paren", "origin": "AI", "status": "PROPOSED", "…": "…"}]
}
```

| 칸 | 뜻 |
|---|---|
| `result` | `DIAGNOSED` · `INSUFFICIENT_EVIDENCE` · `NO_FAILURE_EVIDENCE`. 결과 전(`QUEUED`·`CLAIMED`·`RUNNING`)·결과 없이 끝남(`TIMED_OUT`·`CANCELLED`)이면 null |
| `category` | `CODE` · `CONFIG` · `BUILD` · `RUNTIME` · `PLATFORM`. `DIAGNOSED` 일 때 필수 |
| `cause` | 원인 한 줄(비밀값 가림). `DIAGNOSED` 일 때 필수 |
| `fixable`·`unfixableReason` | `fixable: false` 면 사유 필수이고 `patches` 는 빈 배열 |
| `evidence[]` | `{source: LOG·FILE·CONFIG, line, path, text}`. LOG 는 실패 로그(`failure.logTail`) 줄 번호(1부터), FILE 은 저장소 경로·줄 번호(`path` 필수). 최대 20개 |
| `deployRequestId`·`environmentId`·`sourceCommitSha` | 만들 때 고정한 값이라 결과 전에도 있다 |

모든 문자열은 저장 전에 `mask_secrets` 를 거친다. 러너가 `diagnosis` 를 빼고 결과를 내면(지금 러너) 근거 부족(`INSUFFICIENT_EVIDENCE`, `unfixableReason: "Runner did not report a diagnosis."`)으로 저장한다. 실행이 `FAILED` 이면 `"Diagnosis run failed: <failureCode>"`.

## 승인 뒤 반영 · 재배포 `patches[].apply`

```json
"apply": {
  "runId": "uuid",
  "status": "SUCCEEDED",
  "failureCode": null,
  "finishedAt": "…",
  "appliedCommitSha": "40자",
  "branch": "applied/<반영 실행 id>",
  "simulated": true,
  "pullRequestUrl": null,
  "redeployRequestId": "uuid 또는 null",
  "redeployRejectedCode": null
}
```

| 칸 | 뜻 |
|---|---|
| `pullRequestUrl` | 저장소 반영 PR. **러너가 push 하지 않아 지금은 늘 null** |
| `redeployRequestId` | 반영이 `SUCCEEDED` 가 된 뒤 WAS 가 같은 환경에 만든 재배포 요청 id. 화면은 이 값이 생기면 배포 진행 화면(`GET /deploy-requests/{id}`)으로 잇는다. 반영 전·실패·사전 점검 패치·접수 거절이면 null |
| `redeployRejectedCode` (가칭, 이슈에 없던 칸) | 재배포 접수가 거절된 오류 코드(예: `MIGRATION_IN_PROGRESS`·`SERVICE_DELETING`). 거절이 없으면 null. 이 값이 있으면 화면은 "재배포를 시작하지 못함"을 보이고 사람이 다시 배포한다 |

재배포 요청은 기존 배포 접수 규칙(`POST /services/{id}/deploy-requests`)을 그대로 거친다: `trigger: MANUAL`, `ref: {type: COMMIT, value: <실패한 요청의 commitSha>}`, 같은 환경, 요청자 = 승인한 사람, 감사 `DEPLOY_REQUESTED` 의 `details.diagnosisApplyRunId` = 반영 실행 id. **지금은 원래 실패 커밋을 다시 배포한다** — 러너가 고친 커밋을 고객 저장소에 push 하지 않기 때문이다(push·PR 이 생기면 그 커밋으로 바꾼다, SPEC 7절). 반영 성공을 다시 받아도(같은 결정 재전송, 결과 재전송) 재배포는 한 번만 만든다.

언제 이어지나: 가짜 진단은 승인 응답 안에서, 러너 반영은 러너가 `ApplyPatchResult(SUCCEEDED)` 를 낸 결과 요청 안에서 만든다. 접수 거절은 결과 응답을 실패로 만들지 않는다.

## 러너 계약 (CHANGE_PROPOSAL) — echo-ai-agent #22 가 맞출 기준

### 가져가기

`POST /api/v1/runner/runs:claim` 의 `capabilities` 에 `CHANGE_PROPOSAL` 을 넣은 러너에게만 진단 실행을 준다(넣지 않은 지금 러너는 받지 않는다). 응답 `run.kind = "CHANGE_PROPOSAL"`, `sourceEnvironmentId: null`, `targetEnvironmentId` = 실패한 환경.

### 입력 사본 `inputSnapshot` (v1)

```json
{
  "schemaVersion": 1,
  "service": {"id": "…", "name": "shop-api", "containerPort": 8080, "healthCheckPath": "/health"},
  "deployRequest": {
    "id": "…", "environmentId": "…", "trigger": "MANUAL",
    "repo": "owner/name", "refType": "BRANCH", "refValue": "main",
    "commitSha": "40자", "errorCode": "CONTAINER_START_FAILED", "failedAt": "…"
  },
  "environment": {"id": "…", "kind": "gcp"},
  "variableKeys": ["DATABASE_URL"],
  "failure": {
    "code": "CONTAINER_START_FAILED",
    "message": "The container kept crashing after it started. Last error: …",
    "container": "app", "containerReason": "CrashLoopBackOff", "terminationReason": "Error",
    "exitCode": 1, "restartCount": 3,
    "logTail": ["…", "…"], "logTruncated": false, "previousVersionRunning": true
  }
}
```

- `failure` 는 #97(API 서버 4-1) 배포 상세 `failure` 칸과 **같은 값**(비밀값을 가린 로그 꼬리 최대 50줄)이다. 실패 상세가 없으면 null.
- 비밀값·비밀 참조·변수 값은 싣지 않는다(변수는 이름만). 모든 문자열은 `mask_secrets` 를 거친다.
- `repo` 는 서비스 저장소 `owner/name`(해석 못 하면 null). 저장소를 받아 읽는 일은 러너 몫이다.

### 결과 `POST /{id}/result` — `RunResult` + `diagnosis`

`RunResult`(사전 점검과 같은 칸·검사, [agent-runner.md](../agent-runner.md#결과-post-idresult--runresult))에 선택 칸 `diagnosis` 하나를 더한다.

```json
{
  "status": "SUCCEEDED", "failureCode": null, "simulated": false,
  "sandbox": {"…": "…"}, "model": {"…": "…"}, "processing": {"…": "…"},
  "configDiff": [], "featureGaps": [], "findings": [], "blockedAttempts": [],
  "patches": [{"id": "fix-1", "patchHash": "sha256:…", "files": ["app/static/index.html"],
               "diff": "--- a/…", "reason": "…", "touchesProtectedFiles": false,
               "checks": [{"name": "BUILD", "result": "PASS", "summary": "…"}], "origin": "AI"}],
  "diagnosis": {
    "result": "DIAGNOSED", "category": "CODE", "cause": "…",
    "fixable": true, "unfixableReason": null,
    "evidence": [{"source": "LOG", "line": 42, "text": "…"},
                 {"source": "FILE", "path": "app/static/index.html", "line": 120, "text": "…"}]
  }
}
```

검사(어기면 422 `RESULT_INVALID`, 모르는 칸도 422):

- `result = DIAGNOSED` 면 `category`·`cause` 필수. 그 밖의 결론이면 `fixable` 은 false.
- `fixable = false` 면 `unfixableReason` 필수.
- `patches` 는 `diagnosis.fixable = true` 일 때만 붙일 수 있다(진단 없이 패치만 내면 422).
- `evidence` 최대 20개, `line` 1 이상, `source = FILE` 이면 `path` 필수. 문자열 길이 상한은 RunResult 와 같다.
- `deployRequestId`·`environmentId`·`sourceCommitSha` 는 러너가 보내지 않는다(WAS 가 채운다).
- `diagnosis` 를 빼면 근거 부족으로 저장한다(위).

### 반영 실행(APPLY_PATCH) 입력 차이

진단 패치의 반영 실행 입력 사본은 사전 점검 반영과 같고 두 가지가 다르다: `repo` = 서비스 저장소(`owner/name`, 사전 점검은 echo-ai-agent 시나리오 사본), 그리고 `diagnosis: {deployRequestId, environmentId, sourceCommitSha}` 가 더해진다. 결과(`ApplyPatchResult`)는 그대로다. 러너가 `sourceCommitSha` 에서 패치를 적용·빌드·스캔·healthz 를 확인하고 `SUCCEEDED` 를 내면 WAS 가 재배포를 만든다.

## C-AI-1 · 레지던시

진단 실행은 **C-AI-1 증거가 아니다**(게이트는 `kind = PRECHECK` 만 읽는다). AI 레지던시 요약([ai-residency.md](ai-residency.md))도 진단 실행을 세지 않는다(숫자·판정·위반·최근 실행 모두).

## 감사

`DIAGNOSIS_REQUESTED`(USER 줄, 대상 `agent_run`, 이름표 = 서비스 이름, `details: {serviceId, deployRequestId, environmentId, simulated}`). 200(이미 있음)은 줄을 쓰지 않는다. 승인·거절은 `AGENT_PATCH_APPROVED`·`AGENT_PATCH_REJECTED` 그대로이고, 재배포는 `DEPLOY_REQUESTED`(`details.diagnosisApplyRunId`).

## 시연 메모

- 진단을 누른 사람과 승인하는 사람이 같으면 조직 설정 `allowSelfApproval` 이 켜져 있어야 한다(아니면 403 `SELF_APPROVAL_FORBIDDEN`).
- 가짜 진단이라도 재배포는 실제 배포 요청이다(원래 커밋을 같은 환경에 다시 배포).
