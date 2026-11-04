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

- 에이전트 토큰은 조회만 한다(쓰기 403 `AGENT_READ_ONLY`). 다른 조직의 실행과 `PRECHECK` 가 아닌 실행은 404 다.
- 목록(`GET /prechecks`)은 없다. 화면은 만들 때 받은 id 로 부른다.
- 패치 승인·거절과 `APPLY_PATCH` 는 아직 없다(2차).
- 코드: `app/routers/precheck_router.py`, `app/services/precheck_service.py`.

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
 configDiff: [{key, source, target, origin, severity}],
 featureGaps: [{source, target, gap, action, hasReplacement, severity, origin}],
 findings: [{control, environmentId, result, title, reason, origin, suggestedFix, confirmation}],
 patches: [{id, patchHash, files[], diff, reason, touchesProtectedFiles, checks[],
            status, blockReason, verification: {passed, total}}],
 blockedAttempts: [{action, target, at, reason, auditLogId, confirmed}]}
```

| 칸 | 뜻 |
|---|---|
| `status` | `QUEUED → CLAIMED → RUNNING → SUCCEEDED·FAILED·TIMED_OUT·CANCELLED`. `GET` 은 쓰지 않아 저장된 값 그대로다. 임대가 지났어도 다음 쓰기 호출 전까지는 옛 상태일 수 있다(`leaseUntil` 로 판단) |
| `failureCode` | `FAILED`·`TIMED_OUT` 일 때. 러너 값(`RUNNER_ERROR`·`DEADLINE` 등) 또는 WAS 값 `LEASE_EXPIRED`·`DEADLINE_EXCEEDED` |
| `simulated` | **결과 값을 지어냈는가.** 실제 입력으로 계산했으면 모델이 없어도 false, 러너의 가짜 모드면 true. true 면 C-AI-1 증거가 아니다(`EVIDENCE_SIMULATED`) |
| `aiReviewed` | 결과의 `model` 이 있는가. false 는 "AI 검토 없음 · 규칙 비교만"이다. `simulated` 와 따로 본다 |
| `sandboxId` | 결과 `sandbox.id`. 샌드박스 밖에서 돌았으면 null |
| `steps` | 이벤트에서 계산한다. 단계마다 처음·마지막 시각과 가장 나쁜 `level`, 시작 순. 이벤트가 없으면 빈 배열 |
| `findings[].confirmation` | AI finding 은 `UNCONFIRMED`, 규칙 finding 은 null. **`findings` 표에는 넣지 않는다**(매트릭스·게이트 영향 없음, 2차) |
| `patches[].status` | WAS 가 다시 계산한다. 보호 파일(`PROTECTED_FILES_TOUCHED`), 검사 없음(`CHECKS_MISSING`), PASS 아닌 검사(`CHECK_NOT_PASSED`)면 `BLOCKED` + `blockReason`, 그 밖은 `PROPOSED` |
| `patches[].verification` | `checks` 중 PASS 수와 전체 수(저장 안 함) |
| `blockedAttempts` | 러너가 보낸 시도. 감사 줄과 맞추지 않아 늘 `auditLogId: null`, `confirmed: false`("확인 안 됨")다 |

결과가 없는 상태(`QUEUED`·`CLAIMED`·`RUNNING`·`TIMED_OUT`·`CANCELLED`)면 결과 칸은 빈 배열·null 이다. 단계 이름은 러너 값(`SANDBOX_CREATE`…) 그대로이고 화면 단계와의 대응은 프론트 몫이다.

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

`PRECHECK_REQUESTED`·`PRECHECK_CANCELLED` 는 USER 줄(대상 `agent_run`, 이름표 = 서비스 이름, `details: {serviceId, targetEnvironmentId}`)이다. 러너 쪽 줄은 [audit-logs.md](audit-logs.md) 를 본다.
