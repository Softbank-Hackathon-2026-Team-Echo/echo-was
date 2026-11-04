# AI 러너 ↔ WAS 계약 (러너 쪽, AI 러너 레인 A)

샌드박스 VM 의 러너가 WAS 를 부르는 방법이다. **러너가 WAS 를 폴링한다.** WAS 는 러너를 부르지 않는다. 사람 쪽 API 는 [docs/api/prechecks.md](api/prechecks.md) 다. 근거: SPEC [specs/api-runner-prechecks.md](../specs/api-runner-prechecks.md), echo-ai-agent `specs/ai-1-appendix-a-was-runner-contract.md` v1(계약과 다르면 SPEC 이 이긴다, 아래 "계약과 다른 점").

경로 접두어 `/api/v1/runner/runs`, 응답은 공통 봉투 `ApiResponse`, 본문·응답은 camelCase 다. 이번 범위는 `PRECHECK` 뿐이다(`APPLY_PATCH`·`CHANGE_PROPOSAL` 은 가져가지 못한다).

## 자격 증명

| 토큰 | 누가 | 할 수 있는 것 |
|---|---|---|
| 러너 토큰 `echo_rnr_…` | 제어 프로세스. 어드민 명령으로 발급(API 없음) | `POST :claim` 만 |
| 실행 토큰 `echo_run_…` | 제어 프로세스. `:claim` 응답에 한 번만 온다 | 그 실행 하나의 heartbeat·events·result |

WAS 에는 SHA-256 해시만 있다. 모델(AI)에게 두 토큰을 주지 않는다. 사람 쿠키·에이전트 토큰은 러너 경로에서 403 `RUNNER_FORBIDDEN`(감사 거부 줄 없음)이고, 러너·실행 토큰은 조직 경로에서 401 이다.

| 경우 | 응답 |
|---|---|
| 토큰 없음·`Bearer` 아님·접두어 틀림·해시 없음·철회된 러너 | 401 `RUNNER_TOKEN_INVALID` |
| 실행 토큰의 실행 ≠ 경로의 실행 id | 401 `RUNNER_TOKEN_INVALID` |
| 사람 쿠키만 있음 또는 `Bearer echo_agt_…` | 403 `RUNNER_FORBIDDEN` |
| 끝난 실행의 실행 토큰 | 해시를 지우지 않고 상태로 판정: heartbeat·events 409 `RUN_NOT_ACTIVE`, result 409 `RESULT_ALREADY_SUBMITTED` |

## 경로

| 메서드·경로 | 자격 | 요청 | 응답 | 실패 |
|---|---|---|---|---|
| `POST :claim` | 러너 | `{runnerId, capabilities}` | 200 `{run, runToken}` · 없으면 `{run: null, runToken: null}` | 401 · 403 · 422 `VALIDATION_ERROR` |
| `POST /{id}/heartbeat` | 실행 | `{}` | 200 `{status, leaseUntil}` | 401 · 403 · 409 `RUN_NOT_ACTIVE` |
| `POST /{id}/events` | 실행 | `{events: [...]}` 1~100개 | 200 `{acceptedUpTo}` | 401 · 403 · 409 `RUN_NOT_ACTIVE` · 422 `RESULT_INVALID` |
| `POST /{id}/result` | 실행 | `RunResult` | 200 `{status}` | 401 · 403 · 409 `RESULT_ALREADY_SUBMITTED`·`RUN_NOT_ACTIVE` · 422 `RESULT_INVALID` |

### 가져가기 `POST :claim`

- `runnerId` 는 토큰의 러너 이름과 같아야 한다(다르면 422, `details: [{field: "runnerId", reason: "RUNNER_ID_MISMATCH"}]`).
- `capabilities` 에 `PRECHECK` 가 없으면 `run: null` 이다.
- 대기 실행을 오래된 순으로 하나 준다(`FOR UPDATE SKIP LOCKED` + 조건부 UPDATE). 동시에 불러도 한 러너만 받는다. 없으면 5초쯤 쉬고 다시 부른다.

