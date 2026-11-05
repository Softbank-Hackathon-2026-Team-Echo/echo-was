# AI 데이터 레지던시 요약 API (AI 3)

AI 러너가 실행마다 끝 보고에 적어 낸 레지던시 기록(`agent_runs.processing`, 칸 설명은 [docs/agent-runner.md](../agent-runner.md#레지던시-기록-processing))을 조직 단위로 모아 보여 준다. 외부 전송 건수, **민감 등급 외부 전송 0건 판정**, 기본 정책 위반, 최근 실행 목록을 준다. 근거: SPEC [specs/ai-3-residency.md](../../specs/ai-3-residency.md).

- **러너 신고를 그대로 센다.** WAS 가 바이트를 재거나 값을 다시 계산하지 않는다. 기록을 안 낸 실행은 지어내지 않고 "기록 없음"으로 센다.
- 읽기만 한다. 감사 쓰기 줄이 없다(에이전트 토큰의 성공한 읽기 줄만 다른 조회처럼 남는다).
- 코드: `app/routers/ai_residency_router.py`, `app/services/ai_residency_service.py`(조회), `app/services/ai_residency.py`(순수 계산, `summarize_residency`·`find_violations`·`decide_verdict`).

## GET `/api/v1/orgs/{orgId}/ai/residency?days=7`

| 항목 | 내용 |
|---|---|
| 역할 | 뷰어 이상(에이전트 토큰 가능, `ViewerAccess`) |
| `days` | 정수 1~90, 기본 7. 밖이면 422 `VALIDATION_ERROR` |
| 대상 | 그 조직에서 `finished_at` 이 `[지금 − days, 지금)` 안인 끝난 실행(`SUCCEEDED`·`FAILED`·`TIMED_OUT`·`CANCELLED`, 모든 `kind`). 열린 실행은 세지 않는다 |
| 상한 | 끝난 시각 최신순 5,000건까지 읽는다. 넘으면 `runs.truncated = true` 이고 숫자는 읽은 5,000건 기준이다 |
| 실패 | 401 `UNAUTHENTICATED`, 다른 조직 404 `ORGANIZATION_NOT_FOUND`(모든 조직 경로 공통) |

`TIMED_OUT`·`CANCELLED` 는 끝 보고가 없어 언제나 "기록 없음"이다(나갔는지 모른다). 이 기능 이전의 실행도 같다.

### 응답 예 (지어낸 값)

끝난 실행 4건: 기록 없음 2, 모델 없이 샌드박스 처리 1, Anthropic 직접 호출 1(리전 null, 비밀값 안 보냄 → 조건부 허용이라 위반 없음).

```json
{
  "from": "2026-10-29T05:00:00Z",
  "to": "2026-11-05T05:00:00Z",
  "policy": {
    "source": "DEFAULT",
    "allowedModelRegions": ["asia-northeast3", "ap-northeast-1", "ap-northeast-2"],
    "forbidExternalForClasses": ["SECRETS"],
    "allowedRegionlessProviders": [{"provider": "ANTHROPIC", "condition": "NO_FORBIDDEN_CLASSES"}]
  },
  "runs": {"total": 4, "recorded": 2, "unrecorded": 2, "sandboxed": 2, "externalTransfer": 1, "truncated": false},
  "byProvider": [{"key": "ANTHROPIC", "runs": 1}, {"key": "NONE", "runs": 1}],
  "byModel": [{"key": "claude-opus-5-5", "runs": 1}],
  "byLocation": [{"key": "gcp/asia-northeast3", "runs": 2}],
  "byModelRegion": [{"key": null, "runs": 1}],
  "byDataClass": [{"key": "CONFIG", "runs": 1}, {"key": "FINDINGS", "runs": 1}],
  "externalTransferBytes": 18432,
  "sensitiveExternalTransfer": {"classes": ["SECRETS"], "runs": 0, "verdict": "ZERO_WITH_UNRECORDED"},
  "violations": [],
  "recentRuns": [
    {"runId": "…", "serviceId": "…", "kind": "PRECHECK", "status": "SUCCEEDED", "finishedAt": "…",
     "simulated": false, "aiReviewed": false,
     "processing": {"location": "gcp/asia-northeast3", "sandbox": true, "modelProvider": "ANTHROPIC", "model": "claude-opus-5-5", "modelRegion": null, "dataClasses": ["CONFIG", "FINDINGS"], "externalTransfer": true, "externalTransferBytes": 18432},
     "violations": [], "regionlessAllowed": true},
    {"runId": "…", "serviceId": "…", "kind": "PRECHECK", "status": "SUCCEEDED", "finishedAt": "…",
     "simulated": false, "aiReviewed": false, "processing": null, "violations": [], "regionlessAllowed": false}
  ]
}
```

### 계산

| 칸 | 계산 |
|---|---|
| `runs.total` | 기간 안 끝난 실행 수 |
| `runs.recorded`·`unrecorded` | `processing` 이 있는·없는 실행 수(합 = `total`) |
| `runs.sandboxed` | 기록 중 `sandbox = true` 수 |
| `runs.externalTransfer` | 기록 중 `externalTransfer = true` 수 |
| `by*` | 기록 있는 실행만. `byModel` 은 `model` 이 null 이 아닌 것만, `byModelRegion` 은 제공자가 `NONE` 이 아닌 것만(null 키 = 리전 모름), `byDataClass` 는 등급마다 그 등급을 넘긴 실행 수. 정렬: `runs` 내림차순 → `key` 오름차순(null 마지막) |
| `externalTransferBytes` | `externalTransfer = true` 인 기록의 바이트 합(null 은 건너뛴다). 해당 없으면 0 |
| `sensitiveExternalTransfer` | `classes` = 정책 금지 등급, `runs` = `externalTransfer = true` 이고 금지 등급을 넘긴 실행 수, `verdict` 는 아래 판정 |
| `violations` | 위반 이유가 하나라도 있는 실행. 끝난 시각 최신순, 전부 |
| `recentRuns` | 끝난 시각 최신순 최대 20개(기록 없는 실행 포함), 실행별 `violations` |
| `recentRuns[].aiReviewed` | `model_id` 가 있는가(사전 점검 상세와 같은 규칙). `processing` 과 따로 본다 |
| `recentRuns[].regionlessAllowed` | 리전 없이(`modelRegion` null) [조건부 허용](#리전-없는-제공자-조건부-허용) 항목으로 리전 규칙을 통과했으면 `true`. 화면의 "해외 모델 사용 · 비밀값은 안 보냄" 표시는 이 값으로 고른다. 기록 없음·리전을 밝힌 실행·조건 불충족은 `false` |

### 민감 등급 판정 `verdict`

| 값 | 뜻 | 조건 |
|---|---|---|
| `ZERO` | 0건 | 금지 등급 외부 전송 0건 + 기록 없는 실행 0건. 끝난 실행이 0건이어도 이 값이다 |
| `ZERO_WITH_UNRECORDED` | 기록에서는 0건이지만 **0건이라고 단정하지 않는다** | 금지 등급 외부 전송 0건 + 기록 없는 실행이 있음. 상한을 넘어(`truncated`) 읽지 못한 실행이 있을 때도 이 값이다 |
| `DETECTED` | 1건 이상 | 금지 등급 외부 전송이 있음 |

### 위반 이유 `reasons`

| 값 | 조건 |
|---|---|
| `MODEL_REGION_NOT_ALLOWED` | 제공자가 `NONE`·`LOCAL` 이 아닌데 `modelRegion` 이 null 이거나 `allowedModelRegions` 밖. 단 `modelRegion` null 이고 `allowedRegionlessProviders` 항목의 조건을 채우면 위반이 아니다 |
| `FORBIDDEN_CLASS_SENT_EXTERNALLY` | `externalTransfer = true` 이고 `dataClasses` 가 `forbidExternalForClasses` 와 겹침 |

한 실행에 이유가 여러 개 붙을 수 있다. 기록이 없는 실행은 판단할 것이 없어 위반이 없다.

## 정책

지금은 서버 코드 상수 `DEFAULT_RESIDENCY_POLICY`(`app/services/ai_residency.py`) 하나이고 응답 `policy.source = "DEFAULT"` 로 밝힌다.

| 칸 | 기본값 | 뜻 |
|---|---|---|
| `allowedModelRegions` | `asia-northeast3`·`ap-northeast-1`·`ap-northeast-2` | 모델 리전 허용 목록. null 은 들어가지 않는다 |
| `forbidExternalForClasses` | `SECRETS` | 밖으로 보내면 안 되는 등급(금지 등급) |
| `allowedRegionlessProviders` | `[{"provider": "ANTHROPIC", "condition": "NO_FORBIDDEN_CLASSES"}]` | 리전을 고를 수 없는 제공자의 조건부 허용(아래) |

### 리전 없는 제공자 조건부 허용

Anthropic API 를 직접 부르면(1P, `api.anthropic.com`) 리전을 고를 수 없어 `modelRegion` 이 null 이다. 이런 제공자는 리전 목록에 null 을 넣지 않고 `allowedRegionlessProviders` 항목으로 따로 허용한다(SPEC AD-12, 결정: [echo-ai-agent #4 댓글](https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-ai-agent/issues/4#issuecomment-6653905864)).

| `condition` | 허용 조건 |
|---|---|
| `NO_FORBIDDEN_CLASSES` | `dataClasses` 가 `forbidExternalForClasses`(지금 `SECRETS`)와 겹치지 않음. `externalTransfer` 와 상관없이 모델에 넘긴 등급으로 본다 |

- 통과하면 그 실행은 리전 위반이 없고 `recentRuns[].regionlessAllowed = true` 다.
- 조건을 못 채우면(SECRETS 를 넘김) `MODEL_REGION_NOT_ALLOWED` 가 그대로 붙고, 밖으로 보냈으면 `FORBIDDEN_CLASS_SENT_EXTERNALLY` 도 붙는다(이유 2개).
- 항목에 없는 제공자(`OPENAI`·`GOOGLE_VERTEX`·`AWS_BEDROCK`) + null 은 지금처럼 위반이다. 리전을 밝혔는데 목록 밖이면 제공자와 상관없이 위반이다.

**후속(이번 범위 밖):** 조직별 정책 저장(organizations 칸·설정 PATCH·감사·화면), 레지던시 위반으로 이전을 막는 보안 게이트·매트릭스 연동.
