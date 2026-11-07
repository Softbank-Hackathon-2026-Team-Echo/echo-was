# 작업② 검증 인수 — 2026-11-07

검증 구현 head: 7393fba2efc2c4d42d25f6559c365f6f48c1cd72. 기존 중단본 보존 commit: 0a522cbb90f68c37699746d0fe468101270ee688. 통합 기준 main: 045bd9bf8814b1e4b85609a24e4f3619a921de07. 이 문서 commit은 제품 코드를 바꾸지 않는다. 원장: #130(후속 읽기 상태), OPEN/Project4 In Progress 유지. 부모 독립검수·형래 계약검토·도영 소비검토는 아직 미완이다.

## 실제 실행 결과

- 격리 PostgreSQL 17.11_1, loopback 127.0.0.1:56472. 기존 ② 소유 PID88860/data echo-state-pg-1107을 인수했다. role echo_state, clone hash의 자체 _test DB만 제품 시험 대상으로 사용했다. 표준 fixture의 postgres catalog/lock/시험DB 생성은 동일 서버 내부 관리 작업이다.
- 최신 main 별도 archive 기준선 전체: 2881 PASS, 실패0/건너뜀0, 350.55초.
- 통합 후보 전체: 2913 PASS, 실패0/건너뜀0, 397.85초. 기준선 시험 삭제0, 추가32, 새 실패0(JUnit node ID 집합 비교).
- 관련 시험 132 PASS. 기존 green-06의 101 PASS를 재사용해 현재 결과로 부르지 않았다.
- 이전 candidate-full-01의 4 FAIL은 additive literal 기대값 보완 이전 결과다. `test_patch_approve_with_apply_enabled`, `test_apply_patch_round_trip`, `test_apply_run_detail_and_events_for_applied_run`, `test_openapi_precheck_detail_documents_kind_and_apply` 모두 현재 전체 결과에서 PASS를 확인했다. 이전 실패 로그는 그대로 보존했다.
- ruff check 전체 PASS, format check 418파일 PASS, mypy app 230소스 PASS.
- 원본 probe_backend.py를 수정하지 않고 최신 main/후보에서 각 7 probe PASS. 이는 원래 결함/경계 관측기가 실행됐다는 뜻이며 자동복구 완성 증거가 아니다. 중단·원격 응답 손실·INTERNAL_ERROR 재개 공백은 ③ 범위로 남는다.
- 원본 red-01~06, green-01~06, 이전 전체 실패/기준선/probe 결과의 SHA256 불변을 검증했다. 최초 source 15파일도 별도 원형·SHA256 보존 후 commit했다.

## 재현 명령과 한계

기존 venv(Python3.13.16)/uv frozen 의존성을 사용했다. 실행 환경에서 물려받은 PYTHONPATH가 Hermes의 Python3.14 패키지를 가리켜 첫 pytest/mypy가 import 실패했다. 설치/정책 변경 없이 `PYTHONPATH=.`로 프로젝트 경로를 명시해 정상 재실행했다. 첫 실패 로그도 보존했다.

후보 전체 명령: `env PYTHONPATH=. DATABASE_URL=postgresql+asyncpg://test:test@127.0.0.1:1/test TEST_DATABASE_URL=postgresql+asyncpg://echo_state@127.0.0.1:56472/echo_test uv run --frozen pytest -q --junitxml=<evidence>/resume-candidate-full.xml`.

TEST_DATABASE_URL의 echo_test는 fixture nominal 이름이며 실제 접속 제품 DB는 `echo_f086ccd6[_gwN]_test`다. echo_test 자체는 존재하지 않아 최초 psql 확인이 실패했고, fixture hash 이름으로 identity를 확인한 뒤 시험했다. 다른 PG 서버·운영 DB 접속은 하지 않았다.

로컬 HTTP 검증은 ASGI transport + 실제 PG이며 공개 네트워크의 운영 HTTP E2E가 아니다. Github writer/LLM/클라우드는 합성 대역이다. 실제 고객 저장소·GitHub App 쓰기·운영 변경·main 병합·배포는 실행하지 않았다. 자동복구 worker/스위치/임의 SLA·deadline·schema revision도 추가하지 않았다.

## ③ 착수 게이트

부모가 PR 정확 최종 head를 독립 검수해 인계한 뒤 별도 작업으로 시작한다. source branch를 동시에 수정하지 않는다. base는 최종 PR head이며 위 구현 검증 head나 과거 출발 main으로 임의 후퇴하지 않는다.

인터페이스: `DiagnosisService.redeploy_after_apply(apply_run_id: UUID) -> AgentRun | None`, 열린 transaction 없이 호출. `ApplyCommitService.prepare_redeploy_commit`과 기존 생성 배포의 job worker를 재사용하되 stale claim/REPO_WRITE_FAILED는 원격 읽기 정합성 확인 전 맹목 재송신 금지. terminal result 409는 재개 API가 아니다. INTERNAL_ERROR/null ID 멱등 종료도 이 작업에서 바꾸지 않는다.

소유권: docs/ai-followup/FILE-OWNERSHIP.md의 서비스/serializer/enum은 ② 검수 종료까지 동시수정 금지. ③는 부모의 명시적 인수 뒤 서비스 파일을 넘겨받는다. MigrationPlanService/journal/DB identity/reader/복제·매니페스트/공용worker 전역 동작은 이 계약으로 소유권을 얻지 않는다.

원본 로그·JUnit·hash manifest는 로컬 인계 폴더 `archive/softbank-hackathon-2026/night-1107/ai-redeploy-followup-analysis/server-state-build/evidence/`에 있다. CI는 Draft PR의 새 정확 head 결과로 별도 확인하며 로컬 성공을 CI 성공으로 부르지 않는다.
