# 보안·거버넌스 4-1: 환경 × 4칸 보안 매트릭스·증거 조회 API SPEC

- 상태: Draft (2026-11-04, 김형래)
- 작성일: 2026-11-04
- Depth: Standard (새 읽기 API 2개 + 조회 메서드, DB 변경 없음)
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/28 (보안·거버넌스 4) — 이 문서는 첫 부분(매트릭스·증거). 비교 API는 다음 PR
- 근거: 보안·거버넌스 1 SPEC(`specs/sec-1-security-controls.md`) §5.2 매트릭스 4칸 / 3 규칙표(`summarize_cell`) / 2-2 `findings` 표 / 계약 4-7 수정안(김도영 님께 UNKNOWN 추가 알림, 11/4)
- 저장소: `echo-was` · 브랜치 `feat/sec-4-security-matrix-api` (`main` = `31333b8`에서) · PR로 올린다

## 1. 목표

보안 화면(frontend #7)이 쓸 **환경 × 4칸(권한·암호화·감사 기록·정책)** 매트릭스와 칸의 근거(통제·사유·증거)를 서버 판정으로 준다. 확인하지 못한 칸은 PASS가 아니라 UNKNOWN이다.

## 2. 확인된 사실 (11/4)

- 규칙표에는 칸이 있는 준비 시점 통제가 14개 있다. 범위가 서비스·이미지·설정 커밋인 것(C-IMG-1·C-IMG-2·C-CFG-1·C-TLS-1)과 환경·WAS 버전·없음인 것으로 나뉜다.
- 연결(`service_environments`)의 현재 배포에 지금 도는 이미지 digest가 있다. 증거는 digest가 같아야 쓰인다(규칙표 `_scope_matches`).
- WAS 버전 범위 통제(C-IAM-2·C-AUD-1)는 대상에 WAS 버전 값이 없어 지금은 늘 UNKNOWN이다.
- 기한부 예외 표는 아직 없다.

## 3. 범위

### 3.1 이번에 한다
- `GET /api/v1/orgs/{orgId}/security/matrix`, `GET …/security/findings`(뷰어 이상, 에이전트 가능)
- `app/services/security_matrix_service.py`, `app/schemas/security.py`, `app/routers/security_router.py`
- 조회 메서드: `ServiceEnvironmentRepository.search_live_current_by_organization`, `FindingRepository.search_by_organization`·`count_by_organization`(`FindingConditions`)
- 문서 `docs/api/security.md`, 권한 표(`docs/api/auth.md`, 권한 표 시험), CLAUDE.md

### 3.2 다음 PR
- 이전 전후 비교 `GET …/security/compare`(게이트 #29와 같은 실행 시점 판정)
- WAS 버전 값과 그 증거(C-IAM-2·C-AUD-1), TLS 주기 시험(CronJob), 사람 점검 기록 API, 기한부 예외 표

### 3.3 하지 않는다
- 화면(frontend #7, 김도영 님)

## 4. 결정

| ID | 결정 | 출처 |
|---|---|---|
| D-1 | 판정 시점은 **준비(PREPARATION)**. 칸이 없는 통제와 실행 직전에만 보는 통제는 매트릭스에 없다 | #43 §5.2 |
| D-2 | 환경 단위 통제는 환경마다 한 번, 서비스 단위 통제는 그 환경에 **현재 배포가 있는 서비스마다** 그 이미지 digest로 판정한다. 칸 결과는 규칙표 `summarize_cell`(가장 나쁜 값) | #44 |
| D-3 | 서비스가 없는 환경은 서비스 단위 통제를 세지 않는다. 그 칸에 남은 통제가 모두 NA면 NA | AI-default |
| D-4 | 칸마다 `controls[]`로 통제별 결과·사유·증거 id·모드·시각·`serviceId`를 준다(근거 드로어). `findingCount`는 칸 판정에 쓴 증거 수 | 이슈 완료 기준 2 |
| D-5 | 증거 목록은 조직 안, 최신순, 기본 만료 전만. 조건은 환경·서비스·통제·증거 id(최대 100). `origin`은 `RULE`·`HUMAN`·`AI`, 원문은 `evidenceRef`(위치)만 | 계약 4-7 수정안 |
| D-6 | 예외 표가 없어 예외 없이 판정한다(`hasException` 늘 false) | — |
| AD-1 | 환경은 만든 순 최대 200개 | AI-default |

## 5. Acceptance criteria

| AC | 확인 | 시험 |
|---|---|---|
| AC-1 | 환경마다 4칸이 늘 있고 값이 정해진 다섯 가지 중 하나다. 증거가 없으면 UNKNOWN(`EVIDENCE_MISSING`) | `test_matrix_has_four_cells_per_environment_and_no_blanks` |
| AC-2 | 서비스가 없는 환경은 서비스 단위 통제가 없고, 온프레는 클라우드 감사 통제가 NA | `test_environment_without_services_skips_service_controls`, `test_onprem_environment_marks_cloud_audit_control_not_applicable` |
| AC-3 | 배포된 이미지의 증거가 정책 칸에 들어간다(FAIL, 증거 id·모드·시각·`serviceId`) | `test_policy_cell_fails_on_fixable_high_for_deployed_image` |
| AC-4 | 다른 digest의 증거는 쓰지 않는다(`SCOPE_MISMATCH`, UNKNOWN) | `test_evidence_for_other_digest_is_not_used` |
| AC-5 | 증거 목록: 근거 줄·출처·원문 위치, 증거 id 조건, 다른 조직 증거 안 보임 | `test_findings_list_returns_evidence_with_items_and_origin`, `test_findings_list_filters_by_ids_and_hides_other_organizations` |
| AC-6 | 만료 증거는 기본 목록·매트릭스에서 빠지고 `includeExpired`로만 보인다 | `test_findings_list_hides_expired_unless_asked` |
| AC-7 | 권한: 뷰어·에이전트 읽기, 다른 조직 404, 익명 401 | 권한 표 시험(`test_role_table`) |

## 6. 리스크

- **R-a 화면이 지금 계약(PASS·WARN·FAIL·NA)만 안다** → UNKNOWN 추가를 김도영 님께 알렸다(11/4). 타입은 OpenAPI로 다시 뽑는다.
- **R-b IAM·감사 칸이 늘 UNKNOWN으로 보인다** → WAS 버전 증거·사람 점검 기록이 들어오면 채워진다. 그 전까지는 "확인 안 됨"이 맞는 표시다.
- **R-c 환경·서비스가 많아지면 판정 수가 늘어난다** → 시연 규모(환경 3, 서비스 수 개)에서는 문제 없다. 커지면 캐시를 둔다.
