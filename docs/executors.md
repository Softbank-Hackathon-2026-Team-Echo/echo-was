# 실행기 계약 (빌드·배포·대표 주소·헬스체크·내리기·트래픽·지표·데이터 확인)

원터치 배포의 흐름·상태·DB 기록은 echo-was 일꾼이 맡고, 외부 시스템을 실제로 움직이는 일은 **실행기**가 맡는다. 이번에는 모두 가짜 구현(`EXECUTOR_MODE=fake`)이고, 인프라 3(Argo CD + 빌드·배포 파이프라인)·인프라 5(고정 진입점 프록시)가 준비되면 **실행기 구현만** 바꾼다. 이전 계획(API 서버 8)의 트래픽 나눠 보내기·지표·데이터 복제 확인도 같은 방식이다(실제 연결은 인프라 6 Envoy·인프라 9 OpenTelemetry·인프라 7 복제). 표·API 계약은 바꾸지 않는 것이 목표다.

- 인터페이스: `app/executors/base.py`(Protocol + 값 객체). 가짜 구현: `app/executors/fake.py`. 실제 HTTP 헬스체커: `app/executors/http_health_checker.py`. 실제 고정 진입점 실행기(트래픽·대표 주소·지표): `app/executors/entrypoint.py`. 고르는 곳: `app/executors/__init__.py::build_executor_set`.
- 공통 계약 시험: `tests/executor_contract.py`. 실제 구현도 같은 검사를 스텁 전송으로 통과해야 한다.
- 실행기 패키지는 `app.models` 를 import 하지 않는다. 일꾼이 값 객체(`BuildSpec`·`DeploymentSpec`·`DeployTarget`·`MainRoute`·`SourceAccess`)로 바꿔 넘긴다.

## 인터페이스

| 실행기 | 메서드 | 결과 |
|---|---|---|
| `BuildExecutor` | `start(build, source_access, idempotency_key) -> external_ref` | |
| | `poll(external_ref)` | `BuildRunning` · `BuildSucceeded(image_repository, image_digest)` · `BuildFailed(code)` |
| | `read_logs(external_ref, cursor, limit)` | `LogChunk(lines, next_cursor, has_more)` · `InvalidLogCursor` (API 서버 4) |
| `DeployExecutor` | `apply(deployment, target, idempotency_key) -> external_ref` | |
| | `poll(external_ref)` | `DeployPending` · `DeployApplied` · `DeployFailed(code)` |
| | `revert(target, previous_deployment \| None, idempotency_key)` | `Reverted(external_ref)` · `RevertFailed(code)` |
| | `read_runtime_logs(target, deployment, external_ref, cursor, limit)` | `LogChunk` · `InvalidLogCursor` (API 서버 4. `external_ref` = 요청의 `deploy_external_ref`) |
| `MainRouteExecutor` | `route_main(route, target, idempotency_key)` | `RouteApplied(applied_at)` · `RouteFailed(code)` |
| `HealthChecker` | `probe(url, path, timeout_seconds)` | `HealthOk(status)` · `HealthHttpError(status)` · `HealthUnreachable` |
| `TeardownExecutor` | `remove(target, idempotency_key) -> external_ref` | |
| | `poll(external_ref)` | `TeardownPending` · `TeardownRemoved` · `TeardownFailed(code)` |
| `TrafficExecutor` | `set_weights(route, weights: tuple[WeightedTarget(target, weight)], idempotency_key)` | `TrafficApplied(applied_at)` · `TrafficFailed(code)` (API 서버 8) |
| `MetricsProvider` | `read_counts(target)` · `read(target, since, until)` | `RequestCounts(requests, errors_5xx)` · `CountsUnavailable(code)` (판정용 누적 카운터, API 서버 8-1) · `Metrics(error_rate_percent, p95_ms, request_count)` · `MetricsUnavailable` (p95 참고용) |
| `DataSyncChecker` | `replication_lag(source, target)` | `DataSyncLag(seconds)` · `DataSyncUnavailable` (API 서버 8) |

