# 보안 7 — 환경 신원·내부 TLS 자동 증거

관련: WAS #131·#26, infra #23·#24. 별도 브랜치 구현이며 병합·운영 적용은 하지 않았다.
검증 결과는 이 문서의 마지막 절과 작업 공간 SECU_PROGRESS.md를 함께 본다.

## 현재 상태와 범위
11/8 WAS #143의 사람 점검 API가 main에 합쳐졌다.
[infra #134 완료 보고](https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-infra/issues/134#issuecomment-6715422675)에 따르면
3환경×2통제 수동6건이 이미 등록됐다. AWS TLS FAIL과 승인 예외는 별개이며
자동 수집 완료의 근거가 아니다. 기존 수동 등록을 중복 구현하지 않는다.
이번 구현은 이후 주기 관측을 담당하며 시연 echo-demo에 쓰지 않는다.

## 동작과 신뢰 경계
관리자가 조직/환경 UUID별 신원·전체 경로 목록을 SECURITY_ENVIRONMENT_PROFILES로 지정한다.
고객·AI 입력이나 Terraform 파일만으로 운영 PASS를 만들지 않는다.
환경 프로필은 실제 환경 전체를 자동 발견하지 않으므로, 목록의 완전성은 운영 인수 조건이다.

- 두 스위치가 true인 별도 `python -m app.worker.environment_security` 프로세스만 수집한다. 일반 배포 worker에서는 이 수집기를 시작하지 않는다.
- 기본 OFF, 시작 즉시 및 기본900초 간격(60~900초), 자동 증거 TTL1800초.
- 조직/환경/프로필·환경 config·secretRef 지문/정책/출처/원래 관측 시각을 저장한다.
- 환경 잠금 후 같은 자동 스트림의 과거 행만 superseded 처리한다. 늦게 끝난 옛 수집은 버린다.
- 수동 기록·다른 스트림 FAIL은 교체하지 않는다. 이력 조회는 과거 행을 보존한다.
- matrix/compare/이관 gate는 현재 설정 지문을 확인한다. 선택 통제의 비차단 정책을 유지한다.
- 판정에 비밀 원문·정책 원문·DB 접속 문자열·토큰을 넣지 않는다. 고정 ID/이유 코드만 저장한다.

| 출처 | 관측·판정 | 한계 |
|---|---|---|
| KUBERNETES_API | KSA/Deployment/전체 RoleBinding·ClusterRoleBinding, namespace·허용 동작 비교 | RBAC 외 authorizer·노드/클라우드 권한을 증명하지 않음; 별도 대상 등록 필요 |
| CLOUD_API | AWS IAM inline/attached policy+EKS access, GCP project/folder/org 상속·역할 전개 | SCP/resource policy/deny/PAB 등 전체 실효권한 미입증은 UNKNOWN; 클라우드 IAM 전체 PASS를 보장하지 않음 |
| TLS_PROBE | 관리 HTTPS 실제 접속, Envoy active cluster CA/SAN/SNI+TLS 접속 | 공용 평문 FAIL·사설 평문 WARN, 주소/관측 불명 UNKNOWN. SDS/조건부 transport/별도 CA 경로 미지원 |
| DATA_ENGINE | 실제 PostgreSQL/PgBouncer 세션의 TLS·연결 수 | 평문/구버전 WARN, 무연결·권한 거부 UNKNOWN. TLS 세션만으로 verify-full을 입증하지 않음 |

## 설정
[3환경 형식 예제](examples/environment-security-profiles.json)는 전부 가상 UUID·주소다.
실제 UUID, 실행기/검사기/앱·컷오버/노드 신원, 검토한 allowlist,
서비스별 origin·관리 API·복제·앱→풀·풀→DB를 담당자와 빠짐없이 매핑한 뒤 활성화한다.
API·worker·전용 collector는 같은 프로필을 받아야 한다. JSON 배열을 환경변수 값으로 제공한다.

entrypoint 대상은 HTTPS **고정 IP origin**(경로 없음)과 신뢰 CA, 별도 token_ref를 요구한다.
env 참조는 기존 형식 env:ECHO_SECRET_* 이며 운영에는 조직 접두사를 쓴다.
기존 HTTP 관리 포트에 토큰을 보내지 않는다.
서비스 라우팅 모드는 route=svc_<서비스 UUID의 hex>_<환경 UUID의 hex>(69자)를 사용한다.
legacy 모드만 aws/gcp/onprem 이름을 쓴다. 관측 요청은 GET /security/transport?route=... 이다.
목록에 없는 route는 거절하며 global route로 재시도하지 않는다.
snapshot_revision은 관측 API/DB 관측기 revision과 같아야 한다. 경로/설정 변경 시 함께 올린다.
DB 대상은 echo-security namespace의 보호 ConfigMap만 읽는다.
운영자가 이 ConfigMap의 쓰기 권한을 관측기만 갖도록 바인딩해야 한다.

## 정책 버전 전환 — 기본 OFF여도 영향 있음
정책은 sec-rules-2026.11.06 → sec-rules-2026.11.08이다.
자동 수집 OFF는 정책 버전 상승을 막지 않는다. 예전 정책 증거는 판정에서 거절된다.
**따라서 시연 중 단독 배포하지 않는다.** 기존 기록을 삭제하거나 새 시각/PASS로 복사하지 않는다.

1. API/worker 동시 전환과 보안 증거 재생성 시간을 운영 담당이 확정한다.
2. Alembic 4f8c2d1b7e91(parent1d5d0b3b9598)을 적용한다. 4개 nullable 열이며 기존 행은 유지한다.
3. 현재 실제 배포 이미지 목록을 아래 plan 명령으로 확인한다.
   --apply는 원래 repo/commit/digest/scanned_at을 확인한 산출물만 재정규화한다.
   만료·원문 누락은 UNKNOWN/차단으로 남기며 TTL을 늘리지 않는다.
4. 기존 TLS·플랫폼 자기 시험의 새 정책 증거와 C-AI-1 실제 PRECHECK 등 필수 통제를 재확인한다.
   과거 PRECHECK를 새 실행으로 위조하지 않는다. 필요한 실제 재실행은 별도 담당 인수다.
5. **수동6건은 점검자가 재확인 후 기존 API로 새 정책 기록을 남겨야 한다.**
   AWS TLS FAIL을 PASS로 바꾸지 않는다. 기존 예외의 범위·만료와 감사 근거도 재확인한다.
   자동 수집기가 수동 기록을 재작성하지 않는다.
6. 관측기/최소 읽기 권한/HTTPS/CA 및 3환경 프로필을 적용한 뒤 최초 수집을 검증한다.
   현재 정책 수동 FAIL·다른 스트림 FAIL 보존, 권한 거부·만료·설정 변경·조직 격리를 확인한다.
7. 영향 배포/매트릭스/비교/이관 판정의 인수를 완료하기 전 운영 완료로 표시하지 않는다.
   되돌릴 때도 코드 정책과 증거 버전을 함께 판단하며 DB 이력을 삭제하지 않는다.

```sh
uv run python -m app.worker.refresh_security_evidence --organization-id UUID
# 아래 명령은 findings를 쓴다. 이번 검증에서는 운영에서 실행하지 않았다.
uv run python -m app.worker.refresh_security_evidence --organization-id UUID --apply
```

## 검증 및 남은 인수
WAS 전체 pytest에는 신규 모델의 migration upgrade/downgrade/check·pg_catalog 대조,
수동 API·2회 자동 갱신 후 FAIL 보존, 오래된 정책 거절, 3환경 matrix/compare,
실행 gate·digest/원본 산출물 재수집·계약/신원 읽기 오류를 포함한다.
외부 API는 Stubber/MockTransport로 검사하고 실제 AWS/GCP 운영 권한 시험과 구분한다.
infra는 실제 격리 Envoy·PostgreSQL·PgBouncer와 코드 회귀를 검증한다.

feat/sec-observer-runtime에서는 별도 workload 신원으로 실행한다. reader.aws_role_arn/reader.onprem_token_ref만 선택하며 실행기 자격 fallback을 금지한다. AWS/GCP는 metadata의 GSA 이메일을 reader.google_service_account와 대조한다. 실제 IAM/RBAC 생성·바인딩과 유효 권한은 운영 인수 사항이다.

운영 인수: 동옥/지민은 정확한 신원/읽기 권한과 HTTPS/CA·서비스 route,
도현은 DB/PgBouncer 통계 계정/실제 연결 매핑/관측기 적용,
형래는 프로필·판정·정책 전환 검증, 도영은 화면 확인을 담당한다.
팀원에게 새 운영 적용을 약속하지 않는다. 실제 3환경 자동 결과는 아직 등록하지 않았다.

### 2026-11-08 11:14 KST 직접 검증

WAS main ca918380 기반 최종 전체 **3,109 passed / 실패0 / skip0**(244.55초).
ECHO_INFRA_WEIGHTS_APP에 같은 작업 브랜치의 실제 weights API를 지정했다.
마이그레이션59건(왕복·check·DDL 대조)과 수동 API·matrix/compare·배포/이관 게이트를 포함한다.
ruff/format444파일/mypy245파일 PASS. 3환경 가상 설정 JSON 검증 PASS.
infra main87a64cff 기반 weights32·observer9·journal14 PASS.
실제 Envoy1.39.6 정상 TLS/잘못된 hostname/HTTP·TLS 전환·관측 전후 weights 불변 PASS,
PG17 집계/권한 거부와 PgBouncer1.26.1 stats 관측/PAUSE 거부 PASS.
Docker 두 이미지 build PASS. Terraform fmt/validate PASS(기존 provider 경고), tflint는 미설치로 실행하지 못했다.
자동 수집의 실제 운영 적용·3환경 IAM API 호출·CNI/RBAC 실집행·브라우저 인수와 구분한다.

## 전용 runtime 인계 (feat/sec-observer-runtime)

- 일반 배포 worker는 더 이상 환경 수집기를 시작하지 않는다. API/worker에는 프로필 참조만 동일하게 공급하고, 검사 전용 GSA·AWS role·onprem 토큰·WAS DB 로그인은 별도 workload에 둔다.
- `python -m app.worker.environment_security --preflight`: 환경 존재·전용 자격 참조와 조직별 이전 정책 유효 증거 수를 읽는다. `configurationReady`는 형식·환경 연결 준비만 뜻하며, 실제 권한·CA·네트워크·정책 인수 성공이 아니다. 비밀값을 읽거나 외부 API/DB 쓰기를 실행하지 않는다. `policyTransition=REVIEW_REQUIRED`, `operationalAcceptance=NOT_VERIFIED`를 유지한다.
- `--once`: 한 주기를 실행한다. 옵션 없으면 15분 주기 loop, SIGTERM 정상 종료. 설정 누락/권한 실패는 UNKNOWN으로 남고, 다른 환경 처리를 계속한다. UNKNOWN이나 저장 실패 환경이 있으면 단발 명령은 종료 코드1을 반환한다. 관측된 WARN/FAIL은 수집 성공과 구분하며 그대로 보관한다.
- infra의 `collector-db.sql`은 별도 역할에 findings INSERT/이력 교체와 환경 SELECT를 허용한다. PostgreSQL 행 잠금을 위해 environments.updated_at 한 열의 UPDATE 권한이 필요하다. config/secret_ref/배포 실행/기존 판정 수정 권한은 주지 않는다. 이 역할은 증거 작성 신원이므로 그 로그인도 별도 Secret으로 보호한다.
- 새 DB schema 변경은 없다. 앞선 자동화 migration 4f8c2d1b7e91 적용은 여전히 필요하다. 기존 정책 전환·사람 점검 재확인·실환경 3곳 인수 절차는 그대로 남는다.

검증 재현 시 `TEST_DATABASE_URL`(폐기용 *_test DB), `ECHO_INFRA_WEIGHTS_APP`(짝 app.py 절대 경로), `ECHO_INFRA_OBSERVER_DIR`(짝 security/environment-observer 절대 경로)를 설정해 pytest를 실행한다. 경로를 빼면 교차 저장소 계약 시험이 skip되므로 최종 인수에서는 경로를 반드시 준다.
