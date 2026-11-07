# 서비스별 트래픽 격리 계약 (v1)

주이슈: https://github.com/Softbank-Hackathon-2026-Team-Echo/echo-infra/issues/112
범위: 로컬 제품 코드 + PR. 병합·배포·운영 활성화·Terraform apply 없음. DB 이전 지원 계약이 아니다.

## 활성화 / 신뢰 경계

weights-api SERVICE_ROUTING_ENABLED=1일 때만 v1 경로·서비스 RDS 사용. 기본 OFF는 명시적 legacy 단일 전역 모드다. ON에서는 /weights·/summary·/counters·/status 전역 경로를 거절한다. health·metrics는 유지한다. 보조는 읽기만 한다.
WAS는 ENTRYPOINT_BASE_URL의 마지막 경로가 /v1인 경우에만 v1을 사용한다. 기존 URL은 legacy를 유지한다. /v1의 404·409·503 등 실패를 global PUT으로 재시도하지 않는다. 운영 ConfigMap은 이번 PR에서 바꾸지 않는다.

SERVICE_ROUTES_FILE은 운영자가 WAS의 조직별 services.public_host, service_environments.environment_id, 검증된 origin 접속정보를 대조하여 만드는 읽기 전용 등록 JSON이다. 공개 HTTP로 등록/수정하지 않는다. 임의 Host 헤더나 FE가 origin을 등록할 수 없다. Bearer WEIGHTS_TOKEN은 신뢰된 WAS/운영자 전용이다. tenant에 이 토큰을 주지 않는다. 조직 인증·서비스 소유권 검사는 기존 WAS 조직 API/worker 경로 그대로다. UUID는 전역 서비스 신원이며 publicHost를 반드시 함께 일치시킨다.

등록 파일 형식:
{"services":[{"serviceId":"서비스 UUID","publicHost":"shop-12345678.echoport.uk","environments":{"환경 UUID":{"host":"검증된 ingress DNS 또는 IP","port":80}},"initialWeights":{"환경 UUID":100}}]}

서비스·환경은 canonical UUID, publicHost는 소문자 ASCII DNS 이름(스킴·경로·포트·와일드카드 없음). 서비스 UUID/publicHost 중복 거절. 한 서비스 안에서 같은 host:port를 두 환경 UUID에 중복 등록하면 거절. 서로 다른 서비스가 같은 shared ingress origin을 쓰는 것은 허용: 별도 클러스터 + 정확한 publicHost로 격리한다. 서비스 환경 UUID는 종류(aws/gcp/onprem)로 대체하지 않는다. 등록 시 빠진 initialWeights 키/합계 오류 거절. 등록 변경은 재시작·별도 검증이 필요하며 자동으로 route를 삭제하지 않는다.
HTTP origin만 지원한다. TLS 터널/SNI가 필요한 onprem 경로는 이 계약만으로 지원 완료가 아니다. 검증된 HTTP 내부 ingress를 등록하는 확장 지점은 열려 있다. 운영에 없는 onprem route를 자동 생성하거나 supported:true로 노출하지 않는다. DB writer/복제·Cloudflare 입구 우선과 독립이다.

## 내부 API (FE 직접 호출 금지)

GET /v1/services/{serviceId}/weights?publicHost={정확한 publicHost}
응답 200: {"serviceId":"UUID","publicHost":"host","revision":0,"weights":{"환경 UUID":100}}

PUT 같은 경로
본문: {"publicHost":"host","expectedRevision":0,"weights":{"환경 UUID":90,"환경 UUID":10}}
응답: 같은 scope + 증가한 revision + 적용 weights. 본문/쿼리 host가 다르면 거절. 모든 등록 환경 키 필수, 정수 0~100(bool 불가), 합 100. expectedRevision 불일치 409이며 조용한 덮어쓰기/자동 GET 재시도 없음.

