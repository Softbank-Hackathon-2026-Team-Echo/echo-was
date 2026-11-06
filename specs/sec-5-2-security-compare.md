# 보안 5-2: 이전 전후 보안 비교 조회 API

- 상태: Confirmed (지민 결정 11/5 23:00 — "G 진행, was 영역이니 형래님 통보 없이")
- 기준: echo-was main `7813ecf`(#82)
- 근거: echo-frontend `docs/api-contract.md` 4-7 "이전 전후 비교", `src/lib/api/security.ts`(시뮬레이션 `SECURITY_COMPARE_API_READY=false`, `SecurityCompareRow`), 보안 4·6 매트릭스(`docs/api/security.md`)
- 원래 이슈: echo-was #29(보안·거버넌스 5) — 이 SPEC은 그중 **조회 API 1개**만 한다(아래 8절)

## 1. 목표

SEC Compare 탭과 E01-3(이전 계획 승인) 비교표가 지금 브라우저 시뮬레이션이다. 서버가 두 환경의 보안 매트릭스 칸을 나란히 놓고, 칸마다 **도착 환경이 이전을 막는지**(`blocksMigration`)를 게이트와 같은 판정으로 돌려준다.

- 새 판정을 만들지 않는다. 두 환경의 값은 매트릭스(`SecurityMatrixService`)가 계산한 행 그대로다.
- `blocksMigration`은 규칙표 `evaluate_control`의 `is_blocking`(필수 + FAIL·UNKNOWN(`_BLOCKABLE`) + 유효 예외 없음)에서 온 도착 칸 `gateStatus == BLOCKED`다. 막는 규칙을 다시 쓰지 않는다.
- 마이그레이션 0, 새 환경 변수 0, 새 의존성 0, 기존 응답 변경 0.

## 2. 현재 맥락

| 무엇 | 지금 | 위치 |
|---|---|---|
| 매트릭스 | 조직 환경 × 4칸(`iam`·`encryption`·`auditLog`·`policy`), 칸 `result`(예외 반영 전 증거 값)·`gateStatus`(BLOCKED·WAIVED·PASS)·`hasException`·`exception`·`controls[]` | `app/services/security_matrix_service.py` |
| 막는 규칙 | `evaluate_control` → `is_blocking = rule.is_mandatory and result in _BLOCKABLE and applied_exception is None`. 게이트(`SecurityGate.evaluate_plan` → `evaluate_security`)도 같은 함수 | `app/services/security_rules.py` 579행 |
| 프론트 시뮬레이션 | `rows[{control: 'iam'…, source: {result, findingCount}, target: {result, findingCount}, blocksMigration: target.result === 'FAIL'}]`, 4줄 고정 | echo-frontend `src/lib/api/security.ts` 136-158행 |
| 프론트 판정 칸 | `blocksMigration` → "blocks migration", 한쪽 UNKNOWN → "not collected", 도착 WARN → "warning only", 같으면 "same" | `src/lib/status.ts` `securityCompareNote` |

## 3. API

`GET /api/v1/orgs/{orgId}/security/compare?source={environmentId}&target={environmentId}[&serviceId={serviceId}]`

- 권한: 뷰어 이상(에이전트 토큰 가능) — 매트릭스와 같다.
- `source`·`target`: 필수 UUID. 빠지거나 형식이 틀리면 422 `VALIDATION_ERROR`(스키마).
- `source == target`: 422 `VALIDATION_ERROR`, `details=[{field: "target", reason: "SAME_ENVIRONMENT"}]`(이전 계획·사전 점검과 같은 사유 코드). DB를 읽기 전에 거절한다.
- 두 환경 모두 이 조직의 살아 있는 환경이어야 한다. 아니면(다른 조직·삭제됨·없음) 404 `ENVIRONMENT_NOT_FOUND`.
- `serviceId`(선택): 이 조직의 살아 있는 서비스여야 한다. 아니면 404 `SERVICE_NOT_FOUND`.
- 캐시 없음: 매트릭스에 캐시가 없어서 같은 시각에 두 화면 값이 같도록 맞춘다(외부 호출 없이 DB 읽기만, 9절).

### 응답 (`ApiResponse[SecurityCompareResponse]`)

```json
{
  "policyVersion": "sec-rules-2026.11.04",
  "phase": "PREPARATION",
  "checkedAt": "2026-11-05T14:00:00Z",
  "simulated": false,
  "serviceId": null,
  "source": {"environmentId": "…", "environmentName": "gcp-seoul", "environmentKind": "gcp",
             "services": [{"serviceId": "…", "name": "demo", "imageDigest": "sha256:…"}]},
  "target": {"environmentId": "…", "environmentName": "aws-tokyo", "environmentKind": "aws", "services": []},
  "summary": {"blocking": 1, "worseOnTarget": 1, "same": 2, "better": 1},
  "rows": [
    {"control": "iam", "source": {"result": "UNKNOWN", "findingCount": 0, "gateStatus": "WAIVED", "…": "…"},
     "target": {"result": "UNKNOWN", "findingCount": 0, "gateStatus": "WAIVED", "…": "…"},
     "blocksMigration": false, "blockingControlIds": [], "change": "SAME"},
    {"control": "encryption", "…": "…"},
    {"control": "auditLog", "…": "…"},
    {"control": "policy", "…": "…"}
  ]
}
```

- `rows`: 늘 4줄, 순서 `iam`·`encryption`·`auditLog`·`policy`(매트릭스 칸 순서). `control` = 칸 이름(프론트 `SecurityControl` 값 그대로).
- `source`·`target`(줄 안) = 매트릭스 칸 응답 `SecurityCellResponse` 그대로(`result`·`findingCount`·`hasException`·`gateStatus`·`exception`·`mode`·`checkedAt`·`expiresAt`·`controls[]`). 프론트 `SecurityCell{result, findingCount}`의 상위 집합이다.
- `blocksMigration` = 도착 칸 `gateStatus == BLOCKED`(도착 칸 통제 줄 중 하나라도 `is_blocking`). 출발 칸 값은 보지 않는다.
- `blockingControlIds` = 도착 칸에서 `gateStatus == BLOCKED`인 통제 ID(중복 없음, 칸 통제 순서). `blocksMigration`이 false면 빈 목록.
- `change` = 도착이 출발보다 `WORSE`·`SAME`·`BETTER`. 비교 열쇠는 (`result` 심각도, `gateStatus` 심각도) 순서쌍:
  - `result`: NA < PASS < WARN < UNKNOWN < FAIL(규칙표의 "가장 나쁜 값" 순서와 같다)
  - `gateStatus`: PASS < WAIVED < BLOCKED (`result`가 같을 때만 본다)
- `summary` = 4줄을 센 것. `blocking` = `blocksMigration` true 수, `worseOnTarget`·`same`·`better` = `change` 수(셋의 합은 4).
- `source`·`target`(맨 위) = 매트릭스 행의 환경 머리(`environmentId`·`environmentName`·`environmentKind`·`services[]`). `services`는 그 비교에 셈한 서비스(아래 범위 규칙).
- `simulated` = 늘 false(프론트 `simulated` 배지를 끄는 값). `checkedAt` = 판정 시각(앱 시계). `policyVersion`·`phase`는 매트릭스와 같다(PREPARATION).

### 범위 규칙 (매트릭스와 같다, 범위 매칭 코드는 부르기만 한다)

| `serviceId` | 환경 단위 통제(범위 환경·WAS 버전·없음) | 서비스 단위 통제(범위에 서비스·digest·설정 커밋, C-IMG-1·C-IMG-2·C-CFG-1·C-TLS-1) |
|---|---|---|
| 없음 | 환경마다 한 줄 | 그 환경에 현재 배포가 있는 **모든 서비스**마다 한 줄 → 두 칸은 매트릭스의 그 두 행 칸과 같다 |
| 있음 | 환경마다 한 줄 | **그 서비스의** 현재 배포(지금 도는 digest)로만 한 줄. 그 환경에 배포가 없으면 세지 않는다(매트릭스의 "서비스 없는 환경" 규칙) |

판정 입력은 매트릭스와 같다(유효 증거, 승인·미철회 예외, 서버 `WAS_VERSION`, 조직 `allowSelfApproval`). `evaluate_control`·`_scope_matches`·`summarize_cell`은 고치지 않고 매트릭스 경로로 부른다.

## 4. 바뀌는 파일

| 파일 | 바뀜 |
|---|---|
| `app/services/security_matrix_service.py` | 읽기 묶음을 `_read_judgement`로 빼서 `get_matrix`와 새 `get_rows(organization_id, environment_ids, *, service_id)`가 함께 쓴다. `get_rows`는 환경·서비스를 조직 안에서 `get_live_by_id`로 읽어(404) 그 순서대로 행을 돌려준다. 칸 계산 `_cell_view`·`_evaluate_row`는 그대로 |
| `app/services/security_compare_service.py` (새) | `SecurityCompareService.compare(...)`(같은 환경 422 → `get_rows`) + 순수 함수 `compare_rows(source_row, target_row)`(줄·요약 계산) |
| `app/enums.py` | `SecurityCompareChange(StrEnum)`: `WORSE`·`SAME`·`BETTER` |
| `app/schemas/security.py` | `SecurityCompareResponse`·`SecurityCompareRowResponse`·`SecurityCompareSummaryResponse`·`SecurityCompareEnvironmentResponse` |
| `app/routers/security_router.py` | `GET /compare` (뷰어) |
| `app/dependencies.py` | `get_security_compare_service` |
| `tests/test_security_compare.py` (새) | 단위(6절) |
| `tests/integration/test_security_compare_api.py` (새) | 통합(6절) |
| `tests/integration/test_authorization.py` | 역할 표에 `GET /security/compare` 뷰어 한 줄 |
| `docs/api/security.md` | compare 절 추가, "다음" 줄 정리 |
| `docs/api/auth.md` | 권한 표 매트릭스 줄에 compare |
| `.claude/rules/term-dictionary.md` | `WORSE`·`SAME`·`BETTER`(`SecurityCompareChange`) 한 줄 |
| `CLAUDE.md` | 구조 표 서비스 줄에 SecurityCompareService |

## 5. 프론트 호환 (echo-frontend 변경 범위, 이 PR에서는 프론트를 고치지 않는다)

| 시뮬레이션(지금) | 서버(이 PR) | 프론트가 할 일 |
|---|---|---|
| 봉투 없이 `SecurityCompareRow[]` | `ApiResponse{data: {…, rows}}` | `unwrap(api.GET('/api/v1/orgs/{org_id}/security/compare', {params: {path, query: {source, target}}}))` 뒤 `.rows` |
| `source`/`target` = `{result, findingCount}` | 매트릭스 칸 전체(추가 칸 포함) | 그대로 씀(`SecurityCheckCell`이 `result`·`findingCount`만 읽음) |
| `blocksMigration = target.result === 'FAIL'` | 게이트 규칙(필수 FAIL·UNKNOWN + 예외 없음). 필수 UNKNOWN도 true, WAIVED는 false | 없음(서버 값) |
| 없음 | `summary`·`change`·`blockingControlIds`·`simulated:false`·`checkedAt` | 선택: `simulated` 배지·E01-3 막힌 이유에 `blockingControlIds` |
| 환경 목록에서 이름을 찾음 | 맨 위 `source`·`target`에 이름·종류 | 선택 |

`SECURITY_COMPARE_API_READY`를 켜고 `useSecurityCompare`를 `useQuery`로 바꾸면 표 컴포넌트는 그대로다.

## 6. 완료 기준 (AC)

- **AC-1 단위: 막는 규칙 = 게이트 규칙** — `evaluate_control`로 만든 판정에서 칸을 만들어 `compare_rows`에 넣는다. 도착 칸 통제(필수 C-SEC-1 / 선택 C-IAM-1, C-SEC-1 증거 출처는 11/6 부터 WAS_STATE) × 증거 PASS·WARN·FAIL·UNKNOWN(없음) × 유효 예외 있음·없음: `blocksMigration` ⇔ (필수 ∧ FAIL·UNKNOWN ∧ 예외 없음) ⇔ 그 판정의 `is_blocking`. 예외 있음(WAIVED)은 false. 출발 칸이 막혀도 도착이 막지 않으면 false. `blockingControlIds`는 막은 통제만.
- **AC-2 단위: 요약·변화** — `change` 순서(결과 심각도, 같으면 gateStatus), `summary` 네 값과 합 4, 줄 순서 4칸.
- **AC-3 통합: 두 환경 비교** — (a) `serviceId` 없이: 두 칸이 같은 시각의 매트릭스 행 칸과 같다(`checkedAt` 제외 전체 대조), `simulated` false, 4줄. (b) 출발에 FAIL 이미지(C-IMG-1) 서비스, 도착 서비스 없음 → `policy` 줄 `change` BETTER·`blocksMigration` false. 반대 방향 → WORSE·true·`blockingControlIds`에 C-IMG-1. (c) 도착의 필수 UNKNOWN(C-AUD-1 증거 없음, 11/6 전에는 C-SEC-1) → `auditLog` true, WAS 버전 범위 C-AUD-1 예외를 넣으면 false(WAIVED). (d) `serviceId`: 그 서비스 통제 줄만 세고 다른 서비스 줄은 없다, 모르는 서비스 404 `SERVICE_NOT_FOUND`.
- **AC-4 통합: 거절** — 같은 환경 422 `VALIDATION_ERROR`(`target`/`SAME_ENVIRONMENT`), 쿼리 빠짐 422, 다른 조직 환경·삭제된 환경·없는 id 404 `ENVIRONMENT_NOT_FOUND`(출발·도착 각각), 다른 조직 사람 404 `ORGANIZATION_NOT_FOUND`.
- **AC-5 권한** — 뷰어·에이전트 토큰 200, 역할 표 시험에 경로 추가(`test_role_table_covers_every_api_route` 통과).
- **AC-6 OpenAPI** — `/api/v1/orgs/{org_id}/security/compare` GET, 쿼리 `source`·`target` 필수·`serviceId` 선택, 응답 스키마에 `rows`·`summary`·`simulated`·`blocksMigration`·`change` enum.
- **AC-7 품질** — `uv run ruff check .`·`uv run ruff format --check .`·`uv run mypy app` 통과, 전체 `uv run pytest`(일회용 Postgres) 실패 0·건너뜀 0, `uv run alembic heads` 하나, `alembic/versions` 새 파일 0, 기존 매트릭스 시험 수정 없이 통과.

## 7. 위임한 세부사항

- 서비스 클래스·함수 이름, 시험 도우미 구성, `get_rows` 반환 모양.

## 8. 하지 않는 것 (#29와의 차이)

- #29(보안·거버넌스 5, 담당 김형래)의 게이트 연결·이전 직전·직후 재검사·차단은 이미 `SecurityGate`가 한다. 이 SPEC은 **읽기 API 하나**다.
- 규칙표·판정·범위 매칭(`security_rules.py`) 동작 변경, `POLICY_VERSION` 변경, 매트릭스 칸 계산 변경. C-TLS-1 범위 이슈는 다른 작업이 본다.
- 실행 전용 통제(C-IMG-3·C-DB-1·C-DST-1·C-AI-1) 줄. 준비 시점 비교라 `blocksMigration`이 모두 false여도 게이트(실행 판정)가 이것들이나 실행 직전 C-TLS-1 재확인으로 막을 수 있다(문서에 적는다).
- 캐시, 마이그레이션, 프론트 저장소 수정, 감사 기록(읽기 경로).

## 9. 위험·메모

- `serviceId` 없는 비교는 도착 환경의 다른 서비스 때문에도 막힘이 보일 수 있다(매트릭스와 같은 값). E01-3처럼 서비스가 정해진 화면은 `serviceId`를 주는 것이 게이트에 가깝다.
- 게이트는 도착 연결의 digest로 판정한다. `serviceId`를 준 비교도 도착 환경의 그 서비스 현재 배포 digest를 쓰므로 같다. 도착에 배포가 없으면 서비스 단위 통제를 세지 않는데, 이전 계획은 도착 연결이 준비돼야 만들 수 있어(`TARGET_NOT_READY`) 실제 이전에서는 이 경우가 없다.