```json
{
  "run": {
    "id": "…", "kind": "PRECHECK", "orgId": "…", "serviceId": "…",
    "sourceEnvironmentId": "…", "targetEnvironmentId": "…",
    "inputSnapshot": {"schemaVersion": 1, "…": "…"}, "inputHash": "sha256:…",
    "scenarioRef": null, "deadlineAt": "…", "leaseUntil": "…"
  },
  "runToken": "echo_run_…"
}
```

`inputSnapshot` 모양은 [prechecks.md](api/prechecks.md#입력-사본-inputsnapshot-v1) 를 본다.

### 상태와 임대

| 지금 | 사건 | 다음 |
|---|---|---|
| `QUEUED` | `:claim` | `CLAIMED`(임대 60초) |
| `CLAIMED` | **첫 heartbeat 또는 첫 이벤트** | `RUNNING`(`startedAt`) |
| `CLAIMED`·`RUNNING` | heartbeat·events | 임대를 지금 + 60초로(마감을 넘지 않음) |
| `CLAIMED`·`RUNNING` | result | `SUCCEEDED`·`FAILED` |
| 열린 상태 | 임대 지남(`QUEUED` 제외) 또는 마감 지남 | `TIMED_OUT`(`LEASE_EXPIRED`·`DEADLINE_EXCEEDED`) |
| 열린 상태 | 사람 취소 | `CANCELLED` |

- 마감 15분, 임대 60초(코드 상수). 임대 시각과 정확히 같으면 아직 살아 있다. **30초쯤마다 하트비트를 보낸다.**
- **일꾼 루프가 없다.** 만료는 누가 부를 때 한다: `POST prechecks`(그 서비스)·`:claim`(전체, 한 번에 100행)·실행 토큰 호출(그 실행)·취소(그 실행). `GET` 은 쓰지 않는다. 만료 전환은 커밋하고 그 호출은 409 다.
- 자동 재시도는 없다. 다시 하려면 사람이 새 실행을 만든다.
- 결과를 낼 수 없으면 마감 1분 전까지 `status: "FAILED"`, `failureCode: "DEADLINE"` 으로 낸다.

### 이벤트 `POST /{id}/events`

```json
{"events": [{"seq": 1, "at": "2026-11-05T00:00:00Z", "step": "SANDBOX_CREATE", "level": "INFO", "message": "sandbox created", "data": {"image": "…"}}]}
```

| 칸 | 규칙 |
|---|---|
| `seq` | 실행마다 1 부터. 같은 seq 를 다시 보내면 무시한다(멱등, 처음 값이 남는다) |
| `at` | 타임존 있는 시각 |
| `step` | `SANDBOX_CREATE`·`INPUT_LOAD`·`CONFIG_DIFF`·`ANALYZE`·`PATCH_PROPOSE`·`PATCH_VERIFY`·`SANDBOX_DELETE` |
| `level` | `INFO`·`WARN`·`ERROR` |
| `message` | 1~200자(DB 칸 기준). 고정 영어 문장 |
| `data` | 선택, 객체. JSON 2,000자까지. 비밀값을 넣지 않는다(저장 전에 한 번 더 가린다) |

`acceptedUpTo` 는 그 실행의 가장 큰 seq 다. 건너뛴 번호를 채우지는 않는다.

### 결과 `POST /{id}/result` — `RunResult`

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

| 규칙 | 내용 |
|---|---|
| 필수 | `status`(`SUCCEEDED`·`FAILED`), `simulated`. 빠지면 422 |
| `failureCode` | `SUCCEEDED` 면 null, `FAILED` 면 필수(`^[A-Z][A-Z0-9_]{0,31}$`) |
| `simulated` | **값을 지어냈는가.** 실제 `inputSnapshot` 으로 계산했으면 모델이 없어도 false, 가짜 모드면 true. WAS 가 덮어쓰지 않는다 |
| `sandbox`·`model` | null 허용. `model` 이 null 이면 응답 `aiReviewed = false`("AI 검토 없음"). `model`: `{id(64자), effort, promptVersion(32자), refusal}` |
| `origin` | `RULE`·`AI` |
| `severity` | `INFO`·`WARN`·`FAIL` |
| finding `result` | `PASS`·`WARN`·`FAIL`·`UNKNOWN`. **`AI` 면 PASS 금지(422)**, `RULE` 의 PASS 는 받는다 |
| `configDiff[].source·target` | 아무 JSON 값(문자열·숫자·배열·null). 값마다 JSON 2,000자까지 |
| 패치 | `{id(유일), patchHash(sha256:…), files[], diff, reason, touchesProtectedFiles, checks[]}`. `status` 를 보내면 모르는 칸(422)이다. WAS 가 다시 계산한다. `checks[]`: `{name, result, toolVersion?, policyVersion?, summary?}`. `result` 가 `PASS` 가 아니면 막는다 |
| 상한 | findings 100 · patches 10 · diff 하나 200KB · configDiff 200 · featureGaps 100 · blockedAttempts 50 · 문자열 2,000자 · 본문 1MB |
| 거부 | 모르는 칸·snake_case 이름·NUL 문자·JSON 아님 → 422 `RESULT_INVALID`(`details` 에 칸 경로와 이유만) |
| 저장 | 모든 문자열의 비밀값 모양을 `***` 로 바꾼 뒤 저장한다(`mask_secrets`). `status`·`failureCode`·`simulated` 는 표의 칸으로만 둔다 |

결과를 낸 뒤 실행 토큰은 더 쓰이지 않는다. 임대·마감이 지난 결과는 저장하지 않는다(`TIMED_OUT` 저장 + 409 `RUN_NOT_ACTIVE`).

## 감사

러너 행동은 SYSTEM + `component = ai-runner` 줄이고(새 행위자 종류 없음) 조직은 실행의 조직, 대상은 `agent_run` 이다. heartbeat·events 는 줄을 쓰지 않는다. 경로 → 이름은 `app/audit/actions.py` 의 `RUNNER_ROUTE_ACTIONS` 다.

| 이름 | 언제 | 결과 | `details` |
|---|---|---|---|
| `AGENT_RUN_CLAIMED` | `:claim` 이 실행을 줌 | `SUCCEEDED` | `{runnerName}` |
| `AGENT_RUN_FINISHED` | result 저장 | 상태대로 | `{status, failureCode?, simulated, aiReviewed}` |
| `AGENT_RUN_TIMED_OUT` | 부를 때 만료 | `FAILED` | `{failureCode}` |

## 러너 토큰 발급

병합·배포 뒤 WAS 파드에서 한 번 돌린다(운영 실행 절차는 [README.md](../README.md#ai-러너-토큰)).

```bash
python -m app.scripts.issue_runner_token --name echo-sandbox-runner   # 원문을 표준 출력에 한 번
python -m app.scripts.issue_runner_token --revoke echo-sandbox-runner # 철회(바로 401)
```

같은 이름은 철회 뒤에도 다시 쓸 수 없다. 교체할 때는 새 이름으로 발급한다.

## 계약(v1)과 다른 점

| 항목 | 계약 v1 | 이 구현(SPEC) |
|---|---|---|
| `simulated` | `RunResult` 에 칸 없음, "실제 샌드박스·실제 모델이면 false" | 필수 칸 `simulated: bool`, 뜻은 "값을 지어냈는가". `model: null` 허용, 응답 `aiReviewed` |
| `CLAIMED → RUNNING` | 만드는 호출이 없음 | 첫 하트비트·이벤트. 결과는 `CLAIMED` 에서도 받는다 |
| 이벤트 message | 2KB | 200자(DB 칸) |
| GET 상세 | 계약 4-5 모양 | `sandboxId`·`steps`·`patches[].verification`·`simulated`·`aiReviewed` 추가 |
| `blockedAttempts` 확인 | 감사 줄과 맞춰 `auditLogId` | 2차. 늘 `auditLogId: null`, `confirmed: false` |
| 패치 승인·`APPLY_PATCH` | 있음 | 2차 |
