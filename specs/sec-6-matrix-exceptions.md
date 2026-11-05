# 보안 6: 보안 매트릭스에 기한부 예외를 반영한다

- 상태: Confirmed (지민 결정 A, 11/5 13:55 — 기본안: 증거 값은 그대로, 게이트 관점 칸을 덧붙인다)
- 기준: echo-was main `65b71d3b11098c84f06736b3d71c6390ad28bf09`(#73)
- 근거: 11/5 게이트 ON 리허설 `gate-on-2026-11-05/after.md`("매트릭스 API는 예외 표를 아직 안 읽음 — `hasException` 늘 false"), 사실 확인 `facts.md`
- 노션 태스크: 없음 — 11/5 게이트 ON 리허설 후속 (보안 6, Project #4)

## 1. 목표

게이트는 ACTIVE 기한부 예외(`security_exceptions`)로 막지 않는데, 보안 매트릭스 API(`GET /api/v1/orgs/{orgId}/security/matrix`, 화면 `/o/{org}/security`의 원천)는 예외를 읽지 않아 같은 통제가 막힘처럼 보인다. 매트릭스가 **게이트와 같은 판정 함수·같은 입력**으로 예외를 반영하고, 칸마다 "게이트가 이것을 막는가"를 따로 보여 준다.

- 증거 상태(`result`)의 뜻은 바꾸지 않는다: 지금처럼 **예외 반영 전 증거 값**이다(지금 화면 값 그대로).
- 게이트 관점은 새 칸 `gateStatus: PASS | BLOCKED | WAIVED`로 덧붙인다. 예외로 풀린 것 = `WAIVED`.
- 이미 있는 `hasException`을 실제로 채우고, 걸린 예외 정보 `exception`을 붙인다.
- 마이그레이션 0, 새 환경 변수 0, 새 의존성 0, 기존 응답 칸 이름·형·뜻 변경 0(추가만).

## 2. 현재 맥락 (기준 main의 실제 코드)

원인은 `app/services/security_matrix_service.py` 한 곳에 세 개다(`facts.md` 1절).

| # | 지금 | 게이트(`app/services/security_gate.py`) |
|---|---|---|
| F1 | `evaluate_control(rule, target, MATRIX_PHASE, findings, (), now)` — 예외를 빈 튜플로 넘김 | `SecurityExceptionRepository.search_for_judgement(org, now - EXCEPTION_MAX_DURATION)` → `_to_exception` → `evaluate_security(..., exceptions, ...)` |
| F2 | 판정 대상 `SecurityScope`에 `was_version` 없음 → WAS 버전 범위 통제(C-IAM-2·C-AUD-1)는 예외도 증거도 범위가 안 맞음 | `SecurityGate(..., settings.was_version)`로 대상 범위에 넣음 |
| F3 | `SecurityTarget.allow_self_approval` 기본 False → 자기 승인 예외 무효 | 조직 행 `organizations.allow_self_approval`을 읽어 넣음 |

리허설의 C-IAM-2(`edfb2c5f-3211-486e-af3d-77e1dab884ba`, wasVersion `2026.11.05`)·C-AUD-1·C-SEC-1은 모두 자기 승인(조직 `allowSelfApproval=true`)이라 F1만 고치면 C-SEC-1만 풀린다. **F1~F3을 함께 고친다.**

예외 판정 규칙은 `app/services/security_rules.py`에 이미 하나다: `evaluate_control` → `_find_valid_exception` → `_is_valid_exception` → `_scope_matches`(668행). 매트릭스도 이미 `evaluate_control`을 부르므로, **새 판정 함수를 만들지 않고 입력(예외·WAS 버전·자기 승인 스위치)만 게이트와 같게 준다.** 이것이 "게이트와 같은 함수" 요구의 구현이다(`_scope_matches`를 따로 부르거나 복사하지 않는다).

C-DB-1·C-DST-1(·C-AI-1·C-IMG-3)은 규칙표에서 `cell=None`·실행(EXECUTION) 전용이라 매트릭스에 줄이 없다. 이 예외 4건은 이번 변경 뒤에도 매트릭스에 나타나지 않는 것이 맞다(새 칸·새 줄을 만들지 않는다). 문서에 한 줄로 적는다.

## 3. 응답 모양 전/후

### 칸 `rows[].cells.{iam|encryption|auditLog|policy}`

| 칸 | 전 | 후 |
|---|---|---|
| `result` | 그 칸 통제 결과의 최악값(예외가 없어 증거 값과 같음) | **같음 — 예외 반영 전 증거 값**의 최악값(순서 FAIL > 필수 UNKNOWN > WARN > 선택 UNKNOWN > PASS, 모두 NA면 NA) |
| `findingCount`·`mode`·`checkedAt`·`controls` | — | 같음 |
| `expiresAt` | 쓴 증거의 가장 이른 만료 | 쓴 증거·**적용된 예외**의 가장 이른 만료(규칙표 `evaluate_control`이 이미 이렇게 계산) |
| `hasException` | 늘 false | 칸 통제 중 하나라도 유효한 예외가 적용되면 true |
| `gateStatus` (새) | 없음 | `BLOCKED` / `WAIVED` / `PASS` (4절) |
| `exception` (새) | 없음 | 칸에 적용된 예외 중 **가장 먼저 만료되는 것** 하나, 없으면 null |

### 통제 줄 `controls[]`

| 칸 | 전 | 후 |
|---|---|---|
| `result` | `effective_result`(예외가 없어 증거 값과 같음) | **예외 반영 전 증거 값 `decision.result`** (지금 값과 같음) |
| `reasons` | 판정 사유 | 같은 함수 결과 — 예외가 걸리면 `EXCEPTION_APPLIED`, 만료된 예외만 있으면 `EXCEPTION_EXPIRED`가 붙는다(프론트 라벨 이미 있음) |
| `expiresAt` | 증거 만료 | 증거·적용 예외 중 이른 것(`evaluate_control` 값 그대로) |
| `gateStatus` (새) | 없음 | `BLOCKED` / `WAIVED` / `PASS` |
| `exception` (새) | 없음 | 그 통제에 적용된 예외(`decision.exception_id`의 것), 없으면 null |

### `exception` 객체 (칸·통제 같은 모양)

```json
{
  "id": "edfb2c5f-3211-486e-af3d-77e1dab884ba",
  "controlId": "C-IAM-2",
  "reason": "시연 범위: 증거 수집 전 임시 면제 — 김지민 11/5 게이트 ON 리허설",
  "expiresAt": "2026-11-12T01:21:24Z",
  "approvedBy": {"id": "4b936574-2a6d-4805-a666-8bc0f822bff7", "login": "Saccharine1211"}
}
```

- 요청 모양 `{id, reason, expiresAt, approvedBy}`에 `controlId`를 더했다: 칸 하나에 통제가 여럿이라 어느 통제의 예외인지 화면이 알아야 한다(추가만).
- `approvedBy`는 예외 API와 같은 `UserRefResponse{id, login}`(login = `users.github_login`). 사용자 행을 못 찾으면 null.
- 비밀값·범위 칸 원문(digest 등)은 넣지 않는다. 범위·요청자는 예외 목록 API(`/security/exceptions/{id}`)에서 본다.

### 칸 예시 (리허설 데이터, 후)

```json
"iam": {
  "result": "UNKNOWN", "findingCount": 0, "hasException": true, "gateStatus": "WAIVED",
  "exception": {"id": "edfb2c5f-…", "controlId": "C-IAM-2", "reason": "…", "expiresAt": "2026-11-12T01:21:24Z", "approvedBy": {"id": "4b93…", "login": "Saccharine1211"}},
  "mode": null, "checkedAt": null, "expiresAt": "2026-11-12T01:21:24Z",
  "controls": [
    {"controlId": "C-IAM-1", "result": "UNKNOWN", "isMandatory": false, "reasons": ["EVIDENCE_MISSING"], "gateStatus": "PASS", "exception": null, "…": "…"},
    {"controlId": "C-IAM-2", "result": "UNKNOWN", "isMandatory": true, "reasons": ["EVIDENCE_MISSING", "EXCEPTION_APPLIED"], "gateStatus": "WAIVED", "exception": {"id": "edfb2c5f-…", "…": "…"}, "…": "…"}
  ]
}
```

(사유 코드의 정확한 목록·순서는 `evaluate_control`이 정한 그대로다. 위 예시는 모양 설명용.)

## 4. 판정 규칙

입력은 게이트와 같다: 조직의 유효 증거 + `search_for_judgement(org, now - EXCEPTION_MAX_DURATION)`로 읽은 예외(게이트와 같은 DB 행 → 판정 입력 변환 함수) + 대상 범위에 `settings.was_version` + `allow_self_approval` = 조직 행 값. 판정 시점은 지금처럼 PREPARATION.

### 통제 줄 `gateStatus` (`SecurityControlDecision` 값으로만 정한다)

| 순서 | 조건 | `gateStatus` |
|---|---|---|
| 1 | `decision.is_blocking` (필수 + 증거 FAIL/UNKNOWN + 유효 예외 없음) | `BLOCKED` |
| 2 | `decision.exception_id is not None` (FAIL/UNKNOWN에 유효 예외 적용 — 필수·선택 모두) | `WAIVED` |
| 3 | 그 밖(PASS·WARN·NA, 또는 선택 통제의 FAIL/UNKNOWN 예외 없음) | `PASS` (= 게이트가 이 통제로 막지 않음) |

### 칸 `gateStatus`

| 순서 | 조건 | `gateStatus` |
|---|---|---|
| 1 | 칸 통제 줄 중 하나라도 `BLOCKED` | `BLOCKED` |
| 2 | 아니고 하나라도 `WAIVED` | `WAIVED` |
| 3 | 그 밖(통제가 없거나 모두 NA 포함) | `PASS` |

### 예외 유효성 (규칙표 그대로 — 바꾸지 않음, 확인용)

| 예외 상태 | 적용? | 매트릭스에 보이는 것 |
|---|---|---|
| 승인·미철회·`created_at <= now < expires_at`·통제 같음·`_scope_matches` 범위 맞음·사유 있음·7일 이하 | 예 | `hasException` true, `exception`, `gateStatus` WAIVED, 사유 `EXCEPTION_APPLIED` |
| 자기 승인(요청자 = 승인자) | 조직 `allowSelfApproval`이 true일 때만 | false면 적용 안 됨 |
| `PENDING`(미승인)·`REVOKED` | 아니오 | 저장소 질의가 읽지 않음 |
| 만료(`expires_at <= now`, 최근 7일 안) | 아니오 | 사유 `EXCEPTION_EXPIRED`, `gateStatus` BLOCKED(필수일 때) |
| 범위 다름(다른 환경·다른 WAS 버전·다른 digest), 다른 조직 | 아니오 | 아무것도 안 붙음 |
| WAS 버전 범위 예외인데 서버 `WAS_VERSION` 없음 | 아니오 | 게이트와 같음(범위 불일치) |

- 칸 `result`는 통제들의 **`decision.result`(예외 반영 전)**로 `summarize_cell`과 같은 순서 규칙으로 정한다. `summarize_cell`의 기존 동작(`effective_result` 기준)과 그 시험은 바꾸지 않는다. 구현 방법(예: `dataclasses.replace(d, effective_result=d.result)`로 넘기기, 또는 기본값이 지금 동작인 키워드 인자 추가)은 위임한다.
- 통제 줄의 `title`·`isMandatory`·`findingIds`·`mode`·`checkedAt`·`serviceId`는 지금 그대로.

## 5. 바뀌는 파일

| 파일 | 바뀜 |
|---|---|
| `app/services/security_matrix_service.py` | 같은 읽기 트랜잭션에서 예외(`search_for_judgement`)·조직(`allow_self_approval`)·승인자 사용자(`UserRepository.search_by_ids`) 읽기, 대상 범위에 `was_version`, `evaluate_control`에 예외 전달, 뷰에 `gate_status`·`exception`(칸·통제), 칸 `result`는 증거 값. 모듈 docstring·`MatrixControlView.result` 주석 갱신 |
| `app/services/security_gate.py` | `_to_exception`을 공용으로 옮기거나 공개 이름으로 바꿔 매트릭스와 같이 쓴다(동작 불변). 위치는 위임(순환 import 없게 — 예: `security_exception_service.py` 또는 새 작은 모듈) |
| `app/dependencies.py` | `get_security_matrix_service`가 `settings.was_version`을 넘긴다 |
| `app/schemas/security.py` | `SecurityCellResponse`·`SecurityControlResponse`에 `gate_status`, `exception: SecurityMatrixExceptionResponse | None` 추가(camelCase 별칭은 기존 `ApiModel` 규칙) |
| `app/enums.py` | `SecurityGateStatus(StrEnum)`: `PASS`·`BLOCKED`·`WAIVED` (이름은 위임, 값은 고정) |
| `tests/integration/test_security_matrix_api.py` | 새 시험(6절) |
| `docs/api/security.md` | 응답 예시·칸 설명에 `gateStatus`·`exception`, "기한부 예외 표는 아직 없다" 줄을 판정 규칙(4절 요약)으로 교체, C-DB-1·C-DST-1 등 실행 전용 통제의 예외는 매트릭스에 없다는 한 줄 |
| `docs/api/security-exceptions.md` | 3행 "매트릭스에서 WARN으로 보인다" → 매트릭스는 증거 값 + `gateStatus: WAIVED`로 보인다고 고침, "지금 없는 것" 절 정리 |
| `specs/sec-6-matrix-exceptions.md` | 이 SPEC |

필요하면 `.claude/rules/term-dictionary.md`에 `gateStatus` 값 3개를 더한다(위임). 마이그레이션·`db-schema.sql`·`alembic/versions` 변경 없음.

## 6. 완료 기준 (AC)

- **AC-1 예외가 칸에 붙는다** — 통합 시험(`DeployHarness`, 시험 안에서 예외 행을 직접 넣음):
  (a) 조직 범위 C-SEC-1 ACTIVE 예외 → 모든 환경 `encryption` 칸 `hasException` true, `exception.id/controlId/reason/expiresAt/approvedBy{id,login}`가 넣은 값과 같고, C-SEC-1 줄 `gateStatus` WAIVED·`reasons`에 `EXCEPTION_APPLIED`·`result`는 예외 없을 때와 같은 증거 값. 서비스가 없는 환경(칸에 다른 필수 통제 C-TLS-1 줄이 없음)의 `encryption` 칸 `gateStatus`는 WAIVED, 서비스가 배포돼 C-TLS-1 증거가 없는 환경은 C-TLS-1 때문에 BLOCKED(칸 규칙 1이 2보다 먼저).
  (b) WAS 버전 범위 C-IAM-2 예외: 서버 `was_version`이 예외 값과 같으면 `iam` 칸 WAIVED·`hasException` true, 다르거나 None이면 BLOCKED·`hasException` false·`exception` null.
  (c) 자기 승인 예외(요청자 = 승인자): 조직 `allow_self_approval` true → WAIVED, false → BLOCKED.
- **AC-2 안 맞는 예외는 무시된다** — 승인 전(PENDING)·철회(REVOKED)·만료·다른 조직·다른 환경 범위(환경 단위 통제, 예 C-IAM-1 예외를 환경 A로 만들면 환경 B 칸에는 안 붙음) 예외는 `hasException` false·`exception` null, 그 통제가 필수면 `gateStatus` BLOCKED·선택이면 PASS(환경 단위 통제는 모두 선택이라 C-IAM-1 예는 PASS + `hasException` false로 확인). 만료 예외는 `reasons`에 `EXCEPTION_EXPIRED`. 같은 칸에 유효 예외가 둘이면 칸 `exception`은 먼저 만료되는 것.
- **AC-3 게이트와 같고 기존 값은 그대로다** — (a) 같은 증거·예외·대상으로 `security_rules.evaluate_security(..., PREPARATION, ...)`를 직접 부른 결과와 매트릭스 통제 줄을 대조하는 시험: `is_blocking` ⇔ `gateStatus == BLOCKED`, `exception_id` ⇔ `exception.id`. (b) 예외를 넣어도 칸·통제의 `result`·`findingCount`가 예외 없을 때와 같다. (c) 기존 매트릭스 시험 8개를 고치지 않고 통과(응답 기존 칸 이름·형 불변). (d) 예외가 없는 칸은 `hasException` false·`exception` null·필수 UNKNOWN이 있으면 `gateStatus` BLOCKED.
- **AC-4 품질** — `uv run ruff check .`·`uv run ruff format --check .`·`uv run mypy app` 통과, `TEST_DATABASE_URL`을 준 전체 `uv run pytest -q -rs` 실패 0·건너뜀 0, `uv run alembic heads` 하나, `alembic/versions` 새 파일 0, 문서(5절) 갱신. `parametrize`에 `uuid4()`·시각 금지(xdist 수집 불일치) — 필요하면 `ids=`.

## 7. 위임한 세부사항

- `_to_exception` 공용 위치·이름, `SecurityGateStatus` 이름, 칸 `result` 계산 방법(4절), 시험 도우미 구성(기존 `security_gate_support.add_exception`을 쓰거나 매트릭스 시험 안 작은 도우미), 시험 하네스에서 `was_version`을 주는 방법(설정 덮어쓰기·의존성 override).
- 사용자 읽기는 예외가 있을 때만 하는 등 쿼리 수 최적화.

## 8. 하지 않는 것

- 규칙표·판정 함수(`security_rules.py`의 `evaluate_control`·`_is_valid_exception`·`_scope_matches`·`summarize_cell`) 동작 변경, `POLICY_VERSION` 변경.
- 실행 전용 통제(C-DB-1·C-DST-1·C-AI-1·C-IMG-3)를 매트릭스에 새 줄로 넣기.
- 마이그레이션, 새 환경 변수, 새 의존성, 프론트 저장소 수정, 게이트 동작 변경.

## 9. 프론트 호환

- 프론트는 `cell.result`·`cell.hasException`·`control.result`·`control.reasons`만 쓴다(`SecurityMatrixTable.tsx` 41행, `FindingSheet.tsx` 128·136·173-176행, `lib/api/security.ts` 97행). `result` 뜻·값이 그대로라 칸 색·이슈 수는 안 바뀐다.
- `hasException` true가 되면 근거 시트에 이미 있는 "Exception applied" 문구가, `EXCEPTION_APPLIED` 사유에 "기한부 예외 적용 중" 라벨이 바로 보인다(프론트 변경 없이).
- `gateStatus`·`exception`은 새 칸이라 지금 프론트는 무시한다(OpenAPI 생성 타입, 런타임 검증 없음). WAIVED 배지는 프론트(도영님)가 `schema.d.ts` 재생성 뒤 붙인다.
