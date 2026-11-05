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
| D-2 | **판정은 잠금 전·트랜잭션 밖**(TLS 시험이 네트워크라서), **기록은 잠금 안에서 지문이 같을 때.** 판정 뒤 바뀐 현재 배포·환경 설정은 지문이 잡는다(STALE 이 먼저) | 보안 3 §7, 잠금 규칙 |
| D-3 | 막힌 **승인**: 계획은 `PENDING_APPROVAL` 그대로, `SECURITY_GATE_BLOCKED` 줄을 남기고 커밋한 뒤 409 `SECURITY_GATE_BLOCKED`(`details` = `{field: 통제 ID, reason: 사유 코드}`). 고친 뒤 다시 승인할 수 있다. 막힌 **시작**: 트래픽을 바꾸지 않고 `FAILED(SECURITY_GATE_BLOCKED)`, `rollbackStatus = NOT_NEEDED`, 줄 순서 `SECURITY_GATE_BLOCKED → FAILED` | 보안 1 §6 |
| D-4 | 판정 직전에 **C-IMG-3**(출발·도착 현재 배포 digest, WAS 상태)과 **C-TLS-1**(대표 주소 + 도착 환경 주소)을 새로 확인해 `findings`(`sourceRef = migration-plan:{id}`)에 남긴다. 이 두 통제는 **방금 만든 증거만** 쓴다(일시적인 TLS 시간 초과가 2분 동안 다시 막지 않게) | 보안 1 §5.1 실행 직전 |
| D-5 | TLS 주소 규칙: 공개 도메인 아래 이름만, 443 만, DNS 로 받은 IP 를 확인한 뒤 **그 IP 로 접속**(인증서는 이름으로 검증). 모두 공인 → 시험, 모두 사설 → 접속 안 함 NA, 루프백·링크 로컬·예약·섞임 → 접속 안 함 UNKNOWN, 도메인 밖 → DNS 도 안 찾고 UNKNOWN, DNS 실패 → UNKNOWN. 주소 하나의 확인 전체에 5초(통제 시간 예산) | 11/4 결정, 보안 1 AD-5 |
| D-6 | 기한부 예외는 **승인됐고 철회되지 않은 것만** 읽는다(만료된 지 7일 안의 것도 읽어 "예외 만료" 사유를 붙인다). 요청·승인은 `users` FK 라 사람(USER)이다 | #60 리뷰 ① |
| D-7 | C-AI-1 증거 = 그 서비스·도착 환경의 **24시간 안에 성공한 가장 늦은 AI 사전 점검**(증거 ID = 실행 ID). 가짜 실행은 규칙표가 버린다(`EVIDENCE_SIMULATED`) | 규칙표, 계약 v1 |
| D-8 | 판정 범위 WAS 버전 = 설정 `WAS_VERSION`. 비면 C-IAM-2·C-AUD-1 은 증거도 예외도 맞지 않아 확인 불가다 | 규칙표 범위 규칙 |
| D-9 | 판정 기록 = 로그 줄 data: `policyVersion`·`phase`·`blocking[{controlId, reasons}]`·`findingIds`·`exceptionIds`(값·원문 없음) | 보안 1 AD-3, C-AUD-1 |
| AD-1 | 정책 버전은 지문에 넣지 않는다(§3.3). 사전 승인(#30)에서 다시 본다 | AI-default |

## 5. 켜기 전 준비물 (게이트를 켜면 필수 통제마다 필요한 것)

| 통제 | 통과 조건 | 지금 |
|---|---|---|
| C-IMG-1·C-IMG-2 | 그 digest 의 Trivy·gitleaks 증거(7일) | #53(빌드 검사) 병합 뒤 새 빌드부터 생긴다 |
| C-IMG-3·C-TLS-1 | 게이트가 직접 확인 | 됨 |
| C-SEC-1 | gitleaks·Trivy 비밀 검사 또는 사람 점검 기록 | 빌드 검사에서 C-IMG-2 와 함께 자동으로 생긴다(2-4 SPEC, 새 빌드부터) |
| C-IAM-2·C-AUD-1 | `WAS_VERSION` 의 WAS 시험·감사 기록 증거 | 없음 → 수집기(감사 #32) 또는 기한부 예외 |
| C-AI-1 | 24시간 안의 AI 사전 점검 성공 | 러너 API 다음 PR. **시연 방침 결정 필요**(#60 리뷰 ④: 러너를 돌릴지, 기한부 예외로 둘지) |
| C-DB-1·C-DST-1 | 기한부 예외 | 예외 API 다음 PR(보안 1 §5.3 사유·만료) |

예외 API 가 생기기 전에는 켜도 통과할 수 없다. 그래서 이 PR 은 **꺼진 채 병합**하고, 금요일 종단 시험에서 켠다(보안 1 P-d).

## 6. Acceptance criteria

| AC | 확인 | 시험 |
|---|---|---|
| AC-1 | 꺼져 있으면 판정 줄·증거 행이 없고 계획이 그대로 성공한다 | `test_approve_with_gate_off_runs_no_checks_and_writes_no_gate_lines` + 기존 전체 시험 |
| AC-2 | 증거가 없으면 승인 409, 막은 통제 8개와 사유, 계획 대기 유지, 할 일 없음, 판정 줄·증거 2건 | `test_approve_without_evidence_returns_409_and_keeps_plan_pending` |
| AC-3 | 증거·예외·사전 점검이 다 있으면 승인·시작 두 번 통과하고 이전이 끝난다 | `test_approve_with_evidence_exceptions_and_precheck_runs_migration` |
| AC-4 | 승인 뒤 예외를 철회하면 시작이 `FAILED(SECURITY_GATE_BLOCKED)`, 트래픽 호출 없음 | `test_start_with_revoked_exception_fails_plan_without_traffic_change` |
| AC-5 | 승인 안 된 예외는 안 쓰고, 만료된 예외는 "예외 만료" 사유 | `test_approve_with_unapproved_or_expired_exception_is_blocked` |
| AC-6 | 가짜 AI 사전 점검은 C-AI-1 을 채우지 않는다 | `test_approve_with_simulated_or_old_precheck_is_blocked_on_ai_1` |
| AC-7 | TLS 주소 규칙(D-5) | `test_approve_with_private_target_host_uses_main_host_only`, `test_approve_with_suspicious_or_failing_host_is_blocked_on_tls_1`, `test_classify_addresses_*`, `test_is_public_host_name_*`, `test_public_host_checker_*`, `test_probe_with_address_connects_to_that_ip_and_verifies_host_name` |
| AC-8 | 일시적인 TLS 실패 뒤 다시 승인하면 새 시험 결과로 통과 | `test_approve_after_transient_tls_failure_uses_fresh_probe` |
| AC-9 | 리비전 왕복, 막힌 계획이 있으면 되돌리기가 분명히 실패 | `test_security_gate_revision_downgrade_narrows_check_and_upgrade_restores`, `test_security_gate_revision_downgrade_with_blocked_plans_fails_clearly` |
| AC-10 | 로컬 `upgrade → downgrade -1 → upgrade → alembic check` | 수동(PR에 결과) |

## 7. 리스크

- **R-a 병합 = 운영 마이그레이션 Job.** CHECK 를 지우고 다시 만드는 동안 `migration_plans` 를 잠깐 잠근다(표가 작다). 실행 중인 이전이 없을 때 병합한다.
- **R-b 롤링 배포 중 옛 파드.** 기본 꺼짐이라 동작이 같고, 새 값은 게이트를 켰을 때만 저장된다.
- **R-c 화면이 모르는 코드.** 새 `errorCode`·로그 코드·409 코드는 게이트를 켰을 때만 나온다. 화면은 `errorMessage`·`message` 고정 문장을 보이면 된다(김도영 님께 확인 요청).
- **R-d 승인 응답 시간.** 켜면 TLS 시험 때문에 승인이 최대 약 5초 늘 수 있다(두 주소 동시).
- **R-e 증거 행 증가.** 판정마다 증거 2건이 생긴다. 판정 조회 상한(조직당 최신 500건) 안에서 오래된 빌드 검사 증거가 밀릴 수 있어, 주기 수집을 붙일 때 상한을 다시 본다.
- **R-f 쌓은 PR.** #60 이 먼저 병합되어야 한다. 병합 뒤 이 PR 의 기준 브랜치를 `main` 으로 바꾸고 head 가 하나인지 다시 확인한다. **(11/4)** #62(앱 환경 변수 표 `48b2126a9c37`)가 먼저 병합돼 head 가 둘이 될 뻔했다. main 을 이 브랜치에 merge 하고 `286d9ea982d6` 의 `down_revision` 을 `48b2126a9c37` 로 옮겼다. **(11/5)** 이어서 #66(감사 로그 확장 `b7d41e6f2a93`)이 병합돼 한 번 더 main 을 merge 하고 `b7d41e6f2a93` 뒤로 옮겼다. 서로 다른 표·칸이라 순서와 상관없다. 게이트가 실행 시작에서 막으면 #66 의 이전 종료 감사 기록(FAILED, `SECURITY_GATE_BLOCKED`)도 남는다.
