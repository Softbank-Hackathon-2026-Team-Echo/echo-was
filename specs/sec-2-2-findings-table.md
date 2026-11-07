# 보안·거버넌스 2-2: 보안 증거 표(findings)·기록·판정용 조회 SPEC

- 상태: Draft (2026-11-04, 김형래)
- 작성일: 2026-11-04
- Depth: Standard (새 표 1개 + Alembic 리비전 1개, 기존 표 변경 없음)
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/26 (보안·거버넌스 2, 담당 hyeongrae-kim) — 두 번째 부분
- 근거: 보안·거버넌스 1 SPEC(PR #43) §5.1·§6, 보안·거버넌스 3 SPEC(PR #44) `SecurityFinding`, 보안·거버넌스 2-1 SPEC(PR #45) `NormalizedFinding`
- 저장소: `echo-was` · 브랜치 `feat/sec-2-findings-table` (**#45 위에 쌓임**) · PR로 올린다

## 1. 목표

정규화한 보안 증거와 사람 점검 기록을 `findings` 표에 남기고, 판정 때 조직의 만료 전 증거를 읽어 규칙표에 넘긴다. AI 결과로 PASS를 만들 수 없다는 것과 사람 점검 기록에는 근거가 필요하다는 것을 DB 제약으로도 막는다.

## 2. 확인된 사실 (`main` = `b7c108c`)

- 마지막 리비전은 `a83f2c1d9e47`(이전 계획 종류)이고 head가 하나다.
- 용어 사전 §5 후속 용어에 finding = `findings`(보안 파트)가 있다.
- 시험 DB 정리는 `tests/integration/conftest.py`의 `TABLES` 목록을 TRUNCATE한다. 마이그레이션 시험은 제약·인덱스 기대 목록과 head 리비전 상수를 둔다.
- 모델은 `app.adapters` 밖을 import하지 못한다. 그래서 도메인 타입(`SecurityFinding`)과 모델 사이 변환은 서비스에 둔다.

## 3. 범위

### 3.1 이번에 한다
- `app/models/finding.py` + Alembic 리비전 `eee7a6c8229f`(표 생성만) + `db-schema.sql`
- `app/repositories/finding_repository.py`: `add`, `search_unexpired_by_organization`
- `app/services/security_finding_service.py`: `record`, `record_manual_review`, `search_for_judgement`, 변환 `to_finding_model`·`to_security_finding`
- `docs/schema.md` 표 설명, 용어 사전 값 한 줄, CLAUDE.md 구조

### 3.2 다음 PR
- 수집 실행: 검사 산출물(echo-ai-agent `image-scan`) 읽기 + 주기 TLS 접속 시험 → `record`
- 매트릭스·증거 조회 API(#28), 사람 점검 기록 입력 API

## 4. 결정

| ID | 결정 | 출처 |
|---|---|---|
| D-1 | 표 이름 `findings`, 모델 `Finding`. 결과는 고치지 않고 다시 검사하면 새 행이다. AI 결과의 사람 확인 상태만 바뀔 수 있다 | 용어 사전 §5, #44 AD-1 |
| D-2 | 범위 칸(`service_id`·`environment_id`·`image_digest`·`config_commit`·`was_version`)은 모두 NULL 허용. 통제가 정한 것만 채운다. `organization_id`는 필수이고 조회는 늘 조직 조건을 건다 | #26 완료 기준 2·4 |
| D-3 | 값 CHECK: `control_id`·`result`·`mode`·`source`·`confirmation`. `reason`은 값이 늘 수 있어 CHECK를 걸지 않는다(오류 코드와 같은 방식) | db-migration 규칙 |
| D-4 | 업무 규칙 CHECK: AI 결과는 PASS 불가(`ai_result_not_pass`), AI 결과에만 확인 상태(`ai_confirmation_consistency`), 사람 점검은 점검자·근거 필수(`manual_review_has_basis`), `expires_at > collected_at`, `items`는 배열 | #43 D-7, AD-1 |
| D-5 | AI 결과를 남길 때 확인 상태가 없으면 `UNCONFIRMED`로 남긴다 | #44 D-5 |
| D-6 | 사람 점검 기록은 그 통제가 사람 점검을 받을 때만(`RULES`의 출처에 `MANUAL_REVIEW`), 결과는 PASS·WARN·FAIL만, 근거 링크는 https만 받는다. 아니면 422 `VALIDATION_ERROR`(칸 `controlId`·`result`·`basisUrl`). 만료는 수집 시각 + 통제 유효기간 | #43 AD-1 |
| D-7 | 판정용 조회는 그 조직의 만료 전 비모의 증거를 판정 필드만 500행 단위 커서로 끝까지 읽는다. 결과 총수 제한 없음. 범위·출처·정책 버전 비교와 worst-of는 규칙표가 한다 | #44 D-3 |
| D-8 | 근거 줄(`items`)은 JSON 목록이고 값이 없는 칸은 키로 남기지 않는다. 비밀값 원문은 정규화 단계에서 이미 버렸다 | #45 D-2 |
| AD-1 | 인덱스 `(organization_id, control_id, collected_at)` 하나. 조회 조건(조직·만료)과 #28의 통제별 최신 조회를 함께 받친다 | AI-default |
| AD-2 | FK는 모두 `ON DELETE RESTRICT`(증거가 남은 조직·서비스·환경·사용자 행은 물리 삭제되지 않는다. 서비스·환경은 소프트 삭제라 영향 없음) | AI-default |

## 5. 호환성·배포

- 표를 새로 만들기만 한다. 기존 표·칸·데이터는 바뀌지 않는다. downgrade는 표를 지운다(쌓인 증거도 사라진다).
- 부르는 곳이 아직 없어 운영 동작은 바뀌지 않는다. main 병합 = 운영 마이그레이션 Job 실행이므로 병합 시점은 WAS 담당과 맞춘다.

## 6. Acceptance criteria

| AC | 확인 | 시험 |
|---|---|---|
| AC-1 | 정규화한 증거를 남기고 읽으면 같은 `SecurityFinding`이 나오고, 이유·도구 버전·원문 위치·근거 줄이 남는다 | `test_record_and_search_round_trip_keeps_scope_and_items` |
| AC-2 | 다른 조직의 증거는 조회되지 않고, 만료된 증거는 빠진다 | `test_search_never_returns_other_organization_findings`, `test_search_skips_expired_findings` |
| AC-3 | 저장된 행 어디에도 비밀값 원문·작성자 정보가 없다 | `test_gitleaks_finding_is_stored_without_secret_text` |
| AC-4 | 사람 점검 기록이 근거·만료와 함께 남고, 받지 않는 통제·결과·링크는 422다 | `test_record_manual_review_*` |
| AC-5 | AI 결과는 확인 전 상태로 남고, DB가 AI PASS와 근거 없는 사람 점검을 거부한다 | `test_ai_finding_is_stored_unconfirmed`, `test_database_rejects_*` |
| AC-6 | 남긴 증거로 규칙표가 판정한다(운영 이미지와 같은 경우 C-IMG-1 FAIL로 막힘) | `test_recorded_findings_drive_rule_judgement` |
| AC-7 | 리비전 왕복·`alembic check`·카탈로그 비교가 통과한다 | `test_findings_revision_*`, `test_migrated_schema_matches_expected_catalog` |

## 7. 리스크

- **R-a 수집이 자주 돌면 행이 계속 쌓인다** → 하루 검사 1회 + TLS 1시간 주기면 조직·서비스당 하루 수십 행이다. 보관 정리는 P1(만료 30일 지난 행 정리 작업).
- **R-b 판정 증거 누락(11/7 보완)** → 전역 500건·통제/출처별 배분에서도 같은 출처의 다른 digest/버전 또는 반복 PASS가 유효한 FAIL을 밀어낼 수 있음을 재현했다. 판정 필드만 커서로 끝까지 읽어 원래 worst-of·범위 판정·근거 ID를 보존한다. 원문 items/remediation은 판정 조회에서 읽지 않고 상세 API에서 읽는다. 배치 크기는 전체 메모리 상한이 아니므로 비용은 조직의 유효 실제 증거 수에 비례한다. 규모별 시간 예산·집계 최적화는 후속이며, 최적화해도 유효 FAIL/UNKNOWN과 범위별 PASS를 생략해선 안 된다.
