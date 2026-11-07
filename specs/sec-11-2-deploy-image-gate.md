# 보안·거버넌스 11-2: 실제 고객 이미지 적용 전 판정

- 작성: 2026-11-07 · S04 · WAS [#31](https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-was/issues/31)의 배포 전 차단 부분
- 노션 태스크: https://app.notion.com/p/40d8bee9ada4804a8397e18e86697c27
- [sec-11-build-scan.md](sec-11-build-scan.md)·[sec-2-3-build-scan-collect.md](sec-2-3-build-scan-collect.md)의 당시 비차단/성공 후 수집 제한을 보완한다.
- DB·API 요청 스키마·의존성·운영 환경 변수 변경 없음. 이관 시간 예산 PR #120과 독립이며 최신 main에서 작성했다.

## 목적과 적용 범위

빌드 성공을 보안 통과로 취급하지 않는다. 실제 DeployExecutor의 apply 직전, 적용할 이미지와 그 빌드의 검사 근거를 확인한다.

| 경로 | 판정 지점 | 차단 시 |
|---|---|---|
| 최초/수동/재사용/AI 재배포 | 실제 apply 직전 | 요청·배포 기록 FAILED, 클러스터와 현재 배포/트래픽 유지 |
| 첫 예비/자동 예비 따라가기 | 각 STANDBY_SYNC apply 직전 | 해당 예비 요청만 FAILED, 성공한 주 환경 유지 |
| 사용자가 요청한 ROLLBACK | 되돌릴 이미지의 새 apply 직전 | 기존 현재 버전 유지 |
| apply 요청 기록은 있지만 응답 저장 전 중단 | 같은 멱등 키로 다시 apply하기 직전 | 이미 적용됐을 가능성이 있어 기존 revert 수행 |
| 실패 복구용 revert | 기존 동작 유지 | 이전 정상 서비스 복구. 새로 요청한 ROLLBACK과 다름 |

실제 실행기(deploy.simulated == false)이면 항상 적용한다. 이관용 SECURITY_GATE_ENABLED나 요청의 simulated 값으로 끌 수 없다. 가짜 실행기는 기존 모의 흐름을 유지하며, 가짜 빌드 결과를 실제 실행기에 넘기면 차단한다. Kubernetes의 DNS·네임스페이스·Secret·workload 변경 전에 검사한다.

## 판정 계약

1. **산출물 일치:** 성공한 실제 Build, 요청과 같은 서비스/저장소/커밋, 배포 기록과 같은 저장소/digest/연결/환경을 확인한다. 불일치는 예외로 우회하지 않는다. 실행기에는 확인한 DeploymentSpec의 digest를 그대로 넘긴다.
2. **검사 범위:** DEPLOY_IMAGE_V1, PREPARATION의 C-IMG-1(이미지 취약점)·C-IMG-2(이미지/소스 시크릿). 기존 evaluate_control의 정책 버전·유효기간·조직/digest·출처·worst-of·예외 정책을 쓴다. 동일 digest의 다른 유효 FAIL을 숨기지 않는다. 정책 버전은 sec-rules-2026.11.06 유지(기존 통제 규칙 변경 없음).
3. **빌드별 완전성:** 해당 Build의 TRIVY_IMAGE·TRIVY_SECRET·GITLEAKS 유효 증거가 각각 있어야 한다. 다른 빌드의 PASS나 CVE 감시 PASS만으로 빠진 소스 검사를 대체하지 않는다. 누락은 UNKNOWN, 유효 FAIL은 계속 FAIL이다.
4. **예외:** 같은 조직/digest, 사람 승인, 미철회·미만료·최대 7일, 자기 승인 정책을 충족하는 기존 예외만 허용한다. FAIL/UNKNOWN을 WARN으로 완화한 기록이며 PASS가 아니다. 적용 직전 최신 예외 상태로 다시 판정한다.
5. **재수집:** 성공 기록 뒤 수집 전에 중단됐거나 과거 source_ref 형식만 있으면 apply 전에 원문을 다시 받는다. 수집 시각은 원문의 scanned_at, 실제 빌드의 저장소/커밋/시각 누락·잘못된 미래 시각은 거절한다. 만료 원문을 지금 검사한 것으로 갱신하지 않는다. 이미 수집된 UNKNOWN/FAIL을 재수집으로 지우지 않는다.
6. **실패:** 비동기 배포 요청이 FAILED / SECURITY_GATE_BLOCKED로 끝난다. 접수 HTTP 202는 그대로다. 실패 코드·로그 코드는 자유 문자열 칸이므로 마이그레이션이 없다.
7. **감사:** SYSTEM/deploy-worker DEPLOY_SECURITY_CHECKED. build/deployment/service/environment id, digest, commit, trigger, checkType, policyVersion, decidedAt, phase, 통제별 원래/유효 결과·사유, findingIds, exceptionIds, missingSources, artifactMatches. 비밀값/원문 없음. 요약 로그 SECURITY_CHECK_PASSED/BLOCKED도 남긴다. 재시도마다 다시 판정하므로 여러 감사 줄이 생길 수 있다.

## 실패 산출물 수집

- 워크플로는 설치/이미지/소스 검사 조건에 always()와 선행 결과를 함께 사용한다. meta.json과 security-scan 업로드도 always(). 검사 실패는 build-result를 성공으로 바꾸지 않는다.
- 설치 또는 해당 검사 단계의 outcome이 success가 아니거나 도구 버전을 확인하지 못하면 부분 JSON이 있어도 해당 보고서는 None → UNKNOWN.
- 실패한 Actions 실행에도 실행 이름/build_id가 맞는 검증된 build-result가 있으면 BuildFailed.artifact로 전달한다. Build는 FAILED와 이미지 칸 NULL을 유지하면서 증거만 모은다. 실패 빌드는 배포에 재사용하지 않는다.
- 최종 digest가 없으면 실패 메타/소스 보고서는 Actions 아티팩트에 보존하지만 이미지 finding으로 정규화하지 않는다. 실행기 강제 종료·산출물 만료·업로드 실패까지 보존을 보장하지 않는다.
- 새 findings.source_ref는 build:{build UUID}:github-actions:{repository}/actions/runs/{run id}. 원래 실행 위치와 검사한 Build를 함께 남긴다.

## 기존 보안·남은 인수

이 프로필은 고객 이미지의 배포 준비 검사다. 아직 없는 공개 주소의 TLS, 상대 환경과 비교하는 비밀 키, 이미지 양쪽 현재 상태, 이관 PRECHECK/DB writer/목적지 자격/POST_SWITCH를 최초 배포 조건에 넣지 않는다. 플랫폼 IAM/감사 자기 시험과 환경 네트워크/TLS 검증도 이 이미지 프로필의 PASS로 간주하지 않는다.

기존 양쪽 레지스트리 digest 검증, digest 고정 배포, SecretManager→Kubernetes Secret 전달·권한·오류 처리는 유지한다. IaC/생성 매니페스트 C-CFG-1 실제 수집과 config_commit 바인딩은 S11 잔여다. #31 전체 완료/운영 인수를 주장하지 않는다. 운영 기존 이미지의 실제 FAIL/UNKNOWN이 새 배포에서 차단될 수 있으므로 수정 빌드 또는 승인된 기한부 예외를 사용한다.

## 검증

- 통합 시험: 검사 누락/취약점/시크릿/다른 digest, 정상 최초+예비, 만료·정책 변경·모의·다른 빌드/부분 출처, 유효·철회·만료·다른 digest 예외, 재사용·ROLLBACK·AI 재배포, 예비 독립 재판정, 수집 중단 복구, apply 응답 전 중단 후 복구, 유효 FAIL 보존, 실패 빌드 증거.
- GitHub 실행기 시험: 실패 실행의 검증된 산출물, 검사 실패/취소/미실행 및 설치 실패 시 부분 보고서 거절.
- 로컬 워크플로 검증: YAML 파싱, 모든 run 블록 bash -n, 이미지 유무에 따른 실패 meta 생성 2종.
- 실제 GitHub OIDC 고객 빌드와 AWS/GCP/onprem 적용 인수는 병합·배포 후 수행한다. 이 브랜치에서 고객 배포를 실행하지 않았다.
