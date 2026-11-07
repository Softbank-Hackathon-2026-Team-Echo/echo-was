# WAS 서비스별 진입점 송신 계약

주이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-infra/issues/112
기존 MainRoute에 있는 service_id/public_host를 외부 요청까지 보존한다. 인프라 SPEC 짝은 `echo-infra/specs/infra-22-service-routing.md`, 상세 API 계약은 이 저장소 `docs/executors-service-routing.md`다.

## 변경 경계

제품 변경은 `app/executors/entrypoint.py`뿐이다. API 인증·조직/서비스 소유권 게이트·DB 모델·schemas/service capability·복제/DB Job·운영 ConfigMap은 바꾸지 않는다. URI 끝의 /v1이 명시적 서비스 모드 스위치이며 기본 기존 URL은 legacy다. v1 실패 시 전역 경로로 자동 재시도하지 않는다.

Traffic/MainRoute 실행기는 target의 서비스가 route와 같은지 먼저 확인한다. 서비스 모드는 저장된 대표 host까지 확인하고 환경 kind/name 대신 환경 UUID를 보낸다. 읽기/쓰기가 같은 서비스 UUID+publicHost 경로를 사용하며 응답 scope·revision·적용 weights를 검사한다. 등록 안 된 source/target은 PUT 전에 거부한다. 서로 다른 환경 UUID가 동일 origin이면 weights-api 등록 시 거부한다.

멱등 성공 캐시는 서비스 UUID/publicHost별로 분리한다. 카운터도 같은 scope의 서비스 전용 cluster만 읽는다. 서비스 모드 구간 summary는 미지원(Unavailable)이며 전역 집계를 서비스 지표로 포장하지 않는다. 실제 서비스 대시보드 Prometheus API는 바꾸지 않는다.

## 검증

- `tests/test_entrypoint_service_scope.py`: 실제 송신 경로/본문, 타 서비스 target 사전 거부, 카운터 UUID scope, 실패 시 global fallback 없음, GCP→AWS PLANNED/FAILBACK 분리.
- `tests/test_entrypoint_local_http_contract.py`: 환경 변수 ECHO_INFRA_WEIGHTS_APP으로 지정한 실제 짝 weights-api를 loopback HTTP로 실행. A GCP100/B AWS100+제3 서비스에서 A만 10/50/100 전환, B·tenant 불변, main route 캐시 분리, 미등록 UUID PUT 없음, 잘못된 host 거절, 손상 primary 파일 복구 후 분리 보존. 외부 socket.connect는 금지한다.
- 기존 전체 회귀와 ruff/mypy를 실행한다. 로컬에는 TEST_DATABASE_URL을 주지 않아 운영 DB나 다른 작업 DB를 조회하지 않는다. DB 시험은 baseline과 같은 skip이며 기존 GitHub PR CI가 독립 PostgreSQL에서 확인한다.

## 운영 경계

기능 브랜치 PR까지만 승인됐다. 자체 merge/deploy는 금지다. 기본 설정·실운영은 바뀌지 않는다. 적용 전 완전한 서비스 등록표, proxy별 동일 hash, Envoy 수용·실트래픽 검증과 별도 운영 승인이 필요하다. 원복은 전체 snapshot 보존 뒤 명시적 legacy 전환을 승인받아야 한다. legacy로 복귀하면 서비스별 격리를 잃는다는 점을 숨기지 않는다.

온프레 TLS·DB writer/복제·지원쌍 capability는 별도 이슈다. 이 PR을 전체 이전 지원 완료로 표시하지 않는다. 신규 cloud 비용 $0·자원 변경 0.
