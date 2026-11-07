# API 20 — 명시 DB쌍 안전 기반 (1단계)

관련: echo-was#116(지원쌍 사전판정·DB 바인딩). 상태: Draft, 운영 활성화 미승인.

## 범위

기존 미커밋 등록표/조회/계획 snapshot 구현을 보존하고 실제 processor의 시작·재개·DB Job 직전 거절과 JUDGE 결과쌍 대조를 연결한다. 기본 OFF, 빈 등록표. 라우팅 executor/Route 동작·환경 adapter·Terraform·실 DB·운영 설정은 변경하지 않는다. 상세 계약과 6방향 현재/목표는 [API 계약](../docs/api/migration-capabilities.md)을 따른다.

## 1단계 인수 조건

- [x] 조직·서비스·환경·배포 UUID에 결합한 서버 관리 등록표, extra=forbid, 중복/다른 scope/미등록 거절.
- [x] appTraffic/dbCutover/disasterPromote/failback·신선도·구체 차단사유 구분. API 실행 준비는 false.
- [x] 실제 create_plan의 snapshot 저장, approve_plan의 재검사·NOT_READY 거절. OFF로 내린 bound 계획도 거절.
- [x] 실제 worker 시작·RUNNING 재개·DB Job 직전 재검사. 유효/변경/다른 tenant/writer 불일치/stale/누락/OFF를 외부 송신 전에 차단.
- [x] 명시쌍 Job 요청·manifest·재부착의 지문 보존을 정적 fixture로 확인. 실제 Kubernetes 시작은 NOT_READY 거절.
- [x] 실제 worker JUDGE의 쌍 대조. ALREADY_WRITER도 receipt 없으면 성공 금지. 정합성 검사 유지.
- [x] DB 없는 선택 시험이 실제 실행됨을 JUnit으로 확인. 전체 회귀·ruff·format·mypy와 기준선 비교는 PR/RESULT의 실제 수를 기준으로 한다.

## 완성으로 표시하지 않는 조건

- [ ] 엔진·이미지·VAP가 명시쌍 env를 실제 소비하고 PAUSE 직전 live writer를 검사.
- [ ] cutover_runs receipt 생산·reader 소비·계획 상세 pair/Job/run ID 공개.
- [ ] AWS/GCP/onprem 6방향 복제·단일 writer·승격·재동기화·복귀 실증.
- [ ] DB 통합 시험의 로컬 PostgreSQL 실행, 서버 admission dry-run, 운영 리허설.

## 검증

기존 RED/GREEN·미커밋 사본을 작업 archive에 보존했다. 신규 시험은 tests/test_migration_pair_worker.py, test_migration_pair_pipeline.py에 두어 DB 통합 skip 속에 숨기지 않았다. test_migration_pair_plan_gate.py의 기존 거절 증거는 보존하면서 valid 생성→실제 승인 NOT_READY와 설정 OFF 회귀를 보완했다.

## 활성화 금지

기능 브랜치 PR까지만. merge/deploy/main push/새 자원·계정·권한·Secret 변경 없음. 실행 준비를 false에서 바꾸려면 짝 repo의 명시쌍 엔진·VAP·receipt 및 별도 운영 승인이 먼저 필요하다.
