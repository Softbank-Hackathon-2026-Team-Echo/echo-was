# AI 3: AI 사전 점검 실행의 데이터 레지던시 기록 — SPEC (확정)

- 출처: 킥오프 테마 "AI 리소스·데이터 레지던시"(최종 안내서 10절: "기록에 요청 ID·데이터 등급·보낸 지역을 남기고 '밖으로 나간 민감 데이터 0건'을 보여 줌") · 지민 결정 D(11/5 14:05) · GitHub 이슈 없음(Project #4 카드 없음)
- 저장소 경로: `specs/ai-3-residency.md`
- 상태: **확정(Confirmed)** — 지민 결정 D의 최소판. 아래 AD-n은 AI 기본값(거부권은 PR 리뷰에서)
- 기준: echo-was main `65b71d3b11098c84f06736b3d71c6390ad28bf09`(#73 병합 뒤). Alembic head `250e2535417a`(하나) · echo-ai-agent main `3806f7c`(#14 러프 러너) · 열린 PR echo-ai-agent #15(모델 검토, 형래)
- 갱신(11/5): **AD-12 Anthropic 1P 조건부 허용**(형래·지민 결정, [echo-ai-agent #4 댓글](https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-ai-agent/issues/4#issuecomment-6653905864) 11/5 17:58). 기준 echo-was main `734845f`(#74 뒤), 마이그레이션 없음(서버 상수 정책). AD-10·5-3 예·AC-10 을 함께 고쳤다
- Depth: Standard (러너 결과 칸 1·상세 칸 1·새 GET 1·순수 집계 1, **마이그레이션 1개**)

## 1. 목표 (한 문장)

AI 러너가 실행마다 "어디서 처리했고, 어느 제공자·모델·리전으로, 어떤 종류의 자료가 밖으로 나갔나"를 끝 보고에 적어 내면 WAS가 실행 행에 그대로 저장하고, 실행 상세와 **조직 범위 요약**(외부 전송 건수, 민감 등급 외부 전송 0건 판정, 기본 정책 위반)으로 보여 준다. 기록을 안 낸 실행은 지어내지 않고 "기록 없음"으로 센다.

## 2. 확인된 사실 (기준 main `65b71d3`, 자세한 인용은 archive `ai-residency/facts.md`)

1. 러너의 끝 보고는 `POST /api/v1/runner/runs/{id}/result`(`app/routers/runner_router.py` 65~78행). 본문 `RunResultRequest`(`app/schemas/runner.py`)는 `ApiRequestModel`(`extra="forbid"`)이라 **모르는 칸은 422 `RESULT_INVALID`**. 새 칸은 선택(기본 null)이어야 지금 러너(#14·#15)가 그대로 동작한다.
2. 정규화 `app/services/run_result.py` `normalize_run_result` → `NormalizedRunResult`, 저장 `AgentRunService.submit_result` → `AgentRunRepository.finish(run_id, now, values)`(조건부 UPDATE). 감사 `_record_finished` = `AGENT_RUN_FINISHED` details `{status, simulated, aiReviewed, failureCode?}`. 시험 `tests/integration/test_runner_api.py` 738행이 details를 정확히 비교한다.
3. `agent_runs`(`app/models/agent_run.py`)에 레지던시 칸 없음. `model_id`는 지금 운영에서 항상 null(러프 러너 #14, 실행 06732233·958afc85·0249e537). CHECK `result_only_when_reported`가 새 CHECK의 선례. 덧붙인 칸은 `sort_order=APPENDED_COLUMN_SORT_ORDER`(`organizations.allow_self_approval` 선례).
4. 사람 쪽 사전 점검 경로는 만들기·상세·로그·취소 4개뿐이고 **목록 GET이 없다**(`app/routers/precheck_router.py`). 상세 응답 `PrecheckDetailResponse`(`app/schemas/precheck.py`).
5. 조직 설정 `allowSelfApproval`은 `organizations.allow_self_approval` 전용 칸 + `PATCH /orgs/{org_id}/settings` + 감사 `ORGANIZATION_SETTINGS_CHANGED`. 조직별 정책을 저장하려면 organizations에 칸을 더해야 해 "agent_runs JSONB 칸 1개" 범위를 넘는다.
6. echo-ai-agent #15(열린 PR)는 Anthropic API(`api.anthropic.com`)를 직접 부르고 설정·변수 **이름**·findings·규칙 결과를 보낸다. **모델 실패·거절이면 `model=null`**로 낸다 → `model`이 null이어도 자료는 이미 밖으로 나갔을 수 있다. 그래서 레지던시는 `model` 칸에서 끌어내지 않고 러너가 따로 밝힌다.

## 3. 범위

### 3.1 한다

- `RunResult`에 선택 칸 `processing`(5절 모양) — 검증·저장·감사.
- `agent_runs.processing` JSONB NULL 칸 1개 + CHECK 1개(마이그레이션 1개).
- 사전 점검 상세 `PrecheckDetail.processing`(없으면 null).
- 새 조회 `GET /api/v1/orgs/{org_id}/ai/residency?days=7` — 기간 안에 끝난 실행 집계 + 민감 등급 외부 전송 판정 + 기본 정책 위반 + 최근 실행 목록(`recentRuns`, 사전 점검 목록 GET이 없어 "목록 노출"을 여기서 한다).
- 기본 정책(서버 코드 상수) `allowedModelRegions = ["asia-northeast3","ap-northeast-1","ap-northeast-2"]`, `forbidExternalForClasses = ["SECRETS"]`로 위반 계산.
- 문서: `docs/agent-runner.md`(RunResult 표·감사 표), `docs/api/prechecks.md`(상세 칸), 새 `docs/api/ai-residency.md`, `docs/api/auth.md` 역할 표, `docs/schema.md`, `.claude/rules/db-schema.sql`, CLAUDE.md 문서 표(있으면 한 줄).

### 3.2 하지 않는다 (후속)

- **조직별 정책 저장**(`aiResidencyPolicy`를 조직 설정으로 바꾸기: organizations 칸·PATCH 검증·감사·응답·화면). 어림 1~1.5시간 추가 + organizations 마이그레이션이 필요해 결정 D의 "JSONB 칸 1개" 범위를 넘으므로 후속 PR. 이번에는 응답 `policy.source = "DEFAULT"`로 상수 정책임을 밝힌다.
- claim 응답에 "processing을 채워야 한다" 안내 칸(결정 D: 불필요).
- WAS가 러너 값을 다시 계산·검증(예: 실제 바이트 측정). 러너 신고 그대로 저장한다(simulated와 같은 원칙).
- 사전 점검 목록 GET 경로 새로 만들기, 보안 게이트·매트릭스 연동(레지던시 위반으로 이전을 막기), 러너 쪽 수정(echo-ai-agent는 형래님 몫), 화면(도영님 몫).

## 4. 결정

- D(지민 11/5 14:05): 위 3.1 최소판. 마이그레이션 1(agent_runs JSONB 칸 1개 더하기, downgrade 포함, db-schema.sql 갱신, 시험 DB 왕복). 기존 실행은 `processing: null`, 요약에서 "기록 없음"으로 센다. 감사는 기존 `AGENT_RUN_FINISHED`에 레지던시를 싣는다. 읽기는 조직 구성원.

### 4-1. AI 기본값 (거부 가능)

- AD-1 정책 부분 분리: 조직 설정 저장은 후속(3.2). 위반 계산은 서버 상수 기본 정책으로 이번에 한다(새 칸 없음).
- AD-2 "목록 노출" = 요약 응답 `recentRuns`(끝난 시각 최신순 최대 20개). 사전 점검 목록 GET은 만들지 않는다.
- AD-3 칸 이름 `agent_runs.processing`, CHECK `processing_only_when_reported`: `processing IS NULL OR (status IN ('SUCCEEDED','FAILED') AND jsonb_typeof(processing) = 'object')`(result CHECK와 같은 모양). 인덱스 없음.
- AD-4 `processing`과 `model`은 서로 맞추지 않는다(사실 6: 거절·실패면 `model=null`인데 자료는 나갔다).
- AD-5 감사 `AGENT_RUN_FINISHED` details에 `processing`(저장한 객체 그대로)을 **기록이 있을 때만** 더한다. 없으면 지금 details 그대로(기존 시험 불변).
- AD-6 요약 기간: `days` 1~90, 기본 7, `finished_at`이 `[now−days, now)` 안인 끝난 실행(SUCCEEDED·FAILED·TIMED_OUT·CANCELLED, 모든 kind). TIMED_OUT·CANCELLED는 끝 보고가 없으므로 언제나 "기록 없음"이다(나갔는지 모름 — 지어내지 않음).
- AD-7 집계는 DB에서 필요한 칸만 최대 5,000행(끝난 시각 최신순) 읽어 순수 함수로 계산한다. 넘으면 `truncated: true`.
- AD-8 역할: `ViewerAccess`(조직 VIEWER 이상, 에이전트 토큰 GET 포함 — 다른 조회와 같다). 다른 조직 사람은 403(기존 검사기 그대로).
- AD-9 민감 등급 판정 `verdict`: `ZERO`(기록 있는 실행에서 0건이고 기록 없는 실행도 0건) · `ZERO_WITH_UNRECORDED`(기록 있는 실행에서 0건, 기록 없는 실행이 있음 → "0건"이라고 단정하지 않음) · `DETECTED`(1건 이상). 끝난 실행이 0건이면 `ZERO`.
- AD-10 위반 이유: `MODEL_REGION_NOT_ALLOWED`(제공자가 `NONE`·`LOCAL`이 아닌데 `modelRegion`이 null이거나 허용 목록 밖. **AD-12 조건부 허용을 통과한 실행은 제외**) · `FORBIDDEN_CLASS_SENT_EXTERNALLY`(`externalTransfer=true` ∧ `dataClasses`가 금지 등급과 겹침). 한 실행에 이유 여러 개 가능.
- AD-11 enum 값: `modelProvider` = `NONE`·`LOCAL`·`GOOGLE_VERTEX`·`AWS_BEDROCK`·`OPENAI`·`ANTHROPIC`. `dataClasses` = `CONFIG`·`FINDINGS`·`SOURCE_CODE`·`LOGS`·`SECRETS`(변수 이름·digest·규칙 결과는 CONFIG에 넣는다).

### 4-2. 추가 결정 (11/5 17:58, 배포 뒤)

- AD-12 **Anthropic 1P 조건부 허용**(결정 — 형래·지민, [echo-ai-agent #4 댓글 6653905864](https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-ai-agent/issues/4#issuecomment-6653905864)). 이유: 러너는 Anthropic API(`api.anthropic.com`)를 직접 불러 리전을 고를 수 없어 `modelRegion`이 늘 null 이고, AD-10 그대로면 실행마다 `MODEL_REGION_NOT_ALLOWED`가 찍힌다. Vertex 경유(서울·도쿄)는 형래님이 시간이 남으면 바꾼다.
  - 정책에 칸 `allowedRegionlessProviders`(내부 `ResidencyPolicy.allowed_regionless_providers`)를 더한다. 항목 = `{provider, condition}`. 기본 정책은 `[{"provider": "ANTHROPIC", "condition": "NO_FORBIDDEN_CLASSES"}]` 하나.
  - `NO_FORBIDDEN_CLASSES`(`AiResidencyRegionlessCondition`): `dataClasses`가 정책 금지 등급(`forbidExternalForClasses`, 지금 `SECRETS`)과 겹치지 않을 때만. `externalTransfer`와 상관없이 모델에 넘긴 등급으로 본다.
  - 리전 규칙: 제공자가 `NONE`·`LOCAL`이 아니면 ① `modelRegion`이 `allowedModelRegions` 안이거나 ② `modelRegion`이 null 이고 그 제공자 항목의 조건을 채울 때 통과. **리전 목록에 null 을 넣지 않는다.** 리전을 밝혔는데 목록 밖이면(예 Anthropic `us-east-1`) 지금처럼 위반이다.
  - 조건 불충족(SECRETS를 넘김)이면 지금처럼 `MODEL_REGION_NOT_ALLOWED` + (밖으로 보냈으면) `FORBIDDEN_CLASS_SENT_EXTERNALLY` 둘 다 붙는다. 다른 제공자(`OPENAI`·`GOOGLE_VERTEX`·`AWS_BEDROCK`) + null 은 지금처럼 위반이다.
  - 실행별 표시 `recentRuns[].regionlessAllowed`(bool, 내부 `ResidencyViolation.is_regionless_allowed`): 조건부 허용 항목으로 리전 규칙을 통과했으면 true. 화면 문구 "해외 모델 사용 · 비밀값은 안 보냄"은 이 값으로 고른다(프론트 몫). 정책을 다시 계산하지 않아도 된다.
  - 이름 근거: `allowedRegionlessProviders`는 같은 객체의 `allowedModelRegions`(allowed + 복수 명사)를 따랐다. 조건 값은 용어 사전의 "금지 등급"(`forbidExternalForClasses`·`FORBIDDEN_CLASS_SENT_EXTERNALLY`)과 같은 말로 지어 정책의 금지 등급을 따라가게 했다(조직별 정책 저장 후속에서도 뜻이 같다). 실행 표시는 `aiReviewed`·`truncated`처럼 응답에는 `is` 없이, 내부 bool 은 `is_` 로 쓴다.
  - 감수: 조건 값 `NO_FORBIDDEN_CLASSES`는 "SECRETS 없음"보다 넓은 말이다. 금지 등급이 늘면 조건도 같이 좁아진다(의도).

## 5. API

### 5-1. 러너 결과 `RunResult.processing` (선택, 기본 null)

```json
"processing": {
  "location": "gcp/asia-northeast3",
  "sandbox": true,
  "modelProvider": "NONE",
  "model": null,
  "modelRegion": null,
  "dataClasses": [],
  "externalTransfer": false,
  "externalTransferBytes": null
}
```

| 칸 | 모양 | 규칙 (위반 → 422 `RESULT_INVALID`, 기존과 같이 details에 칸 경로·이유만) |
|---|---|---|
| `location` | 문자열, 필수 | `^[a-z][a-z0-9-]{0,15}/[a-z0-9-]{1,40}$` (`<클라우드·장소>/<리전>`, 예 `gcp/asia-northeast3`, `aws/ap-northeast-1`, `onprem/seoul-dc1`) |
| `sandbox` | bool, 필수 | 샌드박스 안에서 처리했는가 |
| `modelProvider` | enum, 필수 | AD-11 |
| `model` | 문자열·null | `^[A-Za-z0-9._:/@-]{1,64}$`. 제공자 `NONE`이면 null이어야 하고, 그 밖이면 필수(요청한 모델 이름) |
| `modelRegion` | 문자열·null | `^[a-z0-9-]{1,40}$`. `NONE`이면 null이어야 함. 그 밖에서 모르면 null(위반으로 셈) |
| `dataClasses` | enum 배열, 필수 | 모델에 넘긴 자료 종류. 중복 금지, 최대 5. `NONE`이면 빈 배열이어야 함. 저장 때 enum 정의 순서로 정렬 |
| `externalTransfer` | bool, 필수 | 처리 위치(`location`·샌드박스) 밖으로 자료가 나갔는가(모델 API 호출 포함, WAS 호출은 제외). `NONE`이면 false여야 함 |
| `externalTransferBytes` | 정수·null | ≥ 0. `externalTransfer=false`면 null 또는 0 |

- 저장: 검증을 통과한 값(camelCase, `dataClasses` 정렬)을 `agent_runs.processing`에 그대로. `processing`을 안 보내거나 null이면 칸은 null.
- `result` JSON에는 넣지 않는다(칸이 따로 있다).

### 5-2. 상세 `GET /orgs/{orgId}/prechecks/{id}` — 칸 하나 추가

`processing: {location, sandbox, modelProvider, model, modelRegion, dataClasses, externalTransfer, externalTransferBytes} | null` (저장 값 그대로, 없으면 null). 다른 칸 불변.

### 5-3. 조직 요약 `GET /api/v1/orgs/{orgId}/ai/residency?days=7`

- 역할 ViewerAccess. `days` 정수 1~90(밖이면 422 `VALIDATION_ERROR`).
- 응답 예(지어낸 값 — 끝난 실행 4건: 기록 없음 2, 모델 없이 샌드박스 처리 1, Anthropic 호출 1):

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
     "simulated": false, "aiReviewed": false, "processing": {"location": "gcp/asia-northeast3", "sandbox": true, "modelProvider": "ANTHROPIC", "model": "claude-opus-5-5", "modelRegion": null, "dataClasses": ["CONFIG", "FINDINGS"], "externalTransfer": true, "externalTransferBytes": 18432},
     "violations": [], "regionlessAllowed": true},
    {"runId": "…", "serviceId": "…", "kind": "PRECHECK", "status": "SUCCEEDED", "finishedAt": "…",
     "simulated": false, "aiReviewed": false, "processing": null, "violations": [], "regionlessAllowed": false}
  ]
}
```

| 칸 | 계산 |
|---|---|
| `runs.total` | 기간 안 끝난 실행 수(AD-6) |
| `runs.recorded`·`unrecorded` | `processing`이 있는/없는 실행 수(합 = total) |
| `runs.sandboxed` | 기록 중 `sandbox=true` 수 |
| `runs.externalTransfer` | 기록 중 `externalTransfer=true` 수 |
| `by*` | 기록 있는 실행만. `byModel`은 `model`이 null이 아닌 것만, `byModelRegion`은 제공자가 `NONE`이 아닌 것만(null 키 = 리전 모름), `byDataClass`는 등급마다 그 등급을 넘긴 실행 수. 정렬: runs 내림차순 → key 오름차순(null 마지막) |
| `externalTransferBytes` | `externalTransfer=true`인 기록의 바이트 합(null은 0으로 더하지 않고 건너뜀). 해당 없으면 0 |
| `sensitiveExternalTransfer` | `classes` = 정책 금지 등급, `runs` = `externalTransfer=true` ∧ 금지 등급을 넘긴 실행 수, `verdict` AD-9 |
| `violations` | AD-10 이유가 하나라도 있는 실행(끝난 시각 최신순, 전부) |
| `recentRuns` | 끝난 시각 최신순 최대 20개(기록 없는 실행 포함), 실행별 `violations`·`regionlessAllowed`(AD-12) |

- `aiReviewed` = `model_id is not None`(상세와 같은 규칙). 응답은 다른 조회처럼 `ApiResponse{data}`.

## 6. 데이터 — 마이그레이션 1개 (더하기만)

- 구현 전에 `uv run alembic heads`로 head를 다시 읽는다(`250e2535417a` 하나일 것). 새 리비전 `down_revision` = 그 head.
- `agent_runs.processing` JSONB NULL(`JSONB(none_as_null=True)`, `sort_order=APPENDED_COLUMN_SORT_ORDER`) + CHECK `ck_agent_runs_processing_only_when_reported`(AD-3). 기존 칸·CHECK·인덱스는 바꾸지 않는다.
- downgrade: CHECK·칸 DROP만.
- 롤링 배포 호환: 옛 파드는 칸을 모른 채 동작하고, 옛 파드가 받은 `processing` 본문은 422(모르는 칸) → 러너는 이 PR 배포 뒤에 칸을 보낸다(형래님 안내).
- `tests/integration/test_migrations.py`: `HEAD_REVISION`, 새 리비전이 `250e2535417a`를 잇는 사슬, `EXPECTED_CONSTRAINTS` 새 줄, 새 리비전 downgrade→upgrade 왕복. `.claude/rules/db-schema.sql`·`docs/schema.md` 갱신.

## 7. 완료 판정 (AC)

- AC-1 `processing`을 보낸 결과가 200이고 `agent_runs.processing`에 5-1 모양 그대로(정렬 포함) 저장된다. 안 보내거나 null이면 null 저장, 기존 결과 시험 전부 통과.
- AC-2 5-1 표의 규칙 위반(모르는 칸, NONE + model, 제공자 있음 + model 없음, NONE + externalTransfer true, NONE + dataClasses 비지 않음, dataClasses 중복, location 형식, externalTransfer=false + bytes>0, 음수 bytes)마다 422 `RESULT_INVALID`, 실행 상태 불변.
- AC-3 상세 `processing`: 기록 있으면 그 값, 없으면 null.
- AC-4 감사 `AGENT_RUN_FINISHED` details: 기록 있으면 `processing` 키 포함, 없으면 지금과 정확히 같음.
- AC-5 요약: 위 5-3 예와 같은 구성(기록 없음 2 · NONE 1 · ANTHROPIC 리전 null 1)을 시드하면 숫자·판정·위반이 예와 같다. 기간 밖·열린 실행·다른 조직 실행은 세지 않는다. SECRETS 외부 전송 1건이면 `DETECTED` + `FORBIDDEN_CLASS_SENT_EXTERNALLY`. 실행 0건이면 `total 0`·`ZERO`.
- AC-6 순수 집계·위반 함수 단위 시험(AD-9 세 판정, AD-10 두 이유, 정렬, null 키, bytes 합).
- AC-7 권한: 조직 뷰어·에이전트 200, 익명 401, 다른 조직 403. `ROLE_TABLE`·`test_every_route_has_guards` 통과. 감사 쓰기 경로 수 불변(GET만).
- AC-8 마이그레이션 6절: `alembic heads` 하나, 새 리비전 1개, 시험 DB 왕복·`alembic check` 통과, db-schema.sql 일치.
- AC-9 `ruff check`·`ruff format --check`·`mypy app` 통과, 전체 pytest(TEST_DATABASE_URL) 건너뜀 0.
- AC-10 (AD-12) `ANTHROPIC` + `modelRegion` null + SECRETS 없음 → 위반 0·`regionlessAllowed: true` / `ANTHROPIC` + null + SECRETS → `MODEL_REGION_NOT_ALLOWED`·`FORBIDDEN_CLASS_SENT_EXTERNALLY` 2개 / 다른 제공자 + null → `MODEL_REGION_NOT_ALLOWED` / 응답 `policy.allowedRegionlessProviders` 모양·OpenAPI. `alembic heads` 변화 없음.

## 8. 이 PR 밖 (M1·다른 파트)

- 형래님(echo-ai-agent #4): 러너 `build_result`에 `processing` 칸 추가 — 이 PR 배포 뒤. #14 기본값 `{location:"gcp/asia-northeast3", sandbox:false(작업 공간 컨테이너 안 씀) 또는 실제, modelProvider:"NONE", …, externalTransfer:false}`, #15 모델 호출 시 `ANTHROPIC`·`claude-opus-5-5`·`["CONFIG","FINDINGS"]`·`externalTransfer:true`(거절·실패여도 호출했으면 true).
- 도영님(프론트): 상세 배지 + 요약 카드.
- 후속 PR: 조직별 정책 저장(3.2).

## 9. 규모 어림

새 파일 ~6(리비전·라우터·서비스·순수 집계·스키마·문서), 바뀔 파일 ~14, 새 시험 ~30. M5 약 1.5~2시간.

## 10. 위임한 세부사항 (워커가 정하고 보고)

- 새 파일 이름·위치(예 `app/services/ai_residency.py` 순수, `app/services/ai_residency_service.py`, `app/routers/ai_residency_router.py`, `app/schemas/ai_residency.py`), enum 클래스 이름(`app/enums.py` 끝 근처).
- `processing` 검증 모델을 `app/schemas/runner.py`에 둘지 공용 모듈에 둘지(상세·요약 응답이 같은 모양을 재사용).
- 리포지터리 질의 모양(필요 칸만 select, `organization_id` + `finished_at` 범위, 최신순 LIMIT 5,001로 truncated 판정).
- 시험 파일 배치(새 `tests/integration/test_ai_residency_api.py`, 단위 `tests/test_ai_residency.py`, 러너 시험은 기존 `test_runner_api.py`에 추가).
