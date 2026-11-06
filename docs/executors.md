# 실행기 계약 (빌드·배포·대표 주소·헬스체크·내리기·트래픽·지표·데이터 확인)

원터치 배포의 흐름·상태·DB 기록은 echo-was 일꾼이 맡고, 외부 시스템을 실제로 움직이는 일은 **실행기**가 맡는다. 기본은 가짜 구현(`EXECUTOR_MODE=fake`)이고, `EXECUTOR_MODE=real` 이면 빌드는 GitHub Actions, 배포·내리기는 쿠버네티스 직접 반영(API 서버 3-2, 아래 "실제 빌드·배포·내리기 실행기")이다. GitOps(Argo CD)는 2단계다. 이전 계획(API 서버 8)의 트래픽 나눠 보내기·지표·데이터 복제 확인도 같은 방식이다(실제 연결은 인프라 6 Envoy·인프라 9 OpenTelemetry·인프라 7 복제). 표·API 계약은 바꾸지 않는 것이 목표다.

- 인터페이스: `app/executors/base.py`(Protocol + 값 객체). 가짜 구현: `app/executors/fake.py`. 실제 HTTP 헬스체커: `app/executors/http_health_checker.py`. 실제 고정 진입점 실행기(트래픽·대표 주소·지표): `app/executors/entrypoint.py`. 실제 빌드: `app/executors/github_actions.py`. 실제 배포·내리기: `app/executors/kubernetes/`. 고르는 곳: `app/executors/__init__.py::build_executor_set`.
- 공통 계약 시험: `tests/executor_contract.py`. 실제 구현도 같은 검사를 스텁 전송으로 통과해야 한다.
- 실행기 패키지는 `app.models` 를 import 하지 않는다. 일꾼이 값 객체(`BuildSpec`·`DeploymentSpec`·`DeployTarget`·`MainRoute`·`SourceAccess`)로 바꿔 넘긴다.

## 인터페이스

| 실행기 | 메서드 | 결과 |
|---|---|---|
| `BuildExecutor` | `start(build, source_access, idempotency_key) -> external_ref` | |
| | `poll(external_ref)` | `BuildRunning` · `BuildSucceeded(image_repository, image_digest)` · `BuildFailed(code)` |
| | `read_logs(external_ref, cursor, limit)` | `LogChunk(lines, next_cursor, has_more)` · `InvalidLogCursor` (API 서버 4) |
| `BuildSecurityScanReader` (실제 빌드 실행기만) | `read_security_scan(external_ref)` | `BuildSecurityScan` · `None`(없음·모양 틀림, 예외 없음) (보안·거버넌스 2·11) |
| `DeployExecutor` | `apply(deployment, target, idempotency_key) -> external_ref` | |
| | `poll(external_ref)` | `DeployPending` · `DeployApplied` · `DeployFailed(code, diagnostics)` |
| | `revert(target, previous_deployment \| None, idempotency_key)` | `Reverted(external_ref)` · `RevertFailed(code)` |
| | `read_runtime_logs(target, deployment, external_ref, cursor, limit)` | `LogChunk` · `InvalidLogCursor` (API 서버 4. `external_ref` = 요청의 `deploy_external_ref`) |
| `MainRouteExecutor` | `route_main(route, target, idempotency_key)` | `RouteApplied(applied_at)` · `RouteFailed(code)` |
| `HealthChecker` | `probe(url, path, timeout_seconds)` | `HealthOk(status)` · `HealthHttpError(status)` · `HealthUnreachable` |
| `TeardownExecutor` | `remove(target, idempotency_key) -> external_ref` | |
| | `poll(external_ref)` | `TeardownPending` · `TeardownRemoved` · `TeardownFailed(code)` |
| `TrafficExecutor` | `set_weights(route, weights: tuple[WeightedTarget(target, weight)], idempotency_key)` | `TrafficApplied(applied_at)` · `TrafficFailed(code)` (API 서버 8) |
| `MetricsProvider` | `read_counts(target)` · `read(target, since, until)` | `RequestCounts(requests, errors_5xx)` · `CountsUnavailable(code)` (판정용 누적 카운터, API 서버 8-1) · `Metrics(error_rate_percent, p95_ms, request_count)` · `MetricsUnavailable` (p95 참고용) |
| `DataSyncChecker` | `replication_lag(source, target)` | `DataSyncLag(seconds)` · `DataSyncUnavailable` (API 서버 8) |
| `CutoverRunReader` | `find_first(target, kind, since, until)` | `CutoverRun` · `CutoverRunNotFound` · `CutoverRunUnavailable(code)` (API 서버 16, API 프로세스만) |

모든 실행기는 `simulated` 속성을 가진다(가짜면 true). 결과의 `simulated` 는 요청·빌드 행에 저장된다. 이전 계획의 `simulated` 는 만들 때 `EXECUTOR_MODE = fake` 이면 true 로 계획 행에 저장한다. 이 모드의 묶음은 빌드·배포·내리기·데이터 복제 확인이 늘 가짜라 "계획을 실행할 묶음에 가짜가 하나라도 있으면 true" 와 같고, 트래픽·카운터가 실제 진입점(`ENTRYPOINT_EXECUTOR_MODE=http`)·헬스체크가 실제(`HEALTH_CHECKER_MODE=http`)여도 지금은 true 다([api/migration-plans.md](api/migration-plans.md) 응답).

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

- 단계마다 정해진 코드로 실패를 돌려준다. **권한 오류를 시간 초과로 뭉개지 않는다**(예선에서 권한 오류가 시간 초과로만 보여 원인을 못 찾았다). 빌드 `BUILD_FAILED`, 배포 `DEPLOY_FAILED`, 내리기 `TEARDOWN_FAILED`. 실제 실행기(API 서버 3-2)는 더 나눈다: 빌드를 시작조차 못함 `BUILD_DISPATCH_FAILED`, 클러스터·자격 거절 `CLUSTER_UNAUTHORIZED`, 이미지 받기 실패 `IMAGE_PULL_FAILED`, 컨테이너가 계속 죽음 `CONTAINER_START_FAILED`(DB CHECK 없음, 마이그레이션 없음). 시간 초과(`BUILD_TIMEOUT`·`DEPLOY_TIMEOUT`·`TEARDOWN_TIMEOUT`)는 일꾼이 단계 시작 시각으로 정한다(빌드 `builds.started_at`, 배포 `deploy_requests.deploying_at`, 내리기 `teardown_requested_at`).
- 실패 코드·`external_ref` 에 **비밀값을 넣지 않는다**(설치 토큰·kubeconfig·레지스트리 비밀번호 등). 응답의 `errorMessage` 는 코드별 고정 문장이라 외부 원문은 사용자에게 나가지 않는다.

## 실행기별 계약

### 빌드 (`BuildExecutor`)

- 입력: `BuildSpec(build_id, service_id, service_name, repo_url, commit_sha)`, `SourceAccess(repo_url, commit_sha, read_token)`. `read_token` 은 비공개 저장소일 때만 있는 GitHub App 설치 토큰(1시간)이며 **메모리로만** 넘긴다. 저장·로그 금지.
- 결과: 이미지 저장소 주소(tag 없음) + `sha256:` digest. 배포는 digest 로만 한다(`latest` 같은 바뀌는 tag 금지).
- 같은 서비스·저장소·커밋의 빌드는 일꾼이 재사용하므로 실행기는 한 커밋에 한 번만 불린다(실패 뒤에는 새 `builds.id` 로 다시).

### 배포 (`DeployExecutor`)

