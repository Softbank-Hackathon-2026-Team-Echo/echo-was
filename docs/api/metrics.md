# 서비스 대시보드 지표 API (운영 2)

대시보드의 KPI(성공률·p95·복제 지연·에러 버짓)와 환경별 요청 그래프를 서비스 하나 기준으로 돌려준다. 프론트 계약은 echo-frontend `docs/api-contract.md` 4-2 이고, 칸은 그 모양 그대로에 몇 칸을 덧붙였다. SPEC [specs/ops-2-service-metrics.md](../../specs/ops-2-service-metrics.md).

- 권한: 뷰어 이상(에이전트 토큰 가능). 로그인 없음 401, 다른 조직 404 `ORGANIZATION_NOT_FOUND`, 없는·삭제된 서비스 404 `SERVICE_NOT_FOUND`. 사람의 읽기는 감사 줄을 쓰지 않고, 에이전트 읽기는 기존 미들웨어가 기록한다.
- 모든 칸은 항상 있고 값이 없으면 null 이다. **값을 지어내지 않는다** — 그래서 `simulated` 는 늘 false 다.
- 쓰기·표·마이그레이션이 없다. 출처는 진입점 Prometheus 이고 DB 에 저장하지 않는다.

## GET `/api/v1/orgs/{orgId}/services/{serviceId}/metrics?range=15m&step=10s`

| 질의 | 기본 | 규칙 |
|---|---|---|
| `range` | `15m` | `<1~5자리 정수><s\|m\|h>`, 5m ~ 24h |
| `step` | `10s` | 같은 형식, 5s ~ 5m, `step ≤ range`, 점 수 `floor(range/step) + 1 ≤ 1500` |

어기면 422 `VALIDATION_ERROR`, `details: [{field: "range"|"step", reason}]`. reason 은 `INVALID_DURATION`(형식) · `RANGE_OUT_OF_BOUNDS` · `STEP_OUT_OF_BOUNDS` · `TOO_MANY_POINTS`. 응답의 `range`·`step` 은 받은 글자 그대로다.

200 `ApiResponse[ServiceMetricsResponse]`:

```json
{
  "success": true,
  "data": {
    "range": "15m",
    "step": "10s",
    "asOf": "2026-11-06T11:00:10Z",
    "metricsStatus": "OK",
    "metricsErrorCode": null,
    "series": [
      {
        "environmentId": "dcec9925-7af6-4584-867a-12e878b84b1e",
        "environmentName": "aws-tokyo",
        "cloudProvider": "aws",
        "points": [
          {"t": "2026-11-06T10:59:50Z", "rps": 20.0, "successRate": 0.975, "p95Ms": 182.0},
          {"t": "2026-11-06T11:00:00Z", "rps": 20.0, "successRate": 0.975, "p95Ms": 182.0}
        ]
      },
      {
        "environmentId": "5b0c6b0e-8f5e-4f43-9d0a-3d1f6c2b7a10",
        "environmentName": "gcp-seoul",
        "cloudProvider": "gcp",
        "points": [
          {"t": "2026-11-06T10:59:50Z", "rps": 2.0, "successRate": 1.0, "p95Ms": null},
          {"t": "2026-11-06T11:00:00Z", "rps": 2.0, "successRate": 1.0, "p95Ms": null}
        ]
      }
    ],
    "summary": {
      "successRate": 0.99972,
      "p95Ms": 182.0,
      "replicationLagMs": 120.0,
      "errorBudgetLeft": 0.72,
      "sloTarget": 0.999
    },
    "simulated": false
  }
}
```

(값은 통합 시험의 가짜 Prometheus 응답과 같은 숫자다. 점은 실제로 `range/step + 1` 개 안쪽이다.)

