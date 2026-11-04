# 보안·거버넌스 2-3: 빌드 보안 검사 원문 → findings 기록 SPEC

- 상태: Draft (2026-11-04, 김형래)
- 작성일: 2026-11-04
- Depth: Standard (실행기 메서드 하나 + 수집기 + 배포 처리기 호출 한 곳, DB·API 변경 없음)
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/26 (보안·거버넌스 2) — 수집 실행의 첫 부분. #31(보안·거버넌스 11)과 이어짐
- 근거: 보안·거버넌스 11-1 SPEC(`specs/sec-11-build-scan.md`, PR #53)의 `security-scan` 아티팩트 / 2-1 정규화 / 2-2 `findings` 표 / `docs/executors.md` "GitHub Actions 빌드"
- 저장소: `echo-was` · 브랜치 `feat/sec-2-3-build-scan-collect` (`main` = `42d80d5`에서) · PR로 올린다

## 1. 목표

실제 빌드가 성공하면, 같은 실행이 남긴 보안 검사 원문(`security-scan`)을 받아 그 이미지 digest의 증거(C-IMG-1·C-IMG-2)로 `findings`에 남긴다. 그러면 보안 매트릭스와 이전 게이트가 실제로 배포·이전되는 이미지의 증거를 읽을 수 있다. 증거를 못 모아도 배포는 계속한다.

## 2. 확인된 사실 (11/4)

- 빌드 실행기는 실행이 끝나면 같은 실행의 `build-result` 아티팩트를 App 설치 토큰으로 받는다(`_read_result`). 같은 방법으로 다른 이름의 아티팩트도 받을 수 있다. 권한 추가는 없다.
- 배포 처리기는 빌드 결과가 `BuildSucceeded`일 때 빌드 행을 `RUNNING → SUCCEEDED`로 바꾼다(조건부 UPDATE, 반영 여부가 bool). 재사용 빌드는 이 경로를 다시 지나지 않는다.
- 가짜 빌드 실행기에는 검사 원문이 없다.

## 3. 범위

### 3.1 이번에 한다
- `app/executors/base.py`: `BuildSecurityScan`(원문 묶음), `BuildSecurityScanReader`(읽기 Protocol, `runtime_checkable`). `BuildExecutor` Protocol은 바꾸지 않는다
- `app/executors/github_actions.py`: `read_security_scan`. 아티팩트 받기를 `_artifact_archive(run_id, name)`로 묶어 `build-result`와 같이 쓴다(동작 그대로)
- `app/services/security_scan_collector.py`: `BuildSecurityScanCollector.collect(build_id, reader)`
- `app/services/security_finding_service.py`: `record_all`(같은 원문의 증거 여러 건을 한 트랜잭션으로)
- `app/services/deploy_request_processor.py`: 빌드 `SUCCEEDED` 반영 직후 수집기 호출 한 곳
- 시험·문서(`docs/executors.md`, CLAUDE.md 구조)

### 3.2 다음 PR
- 같은 digest 주기 재검사, TLS 접속 시험 주기 실행(실행 위치는 API 서버 담당과 정함). TLS는 우리가 발급한 공개 호스트만 받는다(공인 IP로 확인한 그 IP에만 접속, 사설 주소는 NA, 의심 주소는 UNKNOWN)
- 매트릭스 API(#28), 이전 게이트(#29)

### 3.3 하지 않는다
- 증거 때문에 배포를 막거나 늦추기(차단은 이전 게이트)

## 4. 결정

| ID | 결정 | 출처 |
|---|---|---|
| D-1 | 부르는 곳은 배포 처리기가 빌드를 `SUCCEEDED`로 **반영한 직후**(트랜잭션 밖), 반영됐을 때만 한 번. 재사용 빌드는 다시 모으지 않는다(같은 digest의 증거가 이미 있다) | §2 |
| D-2 | 원문 읽기는 `BuildSecurityScanReader`를 갖춘 실행기만. `BuildExecutor`를 바꾸지 않아 가짜 실행기·다른 구현은 그대로다 | AI-default |
| D-3 | 원문의 `build_id`가 빌드 id와 다르거나 digest가 빌드 digest와 다르면 버리고 경고만 남긴다 | 다른 빌드의 증거 금지 |
| D-4 | 증거 3건: Trivy 이미지 → C-IMG-1(`TRIVY_IMAGE`), Trivy 비밀 → C-IMG-2(`TRIVY_SECRET`), gitleaks → C-IMG-2(`GITLEAKS`). 범위 = 조직·서비스·digest. 보고서가 없으면(검사 실패) 그 증거는 UNKNOWN(`REPORT_INVALID`) | #45 정규화 |
| D-5 | 수집 시각 = 원문의 `scanned_at`. 없거나 우리 시계보다 5분 넘게 앞서면 지금 시각 | AI-default |
| D-6 | 실패해도 예외를 올리지 않는다. 원문 읽기는 `None`(예외 없음), DB 오류는 `SQLAlchemyError`만 잡아 로그를 남기고 0건. 증거가 없으면 판정이 UNKNOWN으로 본다 | 배포 우선 |
| D-7 | 원문은 신뢰하지 않는 입력으로 다룬다. zip 안 파일은 풀린 크기 32MB 상한, JSON만, 도구 버전은 `^[0-9A-Za-z.+-]{1,32}$`일 때만 쓴다. 근거 줄 상한(50)과 비밀값 원문 버리기는 정규화가 한다 | — |
| AD-1 | `source_ref` = `github-actions:{빌드 저장소}/actions/runs/{run id}` | AI-default |

## 5. Acceptance criteria

| AC | 확인 | 시험 |
|---|---|---|
| AC-1 | `read_security_scan`이 meta·보고서를 돌려주고 빌드 성공 판정은 그대로다 | `test_read_security_scan_returns_reports_and_meta`, `…_does_not_change_build_result` |
| AC-2 | 아티팩트 없음·meta 틀림·크기 초과·서버/인증 오류 → `None` 또는 그 칸만 `None`, 예외 없음 | `test_read_security_scan_*` |
| AC-3 | 빌드 성공 직후 같은 digest의 증거 3건이 남는다(C-IMG-1 FAIL, C-IMG-2 PASS×2, 출처·수집 시각·서비스) | `test_build_success_records_image_findings_for_built_digest` |
| AC-4 | 재사용 빌드는 다시 모으지 않는다 | `test_reused_build_is_not_collected_again` |
| AC-5 | 다른 빌드·다른 digest의 원문은 버리고 배포는 성공한다 | `test_scan_for_other_build_or_digest_is_ignored_and_deploy_continues`, `test_scan_for_other_digest_is_ignored` |
| AC-6 | 보고서가 없으면 UNKNOWN 증거 | `test_missing_reports_are_recorded_as_unknown` |
| AC-7 | 원문이 없거나 가짜 실행기면 아무것도 남기지 않고 배포는 성공한다. 미래 검사 시각은 지금으로 | `test_no_scan_records_nothing_and_deploy_succeeds`, `test_fake_build_executor_records_nothing`, `test_scan_time_ahead_of_our_clock_uses_now` |

## 6. 리스크

- **R-a 원문 받기(약 1MB)만큼 배포 단계가 늦어진다** → 같은 실행 안의 아티팩트라 수 초 안. 길면 따로 할 일(JobKind)로 뺀다(API 서버 담당과 정함).
- **R-b #53이 리뷰에서 아티팩트 모양을 바꾸면** 이 PR의 파싱을 함께 고친다. 모양이 맞지 않으면 `None`이라 배포는 영향이 없다.
- **R-c 빌드 뒤 오래 지나면 증거가 만료된다** → 주기 재검사(다음 PR) 전까지는 다시 빌드해야 한다.
