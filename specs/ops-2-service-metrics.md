# SPEC 운영 2: 서비스 대시보드 지표 조회 API

- 상태: Confirmed (지민 지시 2026-11-05, 리허설 11/6 20:00 전 배포 목표) · 노션 태스크 없음 — 프론트 echo-frontend#78 1번(대시보드 KPI 4칸·요청 그래프), 계약 echo-frontend `docs/api-contract.md` 4-2 (Project #4)
- 기준: echo-was main `df6564a080e7f0731dcdc87e9e70e67a6c04d6dc`
- 마이그레이션 0 · 새 의존성 0 · 기존 API 모양 변경 0

## 1. 목적

대시보드의 성공률·p95·복제 지연·에러 버짓 KPI 와 환경별 요청 그래프가 비어 있다. 진입점 Prometheus 에 쌓이는 앱 지표(인프라 9 OTel 수집기 remote write)를 서비스 하나 기준으로 읽어, 프론트 계약 4-2 모양 그대로 돌려주는 읽기 API 하나를 만든다.

## 2. 확인된 사실 (2026-11-05)

1. 프론트 계약 4-2: `GET /orgs/{orgId}/services/{id}/metrics?range=15m&step=10s` → `{range, step, series[{environmentId, points[{t, rps, successRate, p95Ms}]}], summary{successRate, p95Ms, replicationLagMs, errorBudgetLeft}, simulated}`.
2. echo-was 는 지금 Prometheus 를 **직접 부르지 않는다.** 고정 진입점 가중치 API(`ENTRYPOINT_BASE_URL=http://10.60.0.6:8080`)의 `/summary`·`/counters` 를 부르고, 그 API 가 같은 VM 의 Prometheus(:9090)를 읽는다(`app/executors/entrypoint.py`). `/summary` 는 최근 한 구간의 숫자만 줘서 시계열을 만들 수 없다 → 같은 VM 의 Prometheus `:9090/api/v1/query_range` 를 직접 읽는다.
3. 길: 관제 API 는 GKE 서울(`echo-dev-gke`, 진입점과 같은 VPC)에 있다(deploy/README.md). echo-infra `network.tf` 의 `internal` 방화벽이 파드 CIDR 을 허용하고, GKE 수집기 overlay 도 `http://10.60.0.6:9090` 으로 쓴다. echo-was `deploy/base.yaml` 에는 나가는 NetworkPolicy 가 없다.
4. 지표(echo-infra `cluster-addons/otel-collector/base/collector.yaml`): `http_requests_total`·`http_request_duration_seconds_(bucket|sum|count)`. 라벨 `namespace`(파드 네임스페이스) · `pod` · `app` · `cloud_provider`(`aws`·`gcp`·`onprem`) · `cloud_region` · `k8s_cluster_name` + 앱 라벨 `method`·`path`·`status`. 긁기 15초. Grafana 「세 환경 한 그래프」 식은 `sum by (cloud_provider) (rate(http_requests_total{path!~"/healthz|/readyz|/metrics"}[1m]))`.
5. 앱 네임스페이스는 대표 주소의 첫 레이블 `{서비스}-{suffix}`(`app/executors/kubernetes/executors.py::app_namespace`) → `namespace` 라벨로 서비스 하나를 가른다.
6. 인증: 진입점 스위치 `entrypoint_prometheus_remote_write = true` 가 되면 **쿼리도 basic auth** 가 필요하다(사용자 `echo-otel`, 비밀번호는 Secret Manager `echo-dev-entry-prometheus-remote-write`, 값은 비밀번호만). 꺼져 있으면 인증 없이 읽는다. weights-api 는 `PROMETHEUS_BASIC_AUTH="사용자:비밀번호"` 를 쓴다.
7. 환경 이름 매핑: 진입점 실행기는 `ENTRYPOINT_ENVIRONMENT_MAP`(환경 id → 종류 → 종류 그대로)으로 진입점 이름을 정하고, 운영 값은 비어 있다(환경 종류 `aws`·`gcp`·`onprem` = 수집기 `cloud_provider`).
8. 복제 지연: 이전 계획 데이터 확인 단계가 `ExecutorSet.data_sync.replication_lag(출발, 도착)` 로 읽는다(운영 `DATA_SYNC_CHECKER_MODE=postgres`, 출발 PostgreSQL `pg_stat_replication.replay_lag`). 가짜(`FakeDataSyncChecker`)는 `simulated = true` 의 고정 0.3초.
9. 선례: API 서버 16 컷오버 결과는 외부 읽기 실패에도 200 + 칸 null, 프로세스 캐시 5초(`CutoverResultCache`). 범위 밖 질의는 422 `VALIDATION_ERROR` + `details[{field, reason}]`(`RequestFieldInvalidError`).