| 칸 | 뜻 |
|---|---|
| `asOf` | 구간 끝 = 지금을 `step` 배수로 내린 시각(UTC). 시작 = `asOf − range` |
| `metricsStatus` | `OK`(읽음) · `UNAVAILABLE`(Prometheus 를 못 읽음) · `NOT_CONFIGURED`(`ENTRYPOINT_PROMETHEUS_URL` 없음, 외부 호출 없음) |
| `metricsErrorCode` | `UNAVAILABLE` 일 때만: `PROMETHEUS_UNREACHABLE`(연결 실패·시간 초과) · `PROMETHEUS_UNAUTHORIZED`(401·403, basic auth 불일치·미설정) · `PROMETHEUS_SECRET_MISSING`(비밀번호 비밀값 없음, 호출 전) · `PROMETHEUS_ERROR`(그 밖의 응답·형식) |
| `series[]` | 서비스의 살아 있는 연결마다 하나(만든 순서). 배포 없는 연결(현재 배포 없음 + 비율 0, 첫 배포 전 변수 넣기·첫 배포 실패)은 앱이 없어 빠진다. 데이터가 없으면 `points: []`. `UNAVAILABLE`·`NOT_CONFIGURED` 면 배열 자체가 빈다 |
| `series[].cloudProvider` | 그 환경을 가르는 Prometheus `cloud_provider` 라벨 값(아래 "환경 가르기") |
| `points[].t` | 점 시각(UTC) |
| `points[].rps` | 초당 요청 수(소수 4자리). 이 값이 있는 시각만 점이 된다 |
| `points[].successRate` | `1 − 5xx/전체`(0~1, 소수 6자리). 요청이 0 이면 null |
| `points[].p95Ms` | p95 응답 시간(ms, 소수 1자리). 히스토그램이 비면 null |
| `summary.successRate`·`p95Ms` | 구간 전체·환경 합. 요청이 없으면 null |
| `summary.replicationLagMs` | 서비스 중 → 예비 데이터 복제 지연(ms, 아래). 못 재면 null |
| `summary.errorBudgetLeft` | SLO 99.9% 기준 남은 에러 버짓(0~1, 아래) |
| `summary.sloTarget` | 0.999 |
| `simulated` | 늘 false |

**Prometheus 를 못 읽어도 200 이다**(화면이 깨지지 않게). 질의 여러 개 중 하나라도 실패하면 전체를 `UNAVAILABLE` 로 보고 `series = []`, `summary` 의 성공률·p95·에러 버짓을 null 로 둔다. 복제 지연은 따로 읽으므로 그대로 보인다.

## 계산

거르기 `F = namespace="<대표 주소 첫 레이블>", path!~"/healthz|/readyz|/metrics|<서비스 헬스체크 경로>"`. 앱 네임스페이스가 `{서비스}-{suffix}` 이므로 서비스 하나만 센다. 헬스 경로는 Grafana 「세 환경 한 그래프」와 같이 뺀다.

| 값 | PromQL / 식 |
|---|---|
| 점 rps | `sum by (cloud_provider) (rate(http_requests_total{F}[W]))` |
| 점 5xx | `sum by (cloud_provider) (rate(http_requests_total{F,status=~"5.."}[W]))` |
| 점 p95 | `histogram_quantile(0.95, sum by (cloud_provider, le) (rate(http_request_duration_seconds_bucket{F}[W]))) * 1000` |
| 창 `W` | `max(step, 60s)` — 수집기가 15초마다 긁어 `[10s]` 창은 비기 때문이다(Grafana 패널도 1분) |
| 점 successRate | `1 − 5xx/rps`. 같은 시각 5xx 시계열이 없으면 5xx = 0. 나눗셈은 서버 코드에서 한다(5xx 시계열이 아직 없을 때 PromQL 나눗셈은 통째로 빈다) |
| 요약 total·5xx | `sum(increase(http_requests_total{F}[range]))` · 같은 식 + `status=~"5.."`, 끝 시각 instant 질의 |
| 요약 p95 | `histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{F}[range]))) * 1000` |
| 요약 successRate | `1 − 5xx/total`, total 이 0·없음이면 null |
| **errorBudgetLeft** | `clamp(1 − (1 − successRate) / (1 − 0.999), 0, 1)`(소수 4자리). 예: 0.99972 → 1 − 0.00028/0.001 = **0.72**, 0.999 이하면 0. **요청 구간 기준의 간단 계산**이다(30일 창이 아니다) — 구간을 바꾸면 값이 바뀐다 |

질의 6개(범위 3 + 요약 3)는 한꺼번에 보내고, 하나의 제한 시간(`ENTRYPOINT_REQUEST_TIMEOUT_SECONDS`, 기본 5초) 안에 끝난다.

### 환경 가르기

