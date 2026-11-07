# 보안·거버넌스 11-1: 고객 빌드에서 이미지·소스 보안 검사 SPEC

> 2026-11-07 후속: 실제 배포 전 차단·실패 산출물 수집·중단 복구는 [sec-11-2-deploy-image-gate.md](sec-11-2-deploy-image-gate.md)가 이 문서의 당시 비차단 제약을 대체한다. 아래는 최초 구현 결정 이력이다.
- 상태: Draft (2026-11-04, 김형래)
- 작성일: 2026-11-04
- Depth: Standard (빌드 워크플로 단계 추가, 앱 코드·DB·API 변경 없음)
- GitHub 이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/31 (보안·거버넌스 11, 담당 hyeongrae-kim) — 이 문서는 그 첫 부분
- 근거: 보안·거버넌스 1 SPEC(`specs/sec-1-security-controls.md`) §5.1 C-IMG-1·C-IMG-2 / 2-1 정규화(`app/services/security_normalizers.py`) / 2-2 `findings` 표 / `docs/executors.md` "GitHub Actions 빌드"(`build-result` 계약)
- 저장소: `echo-was` · 브랜치 `feat/sec-11-build-scan` (`main` = `42d80d5`에서) · PR로 올린다

## 1. 목표

실제 실행기로 배포·이전되는 이미지의 digest에 묶인 보안 검사 원문을, 그 이미지를 만든 빌드 실행 안에서 남긴다. 판정은 하지 않는다(echo-was 보안 규칙표가 한다). 검사 때문에 빌드가 실패하지 않는다.

## 2. 확인된 사실 (11/4)

- 실제 실행기의 이미지는 이 워크플로가 만든 AR `team-echo-final/echo/app@<digest>`(ECR 같은 digest)다. 시연 서비스 `demo`는 `sha256:b100bc87…`이고, echo-demo-app 공개 이미지(GHCR `3a8211ce…`)와 digest가 다르다. 증거는 digest가 같아야 쓰이므로 이 빌드에서 검사해야 한다.
- 빌드 실행기는 실행이 끝난 뒤 같은 실행의 산출물 `build-result`를 App 토큰으로 받는다(`app/executors/github_actions.py` `_read_result`). 같은 실행에 산출물을 하나 더 두면 같은 방법으로 읽을 수 있다(GitHub App 권한 추가 없음).
- **고객 저장소가 검사를 끌 수 있다**(로컬 재현, Trivy 0.75.0·gitleaks 8.30.1)
  - 저장소 폴더에서 Trivy를 돌리면 그 저장소의 `.trivyignore`·`trivy.yaml`이 적용된다. 시연 이미지 결과가 193건 → 0건이 됐다.
  - gitleaks는 저장소의 `.gitleaks.toml` 허용 목록과 `gitleaks:allow` 주석을 따른다. 심은 가짜 키 2건이 0건이 됐다.
- gitleaks `dir`은 `.git` 폴더를 읽지 않고, 심볼릭 링크는 따라가지 않는다(`--follow-symlinks`가 기본 꺼짐).
- gitleaks JSON은 `--redact`면 `Secret`·`Match`가 `REDACTED`다. Trivy 비밀 결과의 `Match`·`Code`에는 원문 일부가 들어 있다.

## 3. 범위

### 3.1 이번에 한다
- `.github/workflows/customer-build.yml`: `build-result` 업로드 뒤에 단계 5개를 더한다(도구 설치 → 이미지 검사 → 소스 비밀 검사 → `meta.json` → 산출물 `security-scan` 올리기). 기존 단계는 바꾸지 않는다
- 이 SPEC

### 3.2 다음 PR (같은 이슈 #31·#26)
- ~~WAS가 `security-scan`을 읽어 `findings`에 기록~~ → #54·#58 로 이미 병합됐다. **이 PR 을 병합하면 다음 실제 고객 빌드부터 증거가 쌓인다**(게이트는 꺼져 있어 이전에는 영향 없음)
- 같은 digest 주기 재검사(새 취약점), IaC·매니페스트 검사(Trivy `config`, 매니페스트는 WAS가 만들어 대상이 다르다)

### 3.3 하지 않는다
- 검사 결과로 빌드를 막기(차단은 이전 게이트가 UNKNOWN·FAIL로 한다)
- 서명·출처 증명(P2), 클라우드 자격을 빌드 뒤로 옮기기·고객 빌드 전용 CI 신원(인프라 담당과 상의)

## 4. 결정