## 3. 설계 결정

| ID | 결정 | 이유 |
|---|---|---|
| D-1 | `GET /api/v1/orgs/{orgId}/services/{serviceId}/metrics?range=15m&step=10s`. 뷰어 이상(에이전트 가능, `ViewerAccess`). 다른 조직 404 `ORGANIZATION_NOT_FOUND`, 없는·삭제된 서비스 404 `SERVICE_NOT_FOUND`. 감사 줄 없음(에이전트 GET 은 기존 미들웨어). | 읽기 경로 권한 표(뷰어) |
| D-2 | 출처는 진입점 Prometheus `query_range`·`query` 직접 호출. 설정 `ENTRYPOINT_PROMETHEUS_URL`(비면 기능 꺼짐 → `metricsStatus = NOT_CONFIGURED`). 요청 제한 시간은 진입점 실행기와 같은 `ENTRYPOINT_REQUEST_TIMEOUT_SECONDS`. | 사실 2·3 |
| D-3 | basic auth: `ENTRYPOINT_PROMETHEUS_BASIC_AUTH_PASSWORD_REF`(비밀 참조 `env:ECHO_SECRET_…`)가 있으면 `ENTRYPOINT_PROMETHEUS_BASIC_AUTH_USER`(기본 `echo-otel`)와 함께 `Authorization: Basic` 을 붙인다. 참조가 비면 인증 없이(지금 운영과 호환). 비밀번호는 호출마다 비밀 조회기에서 읽고 저장·로그·응답에 넣지 않는다. | Secret Manager 값이 비밀번호만이라 SecretSync 로 그대로 옮길 수 있다. 저장소 규칙: 비밀은 참조로만 |
| D-4 | 서비스 거르기 `namespace="<대표 주소 첫 레이블>"`, 헬스 경로 빼기 `path!~"/healthz|/readyz|/metrics|<서비스 health_check_path>"`(RE2 이스케이프). 환경 ↔ `cloud_provider` 는 진입점 매핑 규칙(환경 id → 종류 → 종류 그대로, `ENTRYPOINT_ENVIRONMENT_MAP`)을 쓴다. 두 연결이 같은 이름이면 그 연결들은 가를 수 없어 `points = []`. | 사실 4·5·7. 같은 데이터를 두 환경에 붙이면 거짓 |
| D-5 | 점 식(`W = max(step, 60s)`, 긁기 15초라 `[10s]` 창은 비므로 Grafana 와 같은 1분 이상): `rps = sum by (cloud_provider) (rate(http_requests_total{F}[W]))`, `err = sum by (cloud_provider) (rate(http_requests_total{F,status=~"5.."}[W]))`, `p95 = histogram_quantile(0.95, sum by (cloud_provider, le) (rate(http_request_duration_seconds_bucket{F}[W]))) * 1000`. 점 `successRate = 1 − err/rps`(같은 시각 err 가 없으면 0, rps = 0 이면 null). NaN·Inf 는 null. | 5xx 시계열이 아직 없으면 나눗셈 식이 통째로 비는 것을 피하려고 나눗셈은 서버 코드에서 한다 |
| D-6 | 요약(구간 전체, 환경 합): `total = sum(increase(http_requests_total{F}[range]))`, `errors = sum(increase(…{F,status=~"5.."}[range]))`, `p95 = histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{F}[range]))) * 1000` 를 끝 시각 instant 질의로. `successRate = 1 − errors/total`(total = 0·없음 → null). | 그래프와 같은 거르기 |
| D-7 | **에러 버짓**: SLO 99.9%(`SERVICE_METRICS_SLO_TARGET = 0.999`). `errorBudgetLeft = clamp(1 − (1 − successRate) / (1 − 0.999), 0, 1)`, successRate 가 null 이면 null. 30일 창이 아니라 **요청한 구간** 기준의 간단 계산이다. 예: successRate 0.99972 → 1 − 0.00028/0.001 = 0.72. | 지시: 간단 계산 + SPEC 에 공식 |
| D-8 | **복제 지연** `replicationLagMs`: 서비스 중 연결(`reference_connection`)을 출발, 현재 배포가 있고 비율 0 인 연결(예비)을 도착으로 `data_sync.replication_lag` 를 부르고(예비가 여럿이면 가장 큰 값), 초 × 1000 을 소수 1자리로. 데이터 확인기가 `simulated` 이거나, 읽지 못했거나(`DataSyncUnavailable`), 재지 않고 통과한 값(`reason` 있음, 예 `TARGET_IS_WRITER`)이거나, 출발·예비가 없으면 null. | 사실 8. 가짜 0.3초를 실제 값처럼 보이지 않게 |
| D-9 | **Prometheus 를 못 읽으면 200**: `metricsStatus = UNAVAILABLE`, `metricsErrorCode`(고정 코드), `series = []`, `summary.successRate·p95Ms·errorBudgetLeft = null`. 복제 지연은 따로 읽은 값을 그대로 둔다. 질의 여러 개 중 하나라도 실패하면 전체를 UNAVAILABLE 로 본다(반쪽 그래프 방지). | 시연 화면이 깨지지 않게, 컷오버 결과 선례 |
| D-10 | 범위 검증: `range`·`step` 은 `<1~5자리 정수><s|m|h>`. `range` 5m~24h, `step` 5s~5m, `step ≤ range`, 점 수 `floor(range/step) + 1 ≤ 1500`. 어기면 422 `VALIDATION_ERROR`, `details[{field: "range"|"step", reason}]`(reason `INVALID_DURATION`·`RANGE_OUT_OF_BOUNDS`·`STEP_OUT_OF_BOUNDS`·`TOO_MANY_POINTS`). 응답의 `range`·`step` 은 받은 글자 그대로. | Prometheus 한 질의 상한(11000점)보다 충분히 작게 |
| D-11 | 시각: 끝 = 지금(주입 시계)을 step 배수로 내림, 시작 = 끝 − range. 점 `t` 는 UTC ISO 8601. 응답 `asOf` = 끝. | 같은 5초 안 요청이 같은 점을 받게 |
| D-12 | **5초 캐시**: 프로세스 메모리, 키 `(서비스 id, range 초, step 초)`, 값은 응답 뷰 전체(복제 지연 포함), 설정 `SERVICE_METRICS_CACHE_SECONDS`(기본 5). 같은 키 동시 요청은 줄 세워 한 번만 읽는다. 최대 512 키, 넘으면 오래된 것부터 버린다. 권한·서비스 존재 확인(DB)은 캐시보다 먼저 매번 한다. | 대시보드 폴링이 Prometheus·고객 DB 를 두드리지 않게 |
| D-13 | 응답은 계약 칸 + 덧붙인 칸(`asOf`·`metricsStatus`·`metricsErrorCode`·`series[].environmentName`·`series[].cloudProvider`·`summary.sloTarget`). `simulated` 는 값을 지어내지 않으므로 늘 false. 숫자 반올림: rps 4자리, successRate 6자리, p95Ms·replicationLagMs 1자리, errorBudgetLeft 4자리. | 계약 칸은 그대로, 덧붙임만 |
| D-14 | 시리즈는 서비스의 살아 있는 연결마다 하나(연결 id 순서 = `search_live_by_service`), 데이터가 없으면 `points = []`. | 환경이 있는데 줄이 사라지지 않게 |

