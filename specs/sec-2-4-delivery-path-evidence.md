# 보안·거버넌스 2-4: C-SEC-1(비밀 전달 경로) 자동 증거 SPEC

- 상태: Draft (2026-11-05, 김형래)
- 작성일: 2026-11-05
- Depth: Light (정규화 함수 하나 + 수집기 한 곳, 규칙표·DB·API 변경 없음)
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/26 (보안·거버넌스 2)
- 근거: 보안 1 SPEC `specs/sec-1-security-controls.md` 133행(C-SEC-1) / 규칙표 `app/services/security_rules.py`(C-SEC-1 범위 조직만·출처 GITLEAKS·TRIVY_SECRET·MANUAL_REVIEW·7일, 판정은 맞는 증거 중 가장 나쁜 값) / 2-3 SPEC `specs/sec-2-3-build-scan-collect.md`(빌드 검사 원문 → findings) / #59·#61 리뷰 정리 "C-SEC-1 을 채울 방법이 예외뿐" / 11/5 지민 님 요청 "매트릭스 UNKNOWN 1~2개 증거 수집기"
- 저장소: `echo-was` · 브랜치 `feat/sec-2-delivery-path-evidence` (`main` = `65b71d3`에서) · PR로 올린다

## 1. 목표

필수 통제 C-SEC-1 을 사람 점검 기록·기한부 예외 없이 자동 증거로 채운다. 빌드마다 이미 돌고 있는 비밀 검사(이미지 Trivy secret·소스 gitleaks) 원문에서 C-IMG-2 와 함께 C-SEC-1 증거를 만든다.

- C-SEC-1 이 보는 것: 실행기가 쓰는 비밀(빌드용 GitHub 토큰 등)이 이미지·저장소에 원문으로 남지 않고, 앱 비밀은 보관함 참조로만 전달되는가.
- 빌드 이미지의 비밀 검사는 "실행기 경로가 이미지에 비밀을 남기지 않았다"를 직접 확인한다(예: #53 의 인증 파일 남김 수정).

## 2. 확인된 사실 (11/5, `main` `65b71d3`)

- 지금 C-SEC-1 증거를 만드는 곳이 없다. 정규화기는 두 비밀 검사를 C-IMG-2(digest 범위)로만 만든다.
- 게이트·매트릭스는 조직의 만료 안 된 증거를 모두 읽는다. C-SEC-1 은 범위가 조직뿐이라 **증거 하나가 그 조직의 모든 서비스·환경에 걸린다.** 판정은 가장 나쁜 값이라, 뒤에 깨끗한 검사가 와도 앞의 FAIL·UNKNOWN 이 7일 동안 남는다.
- 규칙(범위·출처·결과 기준)을 바꾸면 `POLICY_VERSION` 을 올려야 한다. 그러면 운영의 기존 증거(C-IMG-1 WARN, C-IMG-2 PASS 2건)가 모두 `POLICY_VERSION_MISMATCH` 로 무효가 되고, 새 빌드 전까지 필수 C-IMG-2 가 UNKNOWN 이라 게이트가 막힌다.

## 3. 범위

### 3.1 이번에 한다
- `app/services/security_normalizers.py`: `derive_delivery_path_finding(secret)` — 비밀 검사로 만든 C-IMG-2 증거 → C-SEC-1 증거(또는 없음)
- `app/services/security_scan_collector.py`: 빌드 하나에서 두 비밀 검사의 C-SEC-1 증거를 함께 기록한다(최대 5건)
- 문서: sec-1 133행·sec-5 증거 표·2-1/2-3 SPEC 한 줄·`docs/api/security.md`·CLAUDE.md 수집기 설명
- 시험: 정규화 단위 + 수집 통합 + 게이트(다른 서비스의 경고가 막지 않음)

### 3.2 하지 않는다
- 규칙표 변경·`POLICY_VERSION` 올리기(2절 이유)
- 이미 운영에 있는 빌드의 원문 다시 모으기. 새 빌드부터 생긴다
- "로그에 원문 없음" 확인(로그 마스킹·시험이 맡는다)·관제탑(echo-was) 자체 이미지 검사

## 4. 결정

| ID | 결정 | 이유 |
|---|---|---|
| D-1 | 규칙표는 그대로 두고, 수집기가 같은 원문에서 C-SEC-1 증거를 함께 만든다 | 출처(GITLEAKS·TRIVY_SECRET)는 이미 규칙이 받는다. 버전을 올리면 운영 증거가 모두 무효(2절) |
| D-2 | 결과: 비밀 없음 → **PASS** · 비밀 발견 → **WARN** · 원문 없음·모양 틀림·다른 이미지 → **C-SEC-1 증거를 만들지 않는다** | 조직 범위라 FAIL·UNKNOWN 하나가 조직 전체를 7일 막는다. 발견한 그 이미지는 같은 원문의 C-IMG-2 FAIL(필수, digest 범위)이, 검사 실패는 C-IMG-2 UNKNOWN 이 이미 막는다. C-SEC-1 은 조직 전체 경고로 남긴다 |
| D-3 | 7일 안에 깨끗한 검사가 하나도 없으면 C-SEC-1 증거 없음 → UNKNOWN → 막힘(그대로) | 확인하지 못한 것은 PASS 가 아니다 |
| D-4 | 출처·근거 줄·도구 버전은 원래 검사 그대로, 증거 id 는 새로. 범위 칸에는 조직·서비스·digest 를 그대로 적는다(규칙은 조직만 비교) | 근거 드로어에서 어느 이미지 검사인지 보인다 |
| D-5 | sec-1 133행 "원문 발견 → FAIL 차단"을 "그 이미지는 C-IMG-2 FAIL 로 차단, C-SEC-1 은 조직 경고(WARN)"로 고친다 | D-2 를 SPEC 에 맞춘다 |

## 5. 완료 기준

| AC | 내용 | 확인 |
|---|---|---|
| AC-1 | 깨끗한 빌드 검사 → C-IMG-1 1건 + C-IMG-2 PASS 2건 + **C-SEC-1 PASS 2건**(TRIVY_SECRET·GITLEAKS) | 통합 |
| AC-2 | 비밀 발견 → C-IMG-2 FAIL, C-SEC-1 WARN(근거 줄·전체 개수 같음) | 단위·통합 |
| AC-3 | 보고서 없음·모양 틀림·다른 이미지 → C-IMG-2 UNKNOWN, C-SEC-1 없음 | 단위·통합 |
| AC-4 | 게이트: 빌드 검사로 생긴 C-SEC-1 PASS 로 C-SEC-1 을 예외 없이 통과. 다른 서비스의 C-SEC-1 WARN 은 막지 않는다 | 통합 |
| AC-5 | `ruff`·`ruff format --check`·`mypy app`·전체 `pytest` | 아래 |

## 6. 리스크

- 조직에서 한 이미지라도 비밀이 나오면 7일 동안 C-SEC-1 이 WARN 이다(막지는 않음). 그 이미지는 C-IMG-2 가 막는다.
- 운영 매트릭스·게이트에 C-SEC-1 이 보이려면 새 빌드가 한 번 있어야 한다(재사용 빌드는 다시 모으지 않는다).
- 고객 빌드 경로만 본다. 관제탑 자체 비밀(App 개인키 등)의 전달 경로는 이 증거가 덮지 않는다.
