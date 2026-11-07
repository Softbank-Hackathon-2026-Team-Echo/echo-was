# 보안·거버넌스 5: 이전 승인·실행 시작 보안 게이트 SPEC

- 상태: Draft (2026-11-04, 김형래)
- 작성일: 2026-11-04
- Depth: Standard (게이트 서비스 1개 + 승인·일꾼 시작 연결 + 원인 코드 CHECK 넓히는 리비전 1개, 기본 꺼짐)
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/29 (보안·거버넌스 5, 담당 hyeongrae-kim)
- 근거: 보안 1 SPEC `specs/sec-1-security-controls.md` AD-3·AD-4·AD-5·§6·§8 / 보안 3 SPEC `specs/sec-3-rule-table.md` §7(#29 게이트) / 보안·AI 기반 스키마 `specs/sec-ai-base-schema.md`(PR #60)와 그 리뷰 / TLS 시험 주소 규칙(11/4 결정, 지민 님 요청: Echo 가 만든 공개 주소만)
- 저장소: `echo-was` · 브랜치 `feat/sec-5-migration-gate` (`feat/sec-ai-base-schema` = PR #60 위에 쌓음, #60 병합 뒤 `main` 으로 바꿈) · PR로 올린다

## 1. 목표

이전 계획을 **승인할 때**와 **일꾼이 실행을 시작할 때** 같은 보안 판정(규칙표 `evaluate_security`, 시점 `EXECUTION`)을 하고, 필수 통제가 FAIL·UNKNOWN 이고 유효한 기한부 예외가 없으면 막는다. 프론트를 거치지 않은 API 호출도 같은 서버 경로라 똑같이 막힌다. 운영 기본값은 **꺼짐**이고, 꺼져 있으면 지금과 동작이 같다.

## 2. 확인된 사실 (11/4)

- 승인(`MigrationPlanService.approve_plan`)은 잠금 트랜잭션 안에서 대기 상태·자기 승인·만료·지문을 차례로 보고 `_approve` 한다. 실패 전환(EXPIRED·STALE)은 안에서 적고 커밋한 뒤 밖에서 409 를 올린다.
- 일꾼 시작(`MigrationPlanProcessor._start`)은 잠금 안에서 임대·서비스·중단·지문을 보고 `APPROVED → RUNNING` 한다. `_fail` 은 트래픽을 바꾸기 전 `FAILED` 로 끝낸다(`rollbackStatus = NOT_NEEDED`).
- 실행 판정은 준비 통제도 함께 본다. 필수 통제: C-IMG-1·C-IMG-2·C-IMG-3·C-TLS-1·C-IAM-2·C-SEC-1·C-AUD-1·C-AI-1·C-DB-1·C-DST-1.
- `migration_plans.error_code` 는 CHECK 가 있다(새 원인 코드 = 리비전). `migration_plan_logs.code`·`findings.reason` 은 CHECK 가 없다.
- TLS 시험 클라이언트(`SocketTlsProbeClient`)는 있지만 주소를 거르지 않고 DNS 를 스스로 찾는다.
- PR #60 리뷰(Saccharine1211, 11/4): ① 철회된 예외를 판정에 넣지 말 것 ② 예외 7일 경계(예외 API 에서) ③ 마감 지난 열린 실행 정리(러너 API 에서) ④ 시연 때 C-AI-1 을 러너로 채울지 예외로 둘지 결정 + 게이트는 꺼진 채 병합 ⑤ AI 사전 점검과 이전 사전 점검 이름 구분.

## 3. 범위

### 3.1 이번에 한다
- 설정 `SECURITY_GATE_ENABLED`(기본 `false`), `WAS_VERSION`(판정 범위의 WAS 버전, 비면 C-IAM-2·C-AUD-1 확인 불가)
- `app/services/security_gate.py`: `SecurityGate.evaluate_plan`, 로그 data·409 만들기, 시험기 만들기(`build_public_tls_checker`)
- `app/clients/tls_probe.py`: `probe(..., address=)`(확인한 IP 로 접속), `PublicHostTlsChecker`·`classify_addresses`·`is_public_host_name`·`SystemHostResolver`
- 정규화: C-IMG-3 `normalize_image_match`, TLS 이유 3개(`TLS_HOST_NOT_PUBLIC`·`TLS_HOST_SUSPICIOUS`·`TLS_HOST_NOT_ALLOWED`)
- 판정용 조회: `SecurityExceptionRepository.search_for_judgement`(승인·미철회), `AgentRunRepository.find_latest_succeeded_precheck`
- 승인·일꾼 시작 연결, 원인 코드 `SECURITY_GATE_BLOCKED`(리비전 `286d9ea982d6`, CHECK 넓힘만), 로그 줄 `SECURITY_GATE_PASSED`·`SECURITY_GATE_BLOCKED`, 409 `SECURITY_GATE_BLOCKED`
- 문서: `docs/api/migration-plans.md`(보안 게이트 절·오류·로그·설정), `docs/schema.md`, 용어 사전(보안 게이트·AI 사전 점검 행, 이전 사전 점검 이름, §3 값), README·`.env.example`·CLAUDE.md
- 시험: 게이트 통합 시험 9개, 리비전 시험 2개, TLS 주소 거르기·확인한 IP 접속 단위 시험, 정규화 시험

### 3.2 다음 PR
- 기한부 예외 요청·승인·철회 API(사람만, 7일 경계 — #60 리뷰 ②), 감사
- 러너 API·AI 사전 점검 만들기 API, 마감 지난 열린 실행 정리(#60 리뷰 ③)
- 전환 직후(`POST_SWITCH`, C-POST-1) 확인과 결과 기록(이슈 완료 기준 4)
- 사전 승인 자동 DR(#30): 사전 승인을 만들 때의 판정·정책 버전 묶기
- "보안 게이트 꺼짐" 표시(AD-4): API 응답 칸 + 화면(김도영 님과)
- TLS 주기 시험 CronJob(매트릭스용, 같은 `PublicHostTlsChecker`)

### 3.3 하지 않는다
- **판정 전용 표**(`decisions`). 판정은 실행 로그 줄 data 로 남긴다(D-9).
- **정책 버전을 지문에 넣기.** 게이트가 승인·시작 때 지금 규칙표로 다시 판정하므로 옛 판정을 재사용하지 않는다. 지문에 넣으면 배포할 때마다 승인 대기 계획이 모두 STALE 이 된다(AD-1).
- 내부 대역 허용 목록·환경별 TLS 예외 설정(11/4 결정)

## 4. 결정

| ID | 결정 | 출처 |
|---|---|---|
| D-1 | **기본 꺼짐.** 꺼져 있으면 게이트를 만들지 않아 판정·네트워크·로그 줄·증거 행이 없다(기존 시험 전부 그대로 통과). API·일꾼에 같은 값을 넣는다 | 보안 1 AD-4, #60 리뷰 ④ |
| D-2 | **승인 상태·필요한 DB 승격 동의·자기 승인 권한은 판정 전에 순서대로 확인해 먼저 거절한다**(TLS·증거 저장 없음). **판정은 잠금 전·트랜잭션 밖**(TLS 시험이 네트워크라서), **기록은 잠금 안에서 지문이 같을 때.** 상태·DB 승격 동의·자기 승인 권한은 판정 뒤 잠금 트랜잭션에서 다시 확인하며 만료·지문 검사도 유지한다. 판정 뒤 바뀐 현재 배포·환경 설정은 지문이 잡는다(STALE 이 먼저) | 보안 3 §7, 잠금 규칙 |
| D-3 | 막힌 **승인**: 계획은 `PENDING_APPROVAL` 그대로, `SECURITY_GATE_BLOCKED` 줄을 남기고 커밋한 뒤 409 `SECURITY_GATE_BLOCKED`(`details` = `{field: 통제 ID, reason: 사유 코드}`). 고친 뒤 다시 승인할 수 있다. 막힌 **시작**: 트래픽을 바꾸지 않고 `FAILED(SECURITY_GATE_BLOCKED)`, `rollbackStatus = NOT_NEEDED`, 줄 순서 `SECURITY_GATE_BLOCKED → FAILED` | 보안 1 §6 |
| D-4 | 판정 직전에 **C-IMG-3**(출발·도착 현재 배포 digest), **C-TLS-1**(대표 주소 + 도착 환경 주소), **C-SEC-1**(출발/도착 변수 키)을 새로 확인해 `findings`(`sourceRef = migration-plan:{id}`)에 남긴다. IMG-3/TLS-1과 SEC-1의 WAS_STATE는 **방금 만든 증거만** 쓴다(일시적인 TLS 시간 초과가 2분 동안 다시 막지 않게) | 보안 1 §5.1 실행 직전 |
| D-5 | TLS 주소 규칙: 공개 도메인 아래 이름만, 443 만, DNS 로 받은 IP 를 확인한 뒤 **그 IP 로 접속**(인증서는 이름으로 검증). 모두 공인 → 시험, 모두 사설 → 접속 안 함 NA, 루프백·링크 로컬·예약·섞임 → 접속 안 함 UNKNOWN, 도메인 밖 → DNS 도 안 찾고 UNKNOWN, DNS 실패 → UNKNOWN. 주소마다 DNS·접속 확인 전체 5초, 게이트도 모든 주소의 검사·정규화 전체에 5초 예산을 적용 | 11/4 결정, 보안 1 AD-5 |
| D-6 | 기한부 예외는 **승인됐고 철회되지 않은 것만** 읽는다(만료된 지 7일 안의 것도 읽어 "예외 만료" 사유를 붙인다). 요청·승인은 `users` FK 라 사람(USER)이다 | #60 리뷰 ① |
| D-7 | C-AI-1 증거 = **동일 조직·서비스·출발·도착 환경**에서 **24시간 미만에 성공한 실제 AI 사전 점검 중 최신 실행**(증거 ID = 실행 ID). 더 최신의 모의 실행이 실제 실행을 가리지 않는다. 실제 실행이 없을 때만 같은 범위의 최신 모의 실행으로 `PRECHECK_MISSING`·`EVIDENCE_SIMULATED` 차단 사유를 유지한다. 출발 환경이 없거나 범위 불일치·만료·실패·다른 실행 종류는 제외한다. PLANNED·EMERGENCY에 같은 규칙을 쓴다. digest/config 추가 바인딩은 별도 설계 | 규칙표, 계약 v1 |
| D-8 | 판정 범위 WAS 버전 = 설정 `WAS_VERSION`. 비면 C-IAM-2·C-AUD-1 은 증거도 예외도 맞지 않아 확인 불가다 | 규칙표 범위 규칙 |
| D-9 | 판정 기록 = 로그 줄 data: `policyVersion`·`phase`·`blocking[{controlId, reasons}]`·`findingIds`·`exceptionIds`·`timedOutControlIds`(예외로 허용된 시간 초과도 포함, 값·원문 없음) | 보안 1 AD-3, C-AUD-1 |
| D-10 | 통제별 읽기·검사·정규화에 규칙표 예산을 적용: C-IMG-1/3·C-SEC-1·C-AI-1 각 1초, C-TLS-1 전체 5초. 초과 시 읽기 취소·트랜잭션 롤백 후 해당 통제 UNKNOWN·CONTROL_TIMEOUT. 일부 결과·옛 PASS를 재사용하지 않으며 유효 예외는 기존 규칙대로 WARN. DB/DST는 현재 저장 증거 조회에만 5초/2초, 실제 검사 연결은 후속 | #90, #87 시간 예산 후속 |
| D-11 | 조직·통제별 증거를 조회하고, 다시 확인하는 IMG-3/TLS-1 및 SEC-1의 WAS_STATE는 저장 증거 조회에서 제외. SEC-1 사람 기록과 다른 범위/출처의 진단 정보는 유지. 행 수를 자르지 않고 기존 worst-of·근거 ID 보존 | #115 완전성 유지, #90 조회 비용 |
| AD-1 | 정책 버전은 지문에 넣지 않는다(§3.3). 사전 승인(#30)에서 다시 본다 | AI-default |

## 5. 켜기 전 준비물 (게이트를 켜면 필수 통제마다 필요한 것)

| 통제 | 통과 조건 | 지금 |
|---|---|---|
| C-IMG-1·C-IMG-2 | 그 digest 의 Trivy·gitleaks 증거(7일) | #53(빌드 검사) 병합 뒤 새 빌드부터 생긴다 |
| C-IMG-3·C-TLS-1 | 게이트가 직접 확인 | 됨 |
| C-SEC-1 | 출발 연결의 앱 환경 변수 키가 도착 연결에 모두 있는가(WAS 상태) 또는 사람 점검 기록 | 게이트가 판정 때 계산한다(1-2 SPEC, C-IMG-3 와 같은 방식) |
| C-IAM-2·C-AUD-1 | `WAS_VERSION` 의 WAS 시험·감사 기록 증거 | 없음 → 수집기(감사 #32) 또는 기한부 예외 |
| C-AI-1 | 24시간 안의 AI 사전 점검 성공 | 러너 API 다음 PR. **시연 방침 결정 필요**(#60 리뷰 ④: 러너를 돌릴지, 기한부 예외로 둘지) |
| C-DB-1·C-DST-1 | 기한부 예외 | 예외 API 다음 PR(보안 1 §5.3 사유·만료) |

예외 API 가 생기기 전에는 켜도 통과할 수 없다. 그래서 이 PR 은 **꺼진 채 병합**하고, 금요일 종단 시험에서 켠다(보안 1 P-d).

## 6. Acceptance criteria

| AC | 확인 | 시험 |
|---|---|---|
| AC-1 | 꺼져 있으면 판정 줄·증거 행이 없고 계획이 그대로 성공한다 | `test_approve_with_gate_off_runs_no_checks_and_writes_no_gate_lines` + 기존 전체 시험 |
| AC-2 | 증거가 없으면 승인 409, 막은 통제 7개와 사유, 계획 대기 유지, 할 일 없음, 판정 줄·새 증거 3건 | `test_approve_without_evidence_returns_409_and_keeps_plan_pending` |
| AC-3 | 증거·예외·사전 점검이 다 있으면 승인·시작 두 번 통과하고 이전이 끝난다 | `test_approve_with_evidence_exceptions_and_precheck_runs_migration` |
| AC-4 | 승인 뒤 예외를 철회하면 시작이 `FAILED(SECURITY_GATE_BLOCKED)`, 트래픽 호출 없음 | `test_start_with_revoked_exception_fails_plan_without_traffic_change` |
| AC-5 | 승인 안 된 예외는 안 쓰고, 만료된 예외는 "예외 만료" 사유 | `test_approve_with_unapproved_or_expired_exception_is_blocked` |
| AC-6 | 가짜 AI 사전 점검은 C-AI-1 을 채우지 않는다 | `test_approve_with_simulated_or_old_precheck_is_blocked_on_ai_1` |
| AC-13 | 실제 성공 뒤 모의 성공이 와도 최신 실제 실행을 승인·시작에서 사용(PLANNED·EMERGENCY). 실제 실행이 만료됐으면 모의 실행으로 통과하지 않음 | `test_later_simulation_does_not_hide_real_precheck_at_approval_and_start`, `test_latest_real_precheck_is_selected`, `test_expired_real_does_not_override_fresh_simulated_diagnostic` |
| AC-14 | 조직·서비스·출발·도착 불일치, 출발 누락, 다른 종류·실패, 24시간 경계 및 만료는 C-AI-1을 채우지 않음 | `test_precheck_outside_scope_or_validity_does_not_satisfy_gate` |
| AC-7 | TLS 주소 규칙(D-5) | `test_approve_with_private_target_host_uses_main_host_only`, `test_approve_with_suspicious_or_failing_host_is_blocked_on_tls_1`, `test_classify_addresses_*`, `test_is_public_host_name_*`, `test_public_host_checker_*`, `test_probe_with_address_connects_to_that_ip_and_verifies_host_name` |
| AC-8 | 일시적인 TLS 실패 뒤 다시 승인하면 새 시험 결과로 통과 | `test_approve_after_transient_tls_failure_uses_fresh_probe` |
| AC-9 | 리비전 왕복, 막힌 계획이 있으면 되돌리기가 분명히 실패 | `test_security_gate_revision_downgrade_narrows_check_and_upgrade_restores`, `test_security_gate_revision_downgrade_with_blocked_plans_fails_clearly` |
| AC-10 | 로컬 `upgrade → downgrade -1 → upgrade → alembic check` | 수동(PR에 결과) |
| AC-11 | 승인 불가 상태·허용되지 않은 자기 승인·다른 조직 요청은 TLS 호출과 새 증거 없이 기존 403/409/404. 자기 승인 거부 감사 유지 | `test_rejected_approval_does_not_probe_or_record_findings`, `test_cross_organization_approval_does_not_evaluate_gate` |
| AC-12 | 자기 승인 허용 시 게이트 유지. 판정 도중 취소·자기 승인 권한 철회는 다시 거절. 동시 승인도 할 일 하나만 생성 | `test_allowed_self_approval_still_evaluates_gate`, `test_approval_rechecks_state_and_permission_after_gate`, `test_concurrent_approvals_enqueue_one_job` |
| AC-15 | #106의 승격 동의 누락 422가 자기 승인 거부 403보다 먼저이며 TLS·증거 저장 없음. 동의 후 권한이 허용된 요청만 게이트 실행 | `test_missing_promote_ack_rejects_before_gate_with_existing_error_order` |
| AC-16 | 느린 이미지 증거·현재 배포·변수 키·PRECHECK·TLS가 UNKNOWN/CONTROL_TIMEOUT이며 늦은 PASS를 버림. 두 배포 조회 및 SEC-1 증거+키 조회는 각각 하나의 예산 공유 | `test_slow_control_becomes_unknown_and_discards_late_pass`, `test_image_reads_share_one_control_budget`, `test_evidence_and_key_reads_share_one_secret_control_budget` |
| AC-17 | 실제 PostgreSQL 쿼리를 취소해도 같은 세션 재사용·새 증거 저장 가능. 승인 409/대기·워커 실패/트래픽 무변경. 유효 예외는 UNKNOWN→WARN이며 시간 초과 ID 유지 | `test_db_timeout_rolls_back_and_can_record_blocking_decision`, `test_approval_timeout_is_409_and_fresh_retry_succeeds`, `test_worker_timeout_blocks_before_traffic_change`, `test_timed_out_control_uses_existing_exception_without_claiming_pass` |
| AC-18 | 호출자 취소는 전파하고 검사 내부의 다른 TimeoutError를 예산 초과로 바꾸지 않음. 과거 실행 증거 1,800건을 재조회하지 않으면서 사람 기록·기존 유효 FAIL은 보존 | `test_external_cancellation_propagates_and_stops_query`, `test_unrelated_timeout_error_is_not_claimed_as_budget_expiry`, `test_gate_skips_old_live_check_evidence_without_hiding_manual_evidence`, 기존 `test_security_finding_completeness.py` |

## 7. 리스크

- **R-a 병합 = 운영 마이그레이션 Job.** CHECK 를 지우고 다시 만드는 동안 `migration_plans` 를 잠깐 잠근다(표가 작다). 실행 중인 이전이 없을 때 병합한다.
- **R-b 롤링 배포 중 옛 파드.** 기본 꺼짐이라 동작이 같고, 새 값은 게이트를 켰을 때만 저장된다.
- **R-c 화면이 모르는 코드.** 새 `errorCode`·로그 코드·409 코드는 게이트를 켰을 때만 나온다. 화면은 `errorMessage`·`message` 고정 문장을 보이면 된다(김도영 님께 확인 요청).
- **R-d 승인 응답 시간.** 통제별 예산을 강제하지만 공통 읽기·기록·잠금까지 포함한 API 전체 제한은 아니다(아래 범위). TLS 두 주소는 동시 검사·전체 5초.
- **R-e 증거 행 증가.** 완료한 실행 검사당 새 증거를 남긴다(정상 판정은 3건, 통제 전체 시간 초과 시 해당 완료 증거 없음). #115로 전역 500건 상한은 제거했고, 게이트는 통제별로 필요한 저장 근거만 읽는다. 같은 통제의 유효 증거·근거 ID 수에 비례하는 비용은 후속.
- **R-f 쌓은 PR.** #60 이 먼저 병합되어야 한다. 병합 뒤 이 PR 의 기준 브랜치를 `main` 으로 바꾸고 head 가 하나인지 다시 확인한다. **(11/4)** #62(앱 환경 변수 표 `48b2126a9c37`)가 먼저 병합돼 head 가 둘이 될 뻔했다. main 을 이 브랜치에 merge 하고 `286d9ea982d6` 의 `down_revision` 을 `48b2126a9c37` 로 옮겼다. **(11/5)** 이어서 #66(감사 로그 확장 `b7d41e6f2a93`)이 병합돼 한 번 더 main 을 merge 하고 `b7d41e6f2a93` 뒤로 옮겼다. 서로 다른 표·칸이라 순서와 상관없다. 게이트가 실행 시작에서 막으면 #66 의 이전 종료 감사 기록(FAILED, `SECURITY_GATE_BLOCKED`)도 남는다.

### 11/7 시간 예산 보완의 경계

- 예산은 통제의 읽기·변환·검사 단위다. 공통 계획/조직/연결/예외 조회, 예산 없는 준비 통제 조회, 판정 결과 저장·상태 잠금까지 포함하는 HTTP 전체 30초 SLA는 아니다. 동기 계산/DB 취소 정리까지 선점할 수는 없으나 늦게 끝난 결과도 채택하지 않는다.
- C-IMG-3 조회가 시간 초과되면 도착 digest도 확인되지 않으므로 이미지 범위 통제는 기존 범위 규칙에 따라 함께 차단될 수 있다.
- 통제 전체 시간 초과 시 새 검사 완료 증거를 만들지 않고 로그에 남긴다. TLS 클라이언트가 개별 주소의 TLS_TIMEOUT을 정상 결과로 반환한 경우에는 기존 UNKNOWN 증거·EVIDENCE_UNKNOWN 경로를 유지한다.
- 규칙표의 선언 예산·판정 기준·TTL·출처/범위를 변경하지 않고 기존 계약을 실행 경로에서 강제하므로 POLICY_VERSION은 유지한다. C-DB/DST 실제 검사와 POST_SWITCH 연결·TLS 재시도는 별도다.
- 통제별 조회는 불필요한 과거 실행 증거를 줄이지만 전체 메모리 상한을 만들지는 않는다. 같은 통제의 유효 증거·근거 ID 수에 비례하는 비용은 남으며, 유효 FAIL/UNKNOWN을 잘라내는 최적화는 허용하지 않는다.