## 4. 바뀌는 곳

- `app/core/config.py`: `entrypoint_prometheus_url`·`entrypoint_prometheus_basic_auth_user`·`entrypoint_prometheus_basic_auth_password_ref`(비밀 참조 검증)·`service_metrics_cache_seconds`.
- `app/clients/prometheus_client.py`: `PrometheusClient.query_range`·`query`(basic auth, 실패 코드 `PROMETHEUS_UNREACHABLE`·`PROMETHEUS_UNAUTHORIZED`·`PROMETHEUS_SECRET_MISSING`·`PROMETHEUS_ERROR`).
- `app/services/service_metrics.py`: 범위 검증·PromQL 조립·응답 변환(순수 함수) + `ServiceMetricsService` + 캐시.
- `app/schemas/service_metrics.py`, `app/routers/service_metrics_router.py`, `app/main.py`, `app/dependencies.py`, `app/enums.py`(`ServiceMetricsStatus`).
- 문서: `docs/api/metrics.md`(새), `CLAUDE.md` 설계 문서 표·구조, `README.md` 환경 변수 표, 용어 사전.
- 운영 배포 설정: `deploy/base.yaml` ConfigMap 에 `ENTRYPOINT_PROMETHEUS_URL: http://10.60.0.6:9090` 한 줄만 넣는다(병합하면 인증 없이 읽기 시작, 인증이 이미 켜져 있으면 401 → `UNAVAILABLE` 로 화면은 그대로). basic auth 비밀 연결은 권한이 먼저라 이 PR 에서 넣지 않는다(7절).