- 입력: `DeploymentSpec(deployment_id, image_repository, image_digest, config_snapshot, env_values)`(`env_values` 는 아래 "앱 환경 변수"), `DeployTarget(service, service_environment, environment kind·이름·config·secret_ref, public_host(환경별), main_public_host(대표), container_port)`.
- `apply` 는 그 연결(클러스터의 그 서비스)에 digest 이미지를 반영한다. `DeployApplied` 는 "클러스터가 새 버전을 받아들였다"이다. 사용자 트래픽 확인은 일꾼의 헬스체크가 환경별 퍼블릭 주소로 한다.
- `poll` 의 `DeployFailed.diagnostics` 는 선택이다(컨테이너 실패를 판정한 실행기만 채운다). 처리기는 그 배포의 앱 환경 변수 값으로 `log_tail` 을 한 번 더 가린 뒤 배포 작업 로그(`CONTAINER_DIAGNOSTICS`·`CONTAINER_LOG`)에 쓰고 `FAILED` 문장에 원인 한 줄을 붙인다(API 서버 4-1). 가짜 실행기는 배포 실패 때 `crash_log_tail`(기본: `ValidationError: database_url` 로 끝나는 7줄)로 같은 모양을 낸다.
- `revert(target, previous, key)` 는 실패한 새 버전을 거두고 `previous`(연결의 현재 배포, 없으면 아무것도 없는 상태)를 유지한다. 실패해도 일꾼은 요청의 `errorCode` 를 바꾸지 않고 `revertStatus = FAILED` 만 적는다.
- 실제 구현(API 서버 3-2): 쿠버네티스 직접 서버 측 apply(아래 "쿠버네티스 직접 반영"). GitOps(Argo CD)는 2단계.

### 대표 주소 (`MainRouteExecutor`)

