# 보안·거버넌스 3: 공통 보안 규칙표 + 준비·이관 실행 허용 판정 SPEC

- 상태: Draft (2026-11-04, 김형래)
- 작성일: 2026-11-04
- Depth: Standard (새 순수 계산 모듈, DB·API 변경 없음)
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/27 (보안·거버넌스 3, 담당 hyeongrae-kim)
- 근거: 보안·거버넌스 1 SPEC `specs/sec-1-security-controls.md`(PR #43) §4 D-5·D-7·D-8, AD-2·AD-5, §5.1 통제 표, §5.2 매트릭스, §6 판정 규칙, §8 공통 원칙
- 저장소: `echo-was` · 브랜치 `feat/sec-3-rule-table` (`main` = `b7c108c`에서) · PR로 올린다

## 1. 목표

증거(검사 결과를 공통 형태로 바꾼 것)와 기한부 예외를 받아 통제마다 결과를 정하고, 이전 실행을 막을지 판정하는 **버전이 붙은 공통 보안 규칙표**를 만든다. 사람 승인 이전과 사전 승인 자동 DR이 같은 함수·같은 표를 쓴다. 이 모듈은 DB·외부 호출이 없는 순수 계산이고, 수집(#26)·매트릭스 API(#28)·게이트(#29·#30)가 그대로 부른다.

## 2. 확인된 사실 (`main` = `b7c108c`)

- 보안 판정 코드는 아직 없다. 용어 사전 §5 후속 용어 "정책"은 "Rego 파일 하나", "finding"은 `findings` 표(보안 파트)로만 적혀 있다.
- 비슷한 순수 계산 모듈의 선례: `app/services/migration_counting.py`(이전 비율 단계 세기, DB·실행기 호출 없음)와 `tests/test_migration_counting.py`.
- 계획 종류 `MigrationPlanMode`(`PLANNED`·`EMERGENCY`·`FAILBACK`)가 있다. `EMERGENCY`가 사전 승인 자동 DR이다(API 서버 9부터).
- 행위자 종류 `ActorType`은 `USER`·`AGENT` 둘이다.

## 3. 범위

### 3.1 이번에 한다
- `app/enums.py`: 보안 판정 값 8종(§5.3)
- `app/services/security_rules.py`: 규칙표 `RULES`, 정책 버전 `POLICY_VERSION`, 판정 `evaluate_security`·`evaluate_control`, 매트릭스 칸 요약 `summarize_cell`
- `tests/test_security_rules.py`: §8 인수 조건 전부
- 용어 사전: "정책" 정의를 규칙표로 고치고, finding·기한부 예외·보안 판정 행과 §3 값 표를 더한다

### 3.2 하지 않는다
- 증거·예외·판정을 저장하는 표와 Alembic 리비전(#26·#29에서)
- 수집기(Trivy 결과 읽기·TLS 접속 시험)(#26), 매트릭스·compare API(#28), 게이트 호출 위치(#29), 예외 만들기 API·감사(#30·#32)
- Rego(OPA) 정책 엔진은 P1 추가 항목으로 남긴다. 같은 규칙표를 Rego로 옮길 때도 이 시험을 그대로 통과해야 한다

## 4. 결정

| ID | 결정 | 출처 |
|---|---|---|
| D-1 | 규칙표는 코드(`RULES`, 통제 18개)와 시험으로 관리한다. **통제를 끄는 설정·인자를 두지 않는다**(`evaluate_security`는 규칙표를 바꿔 넣는 인자가 없다) | #25 D-8 |
| D-2 | 정책 버전 `sec-rules-YYYY.MM.DD`. 첫 버전 `sec-rules-2026.11.04`. 증거에 그 증거를 결과로 바꾼 버전을 적고, 지금 버전과 다르면 쓰지 않는다(`POLICY_VERSION_MISMATCH`). 규칙이 바뀌면 검사 원문을 다시 정규화한다 | #25 §6.7 |
| D-3 | 증거를 쓰는 조건: 조직 + 통제가 정한 범위 항목이 판정 대상과 같고, 수집 시각에서 통제 유효기간 안이며 증거 자체 만료 전이고, 그 통제가 받는 출처이고, 가짜(`SIMULATED`)가 아니다. 사람 점검 기록은 점검자·근거 링크가 있어야 한다 | #25 §6.1, AD-1 |
| D-4 | 한 통제에 증거가 여럿이면 가장 나쁜 값: FAIL > UNKNOWN > WARN > PASS > NA. 쓸 증거가 없으면 통제의 "없음" 결과(보통 UNKNOWN, C-AI-1은 FAIL + `PRECHECK_MISSING`) | #25 §5.1·§6.1 |
| D-5 | AI 결과(`AI_REVIEW`): PASS·NA는 보태지 않는다. 사람 확인 전(`UNCONFIRMED`)은 WARN, `ESCALATED`는 FAIL, `DISMISSED`는 쓰지 않는다. 규칙 결과와 가장 나쁜 값으로 합치므로 AI가 규칙 결과를 좋게 바꿀 수 없다 | #25 D-7, §6.5·§6.6 |
| D-6 | 필수 통제가 FAIL·UNKNOWN이고 유효한 예외가 없으면 막는다. 선택 통제는 결과를 그대로 보이되 막지 않는다 | #25 §6.2·§6.3 |
| D-7 | 예외가 유효한 조건: 사유가 비어 있지 않음, 요청자·승인자 모두 사람(`USER`), 승인자 있음, 자기 승인은 조직 `allow_self_approval`이 켜졌을 때만, 만든 시각 ≤ 지금 < 만료, 길이 ≤ 7일, 범위 일치. 예외가 걸린 FAIL·UNKNOWN은 `effective_result = WARN` + `EXCEPTION_APPLIED`, 판정 만료는 예외 만료보다 늦지 않다 | #25 §6.4 |
| D-8 | 판정 시점: 준비(`PREPARATION`)는 준비 통제만, 실행(`EXECUTION`)은 준비 + 실행 통제, 전환 직후(`POST_SWITCH`)는 C-POST-1만 본다. 실행 판정에서 C-TLS-1은 2분 안의 접속 시험만 쓴다. 이미지 일치·DB 쓰기·목적지 자격은 실행 때 2분 안의 상태만 쓴다 | #25 D-6, §5.1 |
| D-9 | 계획 종류(`PLANNED`·`EMERGENCY`·`FAILBACK`)는 판정 기록에만 남고 규칙을 바꾸지 않는다. 재난이라는 이유로 UNKNOWN을 PASS로 바꾸거나 유효기간을 늘리지 않는다 | #25 §8 |
| D-10 | 매트릭스 칸 결과: FAIL > 필수 UNKNOWN > WARN(예외 적용 포함) > 선택 UNKNOWN > PASS, NA는 빼고 모두 NA면 NA. 칸의 모드는 가장 나쁜 모드(SIMULATED > CACHE > REAL), 확인 시각은 가장 오래된 것, 만료는 가장 이른 것 | #25 AD-2 |
| AD-1 | 이름은 용어 사전을 따른다: 증거 한 건 = `SecurityFinding`(표 `findings`), 판정 = `SecurityDecision`. 사유 코드는 #25 §6.2의 이름(`EVIDENCE_*`)을 쓴다 | AI-default |
| AD-2 | 통제 ID 값은 문서·화면 표기(`C-IMG-1`) 그대로 저장한다. 내부 코드 UPPER_SNAKE 규칙의 예외로 용어 사전에 적는다 | AI-default(검토 필요) |
| AD-3 | C-IMG-2(시크릿 노출)의 유효기간은 "같은 digest" + 7일로 둔다. 검사 도구 규칙이 바뀌므로 시간 상한을 C-IMG-1과 맞춘다 | AI-default(검토 필요) |
| AD-4 | 플랫폼 통제(C-SEC-1·C-CI-1)는 조직만 범위로 본다. WAS 시험 결과로 보는 통제(C-IAM-2·C-AUD-1)는 WAS 버전이 같아야 한다 | AI-default |
| AD-5 | 미래 시각에 수집된 증거는 만료로 본다(시계 오류를 PASS로 쓰지 않게) | AI-default |

## 5. 계약

### 5.1 입력

| 타입 | 칸 |
|---|---|
| `SecurityTarget` | `scope`(`SecurityScope`), `environment_kind`, `allow_self_approval` |
| `SecurityScope` | `organization_id`(필수), `service_id`, `environment_id`, `image_digest`, `config_commit`, `was_version` |
| `SecurityFinding` | `finding_id`, `control_id`, `result`, `mode`, `source`, `scope`, `collected_at`, `expires_at`, `policy_version`, (사람 점검) `reviewer_user_id`·`basis_url`, (AI) `confirmation` |
| `SecurityException` | `exception_id`, `control_id`, `scope`, `reason`, `requested_by_user_id`·`requested_by_actor_type`, `approved_by_user_id`·`approved_by_actor_type`, `created_at`, `expires_at` |

### 5.2 출력

- `evaluate_security(target, phase, findings, exceptions, now, plan_mode) -> SecurityDecision`
  - `policy_version`, `phase`, `plan_mode`, `decided_at`, `controls`
  - `is_blocked`, `blocking_reasons`(통제 ID + 사유 목록), `finding_ids`, `exception_ids`
- `SecurityControlDecision`: `control_id`, `cell`, `is_mandatory`, `result`(예외 전), `effective_result`(화면 표시), `is_blocking`, `reasons`, `finding_ids`, `exception_id`, `mode`, `checked_at`, `expires_at`
- `summarize_cell(cell, decisions) -> SecurityCellSummary`: `result`, `has_exception`, `mode`, `checked_at`, `expires_at`, `control_ids`

### 5.3 값

용어 사전 §3 "보안 판정 값"에 둔다: `SecurityControlId`, `SecurityResult`, `SecurityFindingMode`, `SecurityFindingSource`, `AiFindingConfirmation`, `SecurityCheckPhase`, `SecurityMatrixCell`, `SecurityReasonCode`.

### 5.4 규칙표 (`RULES`)

| 통제 | 칸 | 필수 | 시점 | 범위 | 받는 출처 | 유효기간 | 실행 예산 |
|---|---|---|---|---|---|---|---|
| C-IMG-1 | policy | 예 | 준비·실행 | digest | TRIVY_IMAGE | 7일 | 1초 |
| C-IMG-2 | policy | 예 | 준비 | digest | GITLEAKS·TRIVY_SECRET | 7일 | — |
| C-IMG-3 | — | 예 | 실행 | 서비스 | WAS_STATE | 2분 | 1초 |
| C-CFG-1 | policy | 아니요 | 준비 | 설정 커밋 | TRIVY_CONFIG | 7일 | — |
| C-TLS-1 | encryption | 예 | 준비·실행 | 서비스·환경 | TLS_PROBE | 준비 1시간 / 실행 2분 | 5초 |
| C-TLS-2 | encryption | 아니요 | 준비 | 환경 | MANUAL_REVIEW | 7일 | — |
| C-NET-1 | policy | 아니요 | 준비 | 환경 | MANUAL_REVIEW·CLOUD_API | 7일 | — |
| C-NET-2 | policy | 아니요 | 준비 | 환경 | MANUAL_REVIEW·KUBERNETES_API | 7일 | — |
| C-IAM-1 | iam | 아니요 | 준비 | 환경 | MANUAL_REVIEW·CLOUD_API | 7일 | — |
| C-IAM-2 | iam | 예 | 준비 | WAS 버전 | WAS_TEST·AUDIT_LOG | 7일 | — |
| C-SEC-1 | encryption | 예 | 준비 | 조직 | GITLEAKS·TRIVY_SECRET·MANUAL_REVIEW | 7일 | — |
| C-AUD-1 | auditLog | 예 | 준비 | WAS 버전 | WAS_TEST·AUDIT_LOG | 7일 | — |
| C-AUD-2 | auditLog | 아니요 | 준비 | 환경 | CLOUD_API (온프레 NA) | 24시간 | — |
| C-CI-1 | policy | 아니요 | 준비 | 조직 | MANUAL_REVIEW·GITHUB_API | 7일 | — |
| C-AI-1 | policy | 예 | 실행 | 서비스·환경 | PRECHECK (없으면 FAIL) | 24시간 | 1초 |
| C-DB-1 | — | 예 | 실행 | 서비스·환경 | DATA_ENGINE | 2분 | 5초 |
| C-DST-1 | — | 예 | 실행 | 서비스·환경 | ENTRYPOINT | 2분 | 2초 |
| C-POST-1 | — | 아니요 | 전환 직후 | 서비스·환경 | ENTRYPOINT·TLS_PROBE | 2분 | 10초 |

- 실행 예산 합계 15초 ≤ 30초(#25 AD-5). 예산을 넘긴 통제를 UNKNOWN으로 만드는 일은 게이트(#29)가 한다.
- 범위의 "조직"은 모든 통제가 늘 비교한다.

## 6. 호환성·배포

- 새 파일 2개와 enum 추가뿐이다. DB·API·설정 변경이 없어 배포 영향이 없다. 아직 부르는 곳이 없다.
- 되돌리기는 이 PR revert다.

## 7. 다른 이슈가 이 모듈을 쓰는 방법

- **#26 수집기:** 검사 원문을 `SecurityFinding`으로 바꿀 때 `POLICY_VERSION`을 적는다. C-IMG-1은 "HIGH 이상 + 수정 버전 있음 → FAIL, 수정 버전 없음·MEDIUM 이하 → WARN"으로 정규화한다.
- **#28 매트릭스:** 환경 행마다 `evaluate_security(..., PREPARATION, ...)` → 칸마다 `summarize_cell`.
- **#29 게이트:** 승인과 일꾼 `_start`에서 `evaluate_security(..., EXECUTION, ...)`. `is_blocked`면 409 `SECURITY_GATE_BLOCKED` + `blocking_reasons`. 판정은 `policy_version`·`finding_ids`·`exception_ids`와 함께 저장하고, 정책 버전을 지문에 넣는다. 100% 전환 뒤 `POST_SWITCH` 한 번.
- **#30 사전 승인 자동 DR:** 사전 승인을 만들 때와 실행 때 같은 함수를 `plan_mode = EMERGENCY`로 부른다.

## 8. Acceptance criteria

| AC | 확인 | 시험 |
|---|---|---|
| AC-1 | 규칙표가 통제 18개를 한 번씩 담고, 칸·필수 여부가 #25 §5.1·§5.2와 같고, 실행 예산 합계가 30초 이하이며, 정책 버전이 날짜 형식이다 | `test_rules_*`, `test_execution_time_budgets_*`, `test_policy_version_*` |
| AC-2 | 모든 증거가 PASS면 막지 않는다 | `test_evaluate_all_passing_finding_is_not_blocked` |
| AC-3 | 필수 통제 FAIL·UNKNOWN(증거 없음·수집기 UNKNOWN)은 기계 판독 사유와 함께 막고, 선택 통제 FAIL은 막지 않는다. 여러 증거는 가장 나쁜 값이다 | `test_evaluate_mandatory_*`, `test_evaluate_missing_*`, `test_evaluate_collector_unknown_*`, `test_evaluate_optional_fail_*`, `test_evaluate_uses_worst_*` |
| AC-4 | 만료·다른 digest·다른 조직·가짜·다른 정책 버전·받지 않는 출처·근거 없는 사람 점검 증거는 PASS가 되지 않는다. 캐시 증거는 유효기간 안에서만 쓰고 모드가 CACHE로 보인다. C-TLS-1은 실행 때 새 시험이 필요하다 | `test_evaluate_unusable_finding_never_passes`, `test_evaluate_manual_review_*`, `test_evaluate_cache_*`, `test_evaluate_tls_*` |
| AC-5 | AI는 PASS를 만들 수 없고, 확인 전에는 WARN으로만, ESCALATED면 FAIL로 막고, DISMISSED는 쓰지 않으며, 규칙 FAIL을 좋게 바꾸지 못한다 | `test_evaluate_ai_*`, `test_evaluate_unconfirmed_*`, `test_evaluate_escalated_*`, `test_evaluate_dismissed_*` |
| AC-6 | 유효한 예외만 막힘을 풀고 WARN으로 보인다. 만료·7일 초과·승인 없음·AGENT·자기 승인(스위치 꺼짐)·빈 사유·다른 범위·다른 통제의 예외는 풀지 않는다. 만료된 예외는 사유로 보인다 | `test_evaluate_valid_exception_*`, `test_evaluate_exception_also_*`, `test_evaluate_invalid_exception_*`, `test_evaluate_expired_exception_*`, `test_evaluate_self_approved_*` |
| AC-7 | 사전 점검이 없거나 다른 환경 것이면 FAIL로 막는다 | `test_evaluate_missing_precheck_*`, `test_evaluate_precheck_for_other_*` |
| AC-8 | 준비 판정은 목적지 자격·DB 쓰기·이미지 일치를 보지 않고, 준비가 끝나도 실행 판정은 목적지 자격 없이 막는다. 원본 연결 불가(UNKNOWN)는 PASS가 되지 않는다 | `test_preparation_*`, `test_ready_environment_*`, `test_source_unreachable_*` |
| AC-9 | 사람 승인(`PLANNED`)과 사전 승인 자동 DR(`EMERGENCY`)의 통제별 판정이 같다 | `test_planned_and_emergency_*` |
| AC-10 | AWS·GCP 권한 이름이 아니라 정규화된 증거로 판정해, 같은 증거면 같은 결과다. 온프레의 C-AUD-2는 NA다 | `test_aws_and_gcp_*`, `test_onprem_*` |
| AC-11 | 전환 직후 판정은 C-POST-1만 보고 막지 않는다 | `test_post_switch_*` |
| AC-12 | 매트릭스 칸 요약이 D-10 순서·모드·시각 규칙을 따른다 | `test_summarize_cell_*` |

## 9. 리스크

- **R-a 수집기가 범위 항목을 빠뜨리면 모든 증거가 `SCOPE_MISMATCH`로 버려져 전부 막힌다** → #26 시험에서 실제 대상 범위로 판정까지 돌려 본다. 막힘 사유에 `SCOPE_MISMATCH`가 보이므로 원인을 바로 안다.
- **R-b 사전 승인 자동 DR도 24시간 안의 사전 점검이 필요하다** → 재난 대비 환경은 사전 점검을 하루 한 번 이상 돌린다(#30에서 일정으로 둔다). 규칙을 재난 때만 느슨하게 하지 않는다.
- **R-c 정책 버전을 올리면 기존 증거를 모두 다시 정규화해야 한다** → 수집기(#26)가 검사 원문(산출물)을 보관하고, 버전이 바뀌면 다시 읽어 새 증거를 만든다.

## 10. 구현 계획으로 위임한 세부사항

- 저장 표 이름(`findings`·`security_exceptions`·`security_decisions` 가칭)과 JSON 모양, 사유 코드의 API 표기, 시점별 범위 항목을 판정 대상에서 채우는 방법.
