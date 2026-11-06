# 보안·거버넌스 5-3: 플랫폼 범위 통제 C-IAM-2·C-AUD-1 증거 수집기(플랫폼 자기 시험) SPEC

- 상태: Draft (2026-11-07, 지민 승인 11/7 02:5x 「병렬로 전부 진행」)
- Depth: Light (일꾼 루프 하나 + 순수 판정 함수, DB·API 변경 없음 — 증거 표 `findings` 재사용)
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/32 (보안·거버넌스 12, 감사 연동 범위 중 C-IAM-2·C-AUD-1 증거 수집기)
- 근거: `specs/sec-1-security-controls.md` 5.1 표 C-IAM-2·C-AUD-1(132·134행) / `specs/sec-3-rule-table.md` 90·92행(범위 = WAS 버전, 출처 `WAS_TEST`·`AUDIT_LOG`, 유효 7일), 116행(「앱 안에 만드는 경로가 없다(수집기는 #32)」) / 기존 주기 시험 관례 `app/services/periodic_tls_probe.py`(C-TLS-1)
- 저장소: `echo-was` · 브랜치 `feat/sec-5-platform-evidence` (`main` = `f9680b8`, #100 포함)

## 1. 왜 하나

- 두 통제는 필수(`예`)인데 증거를 만드는 경로가 없어 늘 UNKNOWN 이었다. 운영은 WAS 버전 범위 기한부 예외(11/12 만료)로 덮어 두었다. 심사에서 "증거 없음 → 예외"는 약한 답이다.
- 실제로는 지켜지고 있다: 에이전트 토큰의 쓰기는 검사기에서 403 `AGENT_READ_ONLY`(`app/auth/guards.py`), 조직 안 쓰기 34경로는 감사 이름표(`WRITE_ROUTE_ACTIONS`)와 시험(`test_every_write_route_has_an_action_name`)으로 묶여 있다. 이것을 **운영에서 주기적으로 직접 확인해 증거로 남기면** 예외 없이 PASS 가 된다.

## 2. 결정

| ID | 결정 | 출처 |
|---|---|---|
| D-1 | 수집은 기존 C-TLS-1 주기 시험과 같은 방식: 보안 게이트가 켜진 일꾼 프로세스의 루프. 시작 때 한 번, 이후 `SECURITY_PLATFORM_SELF_TEST_INTERVAL_SECONDS`(기본 21600 = 6시간, 300~28800)마다 | 지시(11/7), `periodic_tls_probe.py` 관례 |
| D-2 | **C-IAM-2 = 자기 시험(`WAS_TEST`)**: 조직마다 진짜 에이전트 토큰을 일회용으로 발급(이름 `security-self-test`, 유효 1일, 원문은 메모리에만, 발급자 = 조직을 만든 사람)하고, 앱의 쓰기 경로 전부(`POST·PUT·PATCH·DELETE` − 로그아웃 − 러너 경로 = 35개, 승인 경로 3개·배포 실패 진단 1개 포함)를 그 토큰으로 호출한다. **모두 403 `AGENT_READ_ONLY` 면 PASS, 하나라도 다른 응답(2xx·404·다른 403·500)이면 FAIL**. 부른 경로가 없으면 UNKNOWN. 토큰은 끝나면 바로 폐기한다 | 지시(11/7) |
| D-3 | 호출은 일꾼 프로세스 안의 같은 API 앱(ASGI, `httpx.ASGITransport`)으로 한다. 일꾼과 API 는 같은 이미지·같은 ConfigMap(`WAS_VERSION`)이므로 "그 WAS 버전의 검사기"를 시험한다. 네트워크 경로(인그레스)는 보지 않는다 | AI-default(검토 필요) |
| D-4 | 경로 변수는 조직 = 그 조직, 앱 환경 변수 키 = `SELF_TEST_KEY`, 나머지 = 임의 uuid, 본문 = `{}`. 검사기가 뚫려도 리소스를 찾지 못하거나(404) 본문 검증(422)에서 멈추므로 운영 데이터를 바꾸지 않는다(그 응답은 FAIL 로 기록된다) | AI-default |
| D-5 | **C-AUD-1 = 감사 기록 대조(`AUDIT_LOG`)**: (a) 앱의 쓰기 경로가 모두 감사 행동 이름표에 있는가(기존 시험과 같은 계산을 `app/audit/route_coverage.py` 로 옮겨 씀), (b) 최근 24시간에 일어난 일마다 그 대상(실행) id 로 감사 줄이 있는가 — 3절 표, (c) D-2 의 거부 요청이 모두 그 토큰 id 의 `DENIED`·`AGENT_READ_ONLY` 줄로 남았는가. 빠진 것이 하나라도 있으면 FAIL, 없으면 PASS. 그 시간에 일이 없던 종류는 `0/0` 으로 보이고 결과를 바꾸지 않는다 | 지시(11/7) |
| D-6 | 증거 범위 = 조직 + WAS 버전(`SecurityScope(org, was_version=WAS_VERSION)`), 환경·서비스 칸 없음. 매트릭스는 환경 행마다 같은 조직 증거를 쓰므로 **환경 3개 공통**이다(기존 WAS 버전 범위 예외가 모든 환경 행에 붙던 것과 같은 관례 — `test_organization_exception_marks_audit_cell_in_every_environment`). `WAS_VERSION` 이 비면 판정에 쓸 수 없어 돌지 않는다 | sec-3 AD-4 |
| D-7 | 정책 버전은 지금 규칙표 `POLICY_VERSION`(`sec-rules-2026.11.06`). 규칙표는 바꾸지 않는다 | 지시(11/7) |
| D-8 | 증거 만료 = 수집 + **24시간**(규칙 유효기간 7일보다 짧게). 한 통제에 증거가 여럿이면 가장 나쁜 값이라, 한 번의 FAIL 이 7일 동안 남지 않게 하고 일꾼이 멈추면 하루 뒤 UNKNOWN 으로 돌아가게 한다. 간격 상한 8시간 = 유효기간 안에 3번 이상 돈다 | AI-default |
| D-9 | 시험 자체가 예외로 끝나면 증거를 남기지 않고 로그만 남긴다(다음 주기에 다시). 조직 하나의 실패가 다른 조직을 막지 않는다 | `periodic_tls_probe.py` 관례 |
| D-10 | **예외는 건드리지 않는다.** 규칙표 `evaluate_control` 은 결과가 FAIL·UNKNOWN 일 때만 예외를 적용한다. 증거가 PASS 면 `effective_result = PASS`, `gateStatus = PASS`, 예외 표시 없음이고, 기존 예외는 그대로 11/12 에 만료된다 | 코드 확인 |
| D-11 | 일회용 토큰 발급·폐기는 SYSTEM 감사 줄(`component = security-self-test`, `AGENT_TOKEN_ISSUED`·`AGENT_TOKEN_REVOKED`)로 남긴다. 사람 행동이 아니므로 USER 로 쓰지 않는다 | AI-default |
| D-12 | 마이그레이션 없음. 새 값은 `SecurityFindingReason` 5개(DB CHECK 없음)·`AuditComponent` 1개(문자열 칸)·설정 1개 | 지시(11/7) |

## 3. C-AUD-1 대조 표 (최근 24시간, 그 조직)

| 근거 줄 `itemId` | 있어야 할 것(도메인 행) | 찾는 감사 줄(`action`, `target_id`) |
|---|---|---|
| `denial:AGENT_READ_ONLY` | D-2 에서 403 을 받은 경로 | AGENT · 그 토큰 · `DENIED` · `AGENT_READ_ONLY` · `route = METHOD 틀` |
| `approval:MIGRATION_PLAN_APPROVED` | `approved_at` 이 창 안이고 사람이 승인한 이전 계획 | `MIGRATION_PLAN_APPROVED`, 계획 id |
| `gate:SECURITY_GATE_PASSED` (게이트 켜짐일 때) | 위 계획 | 실행 로그 `migration_plan_logs.code = SECURITY_GATE_PASSED`(게이트 판정은 감사 줄이 아니라 계획 id 의 실행 로그에 정책 버전·증거 id 와 함께 남는다) |
| `approval:AGENT_PATCH_APPROVED`·`…REJECTED` | `decided_at` 이 창 안인 AI 패치 결정 | 같은 이름, 사전 점검 실행 id |
| `approval:SECURITY_EXCEPTION_APPROVED` | `approved_at` 이 창 안인 기한부 예외 | 같은 이름, 예외 id |
| `agent:AGENT_RUN_CLAIMED` | `claimed_at` 이 창 안인 AI 실행 | 같은 이름, 실행 id |
| `agent:AGENT_RUN_FINISHED` | `finished_at` 이 창 안이고 `SUCCEEDED`·`FAILED` 인 AI 실행 | 같은 이름, 실행 id |
| `agent:AGENT_RUN_TIMED_OUT` | `finished_at` 이 창 안이고 `TIMED_OUT` 인 AI 실행 | 같은 이름, 실행 id |
| `exception:SECURITY_EXCEPTION_REQUESTED`·`…REVOKED` | `created_at`·`revoked_at` 이 창 안인 기한부 예외 | 같은 이름, 예외 id |

근거 줄(`items`, 최대 50): 빠진 것(`AUDIT_RECORD_MISSING`, `target` = 대상 id · `AUDIT_ROUTE_UNNAMED`, `itemId` = 경로)을 먼저, 그다음 종류마다 `확인/전체`(`target = "3/3"`).

보지 않는 것: 사람이 취소한 AI 실행(`CANCELLED`), 실행 시작 때의 게이트 판정(일꾼 `_start` 의 실행 로그), 배포·되돌리기 요청(실행 ID 가 아니라 배포 요청 id — 이미 `test_audit_logs.py` 가 34경로 전부를 시험한다), 데모 시드(감사 대상 아님).

## 4. 바뀌는 것

| 파일 | 변경 |
|---|---|
| `app/audit/route_coverage.py` (새) | 앱 쓰기 경로 목록·이름표 대조(`unnamed_write_routes`)·에이전트 시험 경로(`agent_probe_routes`) |
| `app/services/platform_self_test.py` (새) | `PlatformSelfTest`(루프·조직별 시험·토큰 발급/폐기·감사 대조), 순수 판정 `judge_agent_writes`·`judge_audit_trail`, `fill_route` |
| `app/worker/__main__.py` | 게이트가 켜져 있으면 TLS 주기 시험 옆에 자기 시험 루프를 띄운다(`app.main.app` 을 넘긴다) |
| `app/core/config.py` | `SECURITY_PLATFORM_SELF_TEST_INTERVAL_SECONDS` |
| `app/enums.py` | `SecurityFindingReason`: `AGENT_WRITES_DENIED`·`AGENT_WRITE_NOT_DENIED`·`AUDIT_TRAIL_COMPLETE`·`AUDIT_RECORD_MISSING`·`AUDIT_ROUTE_UNNAMED` |
| `app/audit/actions.py` | `AuditComponent.SECURITY_SELF_TEST = "security-self-test"` |
| 문서 | `docs/api/security.md`(다음 절), `docs/api/audit-logs.md`(`component` 표), `README.md`(설정 표), `specs/sec-3-rule-table.md` §6.1, 용어 사전 |

## 5. 운영 배포 뒤 기대

1. 병합 = 운영 자동 배포 → 일꾼 재시작 → **시작하자마자** 살아 있는 환경이 있는 조직마다 C-IAM-2·C-AUD-1 증거 2건(`sourceRef = self-test:platform`, `wasVersion = 2026.11.05`, 유효 24시간).
2. 그 즉시 매트릭스의 aws·gcp·onprem 세 행 모두 `iam`·`auditLog` 칸의 두 통제가 `result = PASS`·`gateStatus = PASS`·예외 표시 없음이 된다(C-AUD-1 이 PASS 일 때). 기존 예외는 남아 있지만 적용되지 않고 11/12 에 만료된다.
3. 확인 방법(읽기만): `GET /api/v1/orgs/{org}/security/findings?controlId=C-IAM-2`·`C-AUD-1` 의 최신 행 `result`·`items`, `GET …/security/matrix` 의 두 통제 `exception = null`. 감사 로그에 `security-self-test` SYSTEM 줄 2개 + 에이전트 거부 34줄이 6시간마다 생긴다.
4. C-AUD-1 이 FAIL 이면 근거 줄의 `target`(대상 id)이 감사 줄 없이 생긴 행이다. 24시간 뒤 그 행이 창을 벗어나고 다음 시험이 PASS 를 남겨도 FAIL 증거는 만료(24시간)까지 남는다. 예외가 유효한 동안은 WARN + 예외로 보인다.

## 6. 인수 조건

| AC | 확인 | 시험 |
|---|---|---|
| AC-1 | 모든 쓰기 경로가 403 `AGENT_READ_ONLY` 면 C-IAM-2 PASS(`WAS_TEST`, 조직 + WAS 버전 범위, 정책 버전, 만료 24시간), 승인 경로 3개가 포함된다 | `test_self_test_records_passing_evidence_with_was_version_scope`, `test_agent_probe_routes_cover_every_human_write_route_including_approvals`, `test_all_denied_writes_pass_iam` |
| AC-2 | 쓰기 하나라도 통과(2xx·404·다른 403·500)하면 FAIL, 통과한 경로가 근거 첫 줄 | `test_agent_write_that_passes_fails_iam_evidence`, `test_any_write_that_is_not_agent_read_only_fails_iam`, `test_no_probe_is_unknown_not_pass` |
| AC-3 | 일회용 토큰은 폐기되고 발급·폐기·거부 34건이 감사 줄로 남는다 | `test_self_test_token_is_revoked_and_every_step_is_audited` |
| AC-4 | 최근 활동(승인·AI 실행·예외)이 감사 줄과 맞으면 C-AUD-1 PASS, 감사 줄 없이 생긴 예외·게이트 로그 없는 승인·이름표에 없는 쓰기 경로는 FAIL | `test_recent_activity_with_audit_rows_passes`, `test_exception_without_audit_row_fails_audit_evidence`, `test_approval_without_gate_log_fails_when_gate_is_on`, `test_unnamed_write_route_fails_audit_evidence`, 단위 `test_missing_audit_row_*`·`test_unnamed_write_route_*` |
| AC-5 | 매트릭스 환경 3개 모두 두 통제가 PASS, 유효한 예외가 있어도 적용되지 않고 예외 행은 그대로 | `test_matrix_uses_pass_evidence_in_every_environment_before_exception` |
| AC-6 | 새 FAIL 은 앞선 PASS 가 있어도 막는다 | `test_failed_evidence_blocks_matrix_even_after_older_pass` |
| AC-7 | `WAS_VERSION` 이 없거나 환경이 없는 조직은 시험하지 않는다(토큰도 만들지 않는다) | `test_self_test_skips_when_was_version_is_empty_or_no_environment` |
| AC-8 | 간격이 증거 유효기간 안에 3번 이상, 범위 밖 값은 거절 | `test_default_interval_*`, `test_interval_outside_range_is_rejected` |

## 7. 리스크

- **R-a 운영 데이터에 감사 줄 없이 생긴 행이 최근 24시간에 있으면 첫 C-AUD-1 이 FAIL** 이다(예: DB 에 직접 넣은 예외). 그 FAIL 은 사실이므로 그대로 두고, 근거 줄의 id 로 원인을 본다. 예외가 유효한 11/12 까지는 막지 않는다.
- **R-b 토큰 행이 쌓인다**(조직마다 하루 4개, 폐기 상태). 감사 줄이 그 토큰을 FK 로 가리켜 지울 수 없다. 해커톤 기간 규모에서는 문제없다. 에이전트 토큰 목록에 `security-self-test`(폐기됨)로 보인다.
- **R-c 인그레스·다른 파드는 보지 않는다**(D-3). 같은 이미지라 검사기 코드는 같다.
- **R-d 쓰기 경로가 늘면 시험 수가 자동으로 늘어난다**(앱 라우트에서 계산). 이름표에 넣지 않으면 C-AUD-1 이 FAIL 이 되어 운영에서도 드러난다.