모든 실행기는 `simulated` 속성을 가진다(가짜면 true). 결과의 `simulated` 는 요청·빌드 행에 저장된다.

## 멱등 키와 호출 순서 (반드시 지킬 것)

| 외부 작업 | 멱등 키 |
|---|---|
| 빌드 시작 | `builds.id` |
| 배포 반영 | `deployments.id` |
| 되돌리기 | 실패한 `deployments.id` |
| 대표 주소 연결 | 성공한 `deployments.id` |
| 내리기 | `{service_environments.id}:{teardown_attempt}` (서비스 삭제를 다시 보내면 attempt 가 1 늘어 새 키) |
| 이전 단계 비율 적용 | `{migration_plans.id}:{단계 번호}` (1·2·4) |
| 이전 롤백(출발 100%) | `{migration_plans.id}:rollback:{rollback_attempts}` (시도마다 새 키, 최대 `MIGRATION_ROLLBACK_MAX_ATTEMPTS` 번) |

일꾼은 언제든 죽을 수 있다. 그래서 외부 호출은 항상 이 순서다.

1. **요청함 시각을 저장·커밋한다**(`builds.start_requested_at`, `deploy_requests.deploy_requested_at`, `service_environments.teardown_requested_at`, 이전 `migration_plans.traffic_requested_at`·`rollback_requested_at`).
2. **트랜잭션 밖에서** 실행기를 부른다(멱등 키 포함).
3. 돌아온 `external_ref` 를 저장한다(`builds.external_ref`, `deploy_requests.deploy_external_ref`, `service_environments.teardown_external_ref`). 트래픽은 결과(`TrafficApplied`)가 오면 두 연결의 `traffic_weight`·`traffic_applied_at` 을 **한 트랜잭션에서 함께** 적는다(합계 100).

다시 시작한 일꾼은 요청함 시각이 있고 `external_ref` 가 없으면 **같은 키로 다시 부른다.** 그래서 실제 구현은:

- **같은 멱등 키로 다시 불러도 같은 작업을 돌려줘야 한다**(새 빌드·새 배포를 만들지 않는다). 예: GitOps 커밋이라면 커밋 메시지·브랜치에 키를 넣고, push 전에 이미 있는지 확인한다.
- `external_ref` 는 다른 일꾼 프로세스가 `poll` 해도 같은 답을 받을 수 있는 값이어야 한다(프로세스 메모리에만 있는 번호 금지).
- `poll` 은 상태를 바꾸지 않는 조회다. 일꾼은 실행마다 한 번 부르고, 끝나지 않았으면 `WORKER_POLL_INTERVAL_SECONDS` 뒤 다시 부른다.

## 실패 코드

- 단계마다 정해진 코드로 실패를 돌려준다. **권한 오류를 시간 초과로 뭉개지 않는다**(예선에서 권한 오류가 시간 초과로만 보여 원인을 못 찾았다). 빌드 `BUILD_FAILED`, 배포 `DEPLOY_FAILED`, 내리기 `TEARDOWN_FAILED`. 시간 초과(`BUILD_TIMEOUT`·`DEPLOY_TIMEOUT`·`TEARDOWN_TIMEOUT`)는 일꾼이 단계 시작 시각으로 정한다(빌드 `builds.started_at`, 배포 `deploy_requests.deploying_at`, 내리기 `teardown_requested_at`).
- 실패 코드·`external_ref` 에 **비밀값을 넣지 않는다**(설치 토큰·kubeconfig·레지스트리 비밀번호 등). 응답의 `errorMessage` 는 코드별 고정 문장이라 외부 원문은 사용자에게 나가지 않는다.

## 실행기별 계약

### 빌드 (`BuildExecutor`)