| ID | 결정 | 출처 |
|---|---|---|
| D-1 | 위치는 `build-result` 업로드 **뒤**, 같은 잡. digest가 확정된 뒤이고, `build-result` 계약과 빌드 성공 판정을 건드리지 않는다 | §2 |
| D-2 | 보안 단계 5개는 모두 `continue-on-error`. 검사·기록·업로드가 실패해도 빌드는 성공 그대로다. 원문이 없으면 echo-was가 그 증거를 UNKNOWN으로 보고 이전을 막는다 | #25 SPEC(UNKNOWN 차단) |
| D-3 | 이미지 대상은 `${AR_REPO}@${build-result의 image_digest}`를 레지스트리에서 읽는다(`--image-src remote`, 위 `docker login` 자격). ECR은 같은 digest가 이미 검증돼 다시 검사하지 않는다 | §2 |
| D-4 | 저장소가 검사를 끄지 못하게 한다. Trivy는 `runner.temp`에서 돌리고 빈 `--ignorefile`을 넘긴다. gitleaks는 우리 설정(`[extend] useDefault = true`)·빈 `--gitleaks-ignore-path`·`--ignore-gitleaks-allow` | §2 재현 |
| D-5 | 비밀값 원문은 산출물에 남기지 않는다. gitleaks `--redact`, Trivy `Secrets[].Match`·`Code`는 지운다(정규화는 규칙 ID·대상·줄만 쓴다) | #45 |
| D-6 | 도구는 버전과 sha256을 고정한다. Trivy 0.75.0, gitleaks 8.30.1(2026-03-21 배포). 값은 각 릴리스의 checksums 파일 | 공급망 |
| D-7 | 산출물 `security-scan`(7일, `build-result`와 같음): `trivy-image.json`(취약점·비밀), `gitleaks.json`(소스 비밀), `meta.json` | — |
| D-8 | `meta.json` 칸: `schema_version`(1), `build_id`, `image_digest`, `ar_repository`, `repository`, `commit_sha`, `run_id`, `run_attempt`, `workflow_sha`, `scanned_at`, `tools_outcome`, `trivy{version, vulnerability_db_updated_at, outcome}`, `gitleaks{version, outcome}`. 키는 `build-result.json`처럼 snake_case | 이슈 완료 기준 2 |
| AD-1 | `gitleaks:allow` 주석을 무시해 오탐이 늘 수 있다. 오탐은 기한부 예외(사람 승인·7일)로 다룬다 | AI-default |
| D-9 | GCP 인증 단계는 **자격 파일을 만들지 않는다**(`create_credentials_file: false`). 기본값이면 `$GITHUB_WORKSPACE`(= 고객 소스 폴더 = `docker build` 입력)에 OIDC 요청 토큰이 든 `gha-creds-*.json` 이 생겨 ① gitleaks 가 모든 빌드에서 비밀로 잡고(C-IMG-2 오탐 FAIL) ② 고객 Dockerfile 이 그 파일을 이미지에 복사하거나 빌드 중 읽어 CI 신원 권한을 얻을 수 있다. 뒤 단계는 `access_token` 출력(docker login)만 쓰고, Trivy 는 그 로그인 자격으로 이미지를 받는다 | #53 리뷰(Saccharine1211) |
| AD-2 | 검사 시간은 로컬(캐시 있음) 3초, CI는 DB·이미지 받기 포함 1분 안팎으로 본다(**미측정**, 병합 뒤 첫 실행에서 잰다). 원터치 배포가 그만큼 늘어난다 | AI-default |

## 5. Acceptance criteria

| AC | 확인 | 방법 |
|---|---|---|
| AC-1 | 기존 단계·`build-result` 계약이 그대로다 | diff가 헤더 주석 2줄 + GCP 인증 단계 한 줄(`create_credentials_file: false`, D-9) + 끝에 단계 추가뿐 |
| AC-2 | 같은 digest의 검사 원문 3개가 산출물에 남는다 | 워크플로 `run` 블록을 그대로 꺼내 로컬에서 실행(GHCR 시연 이미지 + echo-demo-app `0ff0d44`) |
| AC-3 | 저장소의 `.trivyignore`·`trivy.yaml`·`.gitleaks.toml`·`gitleaks:allow`로 결과를 숨길 수 없다 | 로컬 재현: 193건·가짜 키 2건이 그대로 나옴 |
| AC-4 | 비밀값 원문이 산출물에 없다 | 심은 가짜 키 문자열 검색 0건 |
| AC-5 | 산출물이 정규화에 그대로 들어간다 | `normalize_trivy_image` → C-IMG-1 FAIL(5건), `normalize_trivy_image_secrets`·`normalize_gitleaks` → C-IMG-2 PASS, 다른 digest → UNKNOWN(`ARTIFACT_MISMATCH`) |
| AC-6 | 도구 체크섬이 맞는다 | 릴리스 checksums 값과 받은 파일의 sha256 비교 |
| AC-7 | 실제 빌드에서 산출물·걸린 시간 | **병합 뒤 첫 고객 빌드에서 확인**(빌드 신원은 main에서만 통과해 브랜치로 미리 돌릴 수 없다) |

## 6. 리스크

- **R-a 원터치 배포가 검사 시간만큼 늘어난다** → 병합 뒤 첫 실행에서 재고, 길면 Trivy DB 캐시를 더한다.
- **R-b 시연 중 Trivy DB 받기 실패** → 그 빌드의 이미지 증거가 UNKNOWN이 되어 이전이 막힌다. 리허설 체크리스트에 산출물 확인을 넣고, 실패하면 빌드를 다시 돌린다.
- **R-c 클라우드 자격을 고객 빌드보다 먼저 받는 기존 구조**는 그대로다(이 PR 범위 밖, 인프라 담당과 상의). 다만 작업 폴더에 남던 GCP 자격 파일은 D-9 로 없앴다. AWS 자격은 환경변수라 `docker build` 에 넘어가지 않는다.