환경 ↔ `cloud_provider` 는 고정 진입점 실행기와 같은 규칙이다: `ENTRYPOINT_ENVIRONMENT_MAP` 의 환경 id 매핑 → 환경 종류 매핑 → 환경 종류 그대로(`aws`·`gcp`·`onprem` = 수집기 `CLOUD_PROVIDER`). 한 서비스의 두 연결이 같은 이름이면 가를 수 없어 **둘 다 `points: []`** 다(같은 값을 두 환경에 붙이지 않는다).

### 복제 지연

이전 계획 데이터 확인 단계와 같은 `data_sync.replication_lag(출발, 도착)` 를 쓴다(운영 `DATA_SYNC_CHECKER_MODE=postgres`, 출발 PostgreSQL `pg_stat_replication.replay_lag`). 출발 = 서비스 중 연결(비율이 가장 큰 연결), 도착 = 현재 배포가 있고 비율 0 인 예비 연결. 예비가 여럿이면 가장 큰 값이다. null 인 경우: 데이터 확인기가 가짜(`simulated`, 고정 0.3초를 실제처럼 보이지 않게), 못 읽음, 재지 않고 통과한 값(`TARGET_IS_WRITER` 등), 서비스 중·예비 연결이 없음.

## 캐시

프로세스 메모리, 키 `(서비스 id, range 초, step 초)`, `SERVICE_METRICS_CACHE_SECONDS`(기본 5초) 동안 같은 응답을 다시 준다(복제 지연 포함). 같은 키 동시 요청은 줄 세워 한 번만 읽는다. 최대 512 키. 권한·서비스 존재 확인(DB)은 캐시보다 먼저 매번 한다. 실패 결과(`UNAVAILABLE`)도 5초 동안 다시 쓴다.

## 설정

| 변수 | 운영 값 | 뜻 |
|---|---|---|
| `ENTRYPOINT_PROMETHEUS_URL` | `http://10.60.0.6:9090` | 진입점 VM a 의 Prometheus(VPC 내부 주소). 비면 `NOT_CONFIGURED` |
| `ENTRYPOINT_PROMETHEUS_BASIC_AUTH_USER` | `echo-otel`(기본) | basic auth 사용자 |
| `ENTRYPOINT_PROMETHEUS_BASIC_AUTH_PASSWORD_REF` | `env:ECHO_SECRET_ENTRYPOINT_PROMETHEUS_PASSWORD`(11/5 진입점 basic auth 켬) | 비밀번호의 비밀 참조. **비면 인증 없이** 읽는다(진입점 `entrypoint_prometheus_remote_write = false` 와 호환). 값은 호출마다 비밀 조회기에서 읽고 저장·로그·응답에 넣지 않는다 |
| `SERVICE_METRICS_CACHE_SECONDS` | `5` | 캐시 초 |

진입점이 basic auth 를 켜면(echo-infra 인프라 9) 쿼리도 인증이 필요하다. 그때 할 일: GSA `echo-dev-echo-was` 에 Secret Manager `echo-dev-entry-prometheus-remote-write`(값 = 비밀번호만) 읽기 권한 → `deploy/base.yaml` `SecretProviderClass` 경로 + `SecretSync` `targetKey: ECHO_SECRET_ENTRYPOINT_PROMETHEUS_PASSWORD` → ConfigMap `ENTRYPOINT_PROMETHEUS_BASIC_AUTH_PASSWORD_REF`. **권한 없이 SecretSync 에 넣으면 동기화 전체가 실패한다**(deploy/README.md) — 권한을 먼저 준다. 그 전까지 응답은 `UNAVAILABLE`/`PROMETHEUS_UNAUTHORIZED` 다.

9090 은 평문 HTTP 라 비밀번호가 VPC 안을 평문으로 지난다(echo-infra 와 같은 시연 기간 한정 판단).

## 지금 한계

- 수집기 적용 전이거나 앱이 `/metrics` 를 내지 않으면 `OK` 이지만 `points: []`, 요약 null 이다.
- 진입점 VM b(보조)는 보지 않는다. a 가 죽으면 `UNAVAILABLE` 이다.
- 트래픽 비율 KPI 는 이 응답에 없다 — 서비스 상세의 연결별 `trafficWeight` 를 쓴다.
