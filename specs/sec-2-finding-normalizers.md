# 보안·거버넌스 2-1: 검사 원문 정규화 + TLS 접속 시험 SPEC

- 상태: Draft (2026-11-04, 김형래)
- 작성일: 2026-11-04
- Depth: Standard (새 순수 계산 모듈 + 네트워크 클라이언트, DB·API 변경 없음)
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/26 (보안·거버넌스 2, 담당 hyeongrae-kim) — 이 문서는 그 첫 부분
- 근거: 보안·거버넌스 1 SPEC(PR #43) §5.1 C-IMG-1·C-IMG-2·C-CFG-1·C-TLS-1, AD-7(수집기 신원) / 보안·거버넌스 3 SPEC(PR #44) `SecurityFinding`·`POLICY_VERSION`
- 저장소: `echo-was` · 브랜치 `feat/sec-2-finding-normalizers` (**#44 위에 쌓임**) · PR로 올린다

## 1. 목표

검사 도구의 원문(Trivy 이미지·비밀·설정, gitleaks, TLS 접속 시험)을 규칙표가 바로 쓰는 공통 증거(`SecurityFinding`)로 바꾼다. 비밀값 원문은 읽지도 남기지도 않는다. 원문이 기대한 대상의 것이 아니거나 모양이 틀리면 PASS가 아니라 UNKNOWN 증거를 만든다.

## 2. 확인된 사실 (11/4)

- 운영 demo-app 이미지는 Trivy 0.75.0 기준 수정 버전이 있는 HIGH 5건이 있다(#43 §2.3). 이 정규화로는 C-IMG-1 FAIL이 된다.
- Trivy JSON(`SchemaVersion` 2)은 `ArtifactName`·`Metadata.RepoDigests`·`Results[].Vulnerabilities`·`Secrets`·`Misconfigurations`를 준다. 비밀 결과의 `Match`·`Code`에는 비밀 원문 일부가 들어 있다.
- gitleaks JSON은 발견 목록이고, 한 건마다 `Secret`·`Match`(원문)와 `Author`·`Email`(개인 정보)이 들어 있다.
- `cryptography`가 이미 간접 의존성으로 있어(`pyjwt[crypto]`) 시험용 CA·인증서를 만들 수 있다.

## 3. 범위

### 3.1 이번에 한다
- `app/services/security_normalizers.py`: `normalize_trivy_image`(C-IMG-1), `normalize_trivy_image_secrets`·`normalize_gitleaks`(C-IMG-2), `normalize_secret_delivery`(C-SEC-1, 앱 환경 변수 키 — 1-2 SPEC), `normalize_trivy_config`(C-CFG-1), `normalize_tls_probes`(C-TLS-1·C-POST-1의 TLS 부분)
- `app/clients/tls_probe.py`: `SocketTlsProbeClient.probe(host, port)`, `judge_tls`
- `app/enums.py`: 정규화 이유 `SecurityFindingReason`
- 시험: `tests/test_security_normalizers.py`, `tests/test_tls_probe.py`(로컬 TLS 서버로 실제 핸드셰이크)

### 3.2 다음 PR (같은 이슈 #26, 남은 완료 기준)
- `findings` 표·Alembic 리비전·저장소, 수집 실행(검사 산출물 읽기 + 주기 TLS 시험), 조직별 접근 격리 시험. 로컬 시험 DB가 필요해 따로 올린다
- 검사 산출물 읽기: GitHub App 설치 토큰을 `actions: read` 하나로 좁혀 발급(#43 AD-7)

### 3.3 하지 않는다
- 클라우드·쿠버네티스 API 읽기(P1), 사람 점검 기록 입력 화면·API(#30·#32)

## 4. 결정

| ID | 결정 | 출처 |
|---|---|---|
| D-1 | C-IMG-1: HIGH·CRITICAL + 수정 버전 있음 → FAIL(`FIXABLE_HIGH_VULNERABILITY`). 그 밖의 취약점이 하나라도 있으면 WARN(`VULNERABILITY_WARNING`). 없으면 PASS | #43 §5.1 |
| D-2 | C-IMG-2: gitleaks·Trivy 비밀 결과가 하나라도 있으면 FAIL(`SECRET_FOUND`). 근거로 규칙 ID·파일·줄·커밋만 남기고 `Secret`·`Match`·`Code`·작성자·메일은 읽지 않는다 | #43 §5.1, #26 완료 기준 3 |
| D-3 | C-CFG-1: `Status = FAIL`인 설정 위반 중 HIGH 이상 → FAIL, 그 밖 → WARN, 없으면 PASS. 선택 통제라 막지 않는다 | #43 §5.1 |
| D-4 | C-TLS-1: 주소마다 접속 시험 결과의 가장 나쁜 값. 인증서 검증 실패·호스트명 불일치·TLS 1.2 미만·만료 7일 미만·핸드셰이크 실패 → FAIL, 시간 초과·DNS·연결 실패 → UNKNOWN. 주소가 없으면 UNKNOWN | #43 §5.1 |
| D-5 | 이미지 원문은 `Metadata.RepoDigests`나 `ArtifactName`이 `@<판정 대상 digest>`로 끝날 때만 그 digest의 증거다. 아니면 UNKNOWN(`ARTIFACT_MISMATCH`). 설정 원문은 설정 커밋을 함께 받아야 한다 | #44 D-3 |
| D-6 | 원문 모양이 틀리면(`SchemaVersion ≠ 2`, `Results`가 목록이 아님 등) UNKNOWN(`REPORT_INVALID`). `Results`가 없는 Trivy 결과는 발견 0건이다 | #43 D-5 |
| D-7 | 만든 증거의 만료 = 수집 시각 + 그 통제의 유효기간(`RULES`), 정책 버전 = `POLICY_VERSION`. 모드(`REAL`·`CACHE`·`SIMULATED`)는 부르는 쪽이 정한다 | #44 D-2·D-3 |
| D-8 | TLS 접속 시험은 HTTP 요청 없이 TLS 핸드셰이크만 한다. 클라우드 자격을 쓰지 않는다. 기본 제한 시간 5초 | #43 AD-7, §5.1 |
| AD-1 | 근거 줄은 심각도 순으로 최대 50개(`MAX_ITEMS`)만 남기고 전체 개수(`total_items`)를 따로 둔다. 같은 취약점(ID·패키지·설치 버전)은 한 번만 센다 | AI-default |
| AD-2 | TLS 1.2 미만 판별은 OpenSSL 사유(`UNSUPPORTED_PROTOCOL`·`TLSV1_ALERT_PROTOCOL_VERSION`·`NO_PROTOCOLS_AVAILABLE`)로 한다. 그 밖의 SSL 오류는 `TLS_HANDSHAKE_FAILED` | AI-default |

## 5. 계약

- 모든 정규화 함수: `(원문, *, scope, collected_at, tool_version=None, mode=REAL) -> NormalizedFinding`
  - `NormalizedFinding`: `finding`(`SecurityFinding`), `reason`(`SecurityFindingReason`), `items`(`FindingItem` 최대 50), `total_items`, `tool_version`
  - `FindingItem`: `item_id`(CVE·설정 위반 ID·규칙 ID·`host:port`), `severity`, `target`(패키지·파일·호스트), `installed_version`, `fixed_version`, `line`, `commit`, `reason`
- `SocketTlsProbeClient(timeout_seconds=5.0, ssl_context=None, clock=None).probe(host, port=443) -> TlsProbeResult(host, port, reason, tls_version, not_after)`

## 6. 호환성·배포

- 새 파일과 enum 추가뿐이다. 부르는 곳이 아직 없어 운영 동작은 바뀌지 않는다. 되돌리기는 이 PR revert다.
- #44(규칙표)의 `SecurityFinding`·`POLICY_VERSION`·`get_rule`을 쓴다. #44를 먼저 병합해야 한다.

## 7. Acceptance criteria

| AC | 확인 | 시험 |
|---|---|---|
| AC-1 | C-IMG-1이 D-1대로 FAIL·WARN·PASS를 내고, 근거 줄·전체 개수·도구 버전·정책 버전·만료를 담는다. 다른 digest·digest 없음·틀린 원문은 UNKNOWN이다 | `test_trivy_image_*`, `test_simulated_mode_*` |
| AC-2 | C-IMG-2가 비밀 발견을 FAIL로 내고, 증거 어디에도 비밀 원문·작성자 정보가 없다 | `test_trivy_secrets_*`, `test_gitleaks_*` |
| AC-3 | C-CFG-1이 D-3대로 판정하고, 설정 커밋이 없으면 UNKNOWN이다 | `test_trivy_config_*` |
| AC-4 | TLS 접속 시험이 실제 핸드셰이크로 정상·만료 임박·신뢰 안 되는 인증서·호스트명 불일치·연결 불가·시간 초과를 가린다 | `tests/test_tls_probe.py` |
| AC-5 | C-TLS-1이 주소별 결과의 가장 나쁜 값이고, 주소가 없으면 UNKNOWN이다 | `test_tls_probes_*` |
| AC-6 | 정규화한 증거를 규칙표가 그대로 받아 판정한다 | `test_normalized_findings_feed_rule_table` |

## 8. 리스크

- **R-a Trivy 원문 모양이 버전마다 조금씩 다르다** → 필요한 칸만 읽고, 모르는 모양은 UNKNOWN으로 둔다(PASS로 두지 않는다). 검사 워크플로가 Trivy 버전을 고정한다.
- **R-b TLS 시험이 실행 직전 5초 예산을 넘긴다** → 주소마다 제한 시간을 두고, 게이트(#29)가 여러 주소를 함께(`asyncio.gather`) 시험한다.

## 9. 구현 계획으로 위임한 세부사항

- `findings` 표의 칸 모양(근거 줄을 JSON으로 둘지 별도 표로 둘지), 수집 실행 주기, 검사 산출물 이름.