## 5. 완료 기준

- AC-1 Prometheus 가짜 응답(`httpx.MockTransport`)으로 계약 모양 200: 환경 2개 시리즈·점 값·요약·에러 버짓 공식 값.
- AC-2 Prometheus 미응답·401·형식 오류 → 200 `UNAVAILABLE`, `series = []`, 요약 null(복제 지연은 별도).
- AC-3 URL 미설정 → 200 `NOT_CONFIGURED`, 외부 호출 0.
- AC-4 basic auth: 비밀 참조가 있으면 `Authorization: Basic base64(echo-otel:<값>)`, 없으면 헤더 없음.
- AC-5 범위 검증 422 네 가지 + 기본값(`15m`·`10s`).
- AC-6 5초 캐시: 같은 키 두 번째 요청은 외부 호출 0, 5초 뒤 다시 읽는다.
- AC-7 복제 지연: 실제(비-simulated) 확인기 값 → ms, simulated·unavailable·예비 없음 → null.
- AC-8 권한 표(뷰어·에이전트 ok), 다른 조직 404, 없는 서비스 404.
- AC-9 단위: PromQL 조립(거르기·창·이스케이프), 응답 변환(NaN·빈 결과·같은 이름 두 연결), 범위 검증.
- AC-10 ruff·format·mypy app·전체 pytest(건너뜀 0)·alembic heads 1.

## 6. 감수 위험

- 수집기 적용 전이거나 서비스 파드가 `/metrics` 를 내지 않으면 `OK` 이지만 `points = []`·요약 null 이다(값을 지어내지 않는다).
- 9090 은 평문 HTTP 라 비밀번호가 VPC 안을 평문으로 지난다(echo-infra 와 같은 시연 기간 한정 판단).
- 에러 버짓은 요청 구간 기준이라 구간을 바꾸면 값이 바뀐다.

## 7. 운영 설정 (병합 뒤, 저장소 밖 포함)

1. (이 PR 에 포함) `deploy/base.yaml` ConfigMap: `ENTRYPOINT_PROMETHEUS_URL: http://10.60.0.6:9090`.
2. basic auth 가 켜지면: GSA `echo-dev-echo-was` 에 Secret Manager `echo-dev-entry-prometheus-remote-write` 읽기 권한(echo-infra `control-plane.tf` 또는 gcloud) → `SecretProviderClass` 경로 + `SecretSync` `targetKey: ECHO_SECRET_ENTRYPOINT_PROMETHEUS_PASSWORD` → ConfigMap `ENTRYPOINT_PROMETHEUS_BASIC_AUTH_PASSWORD_REF: env:ECHO_SECRET_ENTRYPOINT_PROMETHEUS_PASSWORD`(사용자 기본 `echo-otel`). 권한 없이 SecretSync 에 넣으면 동기화 전체가 실패하므로 권한을 먼저 준다.