- 입력: `BuildSpec(build_id, service_id, service_name, repo_url, commit_sha)`, `SourceAccess(repo_url, commit_sha, read_token)`. `read_token` 은 비공개 저장소일 때만 있는 GitHub App 설치 토큰(1시간)이며 **메모리로만** 넘긴다. 저장·로그 금지.
- 결과: 이미지 저장소 주소(tag 없음) + `sha256:` digest. 배포는 digest 로만 한다(`latest` 같은 바뀌는 tag 금지).
- 같은 서비스·저장소·커밋의 빌드는 일꾼이 재사용하므로 실행기는 한 커밋에 한 번만 불린다(실패 뒤에는 새 `builds.id` 로 다시).

### 배포 (`DeployExecutor`)

- 입력: `DeploymentSpec(deployment_id, image_repository, image_digest, config_snapshot)`, `DeployTarget(service, service_environment, environment kind, public_host, container_port)`.
- `apply` 는 그 연결(클러스터의 그 서비스)에 digest 이미지를 반영한다. `DeployApplied` 는 "클러스터가 새 버전을 받아들였다"이다. 사용자 트래픽 확인은 일꾼의 헬스체크가 환경별 퍼블릭 주소로 한다.
- `revert(target, previous, key)` 는 실패한 새 버전을 거두고 `previous`(연결의 현재 배포, 없으면 아무것도 없는 상태)를 유지한다. 실패해도 일꾼은 요청의 `errorCode` 를 바꾸지 않고 `revertStatus = FAILED` 만 적는다.
- 연결안(인프라 3): GitOps 저장소에 digest 를 커밋(커밋 SHA 를 push 전에 기록) → Argo CD 동기화 상태를 `poll`.

### 대표 주소 (`MainRouteExecutor`)