GET /v1/services/{serviceId}/counters?publicHost=...
응답: {"serviceId","publicHost","envs":{"환경 UUID":{"requests":정수,"errors_5xx":정수,"hosts":정수}}}
서비스별 전용 CDS cluster를 읽는다. summary는 서비스 scope를 확인한 뒤 501(미지원)로 명시하며 legacy 전체 집계를 서비스 지표로 포장하지 않는다. 기존 서비스 대시보드 Prometheus API는 바꾸지 않는다.

GET /v1/snapshot: 신뢰된 보조용 전체 서비스 snapshot(schemaVersion, registryHash, generation, services). 등록표 해시가 다르거나 누락·잘못된 scope·rollback generation이면 전체 갱신을 거절하고 마지막 정상 구성을 유지한다. 보조 PUT은 409 READ_ONLY_SECONDARY.

오류: JSON {"error":"CODE"}. 401 인증 없음, 503 토큰 미설정/저장 실패, 404 SERVICE_NOT_REGISTERED, 409 SERVICE_SCOPE_MISMATCH/REVISION_CONFLICT/LEGACY_DISABLED/READ_ONLY_SECONDARY, 400 INVALID_WEIGHTS/INVALID_REQUEST. WAS는 ENTRYPOINT_SERVICE_NOT_REGISTERED / ENTRYPOINT_SCOPE_MISMATCH / ENTRYPOINT_REVISION_CONFLICT / ENTRYPOINT_READ_ONLY_SECONDARY를 구분한다. 미등록 환경은 송신 전 ENTRYPOINT_ENV_UNMAPPED, 다른 서비스 target은 ENTRYPOINT_SCOPE_MISMATCH. 잘못된 응답 scope도 거절한다. legacy 503 매핑은 기존 ENTRYPOINT_TOKEN_MISSING을 유지한다.

## FE 의존 필드 / 변경 없음

기존 서비스 응답 urls.main/url, 환경 id·trafficWeight·trafficAppliedAt, 계획 mode(PLANNED/FAILBACK), 실패 code를 그대로 쓴다. 새 FE 쓰기 API/임의 host 입력은 추가하지 않는다. 서비스별 지원쌍 API는 echo-was#116(지원쌍·DB 대상 결합) 작업자 영역이다. 이 작업만으로 전체 이전 지원을 표시하지 않는다.

## RDS / 저장 / 원복

정확한 publicHost마다 virtual_host 하나, 서비스 UUID+환경 UUID마다 CDS cluster 하나. unknown host의 wildcard는 health 경로 200, 나머지 404 직접 응답뿐이다. 다른 서비스 route로 fallback하지 않는다. 서비스 모드 자동 aggregate failover는 만들지 않는다(목적지 자격은 echo-infra#21(자동 전환 보안) 범위).

하나의 writer 프로세스가 thread lock+파일 lock으로 최신 snapshot을 읽고 해당 서비스만 갱신한다. expectedRevision으로 같은 서비스 stale write 거절. 상태 primary/backup 두 사본은 같은 검증된 snapshot을 원자 rename으로 저장하고 RDS는 그 snapshot으로 재생성한다. 재시작 시 정상 사본에서 전체 서비스를 복원한다. 두 사본 모두 손상되면 시작을 실패시켜 전역/빈 설정으로 조용히 초기화하지 않는다. 디스크 실패는 성공 응답이 아니며 읽기 복구 후 재시작으로 수렴시킨다. 응답은 파일 생성 완료이지 Envoy ACK 증거는 아니다.

활성화 전: 등록표 전체 준비, 두 코드 PR 독립 검증, 보조 포함 같은 등록표 배포, 기존 active host 수와 대조, 로컬/Envoy 검증 후 별도 운영 승인. legacy weights를 모든 서비스로 자동 복제하지 않는다. 원복은 저장 snapshot 보존 후 OFF+기존 URL로 명시 전환하되 단일 전역 비율로 돌아가는 손실을 먼저 승인받는다. 현재 운영은 변경하지 않는다.
