# 보안·거버넌스 1-2: C-SEC-1(비밀 전달 경로)을 고객 서비스 기준으로 다시 정의 SPEC

- 상태: Draft (2026-11-06, 김형래)
- Depth: Light (규칙표 한 줄 + 정규화 함수 하나 + 게이트·매트릭스 계산, DB 변경 없음)
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/26 (보안·거버넌스 2)
- 근거: #74 리뷰(지민 님, 11/5) "gitleaks 를 왜 C-SEC-1 근거로 쓰는가" · #74 답과 동의(11/6) / 보안 1 SPEC `specs/sec-1-security-controls.md` 133행 / 앱 환경 변수 `docs/api/variables.md`·`docs/executors.md` "앱 환경 변수"
- 바꾸는 것: `specs/sec-2-4-delivery-path-evidence.md`(#74)를 대신한다

## 1. 왜 바꾸나

- #74 의 C-SEC-1 증거는 빌드 비밀 검사(C-IMG-2)와 같은 원문을 이름만 바꿔 한 번 더 저장한 것이었다. 새로 확인하는 것이 없는데 PASS 로 보였다.
- 정의("관제탑·실행기 비밀")도 이전과 결이 맞지 않았다. 옮기는 것은 고객 서비스인데, Echo 자신의 비밀은 환경마다 같은 값이라 이전 판정에 쓸모가 없다.

## 2. 새 정의

> **이 서비스의 비밀(앱 환경 변수)이 도착 환경 보관함에 빠짐없이 준비돼 있고, 참조로만 전달되는가.**

| 항목 | 값 |
|---|---|
| 범위 | 서비스 × 환경 (`SERVICE`·`ENVIRONMENT`) |
| 출처 | `WAS_STATE`(앱 환경 변수 행의 키 이름만, 값·보관함 위치·버전은 읽지 않는다) · `MANUAL_REVIEW` |
| 시점 | 준비·실행 (`_PREP_AND_EXEC`), 실행 시간 예산 1초 |
| 판정 | 기준 연결의 키 ⊆ 대상 연결의 키 → PASS(`SECRETS_READY`) / 빠진 키 있음 → FAIL(`SECRETS_MISSING`, 근거 줄 = 빠진 키 이름) / 기준에 키가 없음 → PASS |
| 필수 | 예(FAIL 은 그 서비스의 그 도착 환경만 막는다) |
| 규칙표 버전 | `sec-rules-2026.11.04` → `sec-rules-2026.11.06` |

- **게이트**(승인·일꾼 시작): 기준 = 계획의 출발 연결, 대상 = 도착 연결. 판정마다 새로 계산하고 findings 에 남긴다(`source_ref = migration-plan:<id>`). 저장된 WAS 상태 C-SEC-1 은 판정에 넣지 않는다(C-IMG-3 와 같은 방식). 사람 점검 기록은 그대로 쓴다.
- **매트릭스·비교**(준비 시점): 서비스마다 기준 = 서비스 중 연결(비율이 가장 큰 연결, 같으면 id 가 작은 것), 대상 = 그 서비스의 각 연결. 조회 때 계산하고 **저장하지 않는다**. 증거 id 는 (연결, 기준 연결)로 정해 조회마다 같지만 증거 목록(`/security/findings`)에는 없다. 대기 환경에 비밀이 빠졌는지 이전 전에 보이게 하려는 것이다.
- **"참조로만 전달"**은 코드 구조다. 앱 Deployment 는 `env` 에 Echo 값 두 개(`PORT`·`ECHO_ENVIRONMENT`)만 두고 비밀은 `envFrom` Secret `{ns}-env` 참조로만 받는다. 판정이 다시 보지 않고 시험이 고정한다(`tests/test_secret_delivery_invariant.py`, 배포 경로 전체는 `test_secret_value_appears_nowhere_after_full_deploy`).

## 3. 보지 않는 것 (문서에 "확인 안 됨"으로 둔다)

- 값이 맞는지(환경마다 다른 값이 정상일 수 있다).
- 도착에 넣은 값이 도착의 **지금 도는 배포**에 반영됐는지(`pendingRedeploy`). 후보: 반영 전이면 WARN — 따로 정한다.
- 클러스터 Secret 의 저장 암호화(KMS envelope encryption). echo-infra Terraform 에 GKE `database_encryption`·EKS `encryption_config` 가 없다(11/6 확인) — 클라우드 기본 암호화만 있다.
- Echo 자신의 비밀 경로(GitHub App 키·클러스터 접속 열쇠·러너 토큰). 플랫폼 점검(C-IAM-1 처럼 사람 점검 기록)으로 옮긴다.

## 4. 바뀌는 것

| 파일 | 변경 |
|---|---|
| `app/services/security_rules.py` | C-SEC-1 줄(위 표), `POLICY_VERSION` |
| `app/enums.py` | 정규화 이유 `SECRETS_READY`·`SECRETS_MISSING`(DB CHECK 없음) |
| `app/services/security_normalizers.py` | `normalize_secret_delivery(reference_keys, target_keys, scope, collected_at)`. #74 의 `derive_delivery_path_finding` 삭제 |
| `app/services/security_scan_collector.py` | 빌드 검사 증거 3건으로 되돌림(C-IMG-1·C-IMG-2 둘) |
| `app/repositories/service_environment_variable_repository.py` | `search_live_keys_by_connections` — 연결마다 살아 있는 키(한 번의 조회) |
| `app/services/security_gate.py` | 출발·도착 키를 읽어 계산·기록, `RECHECKED_SOURCES`·`is_rechecked` |
| `app/services/security_matrix_service.py` | 서비스 중 연결 기준 계산, 저장된 WAS 상태 C-SEC-1 은 쓰지 않음 |

DB·API 모양 변경 없음. 비교 API(#84)는 매트릭스 `get_rows` 를 써서 같이 바뀐다.

## 5. 운영 영향

- **`POLICY_VERSION` 을 올리면 저장된 증거가 출처와 상관없이 모두 `POLICY_VERSION_MISMATCH` 로 무효**가 된다(11/6 바로잡음 — 처음에는 빌드 검사만 적었다, #87 리뷰). 배포 직후 매트릭스·게이트가 그 증거의 필수 통제를 UNKNOWN·BLOCKED 로 본다. 다시 만드는 방법은 [규칙표 SPEC 6.1](sec-3-rule-table.md#61-policy_version-을-올린-뒤-할-일-116-추가): 빌드 검사는 demo-app 새 커밋 빌드 1회, TLS 주기 시험은 30분 주기로 다시 쌓임, WAS 시험·감사 기록·사람 점검은 다시 넣거나 기한부 예외. 기한부 예외·AI 사전 점검은 영향이 없다.
- 조직 범위로 만든 옛 C-SEC-1 예외는 범위가 맞지 않아 더는 쓰이지 않는다(새 판정은 키가 맞으면 PASS 라 예외가 필요 없다).
- **시연 서비스의 환경별 변수 키가 서비스 중 환경과 같아야 한다.** 다르면 그 도착 환경이 C-SEC-1 FAIL 로 막힌다(의도한 동작). 배포 전에 확인한다.

## 6. 완료 기준

- AC-1 정규화: 모두 있음·기준 비어 있음·둘 다 비어 있음 → PASS, 빠짐 → FAIL + 빠진 키만 근거(값 칸 없음). FAIL 은 그 서비스·환경만 막는다 — `tests/test_security_normalizers.py`
- AC-2 게이트: 출발 `API_KEY`·`DB_URL`, 도착 `API_KEY` → 409 C-SEC-1 `CONTROL_FAILED`, 증거 FAIL·`SECRETS_MISSING`·근거 `DB_URL`, 값 없음. 도착에 `DB_URL` 을 넣으면 승인 통과. 저장된 WAS 상태 PASS 는 다시 쓰지 않는다 — `tests/integration/test_security_gate.py`
- AC-3 매트릭스: 서비스 중 aws PASS, 대기 gcp FAIL·BLOCKED, id 는 조회마다 같고 증거 목록에 없음, 값 없음. 키를 채우면 PASS — `tests/integration/test_security_matrix_api.py`
- AC-4 전달 방식: Deployment `env` 는 두 값뿐, 비밀은 `envFrom` Secret 참조, 값은 Secret 에만 — `tests/test_secret_delivery_invariant.py`
- AC-5 회귀: 빌드 검사 증거 3건(#74 이전과 같음), 조직 범위 예외 시험은 C-AUD-1(WAS 버전 범위)·C-CI-1 로 옮김