- 서비스에 서비스 중 연결이 없을 때, 성공한 연결로 대표 주소(`{service}-{suffix}.{domain}`, 저장한 `services.public_host`)를 보낸다. `RouteApplied(applied_at)` 일 때만 일꾼이 `traffic_weight = 100`, `traffic_applied_at = applied_at` 을 적는다("프록시에 마지막으로 적용한 비율"이라는 칸 뜻을 지킨다).
- 가짜 구현은 I/O 가 없어 성공 처리 트랜잭션 안에서 부른다. 실제 구현(echo-infra#5 고정 진입점 프록시)의 호출 위치(트랜잭션 밖 + 멱등 키)는 실제 구현 작업에서 정한다.

### 헬스체커 (`HealthChecker`)

- `GET {url}{path}` 한 번. 2xx 면 `HealthOk`, 그 밖의 응답(3xx 포함, **리다이렉트 따르지 않음**)은 `HealthHttpError(status)`, 연결 실패·시간 초과는 `HealthUnreachable`.
- `url` 은 **환경별 퍼블릭 주소**다. 대표 주소는 성공 처리 뒤에야 이 환경을 가리키므로 확인하지 않는다(실제 라우터 연결 뒤 추가).
- 실제 구현 `HttpHealthChecker` 는 이미 있다(`HEALTH_CHECKER_MODE=http`). 요청 하나 제한 시간은 `HEALTH_CHECK_REQUEST_TIMEOUT_SECONDS`(5).

### 로그 읽기 (`read_logs` · `read_runtime_logs`, API 서버 4)

로그 API(`GET /deploy-requests/{id}/logs?source=BUILD|RUNTIME`)가 API 프로세스에서 즉석으로 부른다(저장하지 않는다). API 프로세스는 `app/dependencies.py::get_executor_set` 으로 실행기를 얻고, 실제 실행기가 아직 없는 모드(`EXECUTOR_MODE != fake`)면 실행기를 만들지 않고 빈 페이지를 준다.

- **상태를 바꾸지 않는 조회**다. 트랜잭션 밖에서 부른다.
- `limit` 을 넘지 않는 줄을 **시간 순**으로 돌려준다. **같은 `cursor` 면 같은 줄**이다(다른 프로세스가 불러도). `next_cursor` 로 이어 읽으면 겹침·누락이 없다. 줄이 없으면 받은 `cursor` 를 그대로 돌려준다.
- 알아보지 못하는 `cursor` 는 `InvalidLogCursor`(API 는 422 `VALIDATION_ERROR`, `reason = INVALID_CURSOR`). 보존 기간이 지나 없으면 빈 청크다.
- **비밀값을 가려서 돌려준다**(설치 토큰·레지스트리 비밀번호·환경변수 값·kubeconfig 등). 서버는 응답 직전에 `app/core/log_masking.py::mask_secrets` 로 알려진 모양(GitHub 토큰 `gh[pousr]_…`, AWS 액세스 키 `AKIA…`, `Bearer <8자 이상>`, `sk-…`, `-----BEGIN … PRIVATE KEY-----` 블록)을 `***` 로 한 번 더 바꾸지만, 이것은 방어용이며 실행기의 책임을 대신하지 않는다.
- API 는 실행기의 `simulated` 가 그 빌드·요청 행의 `simulated` 와 다르면(가짜로 만든 기록을 실제 실행기가 읽는 경우 등) 부르지 않고 빈 페이지를 준다.
- 공통 계약 시험: `tests/executor_contract.py::assert_log_reader_contract`(같은 cursor 같은 줄, limit 준수, 시간 순, 이어 읽기, 잘못된 cursor).
- 연결안: 빌드는 빌드 시스템의 작업 로그(`external_ref` = 작업 번호), 실행은 그 연결 네임스페이스의 Pod 로그(`target` + 배포 기록으로 고른다). cursor 는 실행기가 정하는 불투명한 문자열이다.

### 트래픽 (`TrafficExecutor`, API 서버 8)

- 대표 주소(`route`)의 트래픽을 `weights` 대로 연결들에 나눠 보낸다. 이전 계획은 늘 두 연결(출발·도착)을 넘긴다: 단계 1 `(90, 10)`, 단계 2 `(50, 50)`, 단계 4 `(0, 100)`, 롤백 `(100, 0)`.
- 비율 합계가 100 이 아니거나 0~100 밖이면 `ValueError`(`check_weights_total`). 부르는 쪽의 버그라 실행기 결과로 돌려주지 않는다.
- **적용했다고 답할 때만** 일꾼이 비율 칸을 적는다. `TrafficFailed` 면 비율 칸은 바꾸지 않는다(단계 실패 `TRAFFIC_APPLY_FAILED` → 롤백, 롤백 실패면 다음 시도).
- 같은 키면 같은 결과다. 다시 시작한 일꾼은 `traffic_requested_at`(또는 `rollback_requested_at`)만 있고 결과가 없으면 같은 키로 다시 부른다.
- 연결안: Envoy 가중치 클러스터(인프라 6). 같은 키를 Envoy 설정 버전 이름에 넣어 두 번 적용하지 않는다.

### 지표·요청 카운터 (`MetricsProvider`, API 서버 8 · 8-1)

메서드가 둘이다. 이전 판정은 **누적 카운터(`read_counts`)** 로 하고, 구간 지표(`read`)는 p95 참고 값으로만 쓴다(8-1). 둘 다 상태를 바꾸지 않는 조회이고 트랜잭션 밖에서 부른다. 실제 구현은 고정 진입점의 `EntrypointMetricsProvider`(PR #17) 한 클래스가 두 메서드를 함께 구현한다.

```python
@dataclass(frozen=True, slots=True)
class RequestCounts:
    requests: int  # 진입점이 켜진 뒤 그 환경으로 보낸 누적 요청 수(≥ 0)
    errors_5xx: int  # 그중 5xx 응답 수(≥ 0, ≤ requests 보장 안 함)


@dataclass(frozen=True, slots=True)
class CountsUnavailable:
    code: str | None = None  # 실제 구현의 실패 코드(예: ENTRYPOINT_UNREACHABLE), 가짜는 None


type CountsResult = RequestCounts | CountsUnavailable


class MetricsProvider(Protocol):
    async def read(
        self, target: DeployTarget, since: datetime, until: datetime
    ) -> MetricsResult: ...
    async def read_counts(self, target: DeployTarget) -> CountsResult: ...
```

**`read_counts(target)` 계약**

- `target` 연결이 올라간 **환경**의 누적 요청 수와 그중 5xx 수를 돌려준다(진입점의 환경 키 `aws`·`gcp` 를 `target` 의 값 — 예: `environment_kind` — 로 고르는 방법은 실제 구현이 정한다). 구간 값이 아니라 진입점이 켜진 뒤의 **누적값**이다. 일꾼이 두 번 읽은 차이로 센다.
- 값은 음수가 아니고 **단조 증가**가 원칙이다. 진입점이 재시작하면 0부터 다시 쌓여 값이 줄 수 있다 — 그대로 돌려준다(줄어든 것을 재시작으로 처리하는 것은 일꾼 몫, 로그 `COUNTERS_RESET`). 실행기가 값을 보정하지 않는다.
- 읽지 못하면(진입점에 닿지 못함·시간 초과·응답 모양이 다름·그 환경 키가 없음) 예외 대신 `CountsUnavailable(code)` 를 돌려준다. 일꾼은 일시 오류로 보고 다음 주기(`MIGRATION_COUNT_POLL_SECONDS`)에 다시 읽는다. 마감 때까지 도착을 한 번도 못 읽었거나 마지막 성공이 `MIGRATION_COUNTS_STALE_SECONDS` 보다 오래되면 판단 불가(`METRICS_UNAVAILABLE`)로 롤백한다.
- 일꾼은 단계마다 출발·도착을 각각 부른다(1초마다 2번). 한 번의 호출은 짧은 제한 시간(1초 안쪽)을 권장한다.
- `code` 에 비밀값·토큰·응답 원문을 넣지 않는다.

**`GET /counters` 매핑(실제 구현, PR #17)**

진입점 응답 `{"ts": "...", "envs": {"aws": {"requests": 12345, "errors_5xx": 3}, "gcp": {...}}}` 를 이렇게 바꾼다.

| 진입점 | `RequestCounts` | 비고 |
|---|---|---|
| `envs[<환경 키>].requests` | `requests` | 환경 키(`aws`·`gcp`)는 `target` 으로 고른다. 키가 없으면 `CountsUnavailable` |
| `envs[<환경 키>].errors_5xx` | `errors_5xx` | |
| `ts` | (쓰지 않음) | 오래된 읽기 판단은 일꾼이 자기 시계(마지막 성공 읽기 시각)로 한다 |
| HTTP 실패·시간 초과·JSON 모양 다름 | `CountsUnavailable(code)` | 예: `ENTRYPOINT_UNREACHABLE` |

**전제(환경당 고객 앱 1개)**: 카운터와 가중치가 진입점 전체·환경 단위라, 같은 환경에 고객 앱이 둘 이상이면 다른 앱의 요청이 섞여 판정이 틀린다. 시연은 고객 앱 1개라 코드로 막지 않는다(앱별 라우트·카운터는 진입점 후속).

**`read(target, since, until)`**: 연결 하나의 `[since, until)` 구간 오류율(%)·p95(ms)·요청 수. 일꾼은 비율 단계를 끝낼 때(통과·실패 모두) `until = 지금`, `since = 지금 − MIGRATION_P95_REFERENCE_WINDOW_SECONDS`(30초)로 한 번 불러 `p95Ms` 를 참고로 남긴다. `MetricsUnavailable` 이어도 판정에 영향이 없다(`p95Ms = null`). 연결안: 진입점 `GET /summary?window=…s`(PR #17).

### 데이터 확인 (`DataSyncChecker`, API 서버 8)

- 출발 → 도착 데이터 복제 지연(초). 읽을 수 없으면 `DataSyncUnavailable`(판정 `METRICS_UNAVAILABLE`). 연결안: 인프라 7 복제 상태.

### 내리기 (`TeardownExecutor`)

- 서비스 삭제 때 연결 하나를 클러스터에서 내린다(그 서비스의 Deployment·Service·Ingress 등). `TeardownRemoved` 면 일꾼이 연결을 소프트 삭제한다. 배포 기록은 남는다.

## 고정 진입점 실행기 (`ENTRYPOINT_EXECUTOR_MODE=http`)

echo-infra 고정 진입점(인프라 5·6·9, Envoy + 가중치 API + Prometheus)을 부르는 실제 구현이다. 계약은 echo-infra `contracts/executor-credentials.md` §3. 트래픽·대표 주소·지표 세 실행기만 바꾸고 빌드·배포·내리기·데이터 확인은 그대로 가짜다.

| 설정 | 값 | 뜻 |
|---|---|---|
| `ENTRYPOINT_EXECUTOR_MODE` | `fake`(기본) · `http` | `http` 면 아래 세 실행기가 진입점을 부른다 |
| `ENTRYPOINT_BASE_URL` | `http://<진입점 내부 IP>:8080` | 가중치 API 주소. echo-was 는 진입점과 같은 VPC(GKE)에 있으니 **내부 주소**(echo-infra 출력값 `entrypoint_weights_internal_url`, 11/3 기준 `http://10.60.0.6:8080`)를 쓴다 — 토큰이 인터넷을 거치지 않는다. 공인 주소 `entrypoint_weights_url` 은 허용 CIDR 에서만 열린다 |
| `ENTRYPOINT_TOKEN_REF` | `env:ECHO_SECRET_ENTRYPOINT_TOKEN` | Bearer 토큰의 비밀 참조. 값은 Secret Manager `echo-dev-entry-weights-token` 에서 받아 프로세스 환경변수로 넣는다 |
| `ENTRYPOINT_ENVIRONMENT_MAP` | `<환경 id>=<이름>,…` | 진입점 환경 이름 매핑. 비면 환경 종류(`aws`·`gcp`)를 그대로 쓴다. 한 종류에 환경이 여럿이면 환경 id 로 정한다 |
| `ENTRYPOINT_REQUEST_TIMEOUT_SECONDS` | `5` | 요청 하나 제한 시간 |

| 실행기 | 호출 | 결과 |
|---|---|---|
| `EntrypointTrafficExecutor` | `GET /weights` 로 진입점이 아는 환경을 읽고, 계획에 없는 환경은 0 으로 채워 `PUT /weights {"aws": 90, "gcp": 10}` | 2xx → `TrafficApplied(지금 시각)`. 같은 멱등 키는 이 프로세스에서 처음 **성공** 결과를 다시 준다(최근 256 개, 실패는 기억하지 않고 다시 시도. PUT 자체가 멱등이라 다른 프로세스가 다시 불러도 같은 상태) |
| `EntrypointMainRouteExecutor` | 대상 연결 100·나머지 0 으로 같은 PUT | `RouteApplied` · `RouteFailed(code)`. 지금은 가짜와 같은 자리(성공 처리 트랜잭션 안)에서 불린다 — 요청 하나(5초) 동안 서비스 행 잠금을 쥔다. 트랜잭션 밖으로 옮기는 일은 처리기 쪽 후속 작업이다 |
| `EntrypointMetricsProvider` | `GET /summary?window=<until-since 초>s` 의 `envs.<이름>` | `rps`·`error_ratio`(×100 = 오류율 %)·`p95_ms`, 요청 수 = `rps × 구간 초`. `prometheus != ok` 이거나 값이 `null`(전환 직후·그 환경에 요청 없음)이면 `MetricsUnavailable` |

**제약 — 진입점 하나 = 서비스 하나.** 진입점 가중치는 모든 호스트(`domains: ["*"]`)에 하나라 `PUT /weights` 는 진입점 뒤 **모든 앱**의 비율을 함께 바꾼다. `route_main` 이 서비스 첫 배포마다 불리므로 앱이 둘 이상이면 다른 서비스 트래픽까지 움직인다. 시연(앱 1 개)은 괜찮다. 앱이 늘면 echo-infra 쪽 후속(Host 별 라우트 + `PUT /services/{public_host}/weights`, 실행기는 `MainRoute.public_host` 를 넘기면 된다)이 필요하다.

실패 코드(권한 오류를 시간 초과로 뭉개지 않는다): `ENTRYPOINT_UNAUTHORIZED`(401, 토큰 불일치) · `ENTRYPOINT_TOKEN_MISSING`(503 또는 비밀값 조회 실패) · `ENTRYPOINT_REJECTED`(400, 본문 검증) · `ENTRYPOINT_UNREACHABLE`(연결 실패·시간 초과) · `ENTRYPOINT_ERROR`(그 밖의 응답) · `ENTRYPOINT_ENV_UNMAPPED`(진입점이 모르는 이름이거나 두 연결이 같은 이름). 토큰·응답 원문은 로그·코드에 넣지 않는다.

## 가짜 구현

| 항목 | 값 |
|---|---|
| 걸리는 시간 | `FAKE_EXECUTOR_STEP_SECONDS`(기본 2.0, 시험 0) 뒤 성공 |
| `external_ref` | `fake-<종류>\|<멱등 키>\|<시작 ms>\|<결과를 정하는 값>` — 일꾼 프로세스가 바뀌어도 같은 답 |
| 빌드 결과 | 이미지 `registry.echo.test/<서비스 이름>`, digest `sha256(서비스 id + 커밋)` |
| 대표 주소 | 늘 적용(`applied_at` = 그 시각) |
| 헬스체크 | 늘 2xx(`HEALTH_CHECKER_MODE=fake`) |
| 트래픽 | 바로 `TrafficApplied(지금 시각)`. 같은 키면 처음 결과. 연결마다 마지막으로 적용한 비율을 기억한다(가짜 지표가 읽는다) |
| 지표 | 오류율 0.1% · p95 120ms · 요청 수 `max(1, round(rps × 비율/100 × 창 초))` (결정적, rps = `MIGRATION_EXPECTED_RPS` 기본 50) |
| 요청 카운터 | 연결마다 처음 읽은 순간 0 에서 시작해, 읽을 때마다 "rps × 그 연결의 지금 비율/100 × 지난 초"를 더한다(주입 시계, 비율이 바뀌어도 줄지 않음). 5xx 는 0 |
| 데이터 확인 | 복제 지연 0.3초 |
| 이전 실패 주입 | 시험만: 트래픽 `fail_when(키 → 참이면 실패)`, 지표·데이터 확인 `unavailable = True`, 카운터 `freeze`(고정)·`add_errors`(5xx 더하기)·`set_counts`(값 바꾸기, 줄이면 재시작 흉내)·`fail_counts`(읽기 실패, 그동안에도 쌓임) — 연결 id 로 가리킨다. 사용자 계획 단위 실패는 `simulate {failAtStep, reason}`(처리기가 값을 만들고 실행기를 부르지 않는다) |
| 실패 주입 | 시험만: 인스턴스를 만들 때 실패·멈춤 대상 커밋·연결을 넣는다(운영 설정에는 없다). 사용자 요청 단위 실패는 `simulate.failAt` |
| 호출 기록 | `calls`(호출마다 멱등 키), `operation_keys`(만든 작업) — 시험이 "같은 키는 같은 작업"을 확인한다 |
| 로그 | 빌드 5~7줄(저장소 받기·이미지 만들기·digest 기록 등), 실행 5줄(이미지 받기·서버 시작·헬스 경로 응답 등). 줄 시각은 `external_ref` 의 시작 시각부터 2초 간격(시계를 보지 않는다), cursor 는 줄 번호 문자열. 실패 주입 대상이면 마지막 줄이 `ERROR` |
| 로그 주입 | 시험만: `log_injections` 로 "토큰처럼 보이는 줄"을 첫 줄 뒤에 **가리지 않고** 끼워 서버 마스킹을 시험한다(운영 설정에는 없다) |
