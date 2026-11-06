# AI 사전 점검 API — 만들기 · 조회 · 이벤트 · 취소 (AI 러너 레인 A)

**AI 사전 점검**은 이전 대상(서비스 → 도착 환경)을 샌드박스의 러너가 점검하는 실행 한 번이다. 사람이 이 API 로 만들면 러너가 가져가 결과를 내고([docs/agent-runner.md](../agent-runner.md)), `SUCCEEDED` + `simulated = false` 로 끝난 실행은 24시간 동안 보안 게이트의 **C-AI-1 증거**가 된다. 근거: SPEC [specs/api-runner-prechecks.md](../../specs/api-runner-prechecks.md), echo-ai-agent `specs/ai-1-appendix-a-was-runner-contract.md` v1(계약과 다르면 SPEC 이 이긴다).

이전 계획의 단계 0 "사전 확인"(`PREFLIGHT`)과 다르다. 그것은 이전을 실행할 때 일꾼이 하는 헬스·이미지 확인이다.

모든 경로는 `/api/v1/orgs/{orgId}` 아래, 응답은 공통 봉투 `ApiResponse`(성공 `data`, 실패 `code`·`message`·`details`), 본문·응답은 camelCase 다.

## 엔드포인트

| 메서드 · 경로 | 성공 | 주요 실패 | 역할 |
|---|---|---|---|
| `POST /prechecks` `{serviceId, targetEnvironmentId}` | 201 `PrecheckDetail`(`QUEUED`) | 404 `SERVICE_NOT_FOUND`·`ENVIRONMENT_NOT_FOUND`, 409 `SERVICE_DELETING`·`SOURCE_NOT_SINGLE`·`PRECHECK_IN_PROGRESS`, 422 `VALIDATION_ERROR` | 개발자(사람) |
| `GET /prechecks/{id}` | 200 `PrecheckDetail` | 404 `PRECHECK_NOT_FOUND` | 뷰어 |
| `GET /prechecks/{id}/logs?after=&size=` | 200 `{items, nextAfter}` | 404 `PRECHECK_NOT_FOUND`, 422 | 뷰어 |
| `POST /prechecks/{id}/cancel` | 200 `PrecheckDetail`(`CANCELLED`) | 404, 409 `RUN_NOT_ACTIVE` | 개발자(사람) |
| `POST /prechecks/{id}/patches/{patchId}/approve` `{patchHash, reason?}` | 200 `PrecheckPatch` | [패치 승인·거절](#패치-승인거절) | 개발자(사람) |
| `POST /prechecks/{id}/patches/{patchId}/reject` `{patchHash, reason?}` | 200 `PrecheckPatch` | [패치 승인·거절](#패치-승인거절) | 개발자(사람) |

- 에이전트 토큰은 조회만 한다(쓰기 403 `AGENT_READ_ONLY`). 다른 조직의 실행과 `PRECHECK` 가 아닌 실행은 404 다.
- 목록(`GET /prechecks`)은 없다. 화면은 만들 때 받은 id 로 부른다. 최근 실행과 레지던시 기록은 조직 요약 [ai-residency.md](ai-residency.md) 의 `recentRuns` 로 본다.
- 러너는 반영 실행(`APPLY_PATCH`)을 `:claim` 으로 가져가고 `ApplyPatchResult` 로 결과를 낸다([agent-runner.md](../agent-runner.md#반영-실행-apply_patch), SPEC [api-10-2](../../specs/api-10-2-apply-patch-run.md)). 반영 성공 뒤 새 배포로 잇기는 없다(사람이 고객 저장소에 커밋).
- 코드: `app/routers/precheck_router.py`, `app/services/precheck_service.py`, `app/services/precheck_patch_service.py`.

## 만들기 `POST /prechecks`

거절 순서: 서비스(404) → 환경(404) → 삭제 중(409 `SERVICE_DELETING`) → 출발(409 `SOURCE_NOT_SINGLE`) → 같은 환경(422) → 열린 실행(409).

| 규칙 | 내용 |
|---|---|
| 출발 환경 | 서버가 정한다. 트래픽 100% 인 연결 하나 + 현재 배포(이전 계획과 같은 규칙). 아니면 409 `SOURCE_NOT_SINGLE` |
| 도착 환경 | 이 조직의 살아 있는 환경. 아직 연결이 없어도 된다(그때 `target.imageDigest` 는 null). 출발과 같으면 422 `VALIDATION_ERROR`, `details: [{field: "targetEnvironmentId", reason: "SAME_ENVIRONMENT"}]` |
| 서비스당 하나 | 열린 실행(`QUEUED`·`CLAIMED`·`RUNNING`)이 있으면 409 `PRECHECK_IN_PROGRESS`, `details: [{field: "runId", reason: "PRECHECK_IN_PROGRESS", value: "<실행 id>"}]`. 그 실행의 임대·마감이 지났으면 먼저 `TIMED_OUT` 으로 바꾸고 새 실행을 만든다 |
| 마감 | 만든 시각 + 15분(코드 상수). 자동 재시도 없음 |

### 입력 사본 `inputSnapshot` (v1)

러너에게 주는 값이고 사람 쪽 응답에는 해시(`inputHash`)만 싣는다. 만들 때 고정한다.

```json
{
  "schemaVersion": 1,
  "service": {"id": "…", "name": "shop-api", "containerPort": 8080, "healthCheckPath": "/health"},
  "source": {"environmentId": "…", "kind": "aws", "config": {"region": "ap-northeast-2", "clusterName": "…"}, "imageDigest": "sha256:…", "variableKeys": ["API_KEY"]},
  "target": {"environmentId": "…", "kind": "gcp", "config": {"…": "…"}, "imageDigest": null, "variableKeys": []},
  "findings": [{"controlId": "C-IMG-1", "result": "PASS", "source": "TRIVY_IMAGE", "environmentId": null, "collectedAt": "…"}]
}
```

- **비밀값이 없다.** `config` 는 환경 API 와 같은 camelCase 값이고 `secretRef` 는 싣지 않는다. 앱 환경 변수는 이름(`variableKeys`)만 싣는다. 모든 문자열은 저장 전에 `mask_secrets` 를 거친다.
- `findings`: 그 서비스·두 환경·두 이미지의 만료 전 실제 증거(`SIMULATED` 제외), 최신순 최대 50건.
- `inputHash` = `sha256:` + 키를 정렬한 JSON 의 해시. 같은 상태면 같은 값이다.
- `scenarioRef` 는 지금 늘 null 이다(저장소 고정 커밋은 2차).

## `PrecheckDetail`

```text
{id, serviceId, sourceEnvironmentId, targetEnvironmentId, status, failureCode,
 simulated, aiReviewed, requestedByUserId,
 createdAt, claimedAt, startedAt, finishedAt, deadlineAt, leaseUntil,
 inputHash, sandboxId,
 steps: [{name, firstAt, lastAt, level}],
 model: {id, effort, promptVersion, refusal} | null,
 processing: {location, sandbox, modelProvider, model, modelRegion, dataClasses[],
              externalTransfer, externalTransferBytes} | null,
 configDiff: [{key, source, target, origin, severity}],
 featureGaps: [{source, target, gap, action, hasReplacement, severity, origin}],
 findings: [{control, environmentId, result, title, reason, origin, suggestedFix, confirmation}],
 patches: [PrecheckPatch],
 blockedAttempts: [{action, target, at, reason, auditLogId, confirmed}]}
```

| 칸 | 뜻 |
|---|---|
| `status` | `QUEUED → CLAIMED → RUNNING → SUCCEEDED·FAILED·TIMED_OUT·CANCELLED`. `GET` 은 쓰지 않아 저장된 값 그대로다. 임대가 지났어도 다음 쓰기 호출 전까지는 옛 상태일 수 있다(`leaseUntil` 로 판단) |
| `failureCode` | `FAILED`·`TIMED_OUT` 일 때. 러너 값(`RUNNER_ERROR`·`DEADLINE` 등) 또는 WAS 값 `LEASE_EXPIRED`·`DEADLINE_EXCEEDED` |
| `simulated` | **결과 값을 지어냈는가.** 실제 입력으로 계산했으면 모델이 없어도 false, 러너의 가짜 모드면 true. true 면 C-AI-1 증거가 아니다(`EVIDENCE_SIMULATED`) |
| `aiReviewed` | 결과의 `model` 이 있는가. false 는 "AI 검토 없음 · 규칙 비교만"이다. `simulated` 와 따로 본다 |
| `processing` | 러너가 밝힌 데이터 레지던시 기록([칸 설명](../agent-runner.md#레지던시-기록-processing)). **null 은 "기록 없음"이다**(밖으로 안 나갔다는 뜻이 아니다). `model` 과 맞추지 않는다. 거절·실패면 `model` 이 null 이어도 `externalTransfer` 가 true 일 수 있다. 조직 요약은 [ai-residency.md](ai-residency.md) |
| `sandboxId` | 결과 `sandbox.id`. 샌드박스 밖에서 돌았으면 null |
| `steps` | 이벤트에서 계산한다. 단계마다 처음·마지막 시각과 가장 나쁜 `level`, 시작 순. 이벤트가 없으면 빈 배열 |
| `findings[].confirmation` | AI finding 은 `UNCONFIRMED`, 규칙 finding 은 null. **`findings` 표에는 넣지 않는다**(매트릭스·게이트 영향 없음, 2차) |
| `patches[]` | [`PrecheckPatch`](#precheckpatch) |
| `blockedAttempts` | 러너가 보낸 시도. 감사 줄과 맞추지 않아 늘 `auditLogId: null`, `confirmed: false`("확인 안 됨")다 |

결과가 없는 상태(`QUEUED`·`CLAIMED`·`RUNNING`·`TIMED_OUT`·`CANCELLED`)면 결과 칸은 빈 배열·null 이다. 단계 이름은 러너 값(`SANDBOX_CREATE`…) 그대로이고 화면 단계와의 대응은 프론트 몫이다.

## `PrecheckPatch`

상세의 `patches[]` 원소이자 승인·거절 응답이다. 화면은 응답으로 그 원소를 바꿔 끼우면 된다.

```text
{id, patchHash, files[], diff, reason, touchesProtectedFiles, checks[],
 origin, status, blockReason, verification: {passed, total},
 approval: {decision, decidedByUserId, decidedAt, reason} | null,
 apply: {runId, status, failureCode, finishedAt, appliedCommitSha, branch, simulated} | null}
```

| 칸 | 뜻 |
|---|---|
| `origin` | `RULE`·`AI`. 러너가 정한 패치의 출처. 저장된 패치에 없으면 `AI` |
| `status` | 저장하지 않고 아래 표로 계산한다. 승인·거절 버튼은 `status === 'PROPOSED'` 일 때 보인다(`BLOCKED` 는 거절만 된다) |
| `blockReason` | 보호 파일(`PROTECTED_FILES_TOUCHED`), 검사 없음(`CHECKS_MISSING`), PASS 아닌 검사(`CHECK_NOT_PASSED`). 러너 값을 받지 않고 결과를 저장할 때 다시 계산한 값이다 |
| `verification` | `checks` 중 PASS 수와 전체 수(저장 안 함) |
| `approval` | 사람의 결정. 없으면 null |
| `apply` | 반영 실행. `status` 는 그 실행의 상태(`QUEUED`→`CLAIMED`→`RUNNING`→`SUCCEEDED`·`FAILED`·`TIMED_OUT`)다. `appliedCommitSha`·`branch` 는 러너 결과(결과 전·실패면 null, 브랜치는 `applied/<반영 실행 id>`), `simulated` 는 러너 가짜 모드면 true(배지). 실행이 없으면 null(스위치 꺼짐·거절). `GET` 은 만료를 쓰지 않아 임대가 지났어도 다음 쓰기 호출 전까지 옛 상태일 수 있다 |

상태 계산(`compute_patch_status`, 위에서부터 처음 맞는 것):

| 결과 JSON 상태 | 결정 | 반영 실행 | `status` |
|---|---|---|---|
| 아무거나 | `REJECTED` | — | `REJECTED` |
| `BLOCKED` | 없음 | — | `BLOCKED` |
| `PROPOSED` | 없음 | — | `PROPOSED` |
| `PROPOSED` | `APPROVED` | 없음(스위치 꺼짐) 또는 `QUEUED`·`CLAIMED`·`RUNNING` | `APPROVED` |
| `PROPOSED` | `APPROVED` | `SUCCEEDED` | `APPLIED` |
| `PROPOSED` | `APPROVED` | `FAILED`·`TIMED_OUT`·`CANCELLED` | `APPLY_FAILED` |

결과 JSON 에는 `PROPOSED`·`BLOCKED` 만 저장한다.

## 패치 승인·거절

`POST /prechecks/{id}/patches/{patchId}/approve`·`reject`, 본문 `{patchHash, reason?}`.

- `patchHash`: 화면이 본 값(`^sha256:[0-9a-f]{64}$`). `reason`: 선택, 1~2,000자. 모르는 칸·형식 오류는 422 `VALIDATION_ERROR`.
- 결정은 패치마다 한 번이고 지우지 않는다(`agent_patch_approvals`). 패치 원문은 부모 실행의 결과 JSON 그대로다.
- 지문: `patchHash` + `checksDigest`(`sha256:` + `checks` 의 정렬 JSON 해시) + `policyVersion`(코드 상수 `PATCH_POLICY_VERSION`).

거절 순서(위에서부터 처음 맞는 것):

| 순서 | 경우 | 응답 |
|---|---|---|
| — | 에이전트 토큰 · 뷰어 | 403 `AGENT_READ_ONLY` · `ROLE_FORBIDDEN` |
| 1 | 실행 없음·다른 조직·`PRECHECK` 아님 | 404 `PRECHECK_NOT_FOUND` |
| 2 | 결과에 그 패치 없음. **결과가 없는 실행(`QUEUED`·`CLAIMED`·`RUNNING`·`TIMED_OUT`·`CANCELLED`)도 여기다** | 404 `PATCH_NOT_FOUND` |
| 3 | 부모 실행이 `SUCCEEDED` 아님. 결과가 있는 실패 실행(`FAILED`)이다 | 409 `RUN_NOT_SUCCEEDED` |
| 4 | 패치가 `BLOCKED`(승인만. 거절은 된다) | 409 `PATCH_BLOCKED`, `details: [{field: "blockReason", reason: "PATCH_BLOCKED", value: "<막힌 이유>"}]` |
| 5 | 이미 결정됨. **같은 결정이면 200**(새 행·감사 줄 없음, 지금 패치 그대로, 뒤쪽 검사도 안 함), 다른 결정이면 409 | 409 `PATCH_ALREADY_DECIDED`, `details: [{field: "decision", reason: "PATCH_ALREADY_DECIDED", value: "<먼저 난 결정>"}]` |
| 6 | `patchHash` 가 저장 값과 다름, 또는 부모 `finishedAt` 이 24시간 넘게 지남 | 409 `PATCH_STALE`, `details: [{field: "patchHash", reason: "PATCH_HASH_MISMATCH"}]` 또는 `[{field: "finishedAt", reason: "PRECHECK_EXPIRED"}]` |
| 7 | 승인자 = 점검을 요청한 사람이고 조직 `allowSelfApproval` 이 꺼짐(승인만) | 403 `SELF_APPROVAL_FORBIDDEN` + 거부 감사 줄 |
| 8 | 스위치 켜짐 + 승인 + 그 서비스에 열린 실행 있음(임대·마감이 지난 실행은 먼저 `TIMED_OUT`) | 409 `PRECHECK_IN_PROGRESS`, `details.runId` |

동시 요청은 부모 실행 행 `FOR UPDATE` 로 줄 선다. 어느 경우든 결정 행과 USER 감사 줄은 하나다. 잠금을 지나쳐 `(run_id, patch_id)` unique 를 어기면 다시 읽어 5번 규칙을 적용한다(반영 실행도 같은 savepoint 라 함께 되돌린다).

### 스위치 `PATCH_APPLY_ENABLED` (기본 꺼짐)

운영은 11/6 켜짐(`deploy/base.yaml` ConfigMap `PATCH_APPLY_ENABLED: "true"`). 코드 기본값(`app/core/config.py`)은 그대로 꺼짐이다.

| | 꺼짐(기본) | 켜짐 |
|---|---|---|
| 승인 | 결정 행만. 패치는 `APPROVED`·`apply: null`("승인됨 · 반영 대기") | 결정 행 + 반영 실행(`APPLY_PATCH`, `QUEUED`, 마감 15분, `parentRunId` = 부모). `apply.runId`·`apply.status` 가 찬다 |
| 서비스 영향 | 없음 | 반영 실행이 열려 있는 동안 그 서비스의 새 사전 점검·다음 승인이 409 `PRECHECK_IN_PROGRESS` |

WAS 는 반영 실행을 `capabilities` 에 `APPLY_PATCH` 를 넣은 러너에게만 준다([agent-runner.md](../agent-runner.md#반영-실행-apply_patch)). **그런 러너가 배포되기 전에는 켜지 않는다** — 그 전에 켜면 실행이 `QUEUED` 로 15분 머문 뒤 `TIMED_OUT` 이 되고(패치는 `APPLY_FAILED`) 그동안 위 409 가 난다. 반영 실행은 `GET /prechecks/{id}` 로 직접 열리지 않는다(404). 입력 사본:

```json
{"schemaVersion": 1, "parentRunId": "<부모 id>",
 "patch": {"id": "p1", "patchHash": "sha256:…", "files": ["…"], "diff": "--- a/…", "origin": "RULE"},
 "repo": "Softbank-Hackathon-2026-Team-Echo/echo-ai-agent", "branch": "applied/<반영 실행 id>"}
```

## 이벤트 `GET /prechecks/{id}/logs`

`after`(기본 0) 뒤의 줄을 `seq` 순으로 `size`(기본 200, 최대 500)개까지 준다. 다음 호출의 `after` 는 `nextAfter`(마지막 줄의 seq, 새 줄이 없으면 받은 `after` 그대로)다.

```json
{"items": [{"seq": 1, "at": "…", "step": "SANDBOX_CREATE", "level": "INFO", "message": "sandbox created", "data": null}], "nextAfter": 1}
```

## 취소 `POST /prechecks/{id}/cancel`

열린 실행을 `CANCELLED` 로 바꾸고 200 을 준다(본문 없음·`{}`). 이미 끝난 실행은 409 `RUN_NOT_ACTIVE` 이다. 임대·마감이 이미 지났으면 `TIMED_OUT` 을 저장하고 409 `RUN_NOT_ACTIVE` 다. 취소 뒤 그 실행의 러너 호출은 모두 409 다.

## 게이트와의 관계

게이트는 `service_id`·도착 `environment_id`·`kind = PRECHECK`·`status = SUCCEEDED`·`finished_at > 지금 − 24시간` 인 가장 늦은 행 하나를 읽어 PASS 증거를 만든다. **결과 JSON 내용은 보지 않는다.** 그래서 경고가 있어도 점검을 끝냈으면 증거다. `SECURITY_GATE_ENABLED` 가 꺼져 있으면(기본) 쓰이지 않는다. 계획을 만들 때 최신 점검을 필수로 하는 일(계약 8절 2번)은 이번에 하지 않는다.

## 감사

`PRECHECK_REQUESTED`·`PRECHECK_CANCELLED` 는 USER 줄(대상 `agent_run`, 이름표 = 서비스 이름, `details: {serviceId, targetEnvironmentId}`)이다. `AGENT_PATCH_APPROVED`(`{patchId, patchHash, applyRunId}`, `applyRunId` 는 null 가능)·`AGENT_PATCH_REJECTED`(`{patchId, patchHash}`)도 USER 줄이고 대상은 부모 실행이다. 멱등 200 은 줄을 쓰지 않고, 사유 원문은 결정 행에만 있다. 러너 쪽 줄은 [audit-logs.md](audit-logs.md) 를 본다.