- 서비스에 서비스 중 연결이 없을 때, 성공한 연결로 대표 주소(`{service}-{suffix}.{domain}`, 저장한 `services.public_host`)를 보낸다. `RouteApplied(applied_at)` 일 때만 일꾼이 `traffic_weight = 100`, `traffic_applied_at = applied_at` 을 적는다("프록시에 마지막으로 적용한 비율"이라는 칸 뜻을 지킨다).
- 가짜 구현은 I/O 가 없어 성공 처리 트랜잭션 안에서 부른다. 실제 구현(echo-infra#5 고정 진입점 프록시)의 호출 위치(트랜잭션 밖 + 멱등 키)는 실제 구현 작업에서 정한다.

### 헬스체커 (`HealthChecker`)

- `GET {url}{path}` 한 번. 2xx 면 `HealthOk`, 그 밖의 응답(3xx 포함, **리다이렉트 따르지 않음**)은 `HealthHttpError(status)`, 연결 실패·시간 초과는 `HealthUnreachable`.
- `url` 은 **환경별 퍼블릭 주소**다. 대표 주소는 성공 처리 뒤에야 이 환경을 가리키므로 확인하지 않는다(실제 라우터 연결 뒤 추가).
- 실제 구현 `HttpHealthChecker` 는 이미 있다(`HEALTH_CHECKER_MODE=http`). 요청 하나 제한 시간은 `HEALTH_CHECK_REQUEST_TIMEOUT_SECONDS`(5).

### 로그 읽기 (`read_logs` · `read_runtime_logs`, API 서버 4)

로그 API(`GET /deploy-requests/{id}/logs?source=BUILD|RUNTIME`)가 API 프로세스에서 즉석으로 부른다(저장하지 않는다). API 프로세스는 `app/dependencies.py::get_executor_set` 으로 실행기를 얻는다(`fake` 는 요청마다 가짜 묶음, `real` 은 프로세스당 하나인 실제 묶음).

- **상태를 바꾸지 않는 조회**다. 트랜잭션 밖에서 부른다.
- `limit` 을 넘지 않는 줄을 **시간 순**으로 돌려준다. **같은 `cursor` 면 같은 줄**이다(다른 프로세스가 불러도). `next_cursor` 로 이어 읽으면 겹침·누락이 없다. 줄이 없으면 받은 `cursor` 를 그대로 돌려준다.
- 알아보지 못하는 `cursor` 는 `InvalidLogCursor`(API 는 422 `VALIDATION_ERROR`, `reason = INVALID_CURSOR`). 보존 기간이 지나 없으면 빈 청크다.
- **비밀값을 가려서 돌려준다**(설치 토큰·레지스트리 비밀번호·환경변수 값·kubeconfig 등). 서버는 응답 직전에 `app/core/log_masking.py::mask_secrets` 로 알려진 모양(GitHub 토큰 `gh[pousr]_…`, AWS 액세스 키 `AKIA…`, `Bearer <8자 이상>`, `sk-…`, `-----BEGIN … PRIVATE KEY-----` 블록)을 `***` 로 한 번 더 바꾸지만, 이것은 방어용이며 실행기의 책임을 대신하지 않는다.
- API 는 실행기의 `simulated` 가 그 빌드·요청 행의 `simulated` 와 다르면(가짜로 만든 기록을 실제 실행기가 읽는 경우 등) 부르지 않고 빈 페이지를 준다.
- 공통 계약 시험: `tests/executor_contract.py::assert_log_reader_contract`(같은 cursor 같은 줄, limit 준수, 시간 순, 이어 읽기, 잘못된 cursor).
- 실제 구현: 빌드는 Actions 잡 로그(`external_ref` = `gha:{run id}`), 실행은 그 연결 네임스페이스의 Pod 로그(`target` + 배포 기록으로 고른다). cursor 는 실행기가 정하는 불투명한 문자열이다(아래 각 절).

### 트래픽 (`TrafficExecutor`, API 서버 8)

- 대표 주소(`route`)의 트래픽을 `weights` 대로 연결들에 나눠 보낸다. 이전 계획은 늘 두 연결(출발·도착)을 넘긴다: 단계 1 `(90, 10)`, 단계 2 `(50, 50)`, 단계 4 `(0, 100)`, 롤백 `(100, 0)`.
- 비율 합계가 100 이 아니거나 0~100 밖이면 `ValueError`(`check_weights_total`). 부르는 쪽의 버그라 실행기 결과로 돌려주지 않는다.
- **적용했다고 답할 때만** 일꾼이 비율 칸을 적는다. `TrafficFailed` 면 비율 칸은 바꾸지 않는다(단계 실패 `TRAFFIC_APPLY_FAILED` → 롤백, 롤백 실패면 다음 시도).
- 같은 키면 같은 결과다. 다시 시작한 일꾼은 `traffic_requested_at`(또는 `rollback_requested_at`)만 있고 결과가 없으면 같은 키로 다시 부른다.
- 연결안: Envoy 가중치 클러스터(인프라 6). 같은 키를 Envoy 설정 버전 이름에 넣어 두 번 적용하지 않는다.

### 지표·요청 카운터 (`MetricsProvider`, API 서버 8 · 8-1)

메서드가 둘이다. 이전 판정은 **누적 카운터(`read_counts`)** 로 하고, 구간 지표(`read`)는 p95 참고 값으로만 쓴다(8-1). 둘 다 상태를 바꾸지 않는 조회이고 트랜잭션 밖에서 부른다. 실제 구현은 고정 진입점의 `EntrypointMetricsProvider`(#17, `app/executors/entrypoint.py`) 한 클래스가 두 메서드를 함께 구현한다. `read` 는 `GET /summary`, `read_counts` 는 `GET /counters`(API 서버 8-2)를 부른다(아래 "고정 진입점 실행기").

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

**`GET /counters` 매핑(`EntrypointMetricsProvider.read_counts`, API 서버 8-2)**

진입점 응답(Bearer, echo-infra `entrypoint/weights-api`):

```json
{
  "ts": "2026-11-03T06:17:00Z",
  "envs": {
    "aws": {"requests": 12345, "errors_5xx": 3, "hosts": 1},
    "gcp": {"requests": 678, "errors_5xx": 0, "hosts": 1}
  }
}
```

값은 Envoy 가 켜진 뒤의 누적이다. 호스트 교체·재시작 때 줄 수 있고 그대로 돌려준다. 읽기 한 번에 `/counters` 한 번이다(출발·도착을 각각 부르면 두 번, 묶지 않는다). 아래 순서로 본다.

| 순서 | 진입점 | 결과 |
|---|---|---|
| 1 | 호출 실패, 응답이 객체가 아니거나 `envs` 가 객체가 아님 | `CountsUnavailable(code)`, 코드는 아래 실패 코드 표 |
| 2 | `envs[<환경 이름>]` 이 없거나 객체가 아님(이름은 `environment_name(target)`: 환경 id 매핑 → 종류 매핑 → 종류) | `CountsUnavailable("ENTRYPOINT_ENV_UNMAPPED")` |
| 3 | `requests`·`errors_5xx` 가 0 이상 정수가 아님(bool·실수·문자열·음수·없음) | `CountsUnavailable("ENTRYPOINT_ERROR")` |
| 4 | `hosts` 가 있고 0 | `CountsUnavailable("ENTRYPOINT_NO_HOSTS")`. `hosts` 가 0 이상 정수가 아니면 `ENTRYPOINT_ERROR`, 키가 없으면 보지 않는다(옛 진입점 호환) |
| 5 | 그 밖 | `RequestCounts(requests, errors_5xx)`. 값을 보정하지 않는다. `ts` 는 쓰지 않는다(오래된 읽기 판단은 일꾼이 자기 시계로 한다) |

**`hosts` 가 0 이면 판단 불가로 돌려주는 이유**: 그 환경으로 갈 업스트림이 없으면 요청 0건은 "조용한 서비스"가 아니라 "길이 없음"이다. 0건을 그대로 주면 8-1 판정이 최대 대기 끝에 트래픽 적음(`LOW_TRAFFIC`)으로 통과시켜 버린다. `CountsUnavailable` 이면 마감에 `METRICS_UNAVAILABLE` 로 롤백한다.

실패는 `logger.warning("entrypoint counts unavailable")` 에 코드와 진입점 환경 이름만 남긴다(토큰·응답 원문 없음).

**전제(환경당 고객 앱 1개)**: 카운터와 가중치가 진입점 전체·환경 단위라, 같은 환경에 고객 앱이 둘 이상이면 다른 앱의 요청이 섞여 판정이 틀린다. 시연은 고객 앱 1개라 코드로 막지 않는다(앱별 라우트·카운터는 진입점 후속).

**`read(target, since, until)`**: 연결 하나의 `[since, until)` 구간 오류율(%)·p95(ms)·요청 수. 일꾼은 비율 단계를 끝낼 때(통과·실패 모두) `until = 지금`, `since = 지금 − MIGRATION_P95_REFERENCE_WINDOW_SECONDS`(30초)로 한 번 불러 `p95Ms` 를 참고로 남긴다. `MetricsUnavailable` 이어도 판정에 영향이 없다(`p95Ms = null`). 실제 구현: 진입점 `GET /summary?window=30s`(#17, 구간 초를 반올림해 넘긴다).

### 데이터 확인 (`DataSyncChecker`, API 서버 8)

- 출발 → 도착 데이터 복제 지연(초). 읽을 수 없으면 `DataSyncUnavailable`(판정 `METRICS_UNAVAILABLE`).
- 실제 구현(`DATA_SYNC_CHECKER_MODE=postgres`, 인프라 7): `PostgresDataSyncChecker`. **출발(발행 쪽)** PostgreSQL 에 `pg_monitor` 권한 계정(`DATA_SYNC_USER`, 기본 `echo_monitor`)으로 붙어 `pg_stat_replication` 에서 **도착 구독 이름**(= walsender `application_name`)의 `replay_lag` 를 읽는다. echo-infra `replication/k8s/scripts/lag.sh` 와 같은 질의다. 쓰기가 멈춰 `replay_lag` 가 NULL 이면 남은 WAL 바이트가 0 일 때만 0초로 본다.
- **도착이 이미 쓰기 담당이면 통과한다**(API 서버 8-5, 트래픽만 이전하고 DB 컷오버를 하지 않은 뒤의 복귀). 출발에 그 walsender 행이 없을 때 ① 같은 출발 연결에서 `pg_subscription` 에 출발 구독 이름이 enabled 로 있거나 ② 도착 DB `pg_stat_replication` 에 출발 구독 이름의 행이 있으면(상태 무관) `DataSyncLag(seconds=0.0, reason="TARGET_IS_WRITER")` 다. 처리기는 `DATA_SYNC_OK` 로그 data 에 `reason` 을 더한다(단계 metrics·응답 모양은 그대로). 출발에 행이 있으면 ①·② 를 보지 않고 지금처럼 지연을 잰다. 접속은 최대 2번(각각 제한 시간 적용).
- 접속처는 `DATA_SYNC_DATABASES`(`<환경 id 또는 종류>=<host>:<port>/<그 환경이 구독자일 때의 구독 이름>`, 쉼표 구분). 환경 id → 환경 종류 순서로 찾는다(진입점 매핑과 같다). 예: `aws=<A NLB>:5432/echo_sub_rev,gcp=echo-pg.echo-data.svc.cluster.local:5432/echo_sub` — 정방향(aws→gcp)은 A 에서 `echo_sub`, 역방향은 B 에서 `echo_sub_rev` 를 본다.
- 비밀번호는 비밀 참조(`DATA_SYNC_PASSWORD_REF`)로만 받아 호출마다 읽는다. TLS 는 `DATA_SYNC_SSL_MODE`(`require` 기본 = 암호화만·자체 서명 허용, `verify-full`, 로컬 PoC 용 `disable`).
- 매핑 없음·비밀값 없음·출발 접속 실패(시간 초과 `DATA_SYNC_TIMEOUT_SECONDS`, 기본 3초)·출발 구독자가 streaming 아님·도착이 쓰기 담당이라는 근거 없음(도착 접속 실패 포함)은 예외 대신 `DataSyncUnavailable` 이고, 경고 로그에는 코드(`DATA_SYNC_ENV_UNMAPPED`·`DATA_SYNC_SECRET_MISSING`·`DATA_SYNC_UNREACHABLE`·`DATA_SYNC_NOT_STREAMING`)와 환경 id 만 남긴다(주소·비밀값 없음).
- 이전 계획의 `simulated` 는 지금처럼 `EXECUTOR_MODE` 로만 정한다(이 실행기를 켜도 바뀌지 않는다).

### 컷오버 결과 읽기 (`CutoverRunReader`, API 서버 16)

- 인프라 스크립트(`cutover.sh`·`rollback.sh`)가 고객 DB 표 `echo_ops.cutover_runs` 에 쓴 행을 읽어 이전 계획 상세 `result` 로 보여 준다([api/migration-plans.md](api/migration-plans.md)). 쓰지 않는다. 상태를 바꾸지 않아 멱등 키가 없고, 부르는 쪽(`MigrationPlanResultService`)이 트랜잭션 밖에서 부른다.
- 기본 구현 `NoCutoverRuns`: 늘 `CutoverRunNotFound`. 가짜 값을 만들지 않으므로 `simulated` 속성이 없다. 행을 주입하는 읽기는 `tests/` 에만 있다.
- 실제 구현 `PostgresCutoverRunReader`(`DATA_SYNC_CHECKER_MODE=postgres` 이고 `CUTOVER_RUNS_DATABASE` 가 비지 않을 때 `build_executor_set` 이 넣는다): **도착 환경**의 `DATA_SYNC_DATABASES` 매핑(환경 id → 종류)에서 host·port 만 쓰고(구독 이름은 쓰지 않는다), 계정·비밀번호 참조·TLS·제한 시간은 데이터 확인과 같다. DB 이름만 `CUTOVER_RUNS_DATABASE`(기본 `app`)다. 호출마다 연결 → `fetchrow` → `close` 한다(풀 없음, 부르는 쪽의 5초 캐시가 횟수를 줄인다).
- 계정 `echo_monitor` 에는 `echo_ops` 의 `USAGE` 와 `cutover_runs` 의 `SELECT` 만 더 있다(echo-infra `replication/k8s/scripts/common.sh`).
- 제한 시간이 지나면 `close` 를 기다리지 않고 `terminate` 로 끊는다(끊긴 망에서 `close` 가 제한 시간을 넘기지 않게).
- 실패는 예외 대신 `CutoverRunUnavailable(code)`: 매핑 없음 `CUTOVER_RESULT_ENV_UNMAPPED`, 비밀값 없음 `CUTOVER_RESULT_SECRET_MISSING`, 접속 실패·시간 초과(`OSError`·`TimeoutError`·`asyncpg.InterfaceError`) `CUTOVER_RESULT_UNREACHABLE`, 서버가 거절(`asyncpg.PostgresError`: 권한 없음·표 없음 등) `CUTOVER_RESULT_QUERY_FAILED`. 경고 로그는 부르는 쪽이 코드와 계획·환경 id 로만 남긴다.

### 내리기 (`TeardownExecutor`)

- 서비스 삭제 때 연결 하나를 클러스터에서 내린다(실제 구현은 앱 네임스페이스 삭제). `TeardownRemoved` 면 일꾼이 연결을 소프트 삭제한다. 배포 기록은 남는다.

## 실제 빌드·배포·내리기 실행기 (`EXECUTOR_MODE=real`, API 서버 3-2)

SPEC [specs/api-3-2-real-executors.md](../specs/api-3-2-real-executors.md). `EXECUTOR_MODE=real` 이 빌드·배포·내리기 세 실행기를 함께 바꾼다(트래픽·대표 주소·지표는 `ENTRYPOINT_EXECUTOR_MODE` 가 따로 고른다). **운영 기본값은 `fake`** 이고, 저장소 밖 준비(SPEC 7절)와 AC-9 확인 뒤에 바꾼다.

| 실행기 | 파일 | 외부 시스템 |
|---|---|---|
| `GithubActionsBuildExecutor` | `app/executors/github_actions.py` | GitHub Actions `workflow_dispatch`(빌드 저장소의 `customer-build.yml`) |
| `KubernetesDeployExecutor`·`KubernetesTeardownExecutor` | `app/executors/kubernetes/executors.py` | 쿠버네티스 REST 직접 호출(SDK 없음, `httpx`) |
| 클러스터 접속기 `ClusterAccessProvider` | `app/executors/kubernetes/access.py` | GKE API·AWS STS·EKS·메타데이터 서버·환경 secret_ref |
| `CloudflareDnsClient` | `app/executors/cloudflare_dns.py` | Cloudflare DNS(온프레 앱 주소) |
| 매니페스트(순수 함수) | `app/executors/kubernetes/manifests.py` | — |

**만들 때 부작용이 없다.** 비밀값·토큰 조회와 필수값 확인은 처음 호출할 때 한다. 실행기 묶음은 프로세스당 하나다: 일꾼은 `python -m app.worker` 시작 때, API(로그 읽기)는 `app/dependencies.py::ExecutorSetHolder`(첫 요청 때 만들고 lifespan 종료 때 HTTP 클라이언트를 닫음). `poll` 처럼 external_ref 만 받는 호출은 환경 접속 정보를 `DbEnvironmentLookup`(`app/services/executor_environment_lookup.py`)으로 다시 읽는다(실행기 패키지는 `app.models` 를 import 하지 않는다).

### 대상 값 (`DeployTarget` 더한 칸)

| 칸 | 채우는 곳(`deploy_support.deploy_target`) | 쓰임 |
|---|---|---|
| `environment_name` | 환경 이름 | 앱 환경변수 `ECHO_ENVIRONMENT` |
| `environment_config` | `environment.parsed_config()`(읽지 못하면 None → 설정 오류) | 클러스터 접속·Ingress 값 |
| `environment_secret_ref` | 환경 `secret_ref` | 온프레 창구 토큰 위치 |
| `main_public_host` | 서비스 대표 주소 | Ingress 두 번째 규칙, **앱 네임스페이스 = 첫 레이블** `{서비스}-{suffix}` |

`public_host` 는 그대로 환경별 주소다. 헬스 경로·포트는 배포 기록의 `config_snapshot`(`healthCheckPath`·`containerPort`)에서 읽는다(되돌리기의 previous 도 자기 사본).

### 실패를 알리는 예외와 일시 오류

| 상황 | 실행기 | 처리기 |
|---|---|---|
| 빌드를 시작할 수 없다(dispatch 403·404·422, App 설치·개인키 없음) | `BuildStartError(BUILD_DISPATCH_FAILED)` | 빌드를 그 코드로 닫는다 → 다음 실행에서 요청이 그 코드로 `FAILED` |
| 반영할 수 없다(창구·자격 401·403 → `CLUSTER_UNAUTHORIZED`, 환경 설정 누락·남의 네임스페이스·DNS 충돌·창구 4xx → `DEPLOY_FAILED`) | `DeployApplyError(code)` | 요청을 그 코드로 `FAILED`(배포 기록이 있어 되돌리기 호출) |
| 시작 계열(start·apply·remove)이 닿지 못함(연결 실패·5xx·429) | 1·2·4초 간격 3번 재시도 뒤 `ExecutorUnavailableError` | 잡지 않는다. 임대 만료 뒤 같은 키로 다시, 3번이면 `WORKER_LOST` |
| 조회 계열(poll)이 닿지 못함 | 진행 중(`BuildRunning`·`DeployPending`·`TeardownPending`) + 경고 로그(`*_UNREACHABLE`) | 단계 제한 시간(`BUILD_TIMEOUT`·`DEPLOY_TIMEOUT`·`TEARDOWN_TIMEOUT`)이 끝낸다 |
| 되돌리기 실패 | `RevertFailed(code)`(예외를 올리지 않는다 — 임대 상실 경로에서도 끝나게) | `revertStatus = FAILED` |

알아보지 못하는 ref(`fake-…` 등, 진행 중 모드 전환)는 `BuildFailed`/`DeployFailed(DEPLOY_FAILED)`/`TeardownFailed` 다. 경고 로그에는 코드(`ENVIRONMENT_NOT_CONFIGURED`·`CLUSTER_UNAUTHORIZED`·`CLUSTER_UNREACHABLE`·`NAMESPACE_NOT_MANAGED`·`DNS_RECORD_CONFLICT`·`BUILD_RESULT_INVALID` 등)와 id 만 남기고 토큰·응답 원문은 남기지 않는다.

### GitHub Actions 빌드 (`GithubActionsBuildExecutor`)

워크플로 본문은 인프라 담당이 아래 계약대로 만든다(이 저장소는 실행기만 가진다).

| 항목 | 값 |
|---|---|
| 워크플로 | `BUILD_GITHUB_REPOSITORY`(기본 `Softbank-Hackathon-2026-Team-Echo/echo-was`)의 `.github/workflows/customer-build.yml`(`BUILD_GITHUB_WORKFLOW`), ref `BUILD_GITHUB_REF`(`main`) |
| 시작 | `on: workflow_dispatch`, 입력 `build_id`(uuid)·`repo`(`owner/name`)·`commit_sha`(40자)·`private`(`"true"`/`"false"`) — 모두 문자열 |
| 실행 이름 | `run-name: build ${{ inputs.build_id }}` — 실행 객체의 **`display_title`** 칸. 일꾼이 같은 빌드를 찾는 열쇠(`name` 은 워크플로 이름이라 쓰지 않는다) |
| 동시 실행 | `concurrency: customer-build-${{ inputs.build_id }}` |
| 소스 | 비공개면 워크플로 안에서 `actions/create-github-app-token`(App id·개인키 = Actions secret, `owner`·`repositories` = 고객 저장소)으로 1시간 읽기 토큰 → 그 커밋 checkout. **토큰을 입력값으로 받지 않는다** |
| 빌드·게시 | ECR `echo/app` + Artifact Registry `echo/app`, 같은 digest, 태그 `b-<build_id 앞 12자>` |
| 결과 | 아티팩트 `build-result`(파일 `build-result.json`): `{"build_id", "image_digest": "sha256:…", "ar_repository", "ecr_repository"}` |
| 보안 검사 원문 | 아티팩트 `security-scan`(보안·거버넌스 11, `specs/sec-11-build-scan.md`): `meta.json`(`schema_version` 1·`build_id`·`image_digest`·`scanned_at`·`trivy.version`·`gitleaks.version` …) · `trivy-image.json` · `gitleaks.json`. 빌드 성공 판정과 상관없다(없어도 `BuildSucceeded`) |
| 실패 | 단계 이름 고정(`checkout`, `build`, `push`) |

- 인증: GitHub App JWT(`GITHUB_APP_ID`·`GITHUB_APP_PRIVATE_KEY_REF`) → 빌드 저장소 설치 → 설치 토큰(`permissions: {actions: write}`, 만료 5분 전까지 재사용). App 에 **Actions 읽기·쓰기** 권한과 빌드 저장소 설치가 필요하다.
- `start(build, source, key)`: ① `GET …/workflows/{워크플로}/runs?event=workflow_dispatch&branch={ref}&per_page=50` 에서 `display_title == "build {key}"`(여럿이면 먼저 만든 것). ② 없으면 `POST …/dispatches` 본문 `{"ref", "inputs", "return_run_details": true}` → 200 `workflow_run_id`(204 면 ①을 5초 간격 3번). ③ external_ref = `gha:{run id}`.
  - 재시작 경로(`BuildSpec.resumed_after` = 이미 있던 `start_requested_at`): ①을 5초 간격 3번(만든 시각 ≥ 요청함 시각 − 60초) 해 본 뒤에만 dispatch 한다. 그래도 중복이 생기면 ref 가 가리키는 실행만 쓴다(같은 커밋·같은 태그).
  - `SourceAccess.read_token` 은 쓰지 않는다. 처리기는 비공개 저장소 접근 확인을 위해 지금처럼 `issue_read_token` 을 부른다.
- `poll`: `GET …/actions/runs/{id}` — `queued|in_progress|waiting|pending|requested` → `BuildRunning`, `completed`+`success` → 아티팩트를 내려받아(302 로 넘겨받은 저장소 주소에는 우리 토큰을 싣지 않는다) `BuildSucceeded(ar_repository, image_digest)`, 그 밖 → `BuildFailed(BUILD_FAILED)`. 아티팩트가 없거나, zip·JSON 모양이 다르거나, digest 가 `^sha256:[0-9a-f]{64}$` 가 아니거나, 저장소에 tag(`:`)가 있거나, `build_id` 가 실행 이름의 키와 다르면 `BuildFailed` + 경고 `BUILD_RESULT_INVALID`.
- `read_security_scan`(보안·거버넌스 2-3): 같은 실행의 `security-scan` 아티팩트를 `build-result` 와 같은 방법으로 받는다. `meta.json` 이 없거나 `schema_version` 이 1 이 아니거나 `build_id`·digest 모양이 틀리면 `None` + 경고 `SECURITY_SCAN_INVALID`, 아티팩트가 없으면 `None` + `SECURITY_SCAN_MISSING`. 아티팩트 zip 이 8MB(목록의 `size_in_bytes` 또는 받은 크기)를 넘으면 받지 않고 `None` + `SECURITY_SCAN_TOO_LARGE`. 보고서 파일은 없거나 JSON 이 아니거나 깨졌거나(zlib·EOF·지원하지 않는 압축·암호) 풀린 크기가 8MB 를 넘으면 그 칸만 `None`. 일시 오류·인증 실패도 `None`(예외 없음). `source_ref` = `github-actions:{빌드 저장소}/actions/runs/{run id}`.
  - 배포 처리기가 빌드를 `SUCCEEDED` 로 바꾼 직후 한 번 부른다(`BuildSecurityScanCollector`, 재사용 빌드는 다시 부르지 않음). 원문의 `build_id`·digest 가 그 빌드와 다르면 버리고, 맞으면 C-IMG-1·C-IMG-2 증거 3건을 `findings` 에 남긴다. 보고서가 `None` 이면 그 증거는 UNKNOWN. 원문 읽기부터 정규화까지의 어떤 예외도 경고(예외 종류만) + 0건이고 배포는 계속한다. 반영과 수집 사이에 일꾼이 죽은 빌드는 다시 모으지 않는다.
- `read_logs`: 실행의 잡 로그(`GET …/actions/jobs/{job}/logs`)를 잡 순서(시작 시각·id)·줄 순서로 잇는다. cursor = `"{잡 순번}:{줄 번호}"`(다음에 읽을 줄). 실행이 끝나기 전에는 빈 청크(`has_more=true`, 같은 cursor). `##[error]`·`##[warning]` 줄은 `ERROR`·`WARN`. Actions 가 이미 가린 값 위에 `mask_secrets` 를 한 번 더 거친다. 끝난 실행의 로그는 프로세스에 최근 32개 기억한다.

### 쿠버네티스 직접 반영 (`KubernetesDeployExecutor`)

한 연결 = 한 네임스페이스 `{서비스}-{suffix}`(대표 주소 첫 레이블), 라벨 `app.kubernetes.io/managed-by=echo-was`·`echo.service-id`. 서버 측 apply = `PATCH …?fieldManager=echo-was&force=true`, `Content-Type: application/apply-patch+yaml`(본문 JSON).

| 객체 | 이름 | 내용 |
|---|---|---|
| Namespace | `{서비스}-{suffix}` | 없으면 `POST` 로 만든다(apply 권한 없이). 같은 이름이 있는데 echo-was 라벨이 없으면 쓰지 않는다(`DEPLOY_FAILED`, `NAMESPACE_NOT_MANAGED`) |
| Secret | `{ns}-env` | 앱 환경 변수. `env_values` 가 None 이 아닐 때만 다룬다(아래 "앱 환경 변수"). 서버 측 apply 가 아니라 GET → PUT·POST 로 통째로 쓴다 |
| Deployment | `app` | replicas 1, 이미지 `{환경별 저장소}@{digest}`, 포트 `containerPort`, env `PORT`·`ECHO_ENVIRONMENT`(env 가 envFrom 보다 우선 — 고객 Secret 이 덮지 못함), `envFrom: secretRef {ns}-env optional`, readinessProbe `GET {healthCheckPath}`, 어노테이션·파드 라벨 `echo.deployment-id`, 앱 환경 변수가 있으면 파드 템플릿 어노테이션 `echo.env-revision`, 온프레만 `imagePullSecrets: echo-pull`, 요청 100m/128Mi·제한 512Mi |
| Service | `app` | ClusterIP 80 → containerPort |
| Ingress | `app` | 규칙 = 환경별 주소 + 대표 주소(→ `app:80`). class = config `ingressClass`(비면 aws `alb`, gcp `nginx`, onprem `traefik`). aws 는 `group.name`=`ingressGroup`, `scheme: internet-facing`, `listen-ports [{"HTTP":80},{"HTTPS":443}]`, `certificate-arn`=`certificateArn`(명시 — 자동 찾기는 대표 주소용 인증서까지 찾다가 그룹 전체가 실패), `target-type: ip`, `healthcheck-path`. 80→443 강제 이동 없음(진입점이 HTTP 로 대표 주소를 보낸다) |

- 이미지 주소: 배포 기록의 `image_repository`(Artifact Registry 정본). aws 환경이면 `IMAGE_REGISTRY_MIRRORS`(`<AR 접두>=<ECR 접두>`)로 바꾼다(같은 digest). 표에 없으면 그대로.
- `apply(deployment, target, key)`: (온프레) DNS 레코드 → 네임스페이스 → (온프레) RoleBinding `echo-app-deployer`(자기 서비스 계정, 토큰 JWT 의 `sub` 에서 읽음) → 앱 Secret `{ns}-env`(`env_values` 가 None 이 아닐 때) → (온프레) 이미지 받기 자격 `echo-pull` → Deployment·Service·Ingress. external_ref = `k8s:{환경 id}:{네임스페이스}:{배포 기록 id}`. 같은 키 다시 → 같은 내용(멱등). RoleBinding 을 막 만든 직후의 403 은 2초 간격 5번(10초) 다시 시도한다(권한 캐시 반영 지연).
  - 필수 칸이 비면(onprem `apiUrl`·`secret_ref`·`tunnelTarget`·Cloudflare 설정, aws `certificateArn`·`ingressGroup`·역할, config 를 읽지 못함, 대표 주소 없음) 창구를 부르지 않고 `DeployApplyError(DEPLOY_FAILED)` + 경고 `ENVIRONMENT_NOT_CONFIGURED`. 접수 단계에서는 막지 않는다(가짜 모드 시험 유지).
- `poll(ref)`: Deployment `app` 을 읽어
  - 없거나 어노테이션 배포 번호가 ref 와 다르면 `DeployFailed(DEPLOY_FAILED)`(그사이 다른 반영이 덮음)
  - `observedGeneration ≥ generation` 이고 `updatedReplicas == spec.replicas == replicas == availableReplicas` 면 `DeployApplied`
  - 그 배포 번호 파드의 대기 이유가 `ImagePullBackOff`·`InvalidImageName`·`ErrImageNeverPull` 이거나 `ErrImagePull` 이 파드 생성 뒤 `DEPLOY_IMAGE_PULL_GRACE_SECONDS`(30)를 넘으면 `DeployFailed(IMAGE_PULL_FAILED)`, `CrashLoopBackOff` 면 `DeployFailed(CONTAINER_START_FAILED)`
  - 실패를 판정하면 **되돌리기 전에** `DeployFailed.diagnostics`(`ContainerDiagnostics`, API 서버 4-1)를 채운다: 판정한 컨테이너의 `state.waiting.reason`, `state.terminated` 또는 `lastState.terminated` 의 `reason`·`exitCode`, `restartCount`, 그리고 `CONTAINER_START_FAILED` 면 로그 마지막 50줄(`GET …/pods/{pod}/log?container=app&tailLines=51&limitBytes=262144`, 재시작했으면 `previous=true` 먼저 → 비면 지금 컨테이너). 한 줄 400자, 51번째 줄이 있으면 `log_truncated`. 비밀값 모양은 `mask_secrets` 로 가린다. 로그 읽기가 401·403·5xx·400·404 여도 실패 판정은 그대로이고 `log_tail` 만 비며 경고 `CONTAINER_LOGS_UNAVAILABLE`. 이 읽기에는 앱 네임스페이스의 `pods/log` `get` 권한이 필요하다
  - 401·403(토큰을 한 번 새로 받아 다시 불러도) → `DeployFailed(CLUSTER_UNAUTHORIZED)`, 닿지 못함 → `DeployPending` + 경고 `CLUSTER_UNREACHABLE`, 그 밖 → `DeployPending`
- `revert(target, previous, key)`: previous 가 있으면 그 배포 기록으로 같은 apply(어노테이션 = previous 번호) → `Reverted("k8s-revert:{환경}:{ns}:{previous id}")`. 없으면 Ingress·Service·Deployment 를 지운다(네임스페이스·Secret 은 남긴다).
- `read_runtime_logs`: 네임스페이스에서 라벨 `echo.deployment-id=<배포 기록 id>` 파드(가장 최근)의 `app` 컨테이너 로그(`timestamps=true`, 최대 1MB). cursor = `"{파드 이름}|{마지막 줄 시각}|{그 시각 줄 수}"`. 파드가 없으면 빈 청크, 창구 오류도 빈 청크 + 경고.

### 앱 환경 변수 (`{ns}-env`, API 서버 10)

값의 원본은 GCP Secret Manager 한 곳이다(`app/clients/app_secret_store.py`, `AppSecretStore`). DB 에는 이름·참조·버전만 있다(`service_environment_variables`). API 계약은 [api/variables.md](api/variables.md).

**배포 기록에 고정**: `DeployRequestProcessor._deployment_snapshot` 이 배포 기록을 만들 때 `config_snapshot.envVars = {KEY: "<참조>"}` 를 넣는다. 참조는 gcp `gcp-sm:projects/<APP_SECRET_PROJECT_ID>/secrets/<이름>/versions/<n>`(응답의 프로젝트 번호를 쓰지 않는다), 가짜 `fake-sm:<이름>/versions/<n>`.

| 요청 | `envVars` |
|---|---|
| `MANUAL`·`STANDBY_SYNC` | 그 배포의 연결에 살아 있는 변수의 지금 최신 버전. 0개면 `{}`. 예비 따라가기는 원본 환경 값을 복사하지 않는다 |
| `ROLLBACK` | 대상 기록의 사본 그대로(키가 없으면 없는 채로) |
| 실제 실행기 + 가짜 보관함 | 키를 두지 않는다(그동안 Secret 을 맡지 않았으므로, 보관함을 켠 뒤 그 기록으로 되돌려도 Secret 을 건드리지 않게) |
| 재실행(임대 만료 뒤 다른 일꾼) | 이미 만든 기록의 값 그대로 |

**값으로 바꾸기**: 처리기(`AppEnvResolver`)가 실행기 `apply`·`revert` **직전에**, 트랜잭션 밖에서 꺼내 `DeploymentSpec.env_values` 로 넘긴다. 메모리로만 다루고 저장·로그하지 않는다.

| 배포 기록 | `env_values` | 실행기의 `{ns}-env` |
|---|---|---|
| `envVars` 키 없음(이 기능 이전 기록) | None | 읽지도 쓰지도 않음 |
| 참조가 `fake-sm:` 인데 gcp 보관함 | None | 읽지도 쓰지도 않음 |
| 가짜 보관함 + `EXECUTOR_MODE=real` | None | 읽지도 쓰지도 않음 |
| `envVars: {}` | `{}` | 있는 Secret 이 echo-was 소유(라벨 `app.kubernetes.io/managed-by=echo-was`)일 때만 빈 데이터로 교체. 없으면 만들지 않고, 손으로 만든 것은 그대로 |
| `envVars` 1개 이상 | 값 사전 | 통째로 교체 + 파드 템플릿 어노테이션 `echo.env-revision`(정렬한 `KEY=version` 목록의 SHA-256 앞 16자, 값 아님) |

- **통째로 교체**: `GET` → 있으면 `PUT`(`resourceVersion` 을 실어서, `type` 은 있던 값), 없으면 `POST`. 라벨은 `managed-by=echo-was`·`echo.service-id`. 서버 측 apply 는 다른 manager 가 가진 키를 지우지 않아 쓰지 않는다. 그사이 바뀌어 409(또는 사라져 404)면 GET 부터 3번까지 다시 하고, 그래도 안 되면 `DeployApplyError(DEPLOY_FAILED)` 다. 권한·일시 오류는 다른 객체와 같은 규칙이다.
- **손으로 만든 Secret**: 변수가 하나도 없는 연결에서는 절대 건드리지 않는다. 값을 넣고 배포하면 그때 echo-was 소유로 바뀐다(손으로 넣은 키는 남지 않는다).
- **못 꺼낼 때(새 배포)**: 고정 버전을 영구히 못 꺼내면(404·403·`FAILED_PRECONDITION`, 형식이 틀린 참조, 다른 프로젝트 참조) 실행기 `apply` 를 부르지 않고 `ENV_SECRET_UNAVAILABLE` 로 실패한다(배포 기록이 있으므로 기존 규칙대로 `revert` 는 부른다). 5xx·429·연결 실패·메타데이터 일시 오류는 보관함 클라이언트 안에서 1·2·4초 재시도 뒤 `ExecutorUnavailableError`(일꾼이 다시 집고, 임대를 3번 잃으면 `WORKER_LOST`). 로그에는 키 이름만 남긴다.
- **못 꺼낼 때(revert)**: `env_values = None` 으로 `revert` 를 부른다. 직전 Deployment 는 되돌리고 Secret 은 그대로 두며 경고 로그(`ENV_SECRET_UNAVAILABLE`, 키 이름만)를 남긴다.
- **가짜 모드**: 가짜 보관함은 프로세스 메모리라 일꾼이 API 가 넣은 값을 볼 수 없다. `EXECUTOR_MODE=fake` 일 때만 못 찾은 값을 `SecretValue("simulated")` 로 대신한다. 가짜 실행기는 키 이름만 로그로 남긴다.
- **배포 작업 로그**: `apply` 가 돌아온 뒤 `ENV_VARS_APPLIED` 줄에 키 개수와 이름만 쓴다(값 없음, 500자를 넘으면 나머지는 개수로). 변수가 없으면 줄을 쓰지 않는다.

**잠근 채 외부를 부르는 예외**: 트랜잭션 밖에서 외부를 부르는 것이 규칙이지만, 값 넣기 API 는 같은 키의 동시 PUT 순서를 보관함 버전 순서와 맞추려고 **서비스 → 연결 행을 잠근 채** `add_version` 을 부른다. 그래서 호출 전체(재시도 포함)를 `STORE_CALL_TIMEOUT_SECONDS`(10초)로 묶고, 넘으면 502 `SECRET_STORE_UNAVAILABLE` 로 끝낸다. 요청 하나의 제한은 5초다. 호출 뒤 커밋이 실패해도 안 쓰는 버전이 하나 남을 뿐이다(행이 가리키는 버전이 기준).

**보관함**: 비밀 이름은 `{APP_SECRET_NAME_PREFIX}{조직 slug}-{서비스 id 앞 8자}-{환경 id 앞 8자}-{키 소문자, _ → -}`(255자가 넘으면 slug 를 자른다), 라벨 `echo-org`·`echo-service`·`echo-environment`(id 앞 8자)·`managed-by=echo-was`, 복제 automatic. 만들기(409 `ALREADY_EXISTS` 는 성공으로 봄) → `:addVersion`. 토큰은 메타데이터 토큰이고 401 이면 새로 받아 한 번 다시 부른다. 프로세스마다 하나다(API 는 lifespan 보관소, 일꾼은 시작 때).

**보관함 비밀은 echo-was 가 지우지 않는다.** 키 삭제·서비스 삭제 모두 행만 삭제 표시한다(옛 배포 기록·되돌리기가 그 버전을 읽어야 한다). 정리는 라벨로 찾는 후속 일이다. IAM 은 이름 접두어 `echo-app-` 조건으로 좁히고, 접두어를 바꾸면 조건도 같이 바꾼다.

**롤링 배포**: 옛 파드는 새 표와 `envVars` 키를 읽지 않는다(`config_snapshot` 을 읽는 곳은 `healthCheckPath`·`containerPort`·`environmentUrl`·`mainUrl` 키를 골라 읽는다). 새 일꾼이 옛 기록을 집으면 표 첫 줄(None)이다.

### 내리기 (`KubernetesTeardownExecutor`)

- `remove(target, key)`: 네임스페이스 삭제 요청(없으면 성공, echo-was 라벨이 없으면 지우지 않음) + 온프레면 앱 DNS 레코드 삭제(comment 가 같은 것만, 실패는 경고만). external_ref = `k8s-teardown:{환경 id}:{네임스페이스}:{key}`(attempt 마다 다름). 설정이 비면 호출 없이 `k8s-teardown-invalid:{key}` 를 돌려주고 poll 이 `TeardownFailed(TEARDOWN_FAILED)`.
- `poll`: 네임스페이스 404 → `TeardownRemoved`, 있음(Terminating 포함) → `TeardownPending`, 401·403 → `TeardownFailed` + 경고 `CLUSTER_UNAUTHORIZED`, 닿지 못함 → `TeardownPending` + 경고.

### 클러스터 접속기 (`ClusterAccessProvider`)

| 종류 | 창구 주소·CA | 토큰 | 캐시 |
|---|---|---|---|
| gcp | 메타데이터 액세스 토큰으로 `GET container.googleapis.com/v1/projects/{p}/locations/{clusterLocation 또는 region}/clusters/{name}` → `endpoint`, `masterAuth.clusterCaCertificate` | 메타데이터 `…/service-accounts/default/token`(관제탑 GSA) | 토큰 만료 5분 전까지, 창구 정보는 프로세스 동안 |
| aws | STS 임시 키로 서명한 `GET eks.{region}.amazonaws.com/clusters/{name}` → endpoint·CA | ① 메타데이터 `…/identity?audience=sts.amazonaws.com&format=full` ② `AssumeRoleWithWebIdentity`(역할 = config `roleArn` 또는 `EXECUTOR_AWS_ROLE_ARN`, 세션 `echo-executor`) ③ presigned `GetCallerIdentity`(서명 헤더 `x-k8s-aws-id: <cluster>`, 60초) → `k8s-aws-v1.` + base64url(패딩 없음) | 임시 키 만료 5분 전까지, 클러스터 토큰 10분(5분 전 재발급) |
| onprem | config `apiUrl`(공개 인증서, 터널 창구) | 환경 `secret_ref` → 서비스 계정 토큰(만료 없음) | 프로세스 동안 |

- 창구(주소·CA)마다 httpx 클라이언트 하나를 재사용한다(keep-alive. 터널 창구는 새 연결이 1~3초). 요청 하나 제한 시간 `KUBE_REQUEST_TIMEOUT_SECONDS`(15). 401 이면 토큰을 새로 받아 한 번 더 부른다.
- AWS 서명은 `botocore` 만 쓴다(`boto3` 없음). Google 토큰은 메타데이터 서버를 `httpx` 로 직접 부른다(`google-auth` 없음).
- **권한 범위**: 실행기는 앱 네임스페이스(echo-was 라벨) 밖을 쓰지 않는다(이름 규칙 + 라벨 확인 + 시험 `test_executor_writes_only_inside_app_namespace`). GKE·EKS 는 기존 넓은 신원(`roles/container.developer`, EKS Access Entry ClusterAdmin)을 그대로 쓰므로 **이 제한은 코드가 지킨다**(설계 문서에 밝힘). 온프레 서비스 계정 권한은 echo-infra 설치 스크립트가 준다: ClusterRole `echo-executor` = namespaces get·create·delete, rolebindings create, bind 는 `echo-app-deployer` 에만; `echo-app-deployer` = deployments·services·ingresses·secrets·pods·pods/log·events·replicasets 읽기·쓰기. RoleBinding 만들기·네임스페이스 지우기를 라벨 붙은 네임스페이스로 좁히는 ValidatingAdmissionPolicy 는 설치 스크립트 몫이고, 없으면 코드로만 제한된다.

### 온프레 앱 DNS 레코드 (Cloudflare)

- `apply` 전에(온프레만) `{환경별 주소}` CNAME → config `tunnelTarget`, proxied, comment `echo-was:{연결 id}`. 같은 값(CNAME·대상·proxied)이면 그대로, 다른 값이면 `DeployApplyError(DEPLOY_FAILED)` + 경고 `DNS_RECORD_CONFLICT`(남의 레코드를 덮지 않는다). 내리기 때 comment 가 같은 레코드만 지운다.
- 설정 `CLOUDFLARE_API_TOKEN_REF`(`env:ECHO_SECRET_CLOUDFLARE_DNS_TOKEN`, 권한 = 그 존 DNS 편집만)·`CLOUDFLARE_ZONE_ID`. 클라우드 환경은 환경별 와일드카드 레코드 1개를 사람이 미리 만든다.

### 온프레 이미지 받기 자격 (`echo-pull`)

- `apply` 때(온프레만) 메타데이터 액세스 토큰(캐시 없이 새로)으로 dockerconfigjson Secret `echo-pull`(사용자 `oauth2accesstoken`, 서버 = 이미지 주소의 호스트)을 apply 하고 어노테이션 `echo.pull-expires-at` 에 만료 시각(지금 + `expires_in`)을 적는다.
- 일꾼 프로세스의 갱신 루프(`app/services/onprem_pull_refresher.py`): `ONPREM_PULL_CHECK_SECONDS`(300)마다 현재 배포가 있는 살아 있는 온프레 연결을 보고, 남은 수명이 `ONPREM_PULL_MIN_REMAINING_SECONDS`(1800) 미만이면 새 토큰으로 다시 넣는다. 실패는 경고 로그만. 일꾼이 여럿이면 중복으로 돌아도 결과가 같다. 관제탑 GSA 에 Artifact Registry 읽기 권한이 있어야 한다.

### 설정

| 이름 | 기본 | 뜻 |
|---|---|---|
| `EXECUTOR_MODE` | `fake` | `real` 이면 위 실행기. **필수**: `GITHUB_APP_ID`·`GITHUB_APP_PRIVATE_KEY_REF`, `SOURCE_CLIENT_MODE=github`, `HEALTH_CHECKER_MODE=http`(하나라도 빠지면 시작 때 설정 오류) |
| `ENVIRONMENT_ADAPTER_MODE` | `simulated` | 환경 접속 확인 어댑터(API 서버 12). `real` 이면 실제 클러스터의 `/version`·노드 수를 읽는다(접근 경로는 위 실행기와 같다, [api/environments.md](api/environments.md)). 기본 가짜 |
| `EXECUTOR_AWS_ROLE_ARN` | 없음 | aws 환경 config `roleArn` 이 비었을 때의 역할 |
| `BUILD_GITHUB_REPOSITORY`·`BUILD_GITHUB_WORKFLOW`·`BUILD_GITHUB_REF` | `Softbank-Hackathon-2026-Team-Echo/echo-was` · `customer-build.yml` · `main` | 빌드 워크플로 |
| `IMAGE_REGISTRY_MIRRORS` | 빈 목록 | `<AR 접두>=<ECR 접두>,…` 예 `asia-northeast3-docker.pkg.dev/team-echo-final/echo=706878591119.dkr.ecr.ap-northeast-1.amazonaws.com/echo` |
| `CLOUDFLARE_API_TOKEN_REF`·`CLOUDFLARE_ZONE_ID`·`CLOUDFLARE_API_BASE_URL` | 없음 · 없음 · `https://api.cloudflare.com/client/v4` | 온프레 앱 DNS |
| `ONPREM_PULL_CHECK_SECONDS`·`ONPREM_PULL_MIN_REMAINING_SECONDS` | 300 · 1800 | 이미지 받기 자격 갱신 |
| `DEPLOY_IMAGE_PULL_GRACE_SECONDS` | 30 | `ErrImagePull` 유예 |
| `KUBE_REQUEST_TIMEOUT_SECONDS` | 15 | 창구·클라우드 API 요청 하나 |
| `GOOGLE_METADATA_BASE_URL` | `http://metadata.google.internal` | 시험에서 바꾼다 |
| `APP_SECRET_STORE_MODE` | `fake` | 앱 환경 변수 보관함. `gcp` 면 GCP Secret Manager. `fake` + `EXECUTOR_MODE=real` 이면 쓰기 API 가 409 이고 실행기는 `{ns}-env` 를 건드리지 않는다 |
| `APP_SECRET_PROJECT_ID` | 없음 | 보관함 프로젝트 id. `gcp` 면 필수(없으면 시작 때 설정 오류) |
| `APP_SECRET_NAME_PREFIX` | `echo-app-` | 보관함 비밀 이름 접두어. IAM 조건과 같아야 한다 |

- 가짜 모드의 `simulate` 는 `EXECUTOR_MODE=fake` 일 때만 받는다(실제 모드 422 그대로). 실제 모드 기록은 `simulated = false` 로 저장된다.
- **모드 전환 순서**: 이 코드 배포(모드 fake) → 저장소 밖 준비 → 환경 등록 → `EXECUTOR_MODE=real` → 실환경 확인. 진행 중 요청이 있을 때 모드를 바꾸지 않는다(가짜 ref 는 실제 실행기가 실패로 본다). 새 실패 코드는 실제 모드에서만 생기므로 옛 파드가 남아 있는 롤링 중에는 바꾸지 않는다.

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
| `EntrypointMetricsProvider` | `GET /summary?window=<until-since 초>s` 의 `envs.<이름>` | `rps`·`error_ratio`(×100 = 오류율 %)·`p95_ms`, 요청 수 = `rps × 구간 초`. `prometheus != ok` 이거나 `rps` 가 `null`(전환 직후·그 환경에 요청 없음)이면 `MetricsUnavailable`. `p95_ms` 만 `null` 이면 `Metrics.p95_ms = None`(단계 결과 `p95Ms` null, 0 으로 바꾸지 않는다. `error_ratio` 의 null 은 지금처럼 0). 이전 처리기는 p95 참고용으로 `window=30s`(`MIGRATION_P95_REFERENCE_WINDOW_SECONDS`)를 부른다 |
| `EntrypointMetricsProvider.read_counts` | `GET /counters` 의 `envs.<이름>` | `RequestCounts(requests, errors_5xx)` · `CountsUnavailable(code)`. 매핑·실패 순서는 위 "`GET /counters` 매핑". http 모드의 이전 비율 단계는 이 값으로 건수를 센다(8-1) |

**`simulate` 와 실제 진입점**: `simulate` 는 `EXECUTOR_MODE=fake` 면 http 진입점에서도 받는다. 비율은 실제로 `PUT /weights` 로 적용되고 주입은 그 단계의 판정 입력(카운터 읽기·헬스체크·복제 지연)만 처리기가 만든다. 롤백도 실제 가중치를 출발 100 으로 돌린다(`TRAFFIC` 만 그 단계 PUT 을 부르지 않는다).

**관찰 — 처음 요청을 받는 도착의 p95**(API 서버 8-4 리허설, 고치지 않음): 진입점 Prometheus 는 쓰인 지표만 모으고(`usedonly`) 5초마다 긁으므로, 그때까지 요청을 받은 적 없는 도착 환경은 첫 이전 내내 `/summary` 의 `p95_ms` 가 null 일 수 있다(→ `p95Ms` null). 판정에는 쓰지 않으므로 결과는 같다. 설정은 echo-infra 쪽이다.

**제약 — 진입점 하나 = 서비스 하나.** 진입점 가중치는 모든 호스트(`domains: ["*"]`)에 하나라 `PUT /weights` 는 진입점 뒤 **모든 앱**의 비율을 함께 바꾼다. `route_main` 이 서비스 첫 배포마다 불리므로 앱이 둘 이상이면 다른 서비스 트래픽까지 움직인다. 시연(앱 1 개)은 괜찮다. 앱이 늘면 echo-infra 쪽 후속(Host 별 라우트 + `PUT /services/{public_host}/weights`, 실행기는 `MainRoute.public_host` 를 넘기면 된다)이 필요하다.

실패 코드(권한 오류를 시간 초과로 뭉개지 않는다). 토큰·응답 원문은 로그·코드에 넣지 않는다.

| 코드 | 언제 |
|---|---|
| `ENTRYPOINT_UNAUTHORIZED` | 401, 토큰 불일치 |
| `ENTRYPOINT_TOKEN_MISSING` | 비밀값 조회 실패(호출 전), 또는 503. `/counters` 의 503 은 두 경우다: 진입점에 토큰이 설정되지 않음, 진입점이 Envoy admin 에 닿지 못함. 공통 응답 매핑(`_request`)이 503 을 하나로 보므로 둘을 가르지 않는다 |
| `ENTRYPOINT_REJECTED` | 400, 본문 검증 |
| `ENTRYPOINT_UNREACHABLE` | 연결 실패·시간 초과 |
| `ENTRYPOINT_ERROR` | 그 밖의 응답, 응답 모양이 다름(카운터 값이 0 이상 정수가 아님 포함) |
| `ENTRYPOINT_ENV_UNMAPPED` | 진입점이 모르는 이름이거나 두 연결이 같은 이름(카운터는 `envs` 에 그 이름 항목이 없음) |
| `ENTRYPOINT_NO_HOSTS` | 카운터만: 그 환경 항목의 `hosts` 가 0(업스트림 없음) |

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
| 호출 기록 | `calls`(호출마다 멱등 키), `operation_keys`(만든 작업) — 시험이 "같은 키는 같은 작업"을 확인한다. 배포는 받은 앱 환경 변수의 키 이름만 `applied_env_keys`·`reverted_env_keys` 와 로그에 남긴다 |
| 로그 | 빌드 5~7줄(저장소 받기·이미지 만들기·digest 기록 등), 실행 5줄(이미지 받기·서버 시작·헬스 경로 응답 등). 줄 시각은 `external_ref` 의 시작 시각부터 2초 간격(시계를 보지 않는다), cursor 는 줄 번호 문자열. 실패 주입 대상이면 마지막 줄이 `ERROR` |
| 로그 주입 | 시험만: `log_injections` 로 "토큰처럼 보이는 줄"을 첫 줄 뒤에 **가리지 않고** 끼워 서버 마스킹을 시험한다(운영 설정에는 없다) |
